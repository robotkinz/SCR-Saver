/*
 * Host After Dark's Display Properties page (ADPage.dll) in its own
 * property sheet. Wine's desk.cpl does not load that shell extension,
 * so After Dark.scr /c only shows a prompt that goes nowhere.
 *
 * 32-bit PE: clang -target i686-windows-msvc + lld-link (see Makefile).
 */

#define INITGUID
#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <ole2.h>
#include <prsht.h>
#include <shlobj.h>
#include <commctrl.h>

static const CLSID CLSID_AfterDarkPage = {
    0xf19f4ae0, 0xe38f, 0x11ce, {0xa8, 0x6e, 0x00, 0xa0, 0x24, 0x07, 0x94, 0x37}
};

#define MAX_AD_PAGES 8

typedef struct {
    HPROPSHEETPAGE pages[MAX_AD_PAGES];
    UINT count;
} PageList;

static BOOL CALLBACK add_page(HPROPSHEETPAGE page, LPARAM lp) {
    PageList *list = (PageList *)lp;
    if (list->count >= MAX_AD_PAGES) {
        return FALSE;
    }
    list->pages[list->count++] = page;
    return TRUE;
}

static void fail(const char *text) {
    MessageBoxA(NULL, text, "After Dark", MB_OK | MB_ICONERROR);
}

void __stdcall entry(void) {
    IShellPropSheetExt *ext = NULL;
    PageList list;
    PROPSHEETHEADERA header;
    HRESULT hr;
    INITCOMMONCONTROLSEX icc;
    int result;
    HINSTANCE instance = GetModuleHandleA(NULL);

    icc.dwSize = sizeof(icc);
    icc.dwICC = ICC_WIN95_CLASSES | ICC_STANDARD_CLASSES;
    InitCommonControlsEx(&icc);

    hr = CoInitialize(NULL);
    if (FAILED(hr)) {
        fail("COM failed to start.");
        ExitProcess(1);
    }

    hr = CoCreateInstance(
        &CLSID_AfterDarkPage,
        NULL,
        CLSCTX_INPROC_SERVER,
        &IID_IShellPropSheetExt,
        (void **)&ext
    );
    if (FAILED(hr) || ext == NULL) {
        fail(
            "Could not load the After Dark settings page (ADPage.dll).\n\n"
            "Install After Dark into this Wine prefix first."
        );
        CoUninitialize();
        ExitProcess(1);
    }

    list.count = 0;
    hr = IShellPropSheetExt_AddPages(ext, add_page, (LPARAM)&list);
    if (FAILED(hr) || list.count == 0) {
        fail("After Dark did not provide a settings page.");
        IShellPropSheetExt_Release(ext);
        CoUninitialize();
        ExitProcess(1);
    }

    {
        unsigned char *bytes = (unsigned char *)&header;
        unsigned i;
        for (i = 0; i < sizeof(header); i++) {
            bytes[i] = 0;
        }
    }
    header.dwSize = sizeof(header);
    header.dwFlags = PSH_PROPTITLE | PSH_NOAPPLYNOW;
    header.hInstance = instance;
    header.pszCaption = "After Dark";
    header.nPages = list.count;
    header.phpage = list.pages;

    result = (int)PropertySheetA(&header);
    IShellPropSheetExt_Release(ext);
    CoUninitialize();
    ExitProcess(result < 0 ? 1 : 0);
}
