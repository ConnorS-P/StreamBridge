"""Native Kodi Skin OSD Overlay & Silent Chroma-Key Proxy Video Generator.

Implements Option B from the StreamBridge In-Kodi Surface Plan:
- Generates a tiny, lossless 24-bit RGB (`BI_RGB`) uncompressed RIFF `.avi` video stream
  with exact `RGB(1, 2, 1)` (`#010201`) frames and ZERO audio tracks (so Kodi's `VideoPlayer`
  activates `Window 12005` / `FullScreenVideo.xml` / `VideoOSD.xml` without touching WASAPI
  and without YUV->RGB color rounding).
- Uses `StreamBridge.Overlay.dll` (or Win32 `SetLayeredWindowAttributes` with `LWA_COLORKEY`)
  on `kodi_hwnd` so the `RGB(1, 2, 1)` proxy video area becomes a 100% transparent DWM
  hardware cutout revealing `StreamBridge.Player`'s PlayReady `WebView2` video surface
  underneath, while Kodi's native skin OSD renders on top.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import struct
import sys
from typing import Any

CHROMA_KEY_RGB: tuple[int, int, int] = (16, 48, 32)


def generate_chroma_key_avi(
    output_path: Path,
    duration_seconds: int = 120,
    fps: int = 4,
    width: int = 2,
    height: int = 2,
    rgb: tuple[int, int, int] = CHROMA_KEY_RGB,
) -> Path:
    """Write a minimal, valid uncompressed 24-bit BI_RGB RIFF AVI file with 0 audio tracks."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    r, g, b = rgb
    # In 24-bit DIB (BI_RGB), pixel order is B, G, R
    row_bytes = width * 3
    padding = (4 - (row_bytes % 4)) % 4
    frame_data = (bytes([b, g, r]) * width + (b"\x00" * padding)) * height
    frame_size = len(frame_data)
    total_frames = max(1, duration_seconds * fps)
    usec_per_frame = int(1_000_000 // max(1, fps))

    # Build 'avih' (MainAVIHeader - 56 bytes)
    avih_payload = struct.pack(
        "<14I",
        usec_per_frame,          # dwMicroSecPerFrame
        frame_size * fps,        # dwMaxBytesPerSec
        0,                       # dwPaddingGranularity
        0x10,                    # dwFlags (AVIF_HASINDEX)
        total_frames,            # dwTotalFrames
        0,                       # dwInitialFrames
        1,                       # dwStreams (1 video, 0 audio)
        frame_size,              # dwSuggestedBufferSize
        width,                   # dwWidth
        height,                  # dwHeight
        0, 0, 0, 0,              # dwReserved[4]
    )
    avih_chunk = b"avih" + struct.pack("<I", len(avih_payload)) + avih_payload

    # Build 'strh' (AVIStreamHeader - 56 bytes)
    strh_payload = struct.pack(
        "<4s4sIHHIIIIIIII4h",
        b"vids",                 # fccType
        b"\x00\x00\x00\x00",     # fccHandler (BI_RGB uncompressed)
        0,                       # dwFlags
        0,                       # wPriority
        0,                       # wLanguage
        0,                       # dwInitialFrames
        1,                       # dwScale
        fps,                     # dwRate
        0,                       # dwStart
        total_frames,            # dwLength
        frame_size,              # dwSuggestedBufferSize
        10000,                   # dwQuality
        0,                       # dwSampleSize
        0, 0, width, height,     # rcFrame (left, top, right, bottom)
    )
    strh_chunk = b"strh" + struct.pack("<I", len(strh_payload)) + strh_payload

    # Build 'strf' (BITMAPINFOHEADER - 40 bytes)
    strf_payload = struct.pack(
        "<IiiHHIIiiII",
        40,                      # biSize
        width,                   # biWidth
        height,                  # biHeight
        1,                       # biPlanes
        24,                      # biBitCount (24-bit RGB)
        0,                       # biCompression (BI_RGB = 0)
        frame_size,              # biSizeImage
        0,                       # biXPelsPerMeter
        0,                       # biYPelsPerMeter
        0,                       # biClrUsed
        0,                       # biClrImportant
    )
    strf_chunk = b"strf" + struct.pack("<I", len(strf_payload)) + strf_payload

    strl_body = b"strl" + strh_chunk + strf_chunk
    strl_list = b"LIST" + struct.pack("<I", len(strl_body)) + strl_body

    hdrl_body = b"hdrl" + avih_chunk + strl_list
    hdrl_list = b"LIST" + struct.pack("<I", len(hdrl_body)) + hdrl_body

    # Build 'movi' list and 'idx1' index
    single_chunk = b"00db" + struct.pack("<I", frame_size) + frame_data
    movi_frames = single_chunk * total_frames
    movi_body = b"movi" + movi_frames
    movi_list = b"LIST" + struct.pack("<I", len(movi_body)) + movi_body

    idx_entries = bytearray()
    offset = 4  # relative to 'movi'
    chunk_total_len = 8 + frame_size
    for _ in range(total_frames):
        idx_entries += struct.pack("<4sIII", b"00db", 0x10, offset, frame_size)
        offset += chunk_total_len
    idx1_chunk = b"idx1" + struct.pack("<I", len(idx_entries)) + bytes(idx_entries)

    riff_body = b"AVI " + hdrl_list + movi_list + idx1_chunk
    riff_file = b"RIFF" + struct.pack("<I", len(riff_body)) + riff_body
    output_path.write_bytes(riff_file)
    return output_path


def enable_kodi_osd_cutout(
    overlay_dll_path: Path | None = None,
    rgb: tuple[int, int, int] = CHROMA_KEY_RGB,
) -> bool:
    """Enable DWM color-key transparency on `kodi_hwnd` so the chroma-key video area is transparent."""
    if sys.platform != "win32":
        return False

    r, g, b = rgb
    if overlay_dll_path and overlay_dll_path.exists():
        try:
            dll = ctypes.WinDLL(str(overlay_dll_path))
            return bool(dll.SB_EnableKodiOverlayCutout(r, g, b))
        except OSError:
            pass

    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW("Kodi", None) or user32.FindWindowW(None, "Kodi")
    if not hwnd:
        return False

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    LWA_COLORKEY = 0x00000001
    ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED)
    colorref = (r & 0xFF) | ((g & 0xFF) << 8) | ((b & 0xFF) << 16)
    return bool(user32.SetLayeredWindowAttributes(hwnd, colorref, 0, LWA_COLORKEY))


