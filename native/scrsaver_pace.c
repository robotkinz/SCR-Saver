#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdlib.h>
#include <time.h>

static void pace(void) {
    const char *raw = getenv("SCRSAVER_FPS_LIMIT");
    if (!raw || raw[0] == '\0') {
        return;
    }
    int fps = atoi(raw);
    if (fps < 1) {
        return;
    }
    static int primed;
    static struct timespec last;
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return;
    }
    long interval = 1000000000L / fps;
    if (primed) {
        long elapsed = (now.tv_sec - last.tv_sec) * 1000000000L + (now.tv_nsec - last.tv_nsec);
        if (elapsed > 0 && elapsed < interval) {
            struct timespec wait;
            wait.tv_sec = 0;
            wait.tv_nsec = interval - elapsed;
            nanosleep(&wait, NULL);
        }
    }
    clock_gettime(CLOCK_MONOTONIC, &last);
    primed = 1;
}

static void *libgl(void) {
    static void *handle;
    static int tried;
    if (!tried) {
        tried = 1;
        handle = dlopen("libGL.so.1", RTLD_LAZY | RTLD_NOLOAD);
        if (!handle) {
            handle = dlopen("libGL.so.1", RTLD_LAZY);
        }
    }
    return handle;
}

static void *libegl(void) {
    static void *handle;
    static int tried;
    if (!tried) {
        tried = 1;
        handle = dlopen("libEGL.so.1", RTLD_LAZY | RTLD_NOLOAD);
        if (!handle) {
            handle = dlopen("libEGL.so.1", RTLD_LAZY);
        }
    }
    return handle;
}

void glXSwapBuffers(void *dpy, unsigned long drawable) {
    static void (*real)(void *, unsigned long);
    if (!real) {
        void *gl = libgl();
        if (gl) {
            real = dlsym(gl, "glXSwapBuffers");
        }
    }
    if (real && real != glXSwapBuffers) {
        real(dpy, drawable);
    }
    pace();
}

unsigned int eglSwapBuffers(void *display, void *surface) {
    static unsigned int (*real)(void *, void *);
    unsigned int ok = 1;
    if (!real) {
        void *egl = libegl();
        if (egl) {
            real = dlsym(egl, "eglSwapBuffers");
        }
    }
    if (real && (void *)real != (void *)eglSwapBuffers) {
        ok = real(display, surface);
    }
    pace();
    return ok;
}
