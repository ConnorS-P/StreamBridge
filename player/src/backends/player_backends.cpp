#include "player_backends.h"

#include <windows.h>
#include <shlobj.h>
#include <shlwapi.h>
#include <wrl.h>
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
        } else if (action === 'osd_mode' && window.__sbSetOsdMode) {
            window.__sbSetOsdMode(value);
        }
    };
})();
)JS";

const wchar_t* kNetflixOsdCssScript = LR"JS(
(function() {
    window.__ensureOsdDom = function() {
        if (document.getElementById('sb-kodi-osd') || !document.body) return;
        const css = document.createElement('style');
        css.textContent = [
            '#sb-kodi-osd{position:fixed;left:0;right:0;bottom:0;z-index:2147483640;background:rgba(12,15,22,0.78);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border-top:1px solid rgba(255,255,255,0.14);padding:22px 48px 28px;color:#f3f5f8;font-family:"Segoe UI",system-ui,sans-serif;transition:opacity .18s,transform .18s;opacity:0;transform:translateY(20px);pointer-events:none}',
            '#sb-kodi-osd.sb-visible{opacity:1;transform:translateY(0);pointer-events:auto}',
            '.sb-osd-row-top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px;font-size:20px;font-weight:600}',
            '.sb-osd-time{font-size:16px;color:rgba(243,245,248,0.82);font-variant-numeric:tabular-nums}',
            '.sb-osd-bar-bg{width:100%;height:8px;background:rgba(255,255,255,0.22);border-radius:4px;overflow:hidden;margin-bottom:18px}',
            '#sb-osd-bar-fill{height:100%;width:0%;background:#e50914;border-radius:4px;transition:width .2s linear}',
            '.sb-osd-btns{display:flex;gap:14px;align-items:center;flex-wrap:wrap}',
            '.sb-osd-btn{background:rgba(255,255,255,0.10);color:#fff;border:2px solid transparent;border-radius:8px;padding:10px 20px;font-size:16px;font-weight:600;cursor:pointer;pointer-events:auto!important}',
            '.sb-osd-btn.sb-focused{background:rgba(229,9,20,0.88);border-color:#fff;box-shadow:0 0 16px rgba(229,9,20,0.75);transform:scale(1.04)}',
            '#sb-osd-modal{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);z-index:2147483645;min-width:440px;max-width:620px;max-height:70vh;background:rgba(14,18,26,0.86);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,0.18);border-radius:14px;padding:24px 28px;color:#fff;font-family:"Segoe UI",system-ui,sans-serif;display:none;flex-direction:column;gap:10px;box-shadow:0 20px 60px rgba(0,0,0,0.75)}',
            '#sb-osd-modal.sb-visible{display:flex}',
            '.sb-modal-title{font-size:20px;font-weight:700;margin-bottom:8px;border-bottom:1px solid rgba(255,255,255,0.12);padding-bottom:10px}',
            '.sb-modal-item{padding:11px 16px;border-radius:8px;font-size:16px;background:rgba(255,255,255,0.06);border:2px solid transparent}',
            '.sb-modal-item.sb-focused{background:rgba(229,9,20,0.88);border-color:#fff;font-weight:600}'
        ].join('');
        document.head.appendChild(css);

        const osd = document.createElement('div');
        osd.id = 'sb-kodi-osd';
        osd.innerHTML = '<div class="sb-osd-row-top"><span id="sb-osd-title">Netflix Playback</span><span class="sb-osd-time" id="sb-osd-clock">0:00 / 0:00</span></div>' +
            '<div class="sb-osd-bar-bg"><div id="sb-osd-bar-fill"></div></div>' +
            '<div class="sb-osd-btns" id="sb-osd-btn-row">' +
            '<button class="sb-osd-btn sb-focused" data-act="toggle_play" id="sb-btn-play">Pause</button>' +
            '<button class="sb-osd-btn" data-act="back10">-10s</button>' +
            '<button class="sb-osd-btn" data-act="fwd10">+10s</button>' +
            '<button class="sb-osd-btn" data-act="fwd30">+30s</button>' +
            '<button class="sb-osd-btn" data-act="subs" id="sb-btn-subs">Subtitles</button>' +
            '<button class="sb-osd-btn" data-act="audio" id="sb-btn-audio">Audio</button>' +
            '<button class="sb-osd-btn" data-act="restart">Start Over</button>' +
            '<button class="sb-osd-btn" data-act="stop">Stop</button></div>';
        document.body.appendChild(osd);

        const modal = document.createElement('div');
        modal.id = 'sb-osd-modal';
        document.body.appendChild(modal);
    };
})();
)JS";

