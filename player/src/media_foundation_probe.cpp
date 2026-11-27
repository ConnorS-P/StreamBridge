#include "media_foundation_probe.h"

#include <windows.h>
#include <dxgi1_6.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mftransform.h>
#include <shlwapi.h>

#include <cstdio>
#include <sstream>

namespace streambridge {

namespace {

std::string WideToUtf8(const wchar_t* wstr) {
    if (!wstr || !*wstr) return "";
    int len = WideCharToMultiByte(CP_UTF8, 0, wstr, -1, nullptr, 0, nullptr, nullptr);
    if (len <= 0) return "";
    std::string out(len - 1, '\0');
    WideCharToMultiByte(CP_UTF8, 0, wstr, -1, out.data(), len, nullptr, nullptr);
    return out;
}

std::string EscapeJson(const std::string& s) {
    std::ostringstream oss;
    for (unsigned char c : s) {
        switch (c) {
            case '"': oss << "\\\""; break;
            case '\\': oss << "\\\\"; break;
            case '\n': oss << "\\n"; break;
            case '\r': oss << "\\r"; break;
            case '\t': oss << "\\t"; break;
            default: oss << c;
        }
    }
    return oss.str();
}

bool CheckMftDecoder(const GUID& category, const GUID& input_subtype, UINT32 flags) {
    MFT_REGISTER_TYPE_INFO input_info = {};
    input_info.guidMajorType = (category == MFT_CATEGORY_VIDEO_DECODER)
                                   ? MFMediaType_Video
                                   : MFMediaType_Audio;
    input_info.guidSubtype = input_subtype;

    IMFActivate** activates = nullptr;
    UINT32 count = 0;
    HRESULT hr = MFTEnumEx(
        category,
        flags,
        &input_info,
        nullptr,
        &activates,
        &count);

    if (SUCCEEDED(hr) && activates) {
        for (UINT32 i = 0; i < count; ++i) {
            if (activates[i]) activates[i]->Release();
        }
        CoTaskMemFree(activates);
    }
    return SUCCEEDED(hr) && count > 0;
}

bool CheckPlayReadyPmpBinary() {
    wchar_t sys_dir[MAX_PATH] = {};
    if (GetSystemDirectoryW(sys_dir, MAX_PATH) == 0) return false;
    std::wstring pmp_path = std::wstring(sys_dir) + L"\\mfpmp.exe";
    return PathFileExistsW(pmp_path.c_str()) == TRUE;
}

bool CheckPlayReadyRegistry() {
    HKEY hkey = nullptr;
    if (RegOpenKeyExW(
            HKEY_LOCAL_MACHINE,
            L"SOFTWARE\\Microsoft\\Windows Media Foundation\\PlayReady",
            0,
            KEY_READ,
            &hkey) == ERROR_SUCCESS) {
        RegCloseKey(hkey);
        return true;
    }
    if (RegOpenKeyExW(
            HKEY_LOCAL_MACHINE,
            L"SOFTWARE\\Microsoft\\PlayReady",
            0,
            KEY_READ,
            &hkey) == ERROR_SUCCESS) {
        RegCloseKey(hkey);
        return true;
    }
    return CheckPlayReadyPmpBinary();
}

bool CheckWebView2Installed() {
    HKEY hkey = nullptr;
    const wchar_t* kWebView2ClientKey =
        L"SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, kWebView2ClientKey, 0, KEY_READ, &hkey) == ERROR_SUCCESS) {
        RegCloseKey(hkey);
        return true;
    }
    if (RegOpenKeyExW(HKEY_CURRENT_USER,
                      L"Software\\Microsoft\\EdgeUpdate\\Clients\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                      0, KEY_READ, &hkey) == ERROR_SUCCESS) {
        RegCloseKey(hkey);
        return true;
    }
    return false;
}

}  // namespace

MediaFoundationDiagReport ProbeMediaFoundationAndDisplays() {
    MediaFoundationDiagReport report;
    HRESULT hr_com = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    bool should_uninit_com = SUCCEEDED(hr_com);

    HRESULT hr_mf = MFStartup(MF_VERSION);
    if (SUCCEEDED(hr_mf)) {
        report.mf_initialized = true;
        report.hevc_hw_decoder_present = CheckMftDecoder(
            MFT_CATEGORY_VIDEO_DECODER,
            MFVideoFormat_HEVC,
            MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SORTANDFILTER);

        report.hevc_sw_decoder_present = CheckMftDecoder(
            MFT_CATEGORY_VIDEO_DECODER,
            MFVideoFormat_HEVC,
            MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_ASYNCMFT | MFT_ENUM_FLAG_SORTANDFILTER);

        report.dolby_ddplus_mft_present = CheckMftDecoder(
            MFT_CATEGORY_AUDIO_DECODER,
            MFAudioFormat_Dolby_DDPlus,
            MFT_ENUM_FLAG_ALL);

        MFShutdown();
    }

    report.playready_pmp_binary_present = CheckPlayReadyPmpBinary();
    report.playready_hwdrm_reg_present = CheckPlayReadyRegistry();
    report.webview2_runtime_present = CheckWebView2Installed();

    IDXGIFactory6* factory = nullptr;
    if (SUCCEEDED(CreateDXGIFactory1(__uuidof(IDXGIFactory6), reinterpret_cast<void**>(&factory))) &&
        factory) {
        IDXGIAdapter1* adapter = nullptr;
        for (UINT adapter_idx = 0;
             factory->EnumAdapters1(adapter_idx, &adapter) != DXGI_ERROR_NOT_FOUND;
             ++adapter_idx) {
            DXGI_ADAPTER_DESC1 adapter_desc = {};
            adapter->GetDesc1(&adapter_desc);
            std::string adapter_name = WideToUtf8(adapter_desc.Description);
            bool is_software_adapter = (adapter_desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0;

            IDXGIOutput* output = nullptr;
            for (UINT output_idx = 0;
                 adapter->EnumOutputs(output_idx, &output) != DXGI_ERROR_NOT_FOUND;
                 ++output_idx) {
                IDXGIOutput6* output6 = nullptr;
                DisplayOutputDiag disp;
                disp.adapter_name = adapter_name;
                disp.is_virtual_or_remote = is_software_adapter;

                if (SUCCEEDED(output->QueryInterface(
                        __uuidof(IDXGIOutput6), reinterpret_cast<void**>(&output6))) &&
                    output6) {
                    DXGI_OUTPUT_DESC1 desc1 = {};
                    if (SUCCEEDED(output6->GetDesc1(&desc1))) {
                        disp.output_name = WideToUtf8(desc1.DeviceName);
                        disp.width = desc1.DesktopCoordinates.right - desc1.DesktopCoordinates.left;
                        disp.height = desc1.DesktopCoordinates.bottom - desc1.DesktopCoordinates.top;
                        disp.bits_per_color = static_cast<int>(desc1.BitsPerColor);
                        disp.hdr10_active =
                            (desc1.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020);
                    }
                    UINT hw_comp_flags = 0;
                    if (SUCCEEDED(output6->CheckHardwareCompositionSupport(&hw_comp_flags))) {
                        disp.hardware_composition_supported =
                            (hw_comp_flags & DXGI_HARDWARE_COMPOSITION_SUPPORT_FLAG_FULLSCREEN) != 0 ||
                            (hw_comp_flags & DXGI_HARDWARE_COMPOSITION_SUPPORT_FLAG_WINDOWED) != 0;
                    }
                    output6->Release();
                }
                report.displays.push_back(disp);
                output->Release();
            }
            adapter->Release();
        }
        factory->Release();
    }

    report.active_display_count = static_cast<int>(report.displays.size());
    report.multi_monitor_hdcp_risk = (report.active_display_count > 1);

    if (should_uninit_com) CoUninitialize();
    return report;
}

std::string FormatMediaFoundationReportJson(const MediaFoundationDiagReport& report) {
    std::ostringstream oss;
    oss << "{"
        << "\"mf_initialized\":" << (report.mf_initialized ? "true" : "false") << ","
        << "\"hevc_hw_decoder_present\":" << (report.hevc_hw_decoder_present ? "true" : "false") << ","
        << "\"hevc_sw_decoder_present\":" << (report.hevc_sw_decoder_present ? "true" : "false") << ","
        << "\"dolby_ddplus_mft_present\":" << (report.dolby_ddplus_mft_present ? "true" : "false") << ","
        << "\"playready_pmp_binary_present\":" << (report.playready_pmp_binary_present ? "true" : "false") << ","
        << "\"playready_hwdrm_reg_present\":" << (report.playready_hwdrm_reg_present ? "true" : "false") << ","
        << "\"webview2_runtime_present\":" << (report.webview2_runtime_present ? "true" : "false") << ","
        << "\"active_display_count\":" << report.active_display_count << ","
        << "\"multi_monitor_hdcp_risk\":" << (report.multi_monitor_hdcp_risk ? "true" : "false") << ","
        << "\"displays\":[";
    for (size_t i = 0; i < report.displays.size(); ++i) {
        const auto& d = report.displays[i];
        if (i > 0) oss << ",";
        oss << "{"
            << "\"adapter_name\":\"" << EscapeJson(d.adapter_name) << "\","
            << "\"output_name\":\"" << EscapeJson(d.output_name) << "\","
            << "\"width\":" << d.width << ","
            << "\"height\":" << d.height << ","
            << "\"bits_per_color\":" << d.bits_per_color << ","
            << "\"hdr10_active\":" << (d.hdr10_active ? "true" : "false") << ","
            << "\"hardware_composition_supported\":" << (d.hardware_composition_supported ? "true" : "false") << ","
            << "\"is_virtual_or_remote\":" << (d.is_virtual_or_remote ? "true" : "false")
            << "}";
    }
    oss << "]}";
    return oss.str();
}

}  // namespace streambridge
