#pragma once

#include <string>

namespace streambridge {

struct IpcLaunchRequest {
    std::string session_id;
    std::string title_id;
    std::string provider;
    std::string launch_url;
    std::string preferred_backend;
    double resume_position_seconds = 0.0;
};

bool ValidateHttpsNetflixUrl(const std::string& url);
bool ParseIpcLaunchRequest(const std::string& json_line, IpcLaunchRequest* out_req, std::string* out_error);
std::string FormatSessionEventJsonLine(
    const std::string& session_id,
    const std::string& title_id,
    const std::string& state,
    int sequence,
    double position_seconds,
    double duration_seconds,
    const std::string& backend_used,
    bool telemetry_verified,
    const std::string& error_message = "");

bool RunSingleNamedPipeSession(
    const std::wstring& pipe_name,
    bool dry_run_playback,
    std::string* out_error);

}  // namespace streambridge
