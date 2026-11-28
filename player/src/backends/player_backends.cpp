#include "player_backends.h"

#include <windows.h>
#include <unknwn.h>
#include <shlobj.h>
#include <shlwapi.h>
#include <wrl.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <inspectable.h>
#include <roapi.h>
#include <winstring.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <WebView2.h>
#include <WebView2EnvironmentOptions.h>

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <fstream>
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

const wchar_t* kNetflixCadmiumControlScript = LR"JS(
(function() {
    if (window.__sbControlInjected) return;
    window.__sbControlInjected = true;

    const style = document.createElement('style');
    style.textContent = `
        .watch-video--bottom-controls-container,
        .watch-video--back-container,
        .watch-video--flag-container,
        .evidence-overlay,
        .PlayerControlsNeo__layout,
        .nf-player-container .controls,
        .WatchNext-background,
        [data-uia*="postplay-background"],
        .OriginalsPostPlay-BackgroundTrailer,
        .SeamlessControls--container {
            display: none !important;
            opacity: 0 !important;
            pointer-events: none !important;
        }
        .NFPlayer.postplay-mini-player {
            width: 100vw !important;
            height: 100vh !important;
            top: 0 !important;
            left: 0 !important;
            position: fixed !important;
            z-index: 9999 !important;
        }
        video, .watch-video, .nf-player-container { pointer-events: none !important; }
        body { cursor: none !important; overflow: hidden !important; background-color: #000 !important; }
    `;
    if (document.head) {
        document.head.appendChild(style);
    } else {
        document.addEventListener('DOMContentLoaded', () => {
            if (document.head) document.head.appendChild(style);
        });
    }

    window.__getCadmiumPlayer = function() {
        try {
            const vp = window.netflix &&
                window.netflix.appContext &&
                window.netflix.appContext.state &&
                window.netflix.appContext.state.playerApp &&
                window.netflix.appContext.state.playerApp.getAPI &&
                window.netflix.appContext.state.playerApp.getAPI().videoPlayer;
            if (!vp) return null;
            const sids = vp.getAllPlayerSessionIds();
            return (sids && sids.length) ? vp.getVideoPlayerBySessionId(sids[0]) : null;
        } catch (e) {
            return null;
        }
    };

    window.__streambridgeControl = function(action, value) {
        const v = document.querySelector('video');
        const player = window.__getCadmiumPlayer ? window.__getCadmiumPlayer() : null;
        if (action === 'pause') {
            window.__sbUserPaused = true;
            if (player && typeof player.pause === 'function') player.pause();
            else if (v) v.pause();
        } else if (action === 'resume' || action === 'play') {
            window.__sbUserPaused = false;
            if (player && typeof player.play === 'function') player.play();
            else if (v) v.play();
        } else if (action === 'seek' && typeof value === 'number') {
            const targetMs = Math.max(0, Math.round(value * 1000));
            if (player && typeof player.seek === 'function') player.seek(targetMs);
        } else if (action === 'seek_by' && typeof value === 'number') {
            const curMs = (player && typeof player.getCurrentTime === 'function')
                ? player.getCurrentTime()
                : ((v ? (v.currentTime || 0.0) : 0.0) * 1000.0);
            const targetMs = Math.max(0, Math.round(curMs + value * 1000.0));
            if (player && typeof player.seek === 'function') player.seek(targetMs);
        } else if (action === 'set_subtitle' && typeof value === 'number' && player && typeof player.getTimedTextTrackList === 'function') {
            const subs = player.getTimedTextTrackList() || [];
            if (value >= 0 && value < subs.length && typeof player.setTimedTextTrack === 'function') {
                player.setTimedTextTrack(subs[value]);
            }
        } else if (action === 'set_audio' && typeof value === 'number' && player && typeof player.getAudioTrackList === 'function') {
            const auds = player.getAudioTrackList() || [];
            if (value >= 0 && value < auds.length && typeof player.setAudioTrack === 'function') {
                player.setAudioTrack(auds[value]);
            }
        } else if (action === 'osd_mode') {
            window.__sbOsdVisible = Boolean(value > 0);
        }
    };
})();
)JS";

const wchar_t* kNetflixTelemetryAndOsdHideScript = LR"JS(
(function() {
    if (window.__sbInjected) return;
    window.__sbInjected = true;

    function postTelemetry(stateOverride) {
        const profileLink = document.querySelector('a.profile-link, [data-uia="action-select-profile+primary"]');
        if (profileLink && !window.__sbProfileClicked) {
            window.__sbProfileClicked = true;
            profileLink.click();
        }

        const watchCreditsBtn = document.querySelector(
            '[data-uia="watch-credits-seamless-button"], [data-uia="postplay-MinimizedItem"], .WatchNext-still-hover-container, .WatchNext-still-container, .postplay-still-container'
        );
        if (watchCreditsBtn) {
            try { watchCreditsBtn.click(); } catch (e) {}
        }

        const allVideos = Array.from(document.querySelectorAll('video'));
        if (allVideos.length > 1) {
            for (let i = 1; i < allVideos.length; ++i) {
                try {
                    allVideos[i].muted = true;
                    allVideos[i].pause();
                    allVideos[i].remove();
                } catch (e) {}
            }
        }

        const v = allVideos[0] || document.querySelector('video');
        if (!v || !window.chrome || !window.chrome.webview) return;
        const player = window.__getCadmiumPlayer ? window.__getCadmiumPlayer() : null;

        if (window.__sbStartFromBeginning && !window.__sbStartFromBeginningDone && player && typeof player.seek === 'function' && v.readyState >= 2) {
            window.__sbStartFromBeginningDone = true;
            try { player.seek(0); } catch (e) {}
        }

        if (v.paused && !window.__sbUserPaused && !v.ended && v.readyState >= 2) {
            if (player && typeof player.play === 'function') {
                try { player.play(); } catch (e) {}
            }
            try { v.play(); } catch (e) {}
            const playBtn = document.querySelector('[data-uia="control-play-pause-play"]');
            if (playBtn) {
                try { playBtn.click(); } catch (e) {}
            }
        }

        const posSec = (player && typeof player.getCurrentTime === 'function')
            ? (player.getCurrentTime() / 1000.0)
            : (v.currentTime || 0.0);
        const durSec = (player && typeof player.getDuration === 'function' && player.getDuration() > 0)
            ? (player.getDuration() / 1000.0)
            : ((v.duration && isFinite(v.duration)) ? v.duration : 0.0);
        const isBusy = (player && typeof player.getBusy === 'function' && player.getBusy() !== null) || (v.readyState < 3);
        const isPaused = (player && typeof player.isPaused === 'function') ? player.isPaused() : v.paused;

        const subtitles = [];
        const audioTracks = [];
        try {
            if (player && typeof player.getTimedTextTrackList === 'function') {
                const curSub = typeof player.getTimedTextTrack === 'function' ? player.getTimedTextTrack() : null;
                const rawSubs = player.getTimedTextTrackList() || [];
                rawSubs.forEach((t, idx) => {
                    if (!t) return;
                    const lbl = t.isNoneTrack ? 'Off' : (t.displayName || t.bcp47 || ('Subtitle ' + (idx + 1)));
                    const isAct = Boolean(curSub && curSub.trackId === t.trackId);
                    subtitles.push({ index: idx, label: String(lbl), active: isAct });
                });
            }
            if (player && typeof player.getAudioTrackList === 'function') {
                const curAud = typeof player.getAudioTrack === 'function' ? player.getAudioTrack() : null;
                const rawAuds = player.getAudioTrackList() || [];
                rawAuds.forEach((t, idx) => {
                    if (!t) return;
                    const lbl = t.displayName || t.bcp47 || ('Audio ' + (idx + 1));
                    const isAct = Boolean(curAud && curAud.trackId === t.trackId);
                    audioTracks.push({ index: idx, label: String(lbl), active: isAct });
                });
            }
        } catch (e) {}

        let state = stateOverride;
        if (!state) {
            if (v.ended) state = 'stopped';
            else if (isBusy && !window.__sbUserPaused) state = 'buffering';
            else if (isPaused) state = 'paused';
            else state = 'playing';
        }
        const payloadObj = {
            state: state,
            position_seconds: posSec || 0.0,
            duration_seconds: durSec || 0.0,
            updated_at: Date.now() / 1000.0,
            osd_visible: Boolean(window.__sbOsdVisible),
            subtitles: subtitles,
            audio_tracks: audioTracks
        };
        if (window.__sbUpdateOsdHud) {
            try { window.__sbUpdateOsdHud(payloadObj); } catch (e) {}
        }
        const msg = JSON.stringify(payloadObj);
        window.chrome.webview.postMessage(msg);
    }

    setInterval(() => postTelemetry(null), 250);
    document.addEventListener('ended', () => postTelemetry('stopped'), true);
})();
)JS";

