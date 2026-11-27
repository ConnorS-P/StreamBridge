#include "audio_wasapi.h"

#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <mmreg.h>
#include <ks.h>
#include <ksmedia.h>
#include <functiondiscoverykeys_devpkey.h>

#include <cstdio>
#include <sstream>

namespace streambridge {

namespace {

// IEC 61937 Dolby Digital (AC-3) subtype: {00000092-0000-0010-8000-00aa00389b71}
const GUID kSubTypeIec61937DolbyDigital = {
    0x00000092, 0x0000, 0x0010, {0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71}
};

// IEC 61937 Dolby Digital Plus (E-AC-3 / Atmos JOC carrier): {0000000a-0cea-0010-8000-00aa00389b71}
const GUID kSubTypeIec61937DolbyDigitalPlus = {
    0x0000000a, 0x0cea, 0x0010, {0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71}
};

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
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    oss << buf;
                } else {
                    oss << c;
                }
        }
    }
    return oss.str();
}

std::string HrToHex(HRESULT hr) {
    char buf[16];
    std::snprintf(buf, sizeof(buf), "0x%08lX", static_cast<unsigned long>(hr));
    return buf;
}

AudioFormatSupport TestExclusiveFormat(
    IAudioClient* client,
    const std::string& name,
    WORD channels,
    DWORD sample_rate,
    WORD bits_per_sample,
    DWORD channel_mask,
    const GUID& sub_format,
    bool is_bitstream) {
    WAVEFORMATEXTENSIBLE wfx = {};
    wfx.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE;
    wfx.Format.nChannels = channels;
    wfx.Format.nSamplesPerSec = sample_rate;
    wfx.Format.wBitsPerSample = bits_per_sample;
    wfx.Format.nBlockAlign = (channels * bits_per_sample) / 8;
    wfx.Format.nAvgBytesPerSec = wfx.Format.nSamplesPerSec * wfx.Format.nBlockAlign;
    wfx.Format.cbSize = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
    wfx.Samples.wValidBitsPerSample = bits_per_sample;
    wfx.dwChannelMask = channel_mask;
    wfx.SubFormat = sub_format;

    HRESULT hr = client->IsFormatSupported(
        AUDCLNT_SHAREMODE_EXCLUSIVE,
        reinterpret_cast<const WAVEFORMATEX*>(&wfx),
        nullptr);

    AudioFormatSupport result;
    result.format_name = name;
    result.channels = channels;
    result.sample_rate = static_cast<int>(sample_rate);
    result.bits_per_sample = bits_per_sample;
    result.is_bitstream = is_bitstream;
    result.supported_exclusive = (hr == S_OK);
    result.hr_hex = HrToHex(hr);
    return result;
}

}  // namespace

