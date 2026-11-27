#include "ipc_server.h"
#include "backends/player_backends.h"

#include <windows.h>
#include <sddl.h>

#include <cstdio>
#include <sstream>

namespace streambridge {

namespace {

std::string ExtractJsonStringField(const std::string& json, const std::string& key) {
    std::string pattern = "\"" + key + "\":\"";
    size_t pos = json.find(pattern);
    if (pos == std::string::npos) return "";
    size_t start = pos + pattern.size();
    size_t end = start;
    while (end < json.size()) {
        if (json[end] == '"' && json[end - 1] != '\\') break;
        ++end;
    }
    if (end >= json.size()) return "";
    return json.substr(start, end - start);
}

double ExtractJsonNumberField(const std::string& json, const std::string& key) {
    std::string pattern = "\"" + key + "\":";
    size_t pos = json.find(pattern);
    if (pos == std::string::npos) return 0.0;
    size_t start = pos + pattern.size();
    return std::strtod(json.c_str() + start, nullptr);
}

bool ContainsForbiddenSecretKey(const std::string& json) {
    const char* forbidden[] = {
        "\"password\":", "\"passwd\":", "\"cookie\":", "\"cookies\":",
        "\"drm_key\":", "\"license_key\":", "\"api_key\":", "\"apikey\":"
    };
    for (const char* key : forbidden) {
        if (json.find(key) != std::string::npos) return true;
    }
    return false;
}

}  // namespace

bool ValidateHttpsNetflixUrl(const std::string& url) {
    const std::string kPrefix1 = "https://www.netflix.com/";
    const std::string kPrefix2 = "https://netflix.com/";
    if (url.rfind(kPrefix1, 0) != 0 && url.rfind(kPrefix2, 0) != 0) {
        return false;
    }
    for (char c : url) {
        if (c == '\r' || c == '\n' || c == '\0' || c == '"' || c == '\'' || c == '`' || c == ';' || c == '|') {
            return false;
        }
    }
    return true;
}

bool ParseIpcLaunchRequest(
    const std::string& json_line,
    IpcLaunchRequest* out_req,
    std::string* out_error) {
    if (ContainsForbiddenSecretKey(json_line)) {
        if (out_error) *out_error = "Forbidden secret key detected in IPC payload";
        return false;
    }

    out_req->session_id = ExtractJsonStringField(json_line, "session_id");
    out_req->title_id = ExtractJsonStringField(json_line, "title_id");
    out_req->provider = ExtractJsonStringField(json_line, "provider");
    out_req->launch_url = ExtractJsonStringField(json_line, "launch_url");
    out_req->preferred_backend = ExtractJsonStringField(json_line, "preferred_backend");
    out_req->resume_position_seconds = ExtractJsonNumberField(json_line, "resume_position_seconds");

    if (out_req->session_id.empty() || out_req->title_id.empty() || out_req->launch_url.empty()) {
        if (out_error) *out_error = "Missing required session_id, title_id, or launch_url";
        return false;
    }

    if (!ValidateHttpsNetflixUrl(out_req->launch_url)) {
        if (out_error) *out_error = "Launch URL failed HTTPS Netflix allowlist check";
        return false;
    }
    return true;
}

std::string FormatSessionEventJsonLine(
    const std::string& session_id,
    const std::string& title_id,
    const std::string& state,
    int sequence,
    double position_seconds,
    double duration_seconds,
    const std::string& backend_used,
    bool telemetry_verified,
    const std::string& error_message) {
    std::ostringstream oss;
    oss << "{\"type\":\"event\",\"payload\":{"
        << "\"session_id\":\"" << session_id << "\","
        << "\"title_id\":\"" << title_id << "\","
        << "\"state\":\"" << state << "\","
        << "\"sequence\":" << sequence << ","
        << "\"position_seconds\":" << position_seconds << ","
        << "\"duration_seconds\":" << duration_seconds << ","
        << "\"backend_used\":\"" << backend_used << "\","
        << "\"telemetry_verified\":" << (telemetry_verified ? "true" : "false");
    if (!error_message.empty()) {
        oss << ",\"error_message\":\"" << error_message << "\"";
    }
    oss << "}}\n";
    return oss.str();
}

bool RunSingleNamedPipeSession(
    const std::wstring& pipe_name,
    bool dry_run_playback,
    std::string* out_error) {
    // Restrict Named Pipe DACL to Current Owner (OW), Creator Owner (CO), and LocalSystem (SY)
    PSECURITY_DESCRIPTOR sd = nullptr;
    SECURITY_ATTRIBUTES sa = {};
    sa.nLength = sizeof(SECURITY_ATTRIBUTES);
    sa.bInheritHandle = FALSE;

    if (ConvertStringSecurityDescriptorToSecurityDescriptorW(
            L"D:(A;;GA;;;OW)(A;;GA;;;CO)(A;;GA;;;SY)(A;;GA;;;BA)",
            SDDL_REVISION_1,
            &sd,
            nullptr)) {
        sa.lpSecurityDescriptor = sd;
    }

    HANDLE pipe = CreateNamedPipeW(
        pipe_name.c_str(),
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
        1,
        8192,
        8192,
        5000,
        sa.lpSecurityDescriptor ? &sa : nullptr);

    if (sd) LocalFree(sd);

    if (pipe == INVALID_HANDLE_VALUE) {
        if (out_error) *out_error = "CreateNamedPipeW failed";
        return false;
    }

    BOOL connected = ConnectNamedPipe(pipe, nullptr)
                         ? TRUE
                         : (GetLastError() == ERROR_PIPE_CONNECTED);
    if (!connected) {
        CloseHandle(pipe);
        if (out_error) *out_error = "ConnectNamedPipe failed";
        return false;
    }

    std::string request_line;
    char buf[4096];
    DWORD bytes_read = 0;
    while (ReadFile(pipe, buf, sizeof(buf), &bytes_read, nullptr) && bytes_read > 0) {
        request_line.append(buf, bytes_read);
        if (request_line.find('\n') != std::string::npos) break;
    }

    auto write_line = [&](const std::string& line) {
        DWORD written = 0;
        WriteFile(pipe, line.data(), static_cast<DWORD>(line.size()), &written, nullptr);
        FlushFileBuffers(pipe);
    };

    IpcLaunchRequest req;
    std::string parse_err;
    if (!ParseIpcLaunchRequest(request_line, &req, &parse_err)) {
        write_line(FormatSessionEventJsonLine(
            "invalid", "invalid", "failed", 1, 0.0, 0.0, "ipc_validator", false, parse_err));
        FlushFileBuffers(pipe);
        DisconnectNamedPipe(pipe);
        CloseHandle(pipe);
        if (out_error) *out_error = parse_err;
        return false;
    }

    if (dry_run_playback) {
        double start_pos = req.resume_position_seconds;
        write_line(FormatSessionEventJsonLine(
            req.session_id, req.title_id, "buffering", 1, start_pos, 3600.0, "webview2_sim", true));
        write_line(FormatSessionEventJsonLine(
            req.session_id, req.title_id, "playing", 2, start_pos + 120.0, 3600.0, "webview2_sim", true));
        write_line(FormatSessionEventJsonLine(
            req.session_id, req.title_id, "paused", 3, start_pos + 300.0, 3600.0, "webview2_sim", true));
        write_line(FormatSessionEventJsonLine(
            req.session_id, req.title_id, "stopped", 4, start_pos + 600.0, 3600.0, "webview2_sim", true));
    } else {
        ExecutePlayerBackendSession(req, write_line);
    }

    FlushFileBuffers(pipe);
    DisconnectNamedPipe(pipe);
    CloseHandle(pipe);
    return true;
}

}  // namespace streambridge
