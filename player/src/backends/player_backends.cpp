#include "player_backends.h"

#include <windows.h>
#include <shlobj.h>
#include <shlwapi.h>
#include <wrl.h>
#include <WebView2.h>

#include <cstdlib>
#include <string>

using Microsoft::WRL::Callback;
using Microsoft::WRL::ComPtr;

namespace streambridge {

namespace {

constexpr COLORREF kKodiOsdChromaKey = RGB(1, 2, 1);

const wchar_t* kNetflixLoginNavigationScript = LR"JS(
(function() {
    if (window.__sbLoginInjected) return;
    window.__sbLoginInjected = true;

    const style = document.createElement('style');
    style.textContent = `
        input:focus, button:focus, a:focus, [tabindex]:focus {
            outline: 3px solid #e50914 !important;
            outline-offset: 2px !important;
            box-shadow: 0 0 14px rgba(229, 9, 20, 0.85) !important;
        }
    `;
    const injectStyle = () => {
        if (document.head && !style.parentNode) document.head.appendChild(style);
    };
    injectStyle();
    document.addEventListener('DOMContentLoaded', injectStyle);

    function getFocusableItems() {
        const selector = [
            'input:not([type="hidden"]):not([disabled])',
            'button[type="submit"]:not([disabled])',
            'button[data-uia*="login"]:not([disabled])',
            'button[data-uia*="submit"]:not([disabled])',
            'a.profile-link',
            '[data-uia*="profile-button"]',
            'button:not([disabled])'
        ].join(', ');
        return Array.from(document.querySelectorAll(selector)).filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 10 && r.height > 10 && window.getComputedStyle(el).visibility !== 'hidden';
        });
    }

    function autoFocusPrimaryField() {
        const active = document.activeElement;
        if (active && (active.tagName === 'INPUT' || active.tagName === 'BUTTON' || active.tagName === 'A')) {
            return;
        }
        const primaryInput = document.querySelector(
            'input[name="userLoginId"], input[type="email"], input[type="tel"], input[type="text"], input[type="password"]'
        );
        if (primaryInput) {
            primaryInput.focus();
            return;
        }
        const items = getFocusableItems();
        if (items.length > 0) {
            items[0].focus();
        }
    }

    setTimeout(autoFocusPrimaryField, 250);
    setTimeout(autoFocusPrimaryField, 800);
    setTimeout(autoFocusPrimaryField, 1600);

    document.addEventListener('keydown', function(e) {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            const items = getFocusableItems();
            if (items.length === 0) return;
            const idx = items.indexOf(document.activeElement);
            let nextIdx = 0;
            if (e.key === 'ArrowDown') {
                nextIdx = (idx >= 0 && idx + 1 < items.length) ? idx + 1 : 0;
            } else {
                nextIdx = (idx > 0) ? idx - 1 : items.length - 1;
            }
            items[nextIdx].focus();
            e.preventDefault();
        }
    }, true);
})();
)JS";

const wchar_t* kNetflixTelemetryAndOsdHideScript = LR"JS(
(function() {
    if (window.__sbInjected) return;
    window.__sbInjected = true;

    const style = document.createElement('style');
    style.textContent = `
        .watch-video--bottom-controls-container,
        .watch-video--back-container,
        .watch-video--flag-container,
        .evidence-overlay,
        .PlayerControlsNeo__layout,
        .nf-player-container .controls {
            opacity: 0 !important;
            pointer-events: none !important;
        }
        body { cursor: none !important; overflow: hidden !important; }
    `;
    if (document.head) {
        document.head.appendChild(style);
    } else {
        document.addEventListener('DOMContentLoaded', () => {
            if (document.head) document.head.appendChild(style);
        });
    }

    window.__streambridgeControl = function(action, value) {
        const v = document.querySelector('video');
        if (!v) return;
        if (action === 'pause') v.pause();
        else if (action === 'resume' || action === 'play') v.play();
        else if (action === 'seek' && typeof value === 'number') v.currentTime = value;
    };

    function postTelemetry(stateOverride) {
        const v = document.querySelector('video');
        if (!v || !window.chrome || !window.chrome.webview) return;
        let state = stateOverride;
        if (!state) {
            if (v.ended) state = 'stopped';
            else if (v.paused) state = 'paused';
            else if (v.readyState < 3) state = 'buffering';
            else state = 'playing';
        }
        const msg = JSON.stringify({
            state: state,
            position_seconds: v.currentTime || 0.0,
            duration_seconds: (v.duration && isFinite(v.duration)) ? v.duration : 0.0
        });
        window.chrome.webview.postMessage(msg);
    }

    setInterval(() => postTelemetry(null), 1000);
    document.addEventListener('ended', () => postTelemetry('stopped'), true);
})();
)JS";