struct WebViewHostContext {
    HWND main_hwnd = nullptr;
    HWND kodi_hwnd = nullptr;
    HWND osd_hwnd = nullptr;
    HDC osd_mem_dc = nullptr;
    HBITMAP osd_dib = nullptr;
    HGDIOBJ osd_old_bmp = nullptr;
    uint32_t* osd_pixels = nullptr;
    int osd_width = 0;
    int osd_height = 0;
    int osd_mode = 0;
    ComPtr<ID3D11Device> d3d_device;
    ComPtr<ID3D11DeviceContext> d3d_context;
    ComPtr<ID3D11Texture2D> d3d_staging_tex;
    ComPtr<ID3D11Texture2D> d3d_last_frame_tex;
    int staging_width = 0;
    int staging_height = 0;
    winrt::Windows::Graphics::Capture::Direct3D11CaptureFramePool wgc_frame_pool{nullptr};
    winrt::Windows::Graphics::Capture::GraphicsCaptureSession wgc_session{nullptr};
    bool wgc_initialized = false;
    int wgc_frames_captured = 0;
    bool last_ulw_ok = false;
    ComPtr<ICoreWebView2Controller> controller;
    ComPtr<ICoreWebView2> webview;
    std::wstring initial_url;
    bool is_login_session = false;
    bool kodi_layered_applied = false;
    bool entered_watch_page = false;
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

void WriteLiveTelemetryJson(const std::string& json_payload) {
    wchar_t local_app_data[MAX_PATH] = {};
    if (FAILED(SHGetFolderPathW(nullptr, CSIDL_LOCAL_APPDATA, nullptr, 0, local_app_data))) {
        return;
    }
    std::wstring dir = std::wstring(local_app_data) + L"\\StreamBridge";
    CreateDirectoryW(dir.c_str(), nullptr);
    std::wstring path = dir + L"\\live_telemetry.json";
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    if (out.is_open()) {
        out << json_payload;
    }
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

void CleanupOsdOverlayResources(WebViewHostContext* ctx) {
    if (!ctx) return;
    try {
        if (ctx->wgc_session) {
            ctx->wgc_session.Close();
            ctx->wgc_session = nullptr;
        }
        if (ctx->wgc_frame_pool) {
            ctx->wgc_frame_pool.Close();
            ctx->wgc_frame_pool = nullptr;
        }
    } catch (...) {}
    ctx->wgc_initialized = false;
    ctx->d3d_last_frame_tex.Reset();
    ctx->d3d_staging_tex.Reset();
    ctx->d3d_context.Reset();
    ctx->d3d_device.Reset();
    if (ctx->osd_hwnd && IsWindow(ctx->osd_hwnd)) {
        DestroyWindow(ctx->osd_hwnd);
    }
    ctx->osd_hwnd = nullptr;
    if (ctx->osd_mem_dc) {
        if (ctx->osd_old_bmp) {
            SelectObject(ctx->osd_mem_dc, ctx->osd_old_bmp);
            ctx->osd_old_bmp = nullptr;
        }
        DeleteDC(ctx->osd_mem_dc);
        ctx->osd_mem_dc = nullptr;
    }
    if (ctx->osd_dib) {
        DeleteObject(ctx->osd_dib);
        ctx->osd_dib = nullptr;
    }
    ctx->osd_pixels = nullptr;
    ctx->osd_width = 0;
    ctx->osd_height = 0;
}

bool EnsureWgcKodiCapture(WebViewHostContext* ctx) {
    if (!ctx || !ctx->kodi_hwnd || !IsWindow(ctx->kodi_hwnd)) return false;
    if (ctx->wgc_initialized && ctx->wgc_frame_pool && ctx->wgc_session) return true;

    try {
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        HRESULT hr = D3D11CreateDevice(
            nullptr,
            D3D_DRIVER_TYPE_HARDWARE,
            nullptr,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            nullptr,
            0,
            D3D11_SDK_VERSION,
            &ctx->d3d_device,
            &fl,
            &ctx->d3d_context);
        if (FAILED(hr) || !ctx->d3d_device) return false;

        ComPtr<IDXGIDevice> dxgi_dev;
        if (FAILED(ctx->d3d_device.As(&dxgi_dev))) return false;

        ComPtr<IInspectable> inspectable_dev;
        if (FAILED(CreateDirect3D11DeviceFromDXGIDevice(dxgi_dev.Get(), &inspectable_dev))) return false;

        winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice winrt_dev{nullptr};
        winrt::copy_from_abi(winrt_dev, inspectable_dev.Get());

        const wchar_t* kWgcClass = L"Windows.Graphics.Capture.GraphicsCaptureItem";
        HSTRING_HEADER hstr_hdr = {};
        HSTRING hstr_cls = nullptr;
        hr = WindowsCreateStringReference(
            kWgcClass,
            static_cast<UINT32>(wcslen(kWgcClass)),
            &hstr_hdr,
            &hstr_cls);
        if (FAILED(hr)) return false;

        ComPtr<IGraphicsCaptureItemInterop> interop;
        hr = RoGetActivationFactory(hstr_cls, IID_PPV_ARGS(&interop));
        if (FAILED(hr) || !interop) return false;

        winrt::Windows::Graphics::Capture::GraphicsCaptureItem item{nullptr};
        hr = interop->CreateForWindow(
            ctx->kodi_hwnd,
            winrt::guid_of<winrt::Windows::Graphics::Capture::GraphicsCaptureItem>(),
            winrt::put_abi(item));
        if (FAILED(hr) || !item) return false;

        ctx->wgc_frame_pool = winrt::Windows::Graphics::Capture::Direct3D11CaptureFramePool::CreateFreeThreaded(
            winrt_dev,
            winrt::Windows::Graphics::DirectX::DirectXPixelFormat::B8G8R8A8UIntNormalized,
            2,
            item.Size());
        ctx->wgc_session = ctx->wgc_frame_pool.CreateCaptureSession(item);
        try {
            ctx->wgc_session.IsCursorCaptureEnabled(false);
        } catch (...) {}
        try {
            ctx->wgc_session.IsBorderRequired(false);
        } catch (...) {}
        ctx->wgc_session.StartCapture();
        ctx->wgc_initialized = true;
        return true;
    } catch (...) {
        return false;
    }
}

bool DrainWgcFramesToLatest(WebViewHostContext* ctx) {
    if (!ctx || !EnsureWgcKodiCapture(ctx)) return false;
    bool got_new = false;
    try {
        while (auto frame = ctx->wgc_frame_pool.TryGetNextFrame()) {
            auto surface = frame.Surface();
            auto* raw_unk = reinterpret_cast<::IUnknown*>(winrt::get_abi(surface));
            ComPtr<Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess> access;
            ComPtr<ID3D11Texture2D> tex;
            if (raw_unk &&
                SUCCEEDED(raw_unk->QueryInterface(IID_PPV_ARGS(&access))) &&
                access &&
                SUCCEEDED(access->GetInterface(IID_PPV_ARGS(&tex))) &&
                tex) {
                D3D11_TEXTURE2D_DESC desc = {};
                tex->GetDesc(&desc);
                if (!ctx->d3d_last_frame_tex) {
                    D3D11_TEXTURE2D_DESC copy_desc = desc;
                    copy_desc.Usage = D3D11_USAGE_DEFAULT;
                    copy_desc.BindFlags = 0;
                    copy_desc.CPUAccessFlags = 0;
                    copy_desc.MiscFlags = 0;
                    ctx->d3d_device->CreateTexture2D(&copy_desc, nullptr, &ctx->d3d_last_frame_tex);
                } else {
                    D3D11_TEXTURE2D_DESC cur_desc = {};
                    ctx->d3d_last_frame_tex->GetDesc(&cur_desc);
                    if (cur_desc.Width != desc.Width || cur_desc.Height != desc.Height) {
                        ctx->d3d_last_frame_tex.Reset();
                        D3D11_TEXTURE2D_DESC copy_desc = desc;
                        copy_desc.Usage = D3D11_USAGE_DEFAULT;
                        copy_desc.BindFlags = 0;
                        copy_desc.CPUAccessFlags = 0;
                        copy_desc.MiscFlags = 0;
                        ctx->d3d_device->CreateTexture2D(&copy_desc, nullptr, &ctx->d3d_last_frame_tex);
                    }
                }
                if (ctx->d3d_last_frame_tex) {
                    ctx->d3d_context->CopyResource(ctx->d3d_last_frame_tex.Get(), tex.Get());
                    ctx->wgc_frames_captured++;
                    got_new = true;
                }
            }
            frame.Close();
        }
    } catch (...) {}
    return got_new || (ctx->d3d_last_frame_tex != nullptr);
}

void UpdateOsdLayeredWindowWgc(WebViewHostContext* ctx) {
    if (!ctx || !ctx->osd_hwnd || !ctx->kodi_hwnd || !IsWindow(ctx->kodi_hwnd)) return;
    if (!DrainWgcFramesToLatest(ctx) || !ctx->d3d_last_frame_tex) return;

    try {
        D3D11_TEXTURE2D_DESC desc = {};
        ctx->d3d_last_frame_tex->GetDesc(&desc);
        int kw = static_cast<int>(desc.Width);
        int kh = static_cast<int>(desc.Height);
        if (kw <= 0 || kh <= 0) return;

        if (!ctx->d3d_staging_tex || ctx->staging_width != kw || ctx->staging_height != kh) {
            ctx->d3d_staging_tex.Reset();
            D3D11_TEXTURE2D_DESC st_desc = desc;
            st_desc.Usage = D3D11_USAGE_STAGING;
            st_desc.BindFlags = 0;
            st_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
            st_desc.MiscFlags = 0;
            if (FAILED(ctx->d3d_device->CreateTexture2D(&st_desc, nullptr, &ctx->d3d_staging_tex))) return;
            ctx->staging_width = kw;
            ctx->staging_height = kh;
        }

        if (!ctx->osd_dib || ctx->osd_width != kw || ctx->osd_height != kh) {
            if (ctx->osd_mem_dc) {
                if (ctx->osd_old_bmp) SelectObject(ctx->osd_mem_dc, ctx->osd_old_bmp);
                DeleteDC(ctx->osd_mem_dc);
                ctx->osd_mem_dc = nullptr;
            }
            if (ctx->osd_dib) {
                DeleteObject(ctx->osd_dib);
                ctx->osd_dib = nullptr;
            }
            HDC screen_dc = GetDC(nullptr);
            ctx->osd_mem_dc = CreateCompatibleDC(screen_dc);
            BITMAPINFO bmi = {};
            bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
            bmi.bmiHeader.biWidth = kw;
            bmi.bmiHeader.biHeight = -kh;
            bmi.bmiHeader.biPlanes = 1;
            bmi.bmiHeader.biBitCount = 32;
            bmi.bmiHeader.biCompression = BI_RGB;
            void* bits = nullptr;
            ctx->osd_dib = CreateDIBSection(screen_dc, &bmi, DIB_RGB_COLORS, &bits, nullptr, 0);
            ReleaseDC(nullptr, screen_dc);
            if (!ctx->osd_dib || !bits) return;
            ctx->osd_old_bmp = SelectObject(ctx->osd_mem_dc, ctx->osd_dib);
            ctx->osd_pixels = static_cast<uint32_t*>(bits);
            ctx->osd_width = kw;
            ctx->osd_height = kh;
        }

        ctx->d3d_context->CopyResource(ctx->d3d_staging_tex.Get(), ctx->d3d_last_frame_tex.Get());
        D3D11_MAPPED_SUBRESOURCE mapped = {};
        if (FAILED(ctx->d3d_context->Map(ctx->d3d_staging_tex.Get(), 0, D3D11_MAP_READ, 0, &mapped))) return;

        const uint8_t* src_base = static_cast<const uint8_t*>(mapped.pData);
        int sample_y = kh / 5;
        const uint32_t* sample_row = reinterpret_cast<const uint32_t*>(src_base + sample_y * mapped.RowPitch);
        uint32_t p1 = sample_row[kw / 8];
        uint32_t p2 = sample_row[kw - kw / 8];
        uint32_t ref_px = ((p1 >> 8) & 0xFF) >= ((p2 >> 8) & 0xFF) ? p1 : p2;
        int kr = static_cast<int>((ref_px >> 16) & 0xFF);
        int kg = static_cast<int>((ref_px >> 8) & 0xFF);
        int kb = static_cast<int>(ref_px & 0xFF);
        int k_dg_r = std::max(8, kg - kr);

        for (int y = 0; y < kh; ++y) {
            const uint32_t* src_row = reinterpret_cast<const uint32_t*>(src_base + y * mapped.RowPitch);
            uint32_t* dst_row = ctx->osd_pixels + y * kw;
            for (int x = 0; x < kw; ++x) {
                uint32_t c = src_row[x];
                int b = static_cast<int>(c & 0xFF);
                int g = static_cast<int>((c >> 8) & 0xFF);
                int r = static_cast<int>((c >> 16) & 0xFF);
                int max_rgb = std::max(r, std::max(g, b));

                if (max_rgb <= 5 || (std::abs(r - kr) <= 8 && std::abs(g - kg) <= 8 && std::abs(b - kb) <= 8)) {
                    dst_row[x] = 0x00000000;
                    continue;
                }

                if (kg >= 16) {
                    int dg_r = g - r;
                    int dg_b = g - b;
                    int db_r = b - r;
                    if (dg_r > 1 && db_r >= -4 && dg_b >= -4 && std::abs(dg_b - db_r) <= 10 && g <= (kg + 16)) {
                        int bg_frac_256 = std::min(256, std::max(0, (dg_r * 256) / k_dg_r));
                        int alpha = 255 - ((bg_frac_256 * 255) >> 8);
                        if (alpha <= 10) {
                            dst_row[x] = 0x00000000;
                        } else {
                            int r_clean = std::min(alpha, std::max(0, r - ((kr * bg_frac_256) >> 8)));
                            int g_clean = std::min(alpha, std::max(0, g - ((kg * bg_frac_256) >> 8)));
                            int b_clean = std::min(alpha, std::max(0, b - ((kb * bg_frac_256) >> 8)));
                            dst_row[x] = (static_cast<uint32_t>(alpha) << 24) |
                                         (static_cast<uint32_t>(r_clean) << 16) |
                                         (static_cast<uint32_t>(g_clean) << 8) |
                                         static_cast<uint32_t>(b_clean);
                        }
                        continue;
                    }
                } else if (max_rgb <= 64) {
                    int alpha = std::min(255, 110 + max_rgb * 2);
                    int r_pre = std::min(alpha, (r * alpha) / 255);
                    int g_pre = std::min(alpha, (g * alpha) / 255);
                    int b_pre = std::min(alpha, (b * alpha) / 255);
                    dst_row[x] = (static_cast<uint32_t>(alpha) << 24) |
                                 (static_cast<uint32_t>(r_pre) << 16) |
                                 (static_cast<uint32_t>(g_pre) << 8) |
                                 static_cast<uint32_t>(b_pre);
                    continue;
                }

                dst_row[x] = 0xFF000000 | (c & 0x00FFFFFF);
            }
        }

        ctx->d3d_context->Unmap(ctx->d3d_staging_tex.Get(), 0);

        RECT kodi_rect = {};
        GetWindowRect(ctx->kodi_hwnd, &kodi_rect);
        POINT pt_dst = {kodi_rect.left, kodi_rect.top};
        SIZE sz_win = {kw, kh};
        POINT pt_src = {0, 0};
        BLENDFUNCTION blend = {};
        blend.BlendOp = AC_SRC_OVER;
        blend.BlendFlags = 0;
        blend.SourceConstantAlpha = 255;
        blend.AlphaFormat = AC_SRC_ALPHA;

        HDC screen_dc = GetDC(nullptr);
        ctx->last_ulw_ok = (UpdateLayeredWindow(
            ctx->osd_hwnd,
            screen_dc,
            &pt_dst,
            &sz_win,
            ctx->osd_mem_dc,
            &pt_src,
            0,
            &blend,
            ULW_ALPHA) != FALSE);
        ReleaseDC(nullptr, screen_dc);
    } catch (...) {}
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
        case WM_MOUSEACTIVATE:
            if (ctx && !ctx->is_login_session) {
                return MA_NOACTIVATE;
            }
            break;
        case WM_TIMER:
            if (wparam == 1 && ctx && !ctx->is_login_session) {
                if (!ctx->kodi_hwnd || !IsWindow(ctx->kodi_hwnd)) {
                    PostMessageW(hwnd, WM_CLOSE, 0, 0);
                    return 0;
                }
                if (ctx->osd_mode == 0) {
                    DrainWgcFramesToLatest(ctx);
                }
                if (GetForegroundWindow() == hwnd) {
                    SetForegroundWindow(ctx->kodi_hwnd);
                }
                if (ctx->osd_mode > 0 && ctx->osd_hwnd) {
                    SetWindowPos(
                        ctx->osd_hwnd,
                        HWND_TOPMOST,
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
                    SetWindowPos(
                        hwnd,
                        ctx->osd_hwnd,
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
                } else {
                    SetWindowPos(
                        hwnd,
                        HWND_TOPMOST,
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
                }
            } else if (wparam == 2 && ctx && !ctx->is_login_session) {
                if (ctx->osd_mode > 0) {
                    UpdateOsdLayeredWindowWgc(ctx);
                }
            }
            return 0;
        case WM_APP + 1:
            if (ctx && ctx->webview) {
                ctx->webview->ExecuteScript(
                    L"if(window.__streambridgeControl) window.__streambridgeControl('pause', 0);",
                    nullptr);
            }
            return 0;
        case WM_APP + 2:
            if (ctx && ctx->webview) {
                ctx->webview->ExecuteScript(
                    L"if(window.__streambridgeControl) window.__streambridgeControl('resume', 0);",
                    nullptr);
            }
            return 0;
        case WM_APP + 3:
            if (ctx && ctx->webview) {
                int target_sec = static_cast<int>(wparam);
                std::wstring js = L"if(window.__streambridgeControl) window.__streambridgeControl('seek', " +
                                  std::to_wstring(target_sec) + L");";
                ctx->webview->ExecuteScript(js.c_str(), nullptr);
            }
            return 0;
        case WM_APP + 4:
            if (ctx && !ctx->is_login_session) {
                int mode_val = static_cast<int>(wparam);
                ctx->osd_mode = mode_val;
                if (ctx->webview) {
                    std::wstring js = L"if(window.__streambridgeControl) window.__streambridgeControl('osd_mode', " +
                                      std::to_wstring(mode_val) + L");";
                    ctx->webview->ExecuteScript(js.c_str(), nullptr);
                }
                if (ctx->osd_mode > 0 && ctx->osd_hwnd) {
                    UpdateOsdLayeredWindowWgc(ctx);
                    ShowWindow(ctx->osd_hwnd, SW_SHOWNOACTIVATE);
                    SetWindowPos(
                        ctx->osd_hwnd,
                        HWND_TOPMOST,
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
                    SetWindowPos(
                        hwnd,
                        ctx->osd_hwnd,
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
                    SetTimer(hwnd, 2, 33, nullptr);
                } else {
                    KillTimer(hwnd, 2);
                    if (ctx->osd_hwnd) {
                        ShowWindow(ctx->osd_hwnd, SW_HIDE);
                    }
                }
            }
            return 0;
        case WM_APP + 5:
            if (ctx && ctx->webview) {
                int offset_sec = static_cast<int>(lparam);
                std::wstring js = L"if(window.__streambridgeControl) window.__streambridgeControl('seek_by', " +
                                  std::to_wstring(offset_sec) + L");";
                ctx->webview->ExecuteScript(js.c_str(), nullptr);
            }
            return 0;
        case WM_APP + 6:
            if (ctx && ctx->webview) {
                int sub_idx = static_cast<int>(wparam);
                std::wstring js = L"if(window.__streambridgeControl) window.__streambridgeControl('set_subtitle', " +
                                  std::to_wstring(sub_idx) + L");";
                ctx->webview->ExecuteScript(js.c_str(), nullptr);
            }
            return 0;
        case WM_APP + 7:
            if (ctx && ctx->webview) {
                int aud_idx = static_cast<int>(wparam);
                std::wstring js = L"if(window.__streambridgeControl) window.__streambridgeControl('set_audio', " +
                                  std::to_wstring(aud_idx) + L");";
                ctx->webview->ExecuteScript(js.c_str(), nullptr);
            }
            return 0;
        case WM_KEYDOWN:
            if (wparam == VK_ESCAPE || wparam == VK_BROWSER_BACK || wparam == VK_MEDIA_STOP) {
                PostMessageW(hwnd, WM_CLOSE, 0, 0);
                return 0;
            }
            break;
        case WM_CLOSE:
            KillTimer(hwnd, 1);
            KillTimer(hwnd, 2);
            CleanupOsdOverlayResources(ctx);
            if (ctx && !ctx->is_login_session) {
                WriteLiveTelemetryJson("{\"state\":\"stopped\",\"position_seconds\":0.0,\"duration_seconds\":0.0}");
            }
            if (ctx && ctx->kodi_layered_applied && ctx->kodi_hwnd) {
                DisableKodiChromaKeyCutout(ctx->kodi_hwnd);
                ctx->kodi_layered_applied = false;
            }
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
    RoInitialize(RO_INIT_SINGLETHREADED);

    std::string effective_url = target_url;
    if (!is_login_session) {
        size_t title_pos = effective_url.find("netflix.com/title/");
        if (title_pos != std::string::npos) {
            effective_url.replace(title_pos, 18, "netflix.com/watch/");
        }
    }

    WebViewHostContext ctx;
    ctx.initial_url = Utf8ToWide(effective_url);
    ctx.is_login_session = is_login_session;
    ctx.on_state_change = on_state_change;
    ctx.kodi_hwnd = FindKodiWindowHandle();

    HINSTANCE hinst = GetModuleHandleW(nullptr);
    const wchar_t* kClassName = L"StreamBridgeWebViewHostClass";

    if (!is_login_session) {
        HWND prev_hwnd = FindWindowW(kClassName, L"StreamBridge Surface");
        if (prev_hwnd && IsWindow(prev_hwnd)) {
            SendMessageW(prev_hwnd, WM_CLOSE, 0, 0);
        }
    }

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
            // Playback Mode: Create chromeless topmost WS_POPUP with WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_LAYERED
            // so mouse clicks pass 100% through to Kodi and Kodi keeps keyboard focus ('M' OSD).
            RECT kodi_screen = {};
            GetWindowRect(ctx.kodi_hwnd, &kodi_screen);
            pos_x = kodi_screen.left;
            pos_y = kodi_screen.top;
            win_w = kodi_screen.right - kodi_screen.left;
            win_h = kodi_screen.bottom - kodi_screen.top;
            win_style = WS_POPUP | WS_VISIBLE | WS_CLIPCHILDREN;
            ex_style = WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW;
            parent_hwnd = nullptr;
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
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "CreateWindowExW failed";
        return false;
    }
    ctx.main_hwnd = hwnd;

    if (!is_login_session && ctx.kodi_hwnd) {
        SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA);
        const wchar_t* kOsdClassName = L"StreamBridgeOsdOverlayClass";
        WNDCLASSEXW osd_wc = {};
        osd_wc.cbSize = sizeof(WNDCLASSEXW);
        osd_wc.lpfnWndProc = DefWindowProcW;
        osd_wc.hInstance = hinst;
        osd_wc.lpszClassName = kOsdClassName;
        RegisterClassExW(&osd_wc);

        ctx.osd_hwnd = CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            kOsdClassName,
            L"StreamBridge OSD Overlay",
            WS_POPUP,
            pos_x,
            pos_y,
            win_w,
            win_h,
            nullptr,
            nullptr,
            hinst,
            nullptr);

        EnsureWgcKodiCapture(&ctx);

        ShowWindow(hwnd, SW_SHOWNOACTIVATE);
        SetWindowPos(hwnd, HWND_TOPMOST, pos_x, pos_y, win_w, win_h, SWP_NOACTIVATE | SWP_SHOWWINDOW);
        SetForegroundWindow(ctx.kodi_hwnd);
        SetTimer(hwnd, 1, 150, nullptr);
    } else {
        ShowCursor(TRUE);
        ShowWindow(hwnd, SW_SHOW);
        SetWindowPos(hwnd, HWND_TOP, pos_x, pos_y, win_w, win_h, SWP_SHOWWINDOW);
        SetForegroundWindow(hwnd);
        SetFocus(hwnd);
    }

    std::wstring profile_dir = EnsureIsolatedBrowserProfileDir();
    if (ctx.on_state_change) {
        ctx.on_state_change("launching", 0.0, 0.0);
    }

    SetEnvironmentVariableW(
        L"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
        L"--autoplay-policy=no-user-gesture-required");
    auto env_options = Microsoft::WRL::Make<CoreWebView2EnvironmentOptions>();
    if (env_options) {
        env_options->put_AdditionalBrowserArguments(L"--autoplay-policy=no-user-gesture-required");
    }

    HRESULT hr_wv = CreateCoreWebView2EnvironmentWithOptions(
        nullptr,
        profile_dir.empty() ? nullptr : profile_dir.c_str(),
        env_options.Get(),
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
                            controller->MoveFocus(COREWEBVIEW2_MOVE_FOCUS_REASON_PROGRAMMATIC);

                            // Intercept Media Stop (and ESC in login mode); in playback mode, ESC/Backspace close the OSD first
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
                                            (key == VK_MEDIA_STOP ||
                                             (ctx.is_login_session && (key == VK_ESCAPE || key == VK_BROWSER_BACK)))) {
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
                                // Playback Mode: Wire <video> telemetry & auto-close if user clicks Netflix's Back arrow
                                wchar_t start_over_env[8] = {};
                                if (GetEnvironmentVariableW(L"STREAMBRIDGE_START_FROM_BEGINNING", start_over_env, 8) > 0 &&
                                    start_over_env[0] == L'1') {
                                    ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                        L"window.__sbStartFromBeginning = true;", nullptr);
                                }
                                ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                    kNetflixCadmiumControlScript, nullptr);
                                ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                    kNetflixTelemetryAndOsdHideScript, nullptr);

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
                                                if (current_uri.find(L"netflix.com/watch/") != std::wstring::npos) {
                                                    ctx.entered_watch_page = true;
                                                } else if (ctx.entered_watch_page &&
                                                           (current_uri.find(L"netflix.com/browse") != std::wstring::npos ||
                                                            current_uri.find(L"netflix.com/title/") != std::wstring::npos)) {
                                                    PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                                                }
                                            }
                                            return S_OK;
                                        })
                                        .Get(),
                                    &src_token);

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
                                                WriteLiveTelemetryJson(json);
                                                if (ctx.on_state_change && !st.empty()) {
                                                    ctx.on_state_change(st, pos, dur);
                                                }
                                                if (st == "stopped") {
                                                    if (ctx.kodi_hwnd && IsWindow(ctx.kodi_hwnd)) {
                                                        PostMessageW(ctx.kodi_hwnd, WM_KEYDOWN, VK_MEDIA_STOP, 0);
                                                        PostMessageW(ctx.kodi_hwnd, WM_KEYUP, VK_MEDIA_STOP, 0);
                                                    }
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

bool ExportNetflixMyListToJson(
    const std::string& output_json_path,
    std::string* out_error) {
    HRESULT hr_com = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    bool uninit_com = SUCCEEDED(hr_com);

    WebViewHostContext ctx;
    ctx.initial_url = L"https://www.netflix.com/browse/my-list";

    HINSTANCE hinst = GetModuleHandleW(nullptr);
    const wchar_t* kClassName = L"StreamBridgeMyListExporterClass";

    WNDCLASSEXW wc = {};
    wc.cbSize = sizeof(WNDCLASSEXW);
    wc.lpfnWndProc = HostWndProc;
    wc.hInstance = hinst;
    wc.lpszClassName = kClassName;
    RegisterClassExW(&wc);

    HWND hwnd = CreateWindowExW(
        WS_EX_NOACTIVATE,
        kClassName,
        L"StreamBridge MyList Sync",
        WS_POPUP,
        -32000,
        -32000,
        1280,
        720,
        nullptr,
        nullptr,
        hinst,
        &ctx);

    if (!hwnd) {
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "Failed to create hidden MyList window";
        return false;
    }
    ctx.main_hwnd = hwnd;

    std::string captured_json = "{\"items\":[]}";
    std::wstring profile_dir = EnsureIsolatedBrowserProfileDir();

    const wchar_t* kExtractMyListScript = LR"JS(
    (function() {
        setTimeout(() => {
            const seen = new Set();
            const items = [];
            const bookmarks = [];
            const links = document.querySelectorAll('a[href*="/watch/"], a[href*="/title/"]');
            for (const a of links) {
                const href = a.getAttribute('href') || '';
                const m = href.match(/\/(?:watch|title)\/(\d+)/);
                if (m && m[1] && !seen.has(m[1])) {
                    seen.add(m[1]);
                    const label = (a.getAttribute('aria-label') || a.textContent || '').trim();
                    items.push({ provider_content_id: m[1], title: label || ('Netflix #' + m[1]) });
                }
            }
            try {
                const videos = window.netflix &&
                    window.netflix.reactContext &&
                    window.netflix.reactContext.models &&
                    window.netflix.reactContext.models.falcorCache &&
                    window.netflix.reactContext.models.falcorCache.videos;
                if (videos && typeof videos === 'object') {
                    for (const [vid, vnode] of Object.entries(videos)) {
                        if (!vnode || typeof vnode !== 'object') continue;
                        const bm = vnode.bookmark && vnode.bookmark.value;
                        const rt = vnode.runtime && vnode.runtime.value;
                        const posSec = bm && typeof bm.bookmarkPosition === 'number' ? bm.bookmarkPosition : 0;
                        const durSec = typeof rt === 'number' ? rt : 0;
                        if (posSec > 5) {
                            bookmarks.push({
                                provider_content_id: String(vid),
                                position_seconds: posSec,
                                duration_seconds: durSec
                            });
                        }
                    }
                }
            } catch (e) {}
            if (window.chrome && window.chrome.webview) {
                window.chrome.webview.postMessage(JSON.stringify({ items: items, bookmarks: bookmarks }));
            }
        }, 1800);
    })();
    )JS";

    SetTimer(hwnd, 9001, 10000, [](HWND h, UINT, UINT_PTR, DWORD) {
        PostMessageW(h, WM_CLOSE, 0, 0);
    });

    HRESULT hr_wv = CreateCoreWebView2EnvironmentWithOptions(
        nullptr,
        profile_dir.empty() ? nullptr : profile_dir.c_str(),
        nullptr,
        Callback<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler>(
            [&](HRESULT result, ICoreWebView2Environment* env) -> HRESULT {
                if (FAILED(result) || !env) {
                    PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                    return result;
                }
                return env->CreateCoreWebView2Controller(
                    ctx.main_hwnd,
                    Callback<ICoreWebView2CreateCoreWebView2ControllerCompletedHandler>(
                        [&](HRESULT ctrl_hr, ICoreWebView2Controller* controller) -> HRESULT {
                            if (FAILED(ctrl_hr) || !controller) {
                                PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                                return ctrl_hr;
                            }
                            ctx.controller = controller;
                            controller->get_CoreWebView2(&ctx.webview);
                            RECT rc = {0, 0, 1280, 720};
                            controller->put_Bounds(rc);
                            if (!ctx.webview) {
                                PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                                return S_OK;
                            }
                            EventRegistrationToken msg_tok = {};
                            ctx.webview->add_WebMessageReceived(
                                Callback<ICoreWebView2WebMessageReceivedEventHandler>(
                                    [&](ICoreWebView2*, ICoreWebView2WebMessageReceivedEventArgs* args) -> HRESULT {
                                        LPWSTR raw = nullptr;
                                        if (SUCCEEDED(args->TryGetWebMessageAsString(&raw)) && raw) {
                                            captured_json = WideToUtf8(raw);
                                            CoTaskMemFree(raw);
                                        }
                                        PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                                        return S_OK;
                                    })
                                    .Get(),
                                &msg_tok);

                            EventRegistrationToken nav_tok = {};
                            ctx.webview->add_NavigationCompleted(
                                Callback<ICoreWebView2NavigationCompletedEventHandler>(
                                    [&](ICoreWebView2* sender, ICoreWebView2NavigationCompletedEventArgs*) -> HRESULT {
                                        sender->ExecuteScript(kExtractMyListScript, nullptr);
                                        return S_OK;
                                    })
                                    .Get(),
                                &nav_tok);

                            ctx.webview->Navigate(ctx.initial_url.c_str());
                            return S_OK;
                        })
                        .Get());
            })
            .Get());

    if (FAILED(hr_wv)) {
        DestroyWindow(hwnd);
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "Failed to initialize WebView2 for MyList export";
        return false;
    }

    MSG msg = {};
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    std::ofstream ofs(output_json_path, std::ios::binary | std::ios::trunc);
    if (!ofs) {
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "Failed to write output JSON file";
        return false;
    }
    ofs << captured_json;
    ofs.close();

    if (uninit_com) CoUninitialize();
    return true;
}