def disable_kodi_osd_cutout(overlay_dll_path: Path | None = None) -> bool:
    """Remove DWM color-key transparency from `kodi_hwnd` after playback finishes."""
    if sys.platform != "win32":
        return False

    if overlay_dll_path and overlay_dll_path.exists():
        try:
            dll = ctypes.WinDLL(str(overlay_dll_path))
            return bool(dll.SB_DisableKodiOverlayCutout())
        except OSError:
            pass

    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW("Kodi", None) or user32.FindWindowW(None, "Kodi")
    if not hwnd:
        return False

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if ex_style & WS_EX_LAYERED:
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex_style & ~WS_EX_LAYERED)
        user32.RedrawWindow(hwnd, None, None, 0x0001 | 0x0004 | 0x0400 | 0x0080)
    return True


def send_player_control(action: str, value_seconds: int = 0) -> bool:
    """Forward Kodi OSD player actions (`pause`, `resume`, `seek`, `seek_by`, `osd_mode`, `stop`) to `StreamBridge.Player.exe`."""
    if sys.platform != "win32":
        return False

    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW("StreamBridgeWebViewHostClass", "StreamBridge Surface")
    if not hwnd:
        return False

    post_msg = user32.PostMessageW
    post_msg.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    post_msg.restype = wintypes.BOOL

    WM_CLOSE = 0x0010
    WM_APP = 0x8000
    act = action.lower()
    if act == "pause":
        return bool(post_msg(hwnd, WM_APP + 1, 0, 0))
    if act in ("resume", "play"):
        return bool(post_msg(hwnd, WM_APP + 2, 0, 0))
    if act == "seek":
        return bool(post_msg(hwnd, WM_APP + 3, max(0, int(value_seconds)), 0))
    if act == "osd_mode":
        return bool(post_msg(hwnd, WM_APP + 4, max(0, int(value_seconds)), 0))
    if act == "seek_by":
        return bool(post_msg(hwnd, WM_APP + 5, 0, int(value_seconds)))
    if act == "set_subtitle":
        return bool(post_msg(hwnd, WM_APP + 6, max(0, int(value_seconds)), 0))
    if act == "set_audio":
        return bool(post_msg(hwnd, WM_APP + 7, max(0, int(value_seconds)), 0))
    if act in ("stop", "ended", "close"):
        return bool(post_msg(hwnd, WM_CLOSE, 0, 0))
    return False


def read_live_telemetry() -> dict[str, Any] | None:
    """Read live `<video>` telemetry (`state`, `position_seconds`, `duration_seconds`) from `StreamBridge.Player`."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    path = Path(local_app_data) / "StreamBridge" / "live_telemetry.json"
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