const wchar_t* kNetflixOsdUiScript = LR"JS(
(function() {
    if (window.__sbOsdUiInjected) return;
    window.__sbOsdUiInjected = true;

    function ensureOsdDom() {
        if (window.__ensureOsdDom) window.__ensureOsdDom();
    }

    let osdVisible = false, hudAutoHideTimer = null, btnIdx = 0, modalMode = null, modalItems = [], modalIdx = 0;

    function updateBtnFocus() {
        const btns = Array.from(document.querySelectorAll('#sb-osd-btn-row .sb-osd-btn'));
        btns.forEach((b, i) => b.classList.toggle('sb-focused', i === btnIdx));
    }

    function showOsd(interactiveMenu, autoHideMs) {
        ensureOsdDom();
        const osd = document.getElementById('sb-kodi-osd');
        if (!osd) return;
        osdVisible = true;
        window.__sbOsdVisible = true;
        osd.classList.add('sb-visible');
        const btnRow = document.getElementById('sb-osd-btn-row');
        if (btnRow) btnRow.style.display = interactiveMenu ? 'flex' : 'none';
        updateBtnFocus();
        if (hudAutoHideTimer) clearTimeout(hudAutoHideTimer);
        if (autoHideMs > 0) {
            hudAutoHideTimer = setTimeout(() => { if (!modalMode) hideOsd(); }, autoHideMs);
        }
    }

    function hideOsd() {
        const osd = document.getElementById('sb-kodi-osd');
        if (osd) osd.classList.remove('sb-visible');
        osdVisible = false;
        window.__sbOsdVisible = false;
        closeModal();
    }

    function openTrackModal(mode) {
        ensureOsdDom();
        const modal = document.getElementById('sb-osd-modal');
        if (!modal) return;
        modalMode = mode;
        const tel = window.__sbLastTelemetry || {};
        modalItems = (mode === 'subs' ? tel.subtitles : tel.audio_tracks) || [];
        if (!modalItems.length) modalItems = [{ index: 0, label: 'Default Track', active: true }];
        modalIdx = Math.max(0, modalItems.findIndex(x => x.active));
        renderModal();
        modal.classList.add('sb-visible');
    }

    function renderModal() {
        const modal = document.getElementById('sb-osd-modal');
        if (!modal) return;
        const heading = modalMode === 'subs' ? 'Select Subtitles' : 'Select Audio Track';
        let html = '<div class="sb-modal-title">' + heading + '</div>';
        modalItems.forEach((item, i) => {
            const cls = 'sb-modal-item' + (i === modalIdx ? ' sb-focused' : '');
            const prefix = item.active ? '* ' : '';
            html += '<div class="' + cls + '">' + prefix + item.label + '</div>';
        });
        modal.innerHTML = html;
    }

    function closeModal() {
        const modal = document.getElementById('sb-osd-modal');
        if (modal) modal.classList.remove('sb-visible');
        modalMode = null;
    }

    function triggerAction(act) {
        const v = document.querySelector('video');
        const player = window.__getCadmiumPlayer ? window.__getCadmiumPlayer() : null;
        const isPaused = (player && typeof player.isPaused === 'function') ? player.isPaused() : (v ? v.paused : false);
        if (act === 'toggle_play') {
            window.__streambridgeControl(isPaused ? 'resume' : 'pause', 0);
        } else if (act === 'back10') {
            window.__streambridgeControl('seek_by', -10);
        } else if (act === 'fwd10') {
            window.__streambridgeControl('seek_by', 10);
        } else if (act === 'fwd30') {
            window.__streambridgeControl('seek_by', 30);
        } else if (act === 'subs') {
            openTrackModal('subs');
        } else if (act === 'audio') {
            openTrackModal('audio');
        } else if (act === 'restart') {
            window.__streambridgeControl('seek', 0);
            hideOsd();
        } else if (act === 'stop') {
            if (window.chrome && window.chrome.webview) {
                window.chrome.webview.postMessage(JSON.stringify({ state: 'stopped', position_seconds: 0, duration_seconds: 0 }));
            }
        }
    }

    window.__sbSetOsdMode = function(mode) {
        if (mode > 0) showOsd(true, 8000);
        else hideOsd();
    };
    window.__sbHandleOsdKey = function(key) {
        if (modalMode) {
            if (key === 'ArrowDown') { modalIdx = (modalIdx + 1) % modalItems.length; renderModal(); }
            else if (key === 'ArrowUp') { modalIdx = (modalIdx - 1 + modalItems.length) % modalItems.length; renderModal(); }
            else if (key === 'Enter' || key === ' ') {
                const chosen = modalItems[modalIdx];
                if (chosen) window.__streambridgeControl(modalMode === 'subs' ? 'set_subtitle' : 'set_audio', chosen.index);
                closeModal();
            } else if (key === 'Escape' || key === 'Backspace' || key === 'm' || key === 'M') {
                closeModal();
            }
            return true;
        }
        if (osdVisible) {
            const btns = Array.from(document.querySelectorAll('#sb-osd-btn-row .sb-osd-btn'));
            if (key === 'ArrowRight') { btnIdx = (btnIdx + 1) % btns.length; showOsd(true, 8000); }
            else if (key === 'ArrowLeft') { btnIdx = (btnIdx - 1 + btns.length) % btns.length; showOsd(true, 8000); }
            else if (key === 'Enter' || key === ' ') {
                const act = btns[btnIdx] ? btns[btnIdx].getAttribute('data-act') : '';
                if (act) triggerAction(act);
                showOsd(true, 8000);
            } else if (key === 'm' || key === 'M' || key === 'Escape' || key === 'Backspace' || key === 'i' || key === 'I') {
                hideOsd();
            }
            return true;
        }
        if (key === 'm' || key === 'M' || key === 'i' || key === 'I' || key === 'Enter') { showOsd(true, 8000); return true; }
        if (key === ' ' || key === 'p' || key === 'P' || key === 'k' || key === 'K') { triggerAction('toggle_play'); showOsd(false, 3000); return true; }
        if (key === 'ArrowRight') { window.__streambridgeControl('seek_by', 10); showOsd(false, 3000); return true; }
        if (key === 'ArrowLeft') { window.__streambridgeControl('seek_by', -10); showOsd(false, 3000); return true; }
        if (key === 'ArrowUp') { window.__streambridgeControl('seek_by', 60); showOsd(false, 3000); return true; }
        if (key === 'ArrowDown') { window.__streambridgeControl('seek_by', -60); showOsd(false, 3000); return true; }
        if (key === 's' || key === 'S' || key === 'l' || key === 'L') { showOsd(true, 8000); openTrackModal('subs'); return true; }
        if (key === 'a' || key === 'A') { showOsd(true, 8000); openTrackModal('audio'); return true; }
        if (key === 'x' || key === 'X' || key === 'Escape' || key === 'Backspace' || key === 'BrowserBack' || key === 'MediaStop') { triggerAction('stop'); return true; }
        return false;
    };
})();
)JS";

