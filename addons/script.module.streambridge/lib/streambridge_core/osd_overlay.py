"""Native Kodi Skin OSD Overlay & Procedural BI_RLE8 HTTP Proxy Stream.

Provides:
- `VirtualRle8AviStream`: Procedural in-memory Microsoft `BI_RLE8` (`msrle`) `1920x1080` / `3840x2160`
  `24 fps` RIFF AVI generator using a constant 1-second super-block (1 keyframe + 23 2-byte delta frames =
  19.6 KB/s for 1080p) so any byte range `[start, end]` is synthesized in O(1) with 0 disk I/O.
- `OsdProxyHttpServer`: Loopback HTTP 1.1 Range server (`http://127.0.0.1:<port>/proxy/<title_id>.avi`)
  that synthesizes the stream header from Netflix's live telemetry (`video_width`, `video_height`,
  `duration_seconds`) and paces `movi` byte delivery against Netflix's live `position_seconds` so Kodi's
  `CDVDClock` stays locked in sync and natively enters buffering when Netflix stalls.
"""

from __future__ import annotations

from collections.abc import Callable
import ctypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import struct
import sys
import threading
import time
from typing import Any

CHROMA_KEY_RGB: tuple[int, int, int] = (48, 48, 48)


def build_rle8_keyframe(width: int, height: int) -> bytes:
    """Encode a solid palette-index 0 frame of `(width, height)` in Microsoft `BI_RLE8` format."""
    line_runs = bytearray()
    rem = max(1, int(width))
    while rem > 0:
        run = min(254, rem)
        line_runs += bytes([run, 0])
        rem -= run
    line_runs += b"\x00\x00"  # End of scanline
    return bytes(line_runs) * max(1, int(height)) + b"\x00\x01"  # End of bitmap


