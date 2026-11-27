#include "audio_wasapi.h"
#include "backends/player_backends.h"
#include "ipc_server.h"
#include "media_foundation_probe.h"

#include <windows.h>

#include <iostream>
#include <string>
#include <vector>

namespace {

std::wstring Utf8ToWide(const std::string& str) {
    if (str.empty()) return L"";
    int len = MultiByteToWideChar(CP_UTF8, 0, str.c_str(), -1, nullptr, 0);
    if (len <= 0) return L"";
    std::wstring out(len - 1, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, str.c_str(), -1, out.data(), len);
    return out;
}

void PrintDiagnosticsJson() {
    auto wasapi_report = streambridge::ProbeDefaultWasapiEndpoint();
    auto mf_report = streambridge::ProbeMediaFoundationAndDisplays();

    std::cout << "{\n"
              << "  \"player_version\": \"0.1.5\",\n"
              << "  \"wasapi_audio\": " << streambridge::FormatWasapiReportJson(wasapi_report) << ",\n"
              << "  \"media_foundation_and_display\": "
              << streambridge::FormatMediaFoundationReportJson(mf_report) << "\n"
              << "}\n";
}

int RunSelfTest() {
    // 1. Verify URL allowlist enforcement
    if (!streambridge::ValidateHttpsNetflixUrl("https://www.netflix.com/watch/81458416")) {
        std::cerr << "SelfTest FAILED: Valid Netflix URL rejected\n";
        return 1;
    }
    if (streambridge::ValidateHttpsNetflixUrl("http://www.netflix.com/watch/81458416") ||
        streambridge::ValidateHttpsNetflixUrl("https://evil.example.com/watch/81458416") ||
        streambridge::ValidateHttpsNetflixUrl("https://www.netflix.com/watch/123\" --remote-debugging-port=9222")) {
        std::cerr << "SelfTest FAILED: Unsafe URL accepted\n";
        return 1;
    }

    // 2. Verify secret-key rejection in IPC parser
    streambridge::IpcLaunchRequest req;
    std::string err;
    if (streambridge::ParseIpcLaunchRequest(
            "{\"session_id\":\"s1\",\"title_id\":\"t1\",\"launch_url\":\"https://www.netflix.com/watch/1\",\"password\":\"secret\"}",
            &req,
            &err)) {
        std::cerr << "SelfTest FAILED: IPC payload with password was not rejected\n";
        return 1;
    }

    // 3. Verify isolated DPAPI browser profile directory creation
    std::wstring profile_dir = streambridge::EnsureIsolatedBrowserProfileDir();
    if (profile_dir.empty()) {
        std::cerr << "SelfTest FAILED: Could not create isolated browser profile directory\n";
        return 1;
    }

    // 4. Print hardware & OS diagnostic report
    PrintDiagnosticsJson();
    return 0;
}

}  // namespace

int main(int argc, char* argv[]) {
    std::wstring pipe_name = L"\\\\.\\pipe\\StreamBridgePlayer";
    std::string login_url;
    std::string watch_url;
    bool diagnose_only = false;
    bool self_test = false;
    bool dry_run = false;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--diagnose") {
            diagnose_only = true;
        } else if (arg == "--self-test") {
            self_test = true;
        } else if (arg == "--dry-run") {
            dry_run = true;
        } else if (arg == "--pipe" && i + 1 < argc) {
            pipe_name = Utf8ToWide(argv[++i]);
        } else if (arg == "--login" && i + 1 < argc) {
            login_url = argv[++i];
        } else if (arg == "--watch" && i + 1 < argc) {
            watch_url = argv[++i];
        }
    }

    if (self_test) {
        return RunSelfTest();
    }

    if (diagnose_only) {
        PrintDiagnosticsJson();
        return 0;
    }

    if (!login_url.empty()) {
        std::string err;
        if (!streambridge::RunInteractiveWebView2Window(login_url, true, nullptr, &err)) {
            std::cerr << "StreamBridge.Player login window error: " << err << "\n";
            return 1;
        }
        return 0;
    }

    if (!watch_url.empty()) {
        std::string err;
        if (!streambridge::RunInteractiveWebView2Window(watch_url, false, nullptr, &err)) {
            std::cerr << "StreamBridge.Player watch window error: " << err << "\n";
            return 1;
        }
        return 0;
    }

    std::string error;
    if (!streambridge::RunSingleNamedPipeSession(pipe_name, dry_run, &error)) {
        std::cerr << "StreamBridge.Player IPC session error: " << error << "\n";
        return 1;
    }
    return 0;
}
