#pragma once

#include <string>
#include <vector>

namespace streambridge {

struct AudioFormatSupport {
    std::string format_name;
    int channels = 0;
    int sample_rate = 0;
    int bits_per_sample = 0;
    bool is_bitstream = false;
    bool supported_exclusive = false;
    std::string hr_hex;
};

struct WasapiEndpointReport {
    bool endpoint_found = false;
    std::string endpoint_name;
    std::string endpoint_id_redacted;
    int os_shared_channels = 0;
    int os_shared_sample_rate = 0;
    bool os_configured_stereo = false;
    std::vector<AudioFormatSupport> tested_formats;
    std::string error_message;
};

WasapiEndpointReport ProbeDefaultWasapiEndpoint();
std::string FormatWasapiReportJson(const WasapiEndpointReport& report);

}  // namespace streambridge