class VirtualRle8AviStream:
    """Procedural O(1) byte-range `BI_RLE8` (`msrle`) AVI stream with constant 1-second byte stride."""

    def __init__(
        self,
        duration_seconds: int = 7200,
        fps: int = 24,
        width: int = 1920,
        height: int = 1080,
        rgb: tuple[int, int, int] = CHROMA_KEY_RGB,
    ) -> None:
        self.duration_seconds = max(1, int(duration_seconds))
        self.fps = max(1, int(fps))
        self.width = max(2, int(width))
        self.height = max(2, int(height))
        self.rgb = rgb
        self.total_frames = self.duration_seconds * self.fps

        r, g, b = rgb
        self.kf_data = build_rle8_keyframe(self.width, self.height)
        self.kf_size = len(self.kf_data)
        self.kf_chunk = b"00dc" + struct.pack("<I", self.kf_size) + self.kf_data

        self.delta_data = b"\x00\x01"
        self.delta_size = 2
        self.delta_chunk = b"00dc" + struct.pack("<I", self.delta_size) + self.delta_data

        # 1-second super-block: 1 full RLE8 keyframe + (fps - 1) 2-byte EoB delta frames
        self.sec_block = self.kf_chunk + (self.delta_chunk * (self.fps - 1))
        self.sec_stride = len(self.sec_block)

        usec_per_frame = int(1_000_000 // self.fps)
        avih_payload = struct.pack(
            "<14I",
            usec_per_frame,          # dwMicroSecPerFrame
            self.sec_stride,         # dwMaxBytesPerSec
            0,                       # dwPaddingGranularity
            0x10,                    # dwFlags (AVIF_HASINDEX)
            self.total_frames,       # dwTotalFrames
            0,                       # dwInitialFrames
            1,                       # dwStreams (1 video, 0 audio)
            self.kf_size,            # dwSuggestedBufferSize
            self.width,              # dwWidth
            self.height,             # dwHeight
            0, 0, 0, 0,              # dwReserved[4]
        )
        avih_chunk = b"avih" + struct.pack("<I", len(avih_payload)) + avih_payload

        strh_payload = struct.pack(
            "<4s4sIHHIIIIIIII4h",
            b"vids",                 # fccType
            b"MRLE",                 # fccHandler (Microsoft RLE8)
            0,                       # dwFlags
            0,                       # wPriority
            0,                       # wLanguage
            0,                       # dwInitialFrames
            1,                       # dwScale
            self.fps,                # dwRate
            0,                       # dwStart
            self.total_frames,       # dwLength
            self.kf_size,            # dwSuggestedBufferSize
            10000,                   # dwQuality
            0,                       # dwSampleSize
            0, 0, self.width, self.height,
        )
        strh_chunk = b"strh" + struct.pack("<I", len(strh_payload)) + strh_payload

        # BITMAPINFOHEADER (40 bytes) + 1 RGBQUAD palette entry (4 bytes)
        strf_payload = struct.pack(
            "<IiiHHIIiiIIBBBB",
            40,                      # biSize
            self.width,              # biWidth
            self.height,             # biHeight
            1,                       # biPlanes
            8,                       # biBitCount (8-bit palettized)
            1,                       # biCompression (BI_RLE8 = 1)
            self.kf_size,            # biSizeImage
            0,                       # biXPelsPerMeter
            0,                       # biYPelsPerMeter
            1,                       # biClrUsed
            1,                       # biClrImportant
            b & 0xFF,                # rgbBlue
            g & 0xFF,                # rgbGreen
            r & 0xFF,                # rgbRed
            0,                       # rgbReserved
        )
        strf_chunk = b"strf" + struct.pack("<I", len(strf_payload)) + strf_payload

        strl_body = b"strl" + strh_chunk + strf_chunk
        strl_list = b"LIST" + struct.pack("<I", len(strl_body)) + strl_body

        hdrl_body = b"hdrl" + avih_chunk + strl_list
        hdrl_list = b"LIST" + struct.pack("<I", len(hdrl_body)) + hdrl_body

        self.movi_payload_len = self.sec_stride * self.duration_seconds
        movi_list_size = 4 + self.movi_payload_len

        self.idx1_payload_len = 16 * self.total_frames
        self.idx1_header = b"idx1" + struct.pack("<I", self.idx1_payload_len)

        riff_body_len = 4 + len(hdrl_list) + 8 + movi_list_size + 8 + self.idx1_payload_len
        self.prefix = (
            b"RIFF"
            + struct.pack("<I", riff_body_len)
            + b"AVI "
            + hdrl_list
            + b"LIST"
            + struct.pack("<I", movi_list_size)
            + b"movi"
        )
        self.movi_data_start = len(self.prefix)
        self.idx1_start = self.movi_data_start + self.movi_payload_len
        self.idx1_data_start = self.idx1_start + 8
        self.total_size = self.idx1_data_start + self.idx1_payload_len

    def byte_offset_to_second(self, byte_offset: int) -> float:
        """Map a byte offset inside `movi` to its presentation timestamp in seconds (or `-1.0` for header/idx1)."""
        if byte_offset < self.movi_data_start or byte_offset >= self.idx1_start:
            return -1.0
        rel = byte_offset - self.movi_data_start
        sec_idx = rel // self.sec_stride
        rem_in_sec = rel % self.sec_stride
        if rem_in_sec < len(self.kf_chunk):
            frame_in_sec = 0
        else:
            frame_in_sec = 1 + ((rem_in_sec - len(self.kf_chunk)) // len(self.delta_chunk))
        return float(sec_idx) + (float(frame_in_sec) / float(self.fps))

    def _build_idx1_entry(self, frame_index: int) -> bytes:
        sec_idx = frame_index // self.fps
        frame_in_sec = frame_index % self.fps
        base_off = 4 + sec_idx * self.sec_stride
        if frame_in_sec == 0:
            return struct.pack("<4sIII", b"00dc", 0x10, base_off, self.kf_size)
        off = base_off + len(self.kf_chunk) + (frame_in_sec - 1) * len(self.delta_chunk)
        return struct.pack("<4sIII", b"00dc", 0x00, off, self.delta_size)

    def read_range(self, start: int, length: int) -> bytes:
        """Synthesize `[start, start + length)` bytes of the virtual AVI in O(1) memory."""
        if start < 0 or length <= 0 or start >= self.total_size:
            return b""
        end = min(self.total_size, start + length)
        out = bytearray()
        pos = start

        # 1. Prefix (RIFF + hdrl + LIST movi header)
        if pos < self.movi_data_start:
            seg_end = min(end, self.movi_data_start)
            out += self.prefix[pos:seg_end]
            pos = seg_end

        # 2. Virtual movi body (repeated 1-second super-blocks)
        if pos < end and pos < self.idx1_start:
            seg_end = min(end, self.idx1_start)
            while pos < seg_end:
                rel = (pos - self.movi_data_start) % self.sec_stride
                take = min(seg_end - pos, self.sec_stride - rel)
                out += self.sec_block[rel : rel + take]
                pos += take

        # 3. idx1 8-byte header
        if pos < end and pos < self.idx1_data_start:
            seg_end = min(end, self.idx1_data_start)
            hdr_off = pos - self.idx1_start
            out += self.idx1_header[hdr_off : hdr_off + (seg_end - pos)]
            pos = seg_end

        # 4. Virtual idx1 entries (16 bytes per frame)
        if pos < end and pos < self.total_size:
            seg_end = min(end, self.total_size)
            while pos < seg_end:
                idx_rel = pos - self.idx1_data_start
                frame_idx = idx_rel // 16
                entry_off = idx_rel % 16
                entry_bytes = self._build_idx1_entry(frame_idx)
                take = min(seg_end - pos, 16 - entry_off)
                out += entry_bytes[entry_off : entry_off + take]
                pos += take

        return bytes(out)


def generate_chroma_key_avi(
    output_path: Path,
    duration_seconds: int = 120,
    fps: int = 24,
    width: int = 1920,
    height: int = 1080,
    rgb: tuple[int, int, int] = CHROMA_KEY_RGB,
) -> Path:
    """Write a valid `BI_RLE8` (`msrle`) `1920x1080` `24 fps` RIFF AVI file with 0 audio tracks."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stream = VirtualRle8AviStream(
        duration_seconds=duration_seconds,
        fps=fps,
        width=width,
        height=height,
        rgb=rgb,
    )
    if output_path.exists() and output_path.stat().st_size == stream.total_size:
        return output_path

    with output_path.open("wb") as f:
        offset = 0
        chunk_sz = 262144
        while offset < stream.total_size:
            f.write(stream.read_range(offset, chunk_sz))
            offset += chunk_sz
    return output_path


class OsdProxyHttpServer:
    """Loopback HTTP 1.1 Range server streaming procedural `VirtualRle8AviStream` frames paced by Netflix telemetry."""

    def __init__(
        self,
        should_pace_callback: Callable[[], bool] | None = None,
        default_duration_provider: Callable[[str], int] | None = None,
    ) -> None:
        self._should_pace_cb = should_pace_callback
        self._default_dur_cb = default_duration_provider
        self._streams: dict[str, VirtualRle8AviStream] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    def start(self) -> int:
        if self._server is not None and self.port > 0:
            return self.port

        parent = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _resolve_stream(self) -> VirtualRle8AviStream:
                path_part = self.path.split("?", 1)[0].strip("/")
                title_id = Path(path_part).stem or "default"
                return parent.get_or_create_stream(title_id)

            def do_HEAD(self) -> None:
                stream = self._resolve_stream()
                self.send_response(200)
                self.send_header("Content-Type", "video/x-msvideo")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(stream.total_size))
                self.send_header("Connection", "close")
                self.end_headers()

            def do_GET(self) -> None:
                stream = self._resolve_stream()
                range_hdr = self.headers.get("Range", "")
                start = 0
                end = stream.total_size - 1
                is_partial = False

                if range_hdr.startswith("bytes="):
                    is_partial = True
                    spec = range_hdr[6:].split(",", 1)[0].strip()
                    if "-" in spec:
                        s_part, e_part = spec.split("-", 1)
                        if s_part.strip():
                            start = max(0, min(stream.total_size - 1, int(s_part.strip())))
                            if e_part.strip():
                                end = max(start, min(stream.total_size - 1, int(e_part.strip())))
                        elif e_part.strip():
                            suffix = int(e_part.strip())
                            start = max(0, stream.total_size - suffix)

                content_len = (end - start) + 1
                if is_partial:
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{stream.total_size}")
                else:
                    self.send_response(200)
                self.send_header("Content-Type", "video/x-msvideo")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(content_len))
                self.send_header("Connection", "close")
                self.end_headers()

                pos = start
                chunk_size = max(16384, stream.sec_stride)
                try:
                    while pos <= end:
                        take = min(chunk_size, (end - pos) + 1)
                        frame_sec = stream.byte_offset_to_second(pos)
                        if frame_sec >= 0.0:
                            parent.wait_for_stream_allowance(frame_sec)
                        data = stream.read_range(pos, take)
                        if not data:
                            break
                        self.wfile.write(data)
                        self.wfile.flush()
                        pos += len(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, name="StreamBridgeOsdProxyHttp", daemon=True)
        self._thread.start()
        write_proxy_server_port(self.port)
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except OSError as exc:
                _ = exc
            self._server = None
            self.port = 0

    def invalidate_stream(self, title_id: str | None = None) -> None:
        with self._lock:
            if title_id is None:
                self._streams.clear()
            else:
                self._streams.pop(title_id, None)

    def get_or_create_stream(self, title_id: str) -> VirtualRle8AviStream:
        with self._lock:
            existing = self._streams.get(title_id)
            if existing is not None:
                return existing

        # Wait briefly (up to 3.5s) if StreamBridge.Player is still launching so we pick up real dimensions/duration
        tel: dict[str, Any] | None = None
        deadline = time.time() + 3.5
        while time.time() < deadline:
            tel = read_live_telemetry()
            if tel:
                updated_at = float(tel.get("updated_at") or 0.0)
                dur_val = float(tel.get("duration_seconds") or 0.0)
                st_val = str(tel.get("state") or "").lower()
                if (time.time() - updated_at) < 5.0 and dur_val > 30.0 and st_val in ("playing", "paused", "buffering"):
                    break
            time.sleep(0.15)

        vid_w = 1920
        vid_h = 1080
        dur_sec = 7200
        if self._default_dur_cb is not None:
            try:
                fallback_dur = int(self._default_dur_cb(title_id))
                if fallback_dur > 60:
                    dur_sec = fallback_dur + 30
            except (ValueError, TypeError) as exc:
                _ = exc

        if tel:
            tw = int(tel.get("video_width") or 0)
            th = int(tel.get("video_height") or 0)
            tdur = int(float(tel.get("duration_seconds") or 0.0))
            if tw >= 640 and th >= 360:
                vid_w = tw
                vid_h = th
            if tdur > 60:
                dur_sec = tdur + 30

        with self._lock:
            stream = VirtualRle8AviStream(
                duration_seconds=dur_sec,
                fps=24,
                width=vid_w,
                height=vid_h,
            )
            self._streams[title_id] = stream
            return stream

    def wait_for_stream_allowance(self, frame_sec: float) -> None:
        """Gate `movi` frame delivery so Kodi's playhead stays locked within Netflix's buffered/playing window."""
        if self._should_pace_cb is not None and not self._should_pace_cb():
            return

        wait_start = time.time()
        while (time.time() - wait_start) < 2.5:
            if self._should_pace_cb is not None and not self._should_pace_cb():
                return
            tel = read_live_telemetry()
            if not tel:
                return
            updated_at = float(tel.get("updated_at") or 0.0)
            if updated_at <= 0.0 or (time.time() - updated_at) > 4.0:
                return
            raw_state = str(tel.get("state") or "playing").lower()
            if raw_state == "stopped":
                return
            pos_sec = float(tel.get("position_seconds") or 0.0)
            buf_end = float(tel.get("buffered_end_seconds") or pos_sec)

            # Allow up to max(pos_sec + 3.0s, min(buf_end, pos_sec + 8.0s)) when playing so Kodi's
            # demuxer has enough frames for smooth 24fps decode without drifting ahead of Netflix,
            # while holding delivery when Netflix is actively buffering!
            if raw_state == "playing":
                allowed_sec = max(pos_sec + 3.5, min(buf_end, pos_sec + 6.0))
                if frame_sec <= allowed_sec or frame_sec < pos_sec:
                    return
            elif raw_state == "paused":
                if frame_sec <= (pos_sec + 3.5):
                    return
            time.sleep(0.06)


def write_proxy_server_port(port: int) -> None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return
    base = Path(local_app_data) / "StreamBridge"
    base.mkdir(parents=True, exist_ok=True)
    (base / "proxy_port.txt").write_text(str(int(port)), encoding="utf-8")


def read_proxy_server_port() -> int:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return 0
    path = Path(local_app_data) / "StreamBridge" / "proxy_port.txt"
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


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
    return bool(hwnd)


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
    return True


def send_player_control(action: str, value_seconds: int = 0) -> bool:
    """Forward Kodi OSD player actions (`pause`, `resume`, `seek`, `seek_by`, `osd_mode`, `set_rate`, `stop`) to `StreamBridge.Player.exe`."""
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
    if act == "set_rate":
        return bool(post_msg(hwnd, WM_APP + 8, max(50, min(150, int(value_seconds))), 0))
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
