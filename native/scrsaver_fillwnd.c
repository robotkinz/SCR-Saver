/*
 * Keep a Wine virtual desktop and its screensaver window at a given size.
 * Savers such as 3D Maze call ChangeDisplaySettings(640x480); this helper
 * stretches that window back to the desktop so gluPerspective sees 16:9.
 *
 * 32-bit PE: clang -target i686-windows-msvc + lld-link (see Makefile).
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

typedef struct {
    int width;
    int height;
    int found;
} FillState;

static int parse_int(const WCHAR *s) {
    int n = 0;
    while (*s == L' ' || *s == L'\t') {
        s++;
    }
    while (*s >= L'0' && *s <= L'9') {
        n = n * 10 + (*s - L'0');
        s++;
    }
    return n;
}

static WCHAR *next_arg(WCHAR **cursor) {
    WCHAR *s = *cursor;
    WCHAR *start;
    while (*s == L' ' || *s == L'\t') {
        s++;
    }
    if (*s == 0) {
        *cursor = s;
        return s;
    }
    if (*s == L'"') {
        s++;
        start = s;
        while (*s && *s != L'"') {
            s++;
        }
        if (*s == L'"') {
            *s = 0;
            s++;
        }
    } else {
        start = s;
        while (*s && *s != L' ' && *s != L'\t') {
            s++;
        }
        if (*s) {
            *s = 0;
            s++;
        }
    }
    *cursor = s;
    return start;
}

static int class_is_saver(const WCHAR *cls) {
    const WCHAR *a = cls;
    const WCHAR *b = L"WindowsScreenSaverClass";
    const WCHAR *c = L"ScreenSaverClass";
    while (*a && *b && (*a == *b)) {
        a++;
        b++;
    }
    if (*a == 0 && *b == 0) {
        return 1;
    }
    a = cls;
    while (*a && *c && (*a == *c)) {
        a++;
        c++;
    }
    return *a == 0 && *c == 0;
}

static void dbg(const char *msg) {
    OutputDebugStringA(msg);
}

static void dbg_size(const char *tag, int w, int h) {
    char buf[96];
    wsprintfA(buf, "scrsaver_fillwnd %s %dx%d", tag, w, h);
    OutputDebugStringA(buf);
}

static void resize_hwnd(HWND hwnd, int width, int height) {
    RECT rect;
    int w;
    int h;
    if (!GetWindowRect(hwnd, &rect)) {
        return;
    }
    w = rect.right - rect.left;
    h = rect.bottom - rect.top;
    if (w == width && h == height) {
        return;
    }
    dbg_size("resize", width, height);
    SetWindowPos(
        hwnd,
        HWND_TOP,
        0,
        0,
        width,
        height,
        SWP_NOACTIVATE | SWP_SHOWWINDOW
    );
}

static BOOL CALLBACK enum_child(HWND hwnd, LPARAM lp) {
    FillState *state = (FillState *)lp;
    WCHAR cls[64];
    if (!IsWindowVisible(hwnd)) {
        return TRUE;
    }
    cls[0] = 0;
    GetClassNameW(hwnd, cls, 64);
    if (!class_is_saver(cls)) {
        EnumChildWindows(hwnd, enum_child, lp);
        return TRUE;
    }
    resize_hwnd(hwnd, state->width, state->height);
    state->found = 1;
    return TRUE;
}

static BOOL CALLBACK enum_top(HWND hwnd, LPARAM lp) {
    FillState *state = (FillState *)lp;
    WCHAR cls[64];
    cls[0] = 0;
    GetClassNameW(hwnd, cls, 64);
    if (!class_is_saver(cls)) {
        return TRUE;
    }
    if (IsWindowVisible(hwnd)) {
        resize_hwnd(hwnd, state->width, state->height);
        state->found = 1;
    }
    return TRUE;
}

static HWND g_largest;
static int g_largest_area;

static BOOL CALLBACK find_largest(HWND hwnd, LPARAM lp) {
    RECT rect;
    int w;
    int h;
    int area;
    (void)lp;
    if (!IsWindowVisible(hwnd)) {
        return TRUE;
    }
    if (!GetWindowRect(hwnd, &rect)) {
        return TRUE;
    }
    w = rect.right - rect.left;
    h = rect.bottom - rect.top;
    if (w >= 320 && h >= 240) {
        area = w * h;
        if (area > g_largest_area) {
            g_largest_area = area;
            g_largest = hwnd;
        }
    }
    EnumChildWindows(hwnd, find_largest, 0);
    return TRUE;
}

void __stdcall entry(void) {
    WCHAR *cmd = GetCommandLineW();
    WCHAR *exe;
    WCHAR *width_s;
    WCHAR *height_s;
    FillState state;
    int miss = 0;
    int seen = 0;
    HWND desktop;

    exe = next_arg(&cmd);
    (void)exe;
    width_s = next_arg(&cmd);
    height_s = next_arg(&cmd);
    state.width = parse_int(width_s);
    state.height = parse_int(height_s);
    if (state.width < 320 || state.height < 240) {
        dbg("scrsaver_fillwnd bad size");
        ExitProcess(1);
    }
    dbg_size("target", state.width, state.height);

    for (;;) {
        state.found = 0;
        desktop = GetDesktopWindow();
        if (desktop) {
            resize_hwnd(desktop, state.width, state.height);
            EnumChildWindows(desktop, enum_child, (LPARAM)&state);
        }
        EnumWindows(enum_top, (LPARAM)&state);
        if (!state.found && desktop) {
            g_largest = 0;
            g_largest_area = 0;
            EnumChildWindows(desktop, find_largest, 0);
            if (g_largest) {
                resize_hwnd(g_largest, state.width, state.height);
                state.found = 1;
            }
        }
        if (state.found) {
            seen = 1;
            miss = 0;
        } else if (seen) {
            miss++;
            if (miss > 16) {
                dbg("scrsaver_fillwnd saver window gone");
                ExitProcess(0);
            }
        } else {
            miss++;
            if (miss > 80) {
                dbg("scrsaver_fillwnd never found saver window");
                ExitProcess(1);
            }
        }
        Sleep(250);
    }
}
