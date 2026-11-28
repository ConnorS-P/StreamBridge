#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <wrl/client.h>

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstring>

using Microsoft::WRL::ComPtr;

namespace {

constexpr const wchar_t* kSharedMemName = L"Local\\StreamBridgeOsdSharedFrame";
constexpr size_t kMaxFrameWidth = 3840;
constexpr size_t kMaxFrameHeight = 2160;

struct SharedOsdHeader {
    uint32_t magic;       // 0x53424F53 ('SBOS')
    uint32_t width;
    uint32_t height;
    uint32_t frame_seq;
    uint32_t active;
    uint32_t reserved[11];
};

constexpr size_t kSharedMemSize = sizeof(SharedOsdHeader) + (kMaxFrameWidth * kMaxFrameHeight * 4);

std::atomic<bool> g_hook_enabled{false};
std::atomic<uint32_t> g_key_r{32};
std::atomic<uint32_t> g_key_g{112};
std::atomic<uint32_t> g_key_b{72};
std::atomic<uint32_t> g_frame_seq{0};

HANDLE g_shared_map = nullptr;
void* g_shared_view = nullptr;

using PresentFn = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain*, UINT, UINT);
PresentFn g_orig_present = nullptr;
void** g_hooked_vtable_slot = nullptr;

bool EnsureSharedMemory() {
    if (g_shared_view) return true;
    g_shared_map = CreateFileMappingW(
        INVALID_HANDLE_VALUE,
        nullptr,
        PAGE_READWRITE,
        static_cast<DWORD>(kSharedMemSize >> 32),
        static_cast<DWORD>(kSharedMemSize & 0xFFFFFFFFu),
        kSharedMemName);
    if (!g_shared_map) return false;
    g_shared_view = MapViewOfFile(g_shared_map, FILE_MAP_ALL_ACCESS, 0, 0, kSharedMemSize);
    return g_shared_view != nullptr;
}

void CaptureSwapChainBackbufferToSharedMem(IDXGISwapChain* sc) {
    if (!sc || !g_hook_enabled.load(std::memory_order_acquire)) return;
    if (!EnsureSharedMemory()) return;

    ComPtr<ID3D11Texture2D> backbuf;
    if (FAILED(sc->GetBuffer(0, IID_PPV_ARGS(&backbuf))) || !backbuf) return;

    ComPtr<ID3D11Device> dev;
    if (FAILED(sc->GetDevice(IID_PPV_ARGS(&dev))) || !dev) return;

    ComPtr<ID3D11DeviceContext> ctx;
    dev->GetImmediateContext(&ctx);
    if (!ctx) return;

    D3D11_TEXTURE2D_DESC desc = {};
    backbuf->GetDesc(&desc);
    if (desc.Width == 0 || desc.Height == 0 ||
        desc.Width > kMaxFrameWidth || desc.Height > kMaxFrameHeight) {
        return;
    }

    D3D11_TEXTURE2D_DESC st_desc = desc;
    st_desc.Usage = D3D11_USAGE_STAGING;
    st_desc.BindFlags = 0;
    st_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    st_desc.MiscFlags = 0;

    ComPtr<ID3D11Texture2D> staging;
    if (FAILED(dev->CreateTexture2D(&st_desc, nullptr, &staging)) || !staging) return;

    ctx->CopyResource(staging.Get(), backbuf.Get());
    D3D11_MAPPED_SUBRESOURCE mapped = {};
    if (FAILED(ctx->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped))) return;

    int kw = static_cast<int>(desc.Width);
    int kh = static_cast<int>(desc.Height);
    const uint8_t* src_base = static_cast<const uint8_t*>(mapped.pData);

    int sample_y = kh / 5;
    const uint32_t* sample_row = reinterpret_cast<const uint32_t*>(src_base + sample_y * mapped.RowPitch);
    uint32_t p1 = sample_row[kw / 8];
    uint32_t p2 = sample_row[kw - kw / 8];
    uint32_t ref_px = ((p1 >> 8) & 0xFF) >= ((p2 >> 8) & 0xFF) ? p1 : p2;
    int kr = static_cast<int>((ref_px >> 16) & 0xFF);
    int kg = static_cast<int>((ref_px >> 8) & 0xFF);
    int kb = static_cast<int>(ref_px & 0xFF);
    int k_dg_r = std::max(8, kg - kr);

    auto* hdr = static_cast<SharedOsdHeader*>(g_shared_view);
    auto* dst_pixels = reinterpret_cast<uint32_t*>(static_cast<uint8_t*>(g_shared_view) + sizeof(SharedOsdHeader));

    for (int y = 0; y < kh; ++y) {
        const uint32_t* src_row = reinterpret_cast<const uint32_t*>(src_base + y * mapped.RowPitch);
        uint32_t* dst_row = dst_pixels + y * kw;
        for (int x = 0; x < kw; ++x) {
            uint32_t c = src_row[x];
            int a_hw = static_cast<int>((c >> 24) & 0xFF);
            int b = static_cast<int>(c & 0xFF);
            int g = static_cast<int>((c >> 8) & 0xFF);
            int r = static_cast<int>((c >> 16) & 0xFF);
            int max_rgb = std::max(r, std::max(g, b));

            // If Kodi's render target already wrote hardware alpha < 255, preserve it directly!
            if (a_hw < 255) {
                int r_pre = std::min(a_hw, (r * a_hw) / 255);
                int g_pre = std::min(a_hw, (g * a_hw) / 255);
                int b_pre = std::min(a_hw, (b * a_hw) / 255);
                dst_row[x] = (static_cast<uint32_t>(a_hw) << 24) |
                             (static_cast<uint32_t>(r_pre) << 16) |
                             (static_cast<uint32_t>(g_pre) << 8) |
                             static_cast<uint32_t>(b_pre);
                continue;
            }

            if (max_rgb <= 5 || (std::abs(r - kr) <= 8 && std::abs(g - kg) <= 8 && std::abs(b - kb) <= 8)) {
                dst_row[x] = 0x00000000;
                continue;
            }

            if (kg >= 16) {
                int dg_r = g - r;
                int dg_b = g - b;
                int db_r = b - r;
                if (dg_r > 1 && db_r >= -4 && dg_b >= -4 && std::abs(dg_b - db_r) <= 10 && g <= (kg + 16)) {
                    int bg_frac_256 = std::min(256, std::max(0, (dg_r * 256) / k_dg_r));
                    int alpha = 255 - ((bg_frac_256 * 255) >> 8);
                    if (alpha <= 10) {
                        dst_row[x] = 0x00000000;
                    } else {
                        int r_clean = std::min(alpha, std::max(0, r - ((kr * bg_frac_256) >> 8)));
                        int g_clean = std::min(alpha, std::max(0, g - ((kg * bg_frac_256) >> 8)));
                        int b_clean = std::min(alpha, std::max(0, b - ((kb * bg_frac_256) >> 8)));
                        dst_row[x] = (static_cast<uint32_t>(alpha) << 24) |
                                     (static_cast<uint32_t>(r_clean) << 16) |
                                     (static_cast<uint32_t>(g_clean) << 8) |
                                     static_cast<uint32_t>(b_clean);
                    }
                    continue;
                }
            }

            dst_row[x] = 0xFF000000 | (c & 0x00FFFFFF);
        }
    }

    ctx->Unmap(staging.Get(), 0);

    hdr->width = desc.Width;
    hdr->height = desc.Height;
    hdr->active = 1;
    hdr->magic = 0x53424F53u;
    hdr->frame_seq = g_frame_seq.fetch_add(1, std::memory_order_release) + 1;
}

