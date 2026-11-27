"""Kodi JSON-RPC v13.5 client wrapper and scan/clean loop guard for StreamBridge.

Architecture rules:
- Use Kodi's supported library scanner and JSON-RPC (`VideoLibrary.Scan`, `VideoLibrary.Clean`,
  `VideoLibrary.SetMovieDetails`, `VideoLibrary.SetEpisodeDetails`), never direct writes to MyVideos*.db.
- Guard self-generated scan/clean events against infinite refresh loops.
- Coalesce concurrent scan requests.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Optional

from .models import MediaType, PlaybackState, Title


class KodiJsonRpcClient:
    """Thin wrapper over `xbmc.executeJSONRPC` (or an injected transport for unit testing)."""

    def __init__(
        self,
        rpc_Transport: Optional[Callable[[str], str]] = None,
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

    def call(self, method: str, params: Optional[dict[str, Any]] = None) -> Any:
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

    def request_targeted_scan(self, directory: Optional[str] = None, show_dialogs: bool = False) -> bool:
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

    def request_targeted_clean(self, directory: Optional[str] = None, show_dialogs: bool = False) -> bool:
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

    def sync_playback_state_to_kodi(
        self, title: Title, state: PlaybackState, kodi_db_id: Optional[int] = None
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
