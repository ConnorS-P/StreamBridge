#include "player_backends.h"

#include <windows.h>
#include <shlobj.h>
#include <shlwapi.h>

#include <string>

namespace streambridge {

namespace {

std::wstring Utf8ToWide(const std::string& str) {
    if (str.empty()) return L"";
    int len = MultiByteToWideChar(CP_UTF8, 0, str.c_str(), -1, nullptr, 0);
    if (len <= 0) return L"";
    std::wstring out(len - 1, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, str.c_str(), -1, out.data(), len);
    return out;
}

std::wstring FindEdgeExecutable() {
    const wchar_t* candidates[] = {
        L"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        L"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    };
    for (const wchar_t* path : candidates) {
        if (PathFileExistsW(path)) {
            return path;
        }
    }
    return L"";
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

void RestoreKodiWindowFocus() {
    HWND kodi_hwnd = FindWindowW(L"Kodi", nullptr);
    if (!kodi_hwnd) {
        kodi_hwnd = FindWindowW(nullptr, L"Kodi");
    }
    if (kodi_hwnd) {
        if (IsIconic(kodi_hwnd)) {
            ShowWindow(kodi_hwnd, SW_RESTORE);
        }
        SetForegroundWindow(kodi_hwnd);
    }
}

bool ExecutePlayerBackendSession(
    const IpcLaunchRequest& req,
    const std::function<void(const std::string&)>& emit_event_line) {
    if (!ValidateHttpsNetflixUrl(req.launch_url)) {
        emit_event_line(FormatSessionEventJsonLine(
            req.session_id,
            req.title_id,
            "failed",
            1,
            0.0,
            0.0,
            "security_validator",
            false,
            "URL rejected by HTTPS Netflix allowlist"));
        return false;
    }

    std::wstring edge_exe = FindEdgeExecutable();
    if (edge_exe.empty()) {
        emit_event_line(FormatSessionEventJsonLine(
            req.session_id,
            req.title_id,
            "failed",
            1,
            0.0,
            0.0,
            "edge_app_host",
            false,
            "Microsoft Edge executable not found"));
        return false;
    }

    std::wstring profile_dir = EnsureIsolatedBrowserProfileDir();
    std::wstring wide_url = Utf8ToWide(req.launch_url);

    // Build arguments without shell=True and WITHOUT --remote-debugging-port or
    // --disable-direct-composition so PlayReady SL3000 / PMP hardware DRM stays intact.
    std::wstring cmd_line = L"\"" + edge_exe + L"\" --app=\"" + wide_url +
                            L"\" --start-fullscreen --no-first-run";
    if (!profile_dir.empty()) {
        cmd_line += L" --user-data-dir=\"" + profile_dir + L"\"";
    }

    emit_event_line(FormatSessionEventJsonLine(
        req.session_id,
        req.title_id,
        "launching",
        1,
        req.resume_position_seconds,
        0.0,
        "edge_playready_host",
        false));

    STARTUPINFOW si = {};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi = {};

    std::wstring mutable_cmd = cmd_line;
    BOOL started = CreateProcessW(
        edge_exe.c_str(),
        mutable_cmd.data(),
        nullptr,
        nullptr,
        FALSE,
        0,
        nullptr,
        nullptr,
        &si,
        &pi);

    if (!started) {
        emit_event_line(FormatSessionEventJsonLine(
            req.session_id,
            req.title_id,
            "failed",
            2,
            0.0,
            0.0,
            "edge_playready_host",
            false,
            "CreateProcessW failed"));
        return false;
    }

    emit_event_line(FormatSessionEventJsonLine(
        req.session_id,
        req.title_id,
        "playing",
        2,
        req.resume_position_seconds,
        0.0,
        "edge_playready_host",
        false));

    WaitForSingleObject(pi.hProcess, INFINITE);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);

    RestoreKodiWindowFocus();

    emit_event_line(FormatSessionEventJsonLine(
        req.session_id,
        req.title_id,
        "stopped",
        3,
        req.resume_position_seconds,
        0.0,
        "edge_playready_host",
        false));

    return true;
}

}  // namespace streambridge
