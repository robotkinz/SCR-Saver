#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* Wine 10+ (win32u) loads libEGL with dlopen and resolves GL entry points through
 * dlsym and eglGetProcAddress, which bypasses plain LD_PRELOAD interposition, so
 * dlsym and the GetProcAddress functions are hooked to hand out paced wrappers.
 * Wine 11 draws savers inside a virtual desktop offscreen and ends each frame with
 * glFlush instead of a swap; glFlush paces only until the process makes a real swap. */

typedef void *(*dlsym_fn)(void *, const char *);
typedef void (*fnptr)(void);

#ifdef PACE_DEBUG
#include <stdio.h>
static void pace_count(const char *via) {
    static long n;
    static struct timespec t0;
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    if (!n) {
        t0 = t;
    }
    n++;
    if (n % 20 == 0) {
        const char *path = getenv("SCRSAVER_PACE_LOG");
        FILE *f = path ? fopen(path, "a") : NULL;
        if (f) {
            double s = (t.tv_sec - t0.tv_sec) + (t.tv_nsec - t0.tv_nsec) / 1e9;
            fprintf(f, "%s %ld frames %.2fs %.1f fps\n", via, n, s, n / s);
            fclose(f);
        }
    }
}
#else
#define pace_count(via) ((void)0)
#endif

static dlsym_fn real_dlsym(void) {
    static dlsym_fn real;
    if (!real) {
        real = (dlsym_fn)dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.34");
    }
    if (!real) {
        real = (dlsym_fn)dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.2.5");
    }
    return real;
}

static void pace(void) {
    static int fps = -1;
    if (fps < 0) {
        const char *raw = getenv("SCRSAVER_FPS_LIMIT");
        fps = raw ? atoi(raw) : 0;
    }
    if (fps < 1) {
        return;
    }
    static int primed;
    static struct timespec next;
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return;
    }
    long interval = 1000000000L / fps;
    if (primed) {
        long ahead = (next.tv_sec - now.tv_sec) * 1000000000L + (next.tv_nsec - now.tv_nsec);
        if (ahead > 0) {
            clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
        } else if (ahead < -interval) {
            /* More than a frame behind: resync instead of bursting to catch up. */
            next = now;
        }
    } else {
        next = now;
        primed = 1;
    }
    next.tv_nsec += interval;
    while (next.tv_nsec >= 1000000000L) {
        next.tv_nsec -= 1000000000L;
        next.tv_sec += 1;
    }
}

static int swapped;

static void pace_swap(void) {
    swapped = 1;
    pace_count("swap");
    pace();
}

static void *lib(const char *soname) {
    void *handle = dlopen(soname, RTLD_LAZY | RTLD_NOLOAD);
    return handle ? handle : dlopen(soname, RTLD_LAZY);
}

static unsigned int (*real_egl_swap)(void *, void *);
static unsigned int (*real_egl_swap_damage_ext)(void *, void *, int *, int);
static unsigned int (*real_egl_swap_damage_khr)(void *, void *, int *, int);
static void (*real_glx_swap)(void *, unsigned long);
static void (*real_gl_flush)(void);
static fnptr (*real_egl_gpa)(const char *);
static fnptr (*real_glx_gpa)(const unsigned char *);
static fnptr (*real_glx_gpa_arb)(const unsigned char *);

unsigned int eglSwapBuffers(void *display, void *surface) {
    if (!real_egl_swap) {
        void *egl = lib("libEGL.so.1");
        if (egl) {
            real_egl_swap = real_dlsym()(egl, "eglSwapBuffers");
        }
    }
    unsigned int ok = real_egl_swap ? real_egl_swap(display, surface) : 0;
    pace_swap();
    return ok;
}

void glXSwapBuffers(void *dpy, unsigned long drawable) {
    if (!real_glx_swap) {
        void *gl = lib("libGL.so.1");
        if (gl) {
            real_glx_swap = real_dlsym()(gl, "glXSwapBuffers");
        }
    }
    if (real_glx_swap) {
        real_glx_swap(dpy, drawable);
    }
    pace_swap();
}

static unsigned int swap_damage_ext(void *display, void *surface, int *rects, int n) {
    unsigned int ok = real_egl_swap_damage_ext(display, surface, rects, n);
    pace_swap();
    return ok;
}

static unsigned int swap_damage_khr(void *display, void *surface, int *rects, int n) {
    unsigned int ok = real_egl_swap_damage_khr(display, surface, rects, n);
    pace_swap();
    return ok;
}

static void gl_flush(void) {
    real_gl_flush();
    if (!swapped) {
        pace_count("flush");
        pace();
    }
}

/* Returns a paced wrapper for a looked-up entry point, or real unchanged. */
static void *wrap(const char *name, void *real) {
    if (!real || !name) {
        return real;
    }
    if (strcmp(name, "eglSwapBuffers") == 0) {
        real_egl_swap = real;
        return (void *)eglSwapBuffers;
    }
    if (strcmp(name, "eglSwapBuffersWithDamageEXT") == 0) {
        real_egl_swap_damage_ext = real;
        return (void *)swap_damage_ext;
    }
    if (strcmp(name, "eglSwapBuffersWithDamageKHR") == 0) {
        real_egl_swap_damage_khr = real;
        return (void *)swap_damage_khr;
    }
    if (strcmp(name, "glXSwapBuffers") == 0) {
        real_glx_swap = real;
        return (void *)glXSwapBuffers;
    }
    if (strcmp(name, "glFlush") == 0) {
        real_gl_flush = real;
        return (void *)gl_flush;
    }
    return real;
}

fnptr eglGetProcAddress(const char *name) {
    if (!real_egl_gpa) {
        void *egl = lib("libEGL.so.1");
        if (egl) {
            real_egl_gpa = real_dlsym()(egl, "eglGetProcAddress");
        }
    }
    return real_egl_gpa ? (fnptr)wrap(name, (void *)real_egl_gpa(name)) : NULL;
}

fnptr glXGetProcAddress(const unsigned char *name) {
    if (!real_glx_gpa) {
        void *gl = lib("libGL.so.1");
        if (gl) {
            real_glx_gpa = real_dlsym()(gl, "glXGetProcAddress");
        }
    }
    return real_glx_gpa ? (fnptr)wrap((const char *)name, (void *)real_glx_gpa(name)) : NULL;
}

fnptr glXGetProcAddressARB(const unsigned char *name) {
    if (!real_glx_gpa_arb) {
        void *gl = lib("libGL.so.1");
        if (gl) {
            real_glx_gpa_arb = real_dlsym()(gl, "glXGetProcAddressARB");
        }
    }
    return real_glx_gpa_arb ? (fnptr)wrap((const char *)name, (void *)real_glx_gpa_arb(name)) : NULL;
}

void *dlsym(void *handle, const char *name) {
    dlsym_fn real_fn = real_dlsym();
    if (!real_fn) {
        return NULL;
    }
    void *real = real_fn(handle, name);
    if (!real) {
        return NULL;
    }
    if (strcmp(name, "eglGetProcAddress") == 0) {
        real_egl_gpa = real;
        return (void *)eglGetProcAddress;
    }
    if (strcmp(name, "glXGetProcAddress") == 0) {
        real_glx_gpa = real;
        return (void *)glXGetProcAddress;
    }
    if (strcmp(name, "glXGetProcAddressARB") == 0) {
        real_glx_gpa_arb = real;
        return (void *)glXGetProcAddressARB;
    }
    return wrap(name, real);
}