WasapiEndpointReport ProbeDefaultWasapiEndpoint() {
    WasapiEndpointReport report;
    HRESULT hr_init = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    bool should_uninit = SUCCEEDED(hr_init);

    IMMDeviceEnumerator* enumerator = nullptr;
    IMMDevice* device = nullptr;
    IPropertyStore* props = nullptr;
    IAudioClient* audio_client = nullptr;
    WAVEFORMATEX* mix_format = nullptr;

    HRESULT hr = CoCreateInstance(
        __uuidof(MMDeviceEnumerator),
        nullptr,
        CLSCTX_ALL,
        __uuidof(IMMDeviceEnumerator),
        reinterpret_cast<void**>(&enumerator));

    if (FAILED(hr) || !enumerator) {
        report.error_message = "Failed to create MMDeviceEnumerator: " + HrToHex(hr);
        if (should_uninit) CoUninitialize();
        return report;
    }

    hr = enumerator->GetDefaultAudioEndpoint(eRender, eMultimedia, &device);
    if (FAILED(hr) || !device) {
        report.error_message = "No active default audio render endpoint found: " + HrToHex(hr);
        enumerator->Release();
        if (should_uninit) CoUninitialize();
        return report;
    }

    report.endpoint_found = true;
    report.endpoint_id_redacted = "default_multimedia_render";

    if (SUCCEEDED(device->OpenPropertyStore(STGM_READ, &props)) && props) {
        PROPVARIANT var_name;
        PropVariantInit(&var_name);
        if (SUCCEEDED(props->GetValue(PKEY_Device_FriendlyName, &var_name)) &&
            var_name.vt == VT_LPWSTR) {
            report.endpoint_name = WideToUtf8(var_name.pwszVal);
        }
        PropVariantClear(&var_name);
    }

    hr = device->Activate(
        __uuidof(IAudioClient),
        CLSCTX_ALL,
        nullptr,
        reinterpret_cast<void**>(&audio_client));

    if (SUCCEEDED(hr) && audio_client) {
        if (SUCCEEDED(audio_client->GetMixFormat(&mix_format)) && mix_format) {
            report.os_shared_channels = mix_format->nChannels;
            report.os_shared_sample_rate = static_cast<int>(mix_format->nSamplesPerSec);
            report.os_configured_stereo = (mix_format->nChannels == 2);
            CoTaskMemFree(mix_format);
            mix_format = nullptr;
        }

        // Probe Gate G4 Audio Acceptance Contract formats in WASAPI Exclusive mode:
        // 1. Stereo PCM 2.0 (16-bit & 24-bit 48kHz)
        report.tested_formats.push_back(TestExclusiveFormat(
            audio_client,
            "PCM_2.0_48kHz_16bit",
            2,
            48000,
            16,
            KSAUDIO_SPEAKER_STEREO,
            KSDATAFORMAT_SUBTYPE_PCM,
            false));

        report.tested_formats.push_back(TestExclusiveFormat(
            audio_client,
            "PCM_2.0_48kHz_24bit",
            2,
            48000,
            24,
            KSAUDIO_SPEAKER_STEREO,
            KSDATAFORMAT_SUBTYPE_PCM,
            false));

        // 2. Multichannel decoded PCM 5.1 & 7.1
        report.tested_formats.push_back(TestExclusiveFormat(
            audio_client,
            "PCM_5.1_48kHz_16bit",
            6,
            48000,
            16,
            KSAUDIO_SPEAKER_5POINT1_SURROUND,
            KSDATAFORMAT_SUBTYPE_PCM,
            false));

        report.tested_formats.push_back(TestExclusiveFormat(
            audio_client,
            "PCM_7.1_48kHz_16bit",
            8,
            48000,
            16,
            KSAUDIO_SPEAKER_7POINT1_SURROUND,
            KSDATAFORMAT_SUBTYPE_PCM,
            false));

        // 3. IEC 61937 AC-3 (Dolby Digital 48kHz 2ch IEC transport)
        report.tested_formats.push_back(TestExclusiveFormat(
            audio_client,
            "IEC61937_AC3_DolbyDigital",
            2,
            48000,
            16,
            KSAUDIO_SPEAKER_5POINT1,
            kSubTypeIec61937DolbyDigital,
            true));

        // 4. IEC 61937 E-AC-3 (Dolby Digital Plus / Atmos JOC 192kHz 2ch HBR transport)
        report.tested_formats.push_back(TestExclusiveFormat(
            audio_client,
            "IEC61937_EAC3_DolbyDigitalPlus_AtmosJOC",
            2,
            192000,
            16,
            KSAUDIO_SPEAKER_5POINT1,
            kSubTypeIec61937DolbyDigitalPlus,
            true));

        // Release IAudioClient immediately so we never contend with playback
        audio_client->Release();
        audio_client = nullptr;
    } else {
        report.error_message = "Failed to activate IAudioClient: " + HrToHex(hr);
    }

    if (props) props->Release();
    if (device) device->Release();
    if (enumerator) enumerator->Release();
    if (should_uninit) CoUninitialize();
    return report;
}

std::string FormatWasapiReportJson(const WasapiEndpointReport& report) {
    std::ostringstream oss;
    oss << "{"
        << "\"endpoint_found\":" << (report.endpoint_found ? "true" : "false") << ","
        << "\"endpoint_name\":\"" << EscapeJson(report.endpoint_name) << "\","
        << "\"os_shared_channels\":" << report.os_shared_channels << ","
        << "\"os_shared_sample_rate\":" << report.os_shared_sample_rate << ","
        << "\"os_configured_stereo\":" << (report.os_configured_stereo ? "true" : "false") << ","
        << "\"error_message\":\"" << EscapeJson(report.error_message) << "\","
        << "\"tested_formats\":[";
    for (size_t i = 0; i < report.tested_formats.size(); ++i) {
        const auto& f = report.tested_formats[i];
        if (i > 0) oss << ",";
        oss << "{"
            << "\"format_name\":\"" << EscapeJson(f.format_name) << "\","
            << "\"channels\":" << f.channels << ","
            << "\"sample_rate\":" << f.sample_rate << ","
            << "\"bits_per_sample\":" << f.bits_per_sample << ","
            << "\"is_bitstream\":" << (f.is_bitstream ? "true" : "false") << ","
            << "\"supported_exclusive\":" << (f.supported_exclusive ? "true" : "false") << ","
            << "\"hr\":\"" << EscapeJson(f.hr_hex) << "\""
            << "}";
    }
    oss << "]}";
    return oss.str();
}

}  // namespace streambridge
