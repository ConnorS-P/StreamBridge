"""Kodi JSON-RPC v13.5 client wrapper and scan/clean loop guard for StreamBridge.

Architecture rules:
- Use Kodi's supported library scanner and JSON-RPC (`VideoLibrary.Scan`, `VideoLibrary.Clean`,
  `VideoLibrary.SetMovieDetails`, `VideoLibrary.SetEpisodeDetails`), never direct writes to MyVideos*.db.
- Guard self-generated scan/clean events against infinite refresh loops.
- Coalesce concurrent scan requests.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import Any
from collections.abc import Callable

from .models import MediaType, PlaybackState, Title


class KodiJsonRpcClient:
    """Thin wrapper over `xbmc.executeJSONRPC` (or an injected transport for unit testing)."""

    def __init__(
        self,
        rpc_Transport: Callable[[str], str] | None = None,
        self_trigger_ttl_seconds: float = 30.0,
    ) -> None:
        self._transport = rpc_Transport or self._default_kodi_transport
        self._self_trigger_ttl = self_trigger_ttl_seconds
        self._lock = threading.Lock()
        self._self_scan_timestamps: list[float] = []
        self._self_clean_timestamps: list[float] = []
        self._kodi_scanning = False
        self._pending_followup_scan = False
        self._pending_followup_clean = False

    @staticmethod
    def _default_kodi_transport(request_json: str) -> str:
        try:
            import xbmc  # type: ignore[import-not-found]

            return xbmc.executeJSONRPC(request_json)
        except ImportError:
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": "OK"})

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Invoke a JSON-RPC v13.5 method and return `result`."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        raw_response = self._transport(json.dumps(payload))
        parsed = json.loads(raw_response)
        if "error" in parsed:
            raise RuntimeError(f"Kodi JSON-RPC error in {method}: {parsed['error']}")
        return parsed.get("result")

    def mark_kodi_scan_started(self) -> bool:
        """Called by `xbmc.Monitor.onScanStarted`.

        Returns `True` if this scan was initiated by the user/schedule (so StreamBridge should
        queue a provider refresh), or `False` if it was self-triggered by StreamBridge.
        """
        now = time.time()
        with self._lock:
            self._kodi_scanning = True
            self._self_scan_timestamps = [
                t for t in self._self_scan_timestamps if now - t < self._self_trigger_ttl
            ]
            if self._self_scan_timestamps:
                self._self_scan_timestamps.pop(0)
                return False
            return True

    def mark_kodi_scan_finished(self) -> tuple[bool, bool]:
        """Called by `xbmc.Monitor.onScanFinished`.

        Returns `(should_run_followup_scan, should_run_followup_clean)`.
        """
        with self._lock:
            self._kodi_scanning = False
            run_scan = self._pending_followup_scan
            run_clean = self._pending_followup_clean
            self._pending_followup_scan = False
            self._pending_followup_clean = False
            return run_scan, run_clean

    def request_targeted_scan(self, directory: str | None = None, show_dialogs: bool = False) -> bool:
        """Trigger `VideoLibrary.Scan` or queue a follow-up scan if Kodi is currently scanning."""
        with self._lock:
            if self._kodi_scanning:
                self._pending_followup_scan = True
                return False
            self._self_scan_timestamps.append(time.time())

        params: dict[str, Any] = {"showdialogs": show_dialogs}
        if directory:
            params["directory"] = directory
        self.call("VideoLibrary.Scan", params)
        return True

    def request_targeted_clean(self, directory: str | None = None, show_dialogs: bool = False) -> bool:
        """Trigger `VideoLibrary.Clean` or queue a follow-up clean if Kodi is currently busy."""
        with self._lock:
            if self._kodi_scanning:
                self._pending_followup_clean = True
                return False
            self._self_clean_timestamps.append(time.time())

        params: dict[str, Any] = {"showdialogs": show_dialogs}
        if directory:
            params["directory"] = directory
        self.call("VideoLibrary.Clean", params)
        return True

    def is_fake_fullscreen_enabled(self) -> bool:
        """Return `True` if Kodi's `videoscreen.fakefullscreen` ('Use fullscreen window') is enabled.

        StreamBridge's in-Kodi `WS_CHILD` surface and DWM skin OSD overlay require DXGI Flip Model
        ('Use fullscreen window' = ON) and are incompatible with legacy Direct3D Exclusive Fullscreen.
        """
        try:
            res = self.call(
                "Settings.GetSettingValue",
                {"setting": "videoscreen.fakefullscreen"},
            )
            if isinstance(res, dict) and "value" in res:
                return bool(res["value"])
            return True
        except Exception:
            return True

    def get_mouse_enabled(self) -> bool:
        """Return current value of Kodi's `input.enablemouse` setting."""
        try:
            res = self.call(
                "Settings.GetSettingValue",
                {"setting": "input.enablemouse"},
            )
            if isinstance(res, dict) and "value" in res:
                return bool(res["value"])
            return True
        except Exception:
            return True

    def set_mouse_enabled(self, enabled: bool) -> bool:
        """Set Kodi's `input.enablemouse` setting via JSON-RPC."""
        try:
            self.call(
                "Settings.SetSettingValue",
                {"setting": "input.enablemouse", "value": bool(enabled)},
            )
            return True
        except Exception:
            return False

    def reload_current_profile(self) -> str:
        """Reload the active Kodi profile so `CMediaSourceSettings::Load()` re-reads `sources.xml` into memory."""
        profile_label = "Master user"
        try:
            res = self.call("Profiles.GetCurrentProfile", {"properties": ["lockmode"]})
            if isinstance(res, dict) and res.get("label"):
                profile_label = str(res["label"])
            self.call("Profiles.LoadProfile", {"profile": profile_label})
        except Exception:
            return profile_label
        return profile_label

    def is_video_source_loaded_in_kodi(self, folder_path: Path) -> bool:
        """Check via `Files.GetSources` if `folder_path` is already loaded in Kodi's in-memory video sources."""
        norm_target = str(folder_path.resolve()).replace("/", "\\").rstrip("\\").lower()
        try:
            res = self.call("Files.GetSources", {"media": "video"})
            if isinstance(res, dict):
                for src in res.get("sources") or []:
                    file_val = str(src.get("file") or "").replace("/", "\\").rstrip("\\").lower()
                    if file_val == norm_target:
                        return True
        except Exception:
            return False
        return False

    def sync_mylist_to_kodi_favourites(self, titles: list[Title]) -> int:
        """Idempotently add `in_my_list` titles to Kodi's native Favourites via `Favourites.AddFavourite`."""
        existing_paths: set[str] = set()
        try:
            fav_res = self.call("Favourites.GetFavourites", {"properties": ["path"]})
            if isinstance(fav_res, dict):
                for fav in fav_res.get("favourites") or []:
                    if isinstance(fav, dict) and fav.get("path"):
                        existing_paths.add(str(fav["path"]))
        except Exception:
            existing_paths = set()

        synced = 0
        for title in titles:
            if not title.in_my_list:
                continue
            target_uri = f"plugin://plugin.video.streambridge/play/{title.title_id}"
            if target_uri in existing_paths:
                continue
            label = (
                f"{title.show_title} S{title.season:02d}E{title.episode:02d} - {title.title}"
                if title.media_type == MediaType.EPISODE and title.show_title and title.season and title.episode
                else f"{title.title} ({title.year})"
                if title.year
                else title.title
            )
            self.call(
                "Favourites.AddFavourite",
                {
                    "title": label,
                    "type": "media",
                    "path": target_uri,
                },
            )
            existing_paths.add(target_uri)
            synced += 1
        return synced

    def apply_mylist_tag_to_kodi_item(
        self, title: Title, kodi_db_id: int, tag_name: str = "Netflix My List"
    ) -> bool:
        """Apply the `Netflix My List` tag to a scraped Kodi library item via JSON-RPC."""
        if not title.in_my_list:
            return False
        if title.media_type == MediaType.MOVIE:
            self.call(
                "VideoLibrary.SetMovieDetails",
                {"movieid": kodi_db_id, "tag": [tag_name]},
            )
            return True
        if title.media_type == MediaType.SHOW:
            self.call(
                "VideoLibrary.SetTVShowDetails",
                {"tvshowid": kodi_db_id, "tag": [tag_name]},
            )
            return True
        return False

    def sync_playback_state_to_kodi(
        self, title: Title, state: PlaybackState, kodi_db_id: int | None = None
    ) -> bool:
        """Push verified resume point and playcount to Kodi via JSON-RPC when `kodi_db_id` is known."""
        if kodi_db_id is None:
            return False

        resume_payload = {
            "position": round(state.position_seconds, 1),
            "total": round(state.duration_seconds, 1),
        }
        if title.media_type == MediaType.MOVIE:
            self.call(
                "VideoLibrary.SetMovieDetails",
                {
                    "movieid": kodi_db_id,
                    "playcount": state.play_count,
                    "resume": resume_payload,
                },
            )
            return True
        if title.media_type == MediaType.EPISODE:
            self.call(
                "VideoLibrary.SetEpisodeDetails",
                {
                    "episodeid": kodi_db_id,
                    "playcount": state.play_count,
                    "resume": resume_payload,
                },
            )
            return True
        return False