const wchar_t* kNetflixOsdKeyHandlerScript = LR"JS(
(function() {
    if (window.__sbOsdKeysInjected) return;
    window.__sbOsdKeysInjected = true;

    window.__sbUpdateOsdHud = function(tel) {
        window.__sbLastTelemetry = tel;
        const titleEl = document.getElementById('sb-osd-title');
        if (titleEl && document.title && document.title !== 'Netflix') {
            titleEl.textContent = document.title.replace(' - Netflix', '');
        }
        const clockEl = document.getElementById('sb-osd-clock');
        const fillEl = document.getElementById('sb-osd-bar-fill');
        const playBtn = document.getElementById('sb-btn-play');
        const pos = Math.max(0, Math.floor(tel.position_seconds || 0));
        const dur = Math.max(0, Math.floor(tel.duration_seconds || 0));
        const fmt = function(sec) {
            const h = Math.floor(sec / 3600);
            const m = Math.floor((sec % 3600) / 60);
            const s = sec % 60;
            const mm = (h > 0 && m < 10) ? ('0' + m) : String(m);
            const ss = s < 10 ? ('0' + s) : String(s);
            return h > 0 ? (h + ':' + mm + ':' + ss) : (mm + ':' + ss);
        };
        if (clockEl) {
            const rem = Math.max(0, dur - pos);
            clockEl.textContent = fmt(pos) + ' / ' + fmt(dur) + '  (-' + fmt(rem) + ')';
        }
        if (fillEl && dur > 0) {
            fillEl.style.width = Math.min(100, Math.max(0, (pos / dur) * 100)).toFixed(2) + '%';
        }
        if (playBtn) {
            playBtn.textContent = tel.state === 'paused' ? 'Play' : 'Pause';
        }
    };

    document.addEventListener('keydown', function(e) {
        const key = e.key;
        if (window.__sbHandleOsdKey && window.__sbHandleOsdKey(key)) {
            e.preventDefault();
            e.stopPropagation();
        }
    }, true);
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
            if (ctx && ctx->webview) {
                int mode_val = static_cast<int>(wparam);
                std::wstring js = L"if(window.__streambridgeControl) window.__streambridgeControl('osd_mode', " +
                                  std::to_wstring(mode_val) + L");";
                ctx->webview->ExecuteScript(js.c_str(), nullptr);
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
            // Playback Mode: Create chromeless WS_POPUP owned by kodi_hwnd with direct keyboard focus
            // so the In-WebView2 Translucent Frosted-Glass Kodi OSD ('M' menu) responds instantaneously.
            RECT kodi_screen = {};
            GetWindowRect(ctx.kodi_hwnd, &kodi_screen);
            pos_x = kodi_screen.left;
            pos_y = kodi_screen.top;
            win_w = kodi_screen.right - kodi_screen.left;
            win_h = kodi_screen.bottom - kodi_screen.top;
            win_style = WS_POPUP | WS_VISIBLE | WS_CLIPCHILDREN;
            ex_style = 0;
            parent_hwnd = ctx.kodi_hwnd;
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

    ShowCursor(TRUE);
    ShowWindow(hwnd, SW_SHOW);
    SetWindowPos(hwnd, HWND_TOP, pos_x, pos_y, win_w, win_h, SWP_SHOWWINDOW);
    SetForegroundWindow(hwnd);
    SetFocus(hwnd);
    if (!is_login_session && ctx.kodi_hwnd) {
        SetTimer(hwnd, 1, 500, nullptr);
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
                                    kNetflixOsdCssScript, nullptr);
                                ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                    kNetflixOsdUiScript, nullptr);
                                ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                    kNetflixOsdKeyHandlerScript, nullptr);
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
                                kNetflixOsdCssScript, nullptr);
                            ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                kNetflixOsdUiScript, nullptr);
                            ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                kNetflixOsdKeyHandlerScript, nullptr);
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
                                kNetflixOsdCssScript, nullptr);
                            ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                kNetflixOsdUiScript, nullptr);
                            ctx.webview->AddScriptToExecuteOnDocumentCreated(
                                kNetflixOsdKeyHandlerScript, nullptr);
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

}  // namespace streambridge