HRESULT STDMETHODCALLTYPE HookedPresent(IDXGISwapChain* sc, UINT sync_interval, UINT flags) {
    CaptureSwapChainBackbufferToSharedMem(sc);
    if (g_orig_present) {
        return g_orig_present(sc, sync_interval, flags);
    }
    return S_OK;
}

bool InstallDxgiSwapChainVtableHook() {
    if (g_orig_present) return true;

    HINSTANCE hinst = GetModuleHandleW(nullptr);
    const wchar_t* kDummyClass = L"StreamBridgeDummyDxgiHookClass";
    WNDCLASSEXW wc = {};
    wc.cbSize = sizeof(WNDCLASSEXW);
    wc.lpfnWndProc = DefWindowProcW;
    wc.hInstance = hinst;
    wc.lpszClassName = kDummyClass;
    RegisterClassExW(&wc);

    HWND dummy_hwnd = CreateWindowExW(
        0, kDummyClass, L"Dummy", WS_POPUP, 0, 0, 16, 16, nullptr, nullptr, hinst, nullptr);
    if (!dummy_hwnd) return false;

    DXGI_SWAP_CHAIN_DESC sd = {};
    sd.BufferCount = 1;
    sd.BufferDesc.Width = 16;
    sd.BufferDesc.Height = 16;
    sd.BufferDesc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.OutputWindow = dummy_hwnd;
    sd.SampleDesc.Count = 1;
    sd.Windowed = TRUE;
    sd.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;

    ComPtr<ID3D11Device> dev;
    ComPtr<ID3D11DeviceContext> ctx;
    ComPtr<IDXGISwapChain> sc;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    HRESULT hr = D3D11CreateDeviceAndSwapChain(
        nullptr,
        D3D_DRIVER_TYPE_HARDWARE,
        nullptr,
        0,
        nullptr,
        0,
        D3D11_SDK_VERSION,
        &sd,
        &sc,
        &dev,
        &fl,
        &ctx);
    if (FAILED(hr) || !sc) {
        DestroyWindow(dummy_hwnd);
        return false;
    }

    void** vtable = *reinterpret_cast<void***>(sc.Get());
    void** present_slot = &vtable[8];
    DWORD old_prot = 0;
    if (VirtualProtect(present_slot, sizeof(void*), PAGE_EXECUTE_READWRITE, &old_prot)) {
        g_orig_present = reinterpret_cast<PresentFn>(*present_slot);
        *present_slot = reinterpret_cast<void*>(&HookedPresent);
        g_hooked_vtable_slot = present_slot;
        VirtualProtect(present_slot, sizeof(void*), old_prot, &old_prot);
    }

    DestroyWindow(dummy_hwnd);
    return g_orig_present != nullptr;
}

}  // namespace

extern "C" {

__declspec(dllexport) int SB_EnableKodiOverlayCutout(unsigned int r, unsigned int g, unsigned int b) {
    g_key_r.store(r & 0xFF, std::memory_order_relaxed);
    g_key_g.store(g & 0xFF, std::memory_order_relaxed);
    g_key_b.store(b & 0xFF, std::memory_order_relaxed);
    if (!EnsureSharedMemory()) return 0;
    if (!InstallDxgiSwapChainVtableHook()) return 0;
    g_hook_enabled.store(true, std::memory_order_release);
    return 1;
}

__declspec(dllexport) int SB_DisableKodiOverlayCutout() {
    g_hook_enabled.store(false, std::memory_order_release);
    if (g_shared_view) {
        auto* hdr = static_cast<SharedOsdHeader*>(g_shared_view);
        hdr->active = 0;
    }
    return 1;
}

}  // extern "C"
