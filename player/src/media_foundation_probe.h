#pragma once

#include <string>
#include <vector>

namespace streambridge {

struct DisplayOutputDiag {
    std::string adapter_name;
    std::string output_name;
    int width = 0;
    int height = 0;
    int bits_per_color = 0;
    bool hdr10_active = false;
    bool hardware_composition_supported = false;
    bool is_virtual_or_remote = false;
};

struct MediaFoundationDiagReport {
    bool mf_initialized = false;
    bool hevc_hw_decoder_present = false;
    bool hevc_sw_decoder_present = false;
    bool dolby_ddplus_mft_present = false;
    bool playready_pmp_binary_present = false;
    bool playready_hwdrm_reg_present = false;
    bool webview2_runtime_present = false;
    int active_display_count = 0;
    bool multi_monitor_hdcp_risk = false;
    std::vector<DisplayOutputDiag> displays;
};

MediaFoundationDiagReport ProbeMediaFoundationAndDisplays();
std::string FormatMediaFoundationReportJson(const MediaFoundationDiagReport& report);

}  // namespace streambridge