namespace {

std::wstring LocalFilePathToFileUri(const std::string& raw_path) {
    std::wstring wpath = Utf8ToWide(raw_path);
    wchar_t full_path[MAX_PATH] = {};
    if (GetFullPathNameW(wpath.c_str(), MAX_PATH, full_path, nullptr) == 0) {
        return L"";
    }
    std::wstring uri = L"file:///";
    for (wchar_t ch : std::wstring(full_path)) {
        if (ch == L'\\') {
            uri += L'/';
        } else {
            uri += ch;
        }
    }
    return uri;
}

}  // namespace

bool RunCadmiumFixtureTestWindow(
    const std::string& html_file_path,
    std::string* out_error) {
    std::wstring file_uri = LocalFilePathToFileUri(html_file_path);
    if (file_uri.empty()) {
        if (out_error) *out_error = "Invalid fixture HTML path";
        return false;
    }

    HRESULT hr_com = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    bool uninit_com = SUCCEEDED(hr_com);

    WebViewHostContext ctx;
    ctx.initial_url = file_uri;
    ctx.is_login_session = false;

    HINSTANCE hinst = GetModuleHandleW(nullptr);
    const wchar_t* kClassName = L"StreamBridgeWebViewHostClass";

    HWND prev_hwnd = FindWindowW(kClassName, L"StreamBridge Surface");
    if (prev_hwnd && IsWindow(prev_hwnd)) {
        SendMessageW(prev_hwnd, WM_CLOSE, 0, 0);
    }

    WNDCLASSEXW wc = {};
    wc.cbSize = sizeof(WNDCLASSEXW);
    wc.lpfnWndProc = HostWndProc;
    wc.hInstance = hinst;
    wc.lpszClassName = kClassName;
    RegisterClassExW(&wc);

    HWND hwnd = CreateWindowExW(
        WS_EX_NOACTIVATE,
        kClassName,
        L"StreamBridge Surface",
        WS_POPUP,
        -32000,
        -32000,
        1280,
        720,
        nullptr,
        nullptr,
        hinst,
        &ctx);

    if (!hwnd) {
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "Failed to create offscreen fixture test window";
        return false;
    }
    ctx.main_hwnd = hwnd;

    // 25s safety timeout so automated CI never hangs if a test fails to close the window
    SetTimer(hwnd, 9002, 25000, [](HWND h, UINT, UINT_PTR, DWORD) {
        PostMessageW(h, WM_CLOSE, 0, 0);
    });

    std::wstring profile_dir = EnsureIsolatedBrowserProfileDir();
    SetEnvironmentVariableW(
        L"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
        L"--autoplay-policy=no-user-gesture-required");
    auto env_options = Microsoft::WRL::Make<CoreWebView2EnvironmentOptions>();
    if (env_options) {
        env_options->put_AdditionalBrowserArguments(L"--autoplay-policy=no-user-gesture-required");
    }

    HRESULT hr_wv = CreateCoreWebView2EnvironmentWithOptions(
        nullptr,
        profile_dir.empty() ? nullptr : profile_dir.c_str(),
        env_options.Get(),
        Callback<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler>(
            [&](HRESULT result, ICoreWebView2Environment* env) -> HRESULT {
                if (FAILED(result) || !env) {
                    PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                    return result;
                }
                return env->CreateCoreWebView2Controller(
                    ctx.main_hwnd,
                    Callback<ICoreWebView2CreateCoreWebView2ControllerCompletedHandler>(
                        [&](HRESULT ctrl_hr, ICoreWebView2Controller* controller) -> HRESULT {
                            if (FAILED(ctrl_hr) || !controller) {
                                PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                                return ctrl_hr;
                            }
                            ctx.controller = controller;
                            controller->get_CoreWebView2(&ctx.webview);
                            RECT rc = {0, 0, 1280, 720};
                            controller->put_Bounds(rc);
                            controller->put_IsVisible(TRUE);
                            if (!ctx.webview) {
                                PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                                return S_OK;
                            }

                            ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                kNetflixCadmiumControlScript, nullptr);
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
                                            WriteLiveTelemetryJson(json);
                                        }
                                        return S_OK;
                                    })
                                    .Get(),
                                &msg_token);

                            ctx.webview->Navigate(ctx.initial_url.c_str());
                            return S_OK;
                        })
                        .Get());
            })
            .Get());

    if (FAILED(hr_wv)) {
        DestroyWindow(hwnd);
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "Failed to initialize WebView2 for fixture test";
        return false;
    }

    MSG msg = {};
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    if (uninit_com) CoUninitialize();
    return true;
}