struct WebViewHostContext {
    HWND main_hwnd = nullptr;
    HWND kodi_hwnd = nullptr;
    ComPtr<ICoreWebView2Controller> controller;
    ComPtr<ICoreWebView2> webview;
    std::wstring initial_url;
    bool is_login_session = false;
    bool kodi_layered_applied = false;
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

std::string WideToUtf8(const wchar_t* wstr) {
    if (!wstr || !*wstr) return "";
    int len = WideCharToMultiByte(CP_UTF8, 0, wstr, -1, nullptr, 0, nullptr, nullptr);
    if (len <= 0) return "";
    std::string out(len - 1, '\0');
    WideCharToMultiByte(CP_UTF8, 0, wstr, -1, out.data(), len, nullptr, nullptr);
    return out;
}

std::string ExtractJsonString(const std::string& json, const std::string& key) {
    std::string pat = "\"" + key + "\":\"";
    size_t pos = json.find(pat);
    if (pos == std::string::npos) return "";
    size_t start = pos + pat.size();
    size_t end = json.find('"', start);
    if (end == std::string::npos) return "";
    return json.substr(start, end - start);
}

double ExtractJsonDouble(const std::string& json, const std::string& key) {
    std::string pat = "\"" + key + "\":";
    size_t pos = json.find(pat);
    if (pos == std::string::npos) return 0.0;
    return std::strtod(json.c_str() + pos + pat.size(), nullptr);
}

HWND FindKodiWindowHandle() {
    HWND hwnd = FindWindowW(L"Kodi", nullptr);
    if (!hwnd) {
        hwnd = FindWindowW(nullptr, L"Kodi");
    }
    return hwnd;
}

void EnableKodiChromaKeyCutout(HWND kodi_hwnd) {
    if (!kodi_hwnd) return;
    LONG_PTR ex_style = GetWindowLongPtrW(kodi_hwnd, GWL_EXSTYLE);
    SetWindowLongPtrW(kodi_hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED);
    SetLayeredWindowAttributes(kodi_hwnd, kKodiOsdChromaKey, 0, LWA_COLORKEY);
}

void DisableKodiChromaKeyCutout(HWND kodi_hwnd) {
    if (!kodi_hwnd) return;
    LONG_PTR ex_style = GetWindowLongPtrW(kodi_hwnd, GWL_EXSTYLE);
    if (ex_style & WS_EX_LAYERED) {
        SetWindowLongPtrW(kodi_hwnd, GWL_EXSTYLE, ex_style & ~static_cast<LONG_PTR>(WS_EX_LAYERED));
        RedrawWindow(
            kodi_hwnd,
            nullptr,
            nullptr,
            RDW_ERASE | RDW_INVALIDATE | RDW_FRAME | RDW_ALLCHILDREN);
    }
}

bool IsPostLoginUrl(const std::wstring& uri) {
    if (uri.find(L"netflix.com/browse") != std::wstring::npos ||
        uri.find(L"netflix.com/SwitchProfile") != std::wstring::npos ||
        uri.find(L"netflix.com/ProfilesGate") != std::wstring::npos ||
        uri.find(L"netflix.com/YourAccount") != std::wstring::npos) {
        return true;
    }
    return false;
}

void ResizeWebViewBounds(WebViewHostContext* ctx) {
    if (!ctx || !ctx->main_hwnd) return;
    if (ctx->is_login_session && ctx->kodi_hwnd && IsWindow(ctx->kodi_hwnd)) {
        RECT kodi_client = {};
        if (GetClientRect(ctx->kodi_hwnd, &kodi_client)) {
            int kw = kodi_client.right - kodi_client.left;
            int kh = kodi_client.bottom - kodi_client.top;
            if (kw > 0 && kh > 0) {
                int win_w = static_cast<int>(kw * 0.82);
                int win_h = static_cast<int>(kh * 0.82);
                int pos_x = (kw - win_w) / 2;
                int pos_y = (kh - win_h) / 2;
                SetWindowPos(
                    ctx->main_hwnd,
                    HWND_TOP,
                    pos_x,
                    pos_y,
                    win_w,
                    win_h,
                    SWP_NOACTIVATE | SWP_SHOWWINDOW);
            }
        }
    }
    if (ctx->controller) {
        RECT rc = {};
        GetClientRect(ctx->main_hwnd, &rc);
        ctx->controller->put_Bounds(rc);
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
    // Intentionally a no-op under In-Kodi WS_CHILD & OSD Overlay architecture:
    // Kodi is never minimized.
}

void RestoreKodiWindowFocus() {
    HWND kodi_hwnd = FindKodiWindowHandle();
    if (kodi_hwnd) {
        DisableKodiChromaKeyCutout(kodi_hwnd);
        SetForegroundWindow(kodi_hwnd);
        SetFocus(kodi_hwnd);
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
    ctx.kodi_hwnd = FindKodiWindowHandle();

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

    int pos_x = 0;
    int pos_y = 0;
    int win_w = GetSystemMetrics(SM_CXSCREEN);
    int win_h = GetSystemMetrics(SM_CYSCREEN);
    DWORD win_style = WS_POPUP | WS_VISIBLE | WS_CLIPCHILDREN;
    DWORD ex_style = 0;
    HWND parent_hwnd = nullptr;

    if (ctx.kodi_hwnd && IsWindow(ctx.kodi_hwnd)) {
        if (is_login_session) {
            // Login Mode: Embed as a centered 82% x 82% WS_CHILD card directly inside Kodi's HWND
            RECT kodi_client = {};
            GetClientRect(ctx.kodi_hwnd, &kodi_client);
            int kw = kodi_client.right - kodi_client.left;
            int kh = kodi_client.bottom - kodi_client.top;
            win_w = static_cast<int>(kw * 0.82);
            win_h = static_cast<int>(kh * 0.82);
            pos_x = (kw - win_w) / 2;
            pos_y = (kh - win_h) / 2;
            win_style = WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS | WS_CLIPCHILDREN;
            parent_hwnd = ctx.kodi_hwnd;
        } else {
            // Playback Mode (Option B): Place chromeless surface on Kodi's monitor directly
            // underneath Kodi's DWM chroma-keyed HWND so Kodi's native skin OSD renders on top!
            RECT kodi_screen = {};
            GetWindowRect(ctx.kodi_hwnd, &kodi_screen);
            pos_x = kodi_screen.left;
            pos_y = kodi_screen.top;
            win_w = kodi_screen.right - kodi_screen.left;
            win_h = kodi_screen.bottom - kodi_screen.top;
            win_style = WS_POPUP | WS_VISIBLE | WS_CLIPCHILDREN;
            ex_style = WS_EX_NOACTIVATE;
            EnableKodiChromaKeyCutout(ctx.kodi_hwnd);
            ctx.kodi_layered_applied = true;
        }
    }

    HWND hwnd = CreateWindowExW(
        ex_style,
        kClassName,
        L"StreamBridge Surface",
        win_style,
        pos_x,
        pos_y,
        win_w,
        win_h,
        parent_hwnd,
        nullptr,
        hinst,
        &ctx);

    if (!hwnd) {
        if (ctx.kodi_layered_applied) {
            DisableKodiChromaKeyCutout(ctx.kodi_hwnd);
        }
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "CreateWindowExW failed";
        return false;
    }
    ctx.main_hwnd = hwnd;

    if (!is_login_session && ctx.kodi_hwnd) {
        // Place the video underlay directly behind kodi_hwnd in Z-order and keep Kodi focused
        SetWindowPos(hwnd, ctx.kodi_hwnd, pos_x, pos_y, win_w, win_h, SWP_NOACTIVATE | SWP_SHOWWINDOW);
        SetForegroundWindow(ctx.kodi_hwnd);
    } else {
        ShowCursor(TRUE);
        ShowWindow(hwnd, SW_SHOW);
        SetFocus(hwnd);
    }

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
                            if (ctx.is_login_session) {
                                controller->MoveFocus(COREWEBVIEW2_MOVE_FOCUS_REASON_PROGRAMMATIC);
                            }

                            // Intercept ESC, Browser Back, and Media Stop
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

                            if (!ctx.webview) return S_OK;

                            if (ctx.is_login_session) {
                                // Auto-focus email/password fields, add red focus ring, and enable Up/Down arrow navigation
                                ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                    kNetflixLoginNavigationScript, nullptr);

                                // Auto-close the WS_CHILD login card as soon as Netflix redirects to /browse
                                EventRegistrationToken src_token = {};
                                ctx.webview->add_SourceChanged(
                                    Callback<ICoreWebView2SourceChangedEventHandler>(
                                        [&ctx](
                                            ICoreWebView2* sender,
                                            ICoreWebView2SourceChangedEventArgs* /*args*/) -> HRESULT {
                                            LPWSTR uri = nullptr;
                                            if (SUCCEEDED(sender->get_Source(&uri)) && uri) {
                                                std::wstring current_uri(uri);
                                                CoTaskMemFree(uri);
                                                if (IsPostLoginUrl(current_uri)) {
                                                    PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                                                }
                                            }
                                            return S_OK;
                                        })
                                        .Get(),
                                    &src_token);
                            } else {
                                // Playback Mode: Inject CSS to hide Netflix web chrome & wire <video> telemetry
                                ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                    kNetflixTelemetryAndOsdHideScript, nullptr);

                                EventRegistrationToken msg_token = {};
                                ctx.webview->add_WebMessageReceived(
                                    Callback<ICoreWebView2WebMessageReceivedEventHandler>(
                                        [&ctx](
                                            ICoreWebView2* /*sender*/,
                                            ICoreWebView2WebMessageReceivedEventArgs* args) -> HRESULT {
                                            LPWSTR raw_msg = nullptr;
                                            if (SUCCEEDED(args->TryGetWebMessageAsString(&raw_msg)) &&
                                                raw_msg) {
                                                std::string json = WideToUtf8(raw_msg);
                                                CoTaskMemFree(raw_msg);
                                                std::string st = ExtractJsonString(json, "state");
                                                double pos = ExtractJsonDouble(json, "position_seconds");
                                                double dur = ExtractJsonDouble(json, "duration_seconds");
                                                if (ctx.on_state_change && !st.empty()) {
                                                    ctx.on_state_change(st, pos, dur);
                                                }
                                                if (st == "stopped") {
                                                    PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                                                }
                                            }
                                            return S_OK;
                                        })
                                        .Get(),
                                    &msg_token);
                            }

                            ctx.webview->Navigate(ctx.initial_url.c_str());
                            if (ctx.on_state_change) {
                                ctx.on_state_change("playing", 0.0, 0.0);
                            }
                            return S_OK;
                        })
                        .Get());
            })
            .Get());

    if (FAILED(hr_wv)) {
        DestroyWindow(hwnd);
        if (ctx.kodi_layered_applied) {
            DisableKodiChromaKeyCutout(ctx.kodi_hwnd);
        }
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
                dur > 0.0));
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
