#include "player_backends.h"

#include <windows.h>
#include <shlobj.h>
#include <shlwapi.h>
#include <wrl.h>
#include <WebView2.h>

#include <string>

using Microsoft::WRL::Callback;
using Microsoft::WRL::ComPtr;

namespace streambridge {

namespace {

constexpr int kTopBarHeightPx = 42;
constexpr int kReturnBtnId = 1001;

struct WebViewHostContext {
    HWND main_hwnd = nullptr;
    HWND return_btn_hwnd = nullptr;
    ComPtr<ICoreWebView2Controller> controller;
    ComPtr<ICoreWebView2> webview;
    std::wstring initial_url;
    bool is_login_session = false;
    std::function<void(const std::string& state, double pos, double dur)> on_state_change;
};

std::wstring Utf8ToWide(const std::string& str) {
    if (str.empty()) return L"";
    int len = MultiByteToWideChar(CP_UTF8, 0, str.c_str(), -1, nullptr, 0);
    if (len <= 0) return L"";
    std::wstring out(len - 1, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, str.c_str(), -1, out.data(), len);
    return out;
}

HWND FindKodiWindowHandle() {
    HWND hwnd = FindWindowW(L"Kodi", nullptr);
    if (!hwnd) {
        hwnd = FindWindowW(nullptr, L"Kodi");
    }
    return hwnd;
}

void ResizeWebViewBounds(WebViewHostContext* ctx) {
    if (!ctx || !ctx->main_hwnd) return;
    RECT rc = {};
    GetClientRect(ctx->main_hwnd, &rc);
    int width = rc.right - rc.left;

    if (ctx->return_btn_hwnd) {
        MoveWindow(ctx->return_btn_hwnd, 0, 0, width, kTopBarHeightPx, TRUE);
    }

    if (ctx->controller) {
        RECT wv_bounds = rc;
        wv_bounds.top = kTopBarHeightPx;
        if (wv_bounds.bottom < wv_bounds.top) {
            wv_bounds.bottom = wv_bounds.top;
        }
        ctx->controller->put_Bounds(wv_bounds);
    }
}

LRESULT CALLBACK HostWndProc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam) {
    auto* ctx = reinterpret_cast<WebViewHostContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));

    switch (msg) {
        case WM_NCCREATE: {
            auto* cs = reinterpret_cast<CREATESTRUCTW*>(lparam);
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(cs->lpCreateParams));
            return DefWindowProcW(hwnd, msg, wparam, lparam);
        }
        case WM_SIZE:
            ResizeWebViewBounds(ctx);
            return 0;
        case WM_COMMAND:
            if (LOWORD(wparam) == kReturnBtnId) {
                PostMessageW(hwnd, WM_CLOSE, 0, 0);
                return 0;
            }
            break;
        case WM_KEYDOWN:
            if (wparam == VK_ESCAPE || wparam == VK_BROWSER_BACK || wparam == VK_MEDIA_STOP) {
                PostMessageW(hwnd, WM_CLOSE, 0, 0);
                return 0;
            }
            break;
        case WM_CLOSE:
            if (ctx && ctx->controller) {
                ctx->controller->Close();
                ctx->controller.Reset();
                ctx->webview.Reset();
            }
            DestroyWindow(hwnd);
            return 0;
        case WM_DESTROY:
            PostQuitMessage(0);
            return 0;
    }
    return DefWindowProcW(hwnd, msg, wparam, lparam);
}

}  // namespace

std::wstring EnsureIsolatedBrowserProfileDir() {
    wchar_t local_app_data[MAX_PATH] = {};
    if (FAILED(SHGetFolderPathW(nullptr, CSIDL_LOCAL_APPDATA, nullptr, 0, local_app_data))) {
        return L"";
    }
    std::wstring base_dir = std::wstring(local_app_data) + L"\\StreamBridge";
    CreateDirectoryW(base_dir.c_str(), nullptr);
    std::wstring profile_dir = base_dir + L"\\BrowserProfile";
    CreateDirectoryW(profile_dir.c_str(), nullptr);
    return profile_dir;
}

void MinimizeKodiWindow() {
    HWND kodi_hwnd = FindKodiWindowHandle();
    if (kodi_hwnd && !IsIconic(kodi_hwnd)) {
        ShowWindow(kodi_hwnd, SW_MINIMIZE);
    }
}

void RestoreKodiWindowFocus() {
    HWND kodi_hwnd = FindKodiWindowHandle();
    if (kodi_hwnd) {
        if (IsIconic(kodi_hwnd)) {
            ShowWindow(kodi_hwnd, SW_RESTORE);
        }
        SetForegroundWindow(kodi_hwnd);
    }
}