bool ProbeNetflixCadmiumContractToJson(
    const std::string& target_url,
    const std::string& output_json_path,
    std::string* out_error) {
    std::wstring nav_uri;
    if (ValidateHttpsNetflixUrl(target_url)) {
        nav_uri = Utf8ToWide(target_url);
    } else {
        nav_uri = LocalFilePathToFileUri(target_url);
        if (nav_uri.empty()) {
            if (out_error) *out_error = "Probe target must be an HTTPS Netflix URL or local fixture HTML path";
            return false;
        }
    }

    HRESULT hr_com = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    bool uninit_com = SUCCEEDED(hr_com);

    WebViewHostContext ctx;
    ctx.initial_url = nav_uri;

    HINSTANCE hinst = GetModuleHandleW(nullptr);
    const wchar_t* kClassName = L"StreamBridgeCadmiumProbeClass";

    WNDCLASSEXW wc = {};
    wc.cbSize = sizeof(WNDCLASSEXW);
    wc.lpfnWndProc = HostWndProc;
    wc.hInstance = hinst;
    wc.lpszClassName = kClassName;
    RegisterClassExW(&wc);

    HWND hwnd = CreateWindowExW(
        WS_EX_NOACTIVATE,
        kClassName,
        L"StreamBridge Cadmium Probe",
        WS_POPUP,
        -32000,
        -32000,
        1280,
        720,
        nullptr,
        nullptr,
        hinst,
        &ctx);

    if (!hwnd) {
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "Failed to create probe window";
        return false;
    }
    ctx.main_hwnd = hwnd;

    std::string captured_json = "{\"contract_valid\":false,\"error\":\"timeout\"}";
    std::wstring profile_dir = EnsureIsolatedBrowserProfileDir();

    const wchar_t* kProbeContractScript = LR"JS(
    (function() {
        setTimeout(() => {
            const methods = [
                'play', 'pause', 'seek', 'getCurrentTime', 'getDuration',
                'getBusy', 'isPaused', 'getTimedTextTrackList', 'getTimedTextTrack',
                'setTimedTextTrack', 'getAudioTrackList', 'getAudioTrack', 'setAudioTrack'
            ];
            const player = window.__getCadmiumPlayer ? window.__getCadmiumPlayer() : null;
            const supported = [];
            const missing = [];
            for (const m of methods) {
                if (player && typeof player[m] === 'function') supported.push(m);
                else missing.push(m);
            }
            const hasFalcor = Boolean(
                window.netflix &&
                window.netflix.reactContext &&
                window.netflix.reactContext.models &&
                window.netflix.reactContext.models.falcorCache &&
                window.netflix.reactContext.models.falcorCache.videos
            );
            const subCount = (player && typeof player.getTimedTextTrackList === 'function')
                ? (player.getTimedTextTrackList() || []).length : 0;
            const audCount = (player && typeof player.getAudioTrackList === 'function')
                ? (player.getAudioTrackList() || []).length : 0;
            const report = {
                contract_valid: Boolean(player && missing.length === 0),
                has_netflix_global: Boolean(window.netflix),
                has_cadmium_video_player_api: Boolean(player),
                has_falcor_videos_cache: hasFalcor,
                supported_methods: supported,
                missing_methods: missing,
                video_element_count: document.querySelectorAll('video').length,
                subtitle_track_count: subCount,
                audio_track_count: audCount,
                raw_current_time_mutated: Boolean(window.__rawCurrentTimeMutated),
                watch_credits_clicked: Boolean(window.__watchCreditsClicked)
            };
            if (window.chrome && window.chrome.webview) {
                window.chrome.webview.postMessage(JSON.stringify(report));
            }
        }, 1200);
    })();
    )JS";

    SetTimer(hwnd, 9003, 12000, [](HWND h, UINT, UINT_PTR, DWORD) {
        PostMessageW(h, WM_CLOSE, 0, 0);
    });

    HRESULT hr_wv = CreateCoreWebView2EnvironmentWithOptions(
        nullptr,
        profile_dir.empty() ? nullptr : profile_dir.c_str(),
        nullptr,
        Callback<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler>(
            [&](HRESULT result, ICoreWebView2Environment* env) -> HRESULT {
                if (FAILED(result) || !env) {
                    PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                    return result;
                }
                return env->CreateCoreWebView2Controller(
                    ctx.main_hwnd,
                    Callback<ICoreWebView2CreateCoreWebView2ControllerCompletedHandler>(
                        [&](HRESULT ctrl_hr, ICoreWebView2Controller* controller) -> HRESULT {
                            if (FAILED(ctrl_hr) || !controller) {
                                PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                                return ctrl_hr;
                            }
                            ctx.controller = controller;
                            controller->get_CoreWebView2(&ctx.webview);
                            RECT rc = {0, 0, 1280, 720};
                            controller->put_Bounds(rc);
                            if (!ctx.webview) {
                                PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                                return S_OK;
                            }
                            ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                kNetflixCadmiumControlScript, nullptr);
                            ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                kNetflixTelemetryAndOsdHideScript, nullptr);

                            EventRegistrationToken msg_tok = {};
                            ctx.webview->add_WebMessageReceived(
                                Callback<ICoreWebView2WebMessageReceivedEventHandler>(
                                    [&](ICoreWebView2*, ICoreWebView2WebMessageReceivedEventArgs* args) -> HRESULT {
                                        LPWSTR raw = nullptr;
                                        if (SUCCEEDED(args->TryGetWebMessageAsString(&raw)) && raw) {
                                            std::string msg = WideToUtf8(raw);
                                            CoTaskMemFree(raw);
                                            if (msg.find("\"contract_valid\"") != std::string::npos) {
                                                captured_json = msg;
                                                PostMessageW(ctx.main_hwnd, WM_CLOSE, 0, 0);
                                            }
                                        }
                                        return S_OK;
                                    })
                                    .Get(),
                                &msg_tok);

                            EventRegistrationToken nav_tok = {};
                            ctx.webview->add_NavigationCompleted(
                                Callback<ICoreWebView2NavigationCompletedEventHandler>(
                                    [&](ICoreWebView2* sender, ICoreWebView2NavigationCompletedEventArgs*) -> HRESULT {
                                        sender->ExecuteScript(kProbeContractScript, nullptr);
                                        return S_OK;
                                    })
                                    .Get(),
                                &nav_tok);

                            ctx.webview->Navigate(ctx.initial_url.c_str());
                            return S_OK;
                        })
                        .Get());
            })
            .Get());

    if (FAILED(hr_wv)) {
        DestroyWindow(hwnd);
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "Failed to initialize WebView2 for Cadmium probe";
        return false;
    }

    MSG msg = {};
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    std::ofstream ofs(output_json_path, std::ios::binary | std::ios::trunc);
    if (!ofs) {
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "Failed to write probe JSON file";
        return false;
    }
    ofs << captured_json;
    ofs.close();

    if (uninit_com) CoUninitialize();
    return true;
}

