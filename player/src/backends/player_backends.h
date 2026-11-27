#pragma once

#include <functional>
#include <string>

#include "../ipc_server.h"

namespace streambridge {

std::wstring EnsureIsolatedBrowserProfileDir();
void RestoreKodiWindowFocus();
bool ExecutePlayerBackendSession(
    const IpcLaunchRequest& req,
    const std::function<void(const std::string&)>& emit_event_line);

}  // namespace streambridge