bool RunInteractiveWebView2Window(
    const std::string& target_url,
    bool is_login_session,
    const std::function<void(const std::string& state, double pos, double dur)>& on_state_change,
    std::string* out_error) {
    if (!ValidateHttpsNetflixUrl(target_url)) {
        if (out_error) *out_error = "URL rejected by HTTPS Netflix allowlist";
        return false;
    }

    HRESULT hr_com = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    bool uninit_com = SUCCEEDED(hr_com);

    WebViewHostContext ctx;
    ctx.initial_url = Utf8ToWide(target_url);
    ctx.is_login_session = is_login_session;
    ctx.on_state_change = on_state_change;

    HINSTANCE hinst = GetModuleHandleW(nullptr);
    const wchar_t* kClassName = L"StreamBridgeWebViewHostClass";

    WNDCLASSEXW wc = {};
    wc.cbSize = sizeof(WNDCLASSEXW);
    wc.style = CS_HREDRAW | CS_VREDRAW;
    wc.lpfnWndProc = HostWndProc;
    wc.hInstance = hinst;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hbrBackground = static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH));
    wc.lpszClassName = kClassName;
    RegisterClassExW(&wc);

    // Yield Kodi's exclusive fullscreen window before showing the player/login window
    MinimizeKodiWindow();

    int screen_w = GetSystemMetrics(SM_CXSCREEN);
    int screen_h = GetSystemMetrics(SM_CYSCREEN);

    std::wstring window_title = is_login_session
                                    ? L"StreamBridge — Netflix Sign In"
                                    : L"StreamBridge Player";

    HWND hwnd = CreateWindowExW(
        WS_EX_TOPMOST,
        kClassName,
        window_title.c_str(),
        WS_POPUP | WS_VISIBLE | WS_CLIPCHILDREN,
        0,
        0,
        screen_w,
        screen_h,
        nullptr,
        nullptr,
        hinst,
        &ctx);

    if (!hwnd) {
        RestoreKodiWindowFocus();
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "CreateWindowExW failed";
        return false;
    }
    ctx.main_hwnd = hwnd;

    const wchar_t* bar_label =
        is_login_session
            ? L"\u2190  Done Signing In? Click Here or Press ESC / Back to Return to Kodi"
            : L"\u2190  Return to Kodi (Click Here or Press ESC / Back)";

    ctx.return_btn_hwnd = CreateWindowExW(
        0,
        L"BUTTON",
        bar_label,
        WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON | BS_FLAT,
        0,
        0,
        screen_w,
        kTopBarHeightPx,
        hwnd,
        reinterpret_cast<HMENU>(static_cast<INT_PTR>(kReturnBtnId)),
        hinst,
        nullptr);

    ShowWindow(hwnd, SW_SHOWMAXIMIZED);
    SetForegroundWindow(hwnd);
    SetFocus(hwnd);

    std::wstring profile_dir = EnsureIsolatedBrowserProfileDir();
    if (ctx.on_state_change) {
        ctx.on_state_change("launching", 0.0, 0.0);
    }

    HRESULT hr_wv = CreateCoreWebView2EnvironmentWithOptions(
        nullptr,
        profile_dir.empty() ? nullptr : profile_dir.c_str(),
        nullptr,
        Callback<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler>(
            [&ctx](HRESULT result, ICoreWebView2Environment* env) -> HRESULT {
                if (FAILED(result) || !env) {
                    PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                    return result;
                }
                return env->CreateCoreWebView2Controller(
                    ctx.main_hwnd,
                    Callback<ICoreWebView2CreateCoreWebView2ControllerCompletedHandler>(
                        [&ctx](HRESULT ctrl_hr, ICoreWebView2Controller* controller) -> HRESULT {
                            if (FAILED(ctrl_hr) || !controller) {
                                PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                                return ctrl_hr;
                            }
                            ctx.controller = controller;
                            controller->get_CoreWebView2(&ctx.webview);

                            ResizeWebViewBounds(&ctx);
                            controller->put_IsVisible(TRUE);

                            // Intercept ESC, Browser Back, and Media Stop even when WebView2 has focus
                            EventRegistrationToken accel_token = {};
                            controller->add_AcceleratorKeyPressed(
                                Callback<ICoreWebView2AcceleratorKeyPressedEventHandler>(
                                    [&ctx](
                                        ICoreWebView2Controller* /*sender*/,
                                        ICoreWebView2AcceleratorKeyPressedEventArgs* args) -> HRESULT {
                                        COREWEBVIEW2_KEY_EVENT_KIND kind;
                                        UINT key = 0;
                                        args->get_KeyEventKind(&kind);
                                        args->get_VirtualKey(&key);
                                        if (kind == COREWEBVIEW2_KEY_EVENT_KIND_KEY_DOWN &&
                                            (key == VK_ESCAPE || key == VK_BROWSER_BACK ||
                                             key == VK_MEDIA_STOP)) {
                                            args->put_Handled(TRUE);
                                            PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                                        }
                                        return S_OK;
                                    })
                                    .Get(),
                                &accel_token);

                            if (ctx.webview) {
                                ctx.webview->Navigate(ctx.initial_url.c_str());
                                if (ctx.on_state_change) {
                                    ctx.on_state_change("playing", 0.0, 0.0);
                                }
                            }
                            return S_OK;
                        })
                        .Get());
            })
            .Get());

    if (FAILED(hr_wv)) {
        DestroyWindow(hwnd);
        RestoreKodiWindowFocus();
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "CreateCoreWebView2EnvironmentWithOptions failed";
        return false;
    }

    MSG msg = {};
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    if (ctx.on_state_change) {
        ctx.on_state_change("stopped", 0.0, 0.0);
    }

    RestoreKodiWindowFocus();
    if (uninit_com) CoUninitialize();
    return true;
}

bool ExecutePlayerBackendSession(
    const IpcLaunchRequest& req,
    const std::function<void(const std::string&)>& emit_event_line) {
    int seq = 1;
    std::string err;
    bool ok = RunInteractiveWebView2Window(
        req.launch_url,
        false,
        [&](const std::string& state, double pos, double dur) {
            emit_event_line(FormatSessionEventJsonLine(
                req.session_id,
                req.title_id,
                state,
                seq++,
                pos > 0.0 ? pos : req.resume_position_seconds,
                dur,
                "webview2_native_host",
                false));
        },
        &err);

    if (!ok) {
        emit_event_line(FormatSessionEventJsonLine(
            req.session_id,
            req.title_id,
            "failed",
            seq++,
            0.0,
            0.0,
            "webview2_native_host",
            false,
            err));
    }
    return ok;
}

}  // namespace streambridge
