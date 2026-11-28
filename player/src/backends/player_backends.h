#pragma once

#include <functional>
#include <string>

#include "../ipc_server.h"

namespace streambridge {

std::wstring EnsureIsolatedBrowserProfileDir();
void MinimizeKodiWindow();
void RestoreKodiWindowFocus();

bool RunInteractiveWebView2Window(
    const std::string& target_url,
    bool is_login_session,
    const std::function<void(const std::string& state, double pos, double dur)>& on_state_change,
    std::string* out_error);

bool ExportNetflixMyListToJson(
    const std::string& output_json_path,
    std::string* out_error);

bool RunCadmiumFixtureTestWindow(
    const std::string& html_file_path,
    std::string* out_error);

bool ProbeNetflixCadmiumContractToJson(
    const std::string& target_url,
    const std::string& output_json_path,
    std::string* out_error);

bool ExecutePlayerBackendSession(
    const IpcLaunchRequest& req,
    const std::function<void(const std::string&)>& emit_event_line);

}  // namespace streambridge