bool RunWgcFlipSwapChainTest(
    const std::string& output_json_path,
    std::string* out_error) {
    HRESULT hr_com = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    bool uninit_com = SUCCEEDED(hr_com);
    RoInitialize(RO_INIT_SINGLETHREADED);

    HINSTANCE hinst = GetModuleHandleW(nullptr);
    const wchar_t* kMockKodiClass = L"StreamBridgeMockKodiD3D11Class";
    WNDCLASSEXW wc = {};
    wc.cbSize = sizeof(WNDCLASSEXW);
    wc.lpfnWndProc = DefWindowProcW;
    wc.hInstance = hinst;
    wc.lpszClassName = kMockKodiClass;
    RegisterClassExW(&wc);

    const int kW = 640;
    const int kH = 360;
    HWND mock_kodi = CreateWindowExW(
        0,
        kMockKodiClass,
        L"MockKodiFlipSwapChain",
        WS_POPUP | WS_VISIBLE,
        80,
        80,
        kW,
        kH,
        nullptr,
        nullptr,
        hinst,
        nullptr);
    if (!mock_kodi) {
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "Failed to create mock Kodi window";
        return false;
    }

    ComPtr<ID3D11Device> dev;
    ComPtr<ID3D11DeviceContext> dctx;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    HRESULT hr = D3D11CreateDevice(
        nullptr,
        D3D_DRIVER_TYPE_HARDWARE,
        nullptr,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT,
        nullptr,
        0,
        D3D11_SDK_VERSION,
        &dev,
        &fl,
        &dctx);
    if (FAILED(hr) || !dev) {
        DestroyWindow(mock_kodi);
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "D3D11CreateDevice failed";
        return false;
    }

    ComPtr<IDXGIDevice> dxgi_dev;
    ComPtr<IDXGIAdapter> adapter;
    ComPtr<IDXGIFactory2> factory;
    dev.As(&dxgi_dev);
    if (dxgi_dev) dxgi_dev->GetAdapter(&adapter);
    if (adapter) adapter->GetParent(IID_PPV_ARGS(&factory));
    if (!factory) {
        DestroyWindow(mock_kodi);
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "IDXGIFactory2 query failed";
        return false;
    }

    DXGI_SWAP_CHAIN_DESC1 sc_desc = {};
    sc_desc.Width = kW;
    sc_desc.Height = kH;
    sc_desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    sc_desc.SampleDesc.Count = 1;
    sc_desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sc_desc.BufferCount = 2;
    sc_desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    sc_desc.AlphaMode = DXGI_ALPHA_MODE_IGNORE;

    ComPtr<IDXGISwapChain1> swapchain;
    hr = factory->CreateSwapChainForHwnd(dev.Get(), mock_kodi, &sc_desc, nullptr, nullptr, &swapchain);
    if (FAILED(hr) || !swapchain) {
        DestroyWindow(mock_kodi);
        if (uninit_com) CoUninitialize();
        if (out_error) *out_error = "CreateSwapChainForHwnd (FLIP_DISCARD) failed";
        return false;
    }

    // Build CPU frame buffer matching Kodi's rendered output:
    // - Background: RGB(32, 112, 72) -> BGRA(72, 112, 32, 255)
    // - Translucent OSD bar (y = 240..320): 50% black over RGB(32, 112, 72) -> RGB(16, 56, 36)
    // - Opaque white OSD text/icon (x = 280..360, y = 260..300): RGB(255, 255, 255)
    std::vector<uint32_t> frame_buf(kW * kH, 0xFF207048u);
    for (int y = 240; y < 320; ++y) {
        for (int x = 0; x < kW; ++x) {
            frame_buf[y * kW + x] = 0xFF103824u;
        }
    }
    for (int y = 260; y < 300; ++y) {
        for (int x = 280; x < 360; ++x) {
            frame_buf[y * kW + x] = 0xFFFFFFFFu;
        }
    }

    const wchar_t* kOsdClassName = L"StreamBridgeOsdOverlayTestClass";
    WNDCLASSEXW osd_wc = {};
    osd_wc.cbSize = sizeof(WNDCLASSEXW);
    osd_wc.lpfnWndProc = DefWindowProcW;
    osd_wc.hInstance = hinst;
    osd_wc.lpszClassName = kOsdClassName;
    RegisterClassExW(&osd_wc);

    WebViewHostContext ctx;
    ctx.kodi_hwnd = mock_kodi;
    ctx.osd_mode = 1;
    ctx.osd_hwnd = CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
        kOsdClassName,
        L"StreamBridge OSD Overlay Test",
        WS_POPUP | WS_VISIBLE,
        80,
        80,
        kW,
        kH,
        nullptr,
        nullptr,
        hinst,
        nullptr);

    EnsureWgcKodiCapture(&ctx);

    for (int i = 0; i < 20; ++i) {
        ComPtr<ID3D11Texture2D> backbuf;
        if (SUCCEEDED(swapchain->GetBuffer(0, IID_PPV_ARGS(&backbuf))) && backbuf) {
            dctx->UpdateSubresource(backbuf.Get(), 0, nullptr, frame_buf.data(), kW * 4, 0);
            swapchain->Present(1, 0);
        }
        MSG msg = {};
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        Sleep(25);
        UpdateOsdLayeredWindowWgc(&ctx);
        if (ctx.wgc_frames_captured >= 2 && ctx.last_ulw_ok && ctx.osd_pixels) {
            break;
        }
    }

    int bg_alpha = -1;
    int panel_alpha = -1;
    int text_alpha = -1;
    int text_r = -1;
    if (ctx.osd_pixels && ctx.osd_width == kW && ctx.osd_height == kH) {
        uint32_t px_bg = ctx.osd_pixels[100 * kW + 100];
        uint32_t px_panel = ctx.osd_pixels[280 * kW + 100];
        uint32_t px_text = ctx.osd_pixels[280 * kW + 320];
        bg_alpha = static_cast<int>((px_bg >> 24) & 0xFF);
        panel_alpha = static_cast<int>((px_panel >> 24) & 0xFF);
        text_alpha = static_cast<int>((px_text >> 24) & 0xFF);
        text_r = static_cast<int>((px_text >> 16) & 0xFF);
    }

    bool pass = (ctx.wgc_initialized &&
                 ctx.wgc_frames_captured > 0 &&
                 ctx.last_ulw_ok &&
                 bg_alpha == 0 &&
                 panel_alpha >= 90 && panel_alpha <= 170 &&
                 text_alpha == 255 && text_r == 255);

    std::ofstream ofs(output_json_path, std::ios::binary | std::ios::trunc);
    if (ofs.is_open()) {
        ofs << "{\n"
            << "  \"branch\": \"osd/path1-wgc-chroma-unmix\",\n"
            << "  \"wgc_initialized\": " << (ctx.wgc_initialized ? "true" : "false") << ",\n"
            << "  \"wgc_frames_captured\": " << ctx.wgc_frames_captured << ",\n"
            << "  \"ulw_ok\": " << (ctx.last_ulw_ok ? "true" : "false") << ",\n"
            << "  \"bg_alpha\": " << bg_alpha << ",\n"
            << "  \"panel_alpha\": " << panel_alpha << ",\n"
            << "  \"text_alpha\": " << text_alpha << ",\n"
            << "  \"text_r\": " << text_r << ",\n"
            << "  \"pass\": " << (pass ? "true" : "false") << "\n"
            << "}\n";
        ofs.close();
    }

    CleanupOsdOverlayResources(&ctx);
    DestroyWindow(mock_kodi);
    if (uninit_com) CoUninitialize();
    if (!pass && out_error) {
        *out_error = "WGC D3D11 Flip-Model capture or alpha unmix check failed";
    }
    return pass;
}

}  // namespace streambridge
