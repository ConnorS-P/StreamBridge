#include <windows.h>

namespace {

HWND FindKodiWindow() {
    HWND hwnd = FindWindowW(L"Kodi", nullptr);
    if (!hwnd) {
        hwnd = FindWindowW(nullptr, L"Kodi");
    }
    return hwnd;
}

}  // namespace

extern "C" {

__declspec(dllexport) int SB_EnableKodiOverlayCutout(unsigned int r, unsigned int g, unsigned int b) {
    HWND kodi_hwnd = FindKodiWindow();
    if (!kodi_hwnd) {
        return 0;
    }
    LONG_PTR ex_style = GetWindowLongPtrW(kodi_hwnd, GWL_EXSTYLE);
    SetWindowLongPtrW(kodi_hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED);
    COLORREF key_color = RGB(r & 0xFF, g & 0xFF, b & 0xFF);
    if (!SetLayeredWindowAttributes(kodi_hwnd, key_color, 0, LWA_COLORKEY)) {
        return 0;
    }
    return 1;
}

__declspec(dllexport) int SB_DisableKodiOverlayCutout() {
    HWND kodi_hwnd = FindKodiWindow();
    if (!kodi_hwnd) {
        return 0;
    }
    LONG_PTR ex_style = GetWindowLongPtrW(kodi_hwnd, GWL_EXSTYLE);
    if (ex_style & WS_EX_LAYERED) {
        SetWindowLongPtrW(kodi_hwnd, GWL_EXSTYLE, ex_style & ~static_cast<LONG_PTR>(WS_EX_LAYERED));
        RedrawWindow(
            kodi_hwnd,
            nullptr,
            nullptr,
            RDW_ERASE | RDW_INVALIDATE | RDW_FRAME | RDW_ALLCHILDREN);
    }
    return 1;
}

}  // extern "C"
