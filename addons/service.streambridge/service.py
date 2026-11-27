"""StreamBridge Background Service (`service.streambridge`).

Responsibilities (Section 1 & Section 3 of Design v0.1):
- Queue due provider refresh on startup and user-initiated library scans.
- Coalesce concurrent scan events and guard against self-generated scan loops.
- After reconciliation commit, request one targeted follow-up scan/clean if changes arrived
  while Kodi was already scanning.
"""

from __future__ import annotations

from pathlib import Path
import time

from streambridge_core.adapters.base import ProviderAdapter
from streambridge_core.adapters.fixture import FixtureAdapter
from streambridge_core.adapters.netflix_catalog import NetflixCatalogAdapter
from streambridge_core.db import StreamBridgeStore
from streambridge_core.kodi_bridge import IN_KODI, KodiBridge
from streambridge_core.kodi_rpc import KodiJsonRpcClient
from streambridge_core.osd_overlay import disable_kodi_osd_cutout, send_player_control
from streambridge_core.projection import ProjectionEngine
from streambridge_core.reconciler import ReconciliationEngine, ReconciliationResult

try:
    import xbmc  # type: ignore[import-not-found]

    _BaseMonitor = xbmc.Monitor
    _BasePlayer = xbmc.Player
except ImportError:

    class _BaseMonitor:  # type: ignore[no-redef]
        def waitForAbort(self, timeout: float = 1.0) -> bool:
            return True

        def abortRequested(self) -> bool:
            return True

    class _BasePlayer:  # type: ignore[no-redef]
        pass


class StreamBridgePlayerMonitor(_BasePlayer):  # type: ignore[valid-type,misc]
    """Monitors Kodi's native VideoPlayer during OSD proxy playback to keep skin OSD and underlay synced."""

    def __init__(self, kodi_bridge: KodiBridge) -> None:
        super().__init__()
        self.kodi = kodi_bridge
        self.last_action: str | None = None
        self.last_seek_seconds: float | None = None

    def onPlayBackPaused(self) -> None:
        self.last_action = "pause"
        send_player_control("pause")

    def onPlayBackResumed(self) -> None:
        self.last_action = "resume"
        send_player_control("resume")

    def onPlayBackSeek(self, time_ms: int, seek_offset: int) -> None:
        self.last_action = "seek"
        self.last_seek_seconds = float(time_ms) / 1000.0
        send_player_control("seek", int(self.last_seek_seconds))

    def onPlayBackStopped(self) -> None:
        self.last_action = "stop"
        send_player_control("stop")
        disable_kodi_osd_cutout()
        self.kodi.clear_ecosystem_window_properties()

    def onPlayBackEnded(self) -> None:
        self.last_action = "ended"
        send_player_control("stop")
        disable_kodi_osd_cutout()
        self.kodi.clear_ecosystem_window_properties()


class StreamBridgeServiceMonitor(_BaseMonitor):  # type: ignore[valid-type,misc]
    """Kodi `xbmc.Monitor` subclass managing non-blocking catalog sync and scan/clean hooks."""

    def __init__(
        self,
        store: StreamBridgeStore | None = None,
        export_root: Path | None = None,
        adapter: ProviderAdapter | None = None,
        kodi_bridge: KodiBridge | None = None,
        kodi_rpc: KodiJsonRpcClient | None = None,
        region: str = "US",
    ) -> None:
        super().__init__()
        self.kodi = kodi_bridge or KodiBridge("plugin.video.streambridge")
        self.rpc = kodi_rpc or KodiJsonRpcClient()
        profile_dir = self.kodi.get_profile_dir()
        self.store = store or StreamBridgeStore(profile_dir / "streambridge.db")
        self.export_root = export_root or self.kodi.get_export_root(profile_dir / "library")
        self.export_root.mkdir(parents=True, exist_ok=True)
        self.projection = ProjectionEngine(self.export_root, write_nfo=False)
        self.reconciler = ReconciliationEngine(self.store, self.projection)
        default_fixture = (
            Path(__file__).resolve().parents[1]
            / "script.module.streambridge"
            / "lib"
            / "streambridge_core"
            / "providers"
            / "netflix_sample_catalog.json"
        )
        if adapter is not None:
            self.adapter = adapter
        elif IN_KODI:
            import xbmcaddon  # type: ignore[import-not-found]

            addon_inst = xbmcaddon.Addon("plugin.video.streambridge")
            cat_mode = addon_inst.getSettingString("netflix_catalog_mode") or "live_justwatch"
            if cat_mode == "fixture":
                cat_mode = "live_justwatch"
                addon_inst.setSettingString("netflix_catalog_mode", "live_justwatch")
            wm_key = addon_inst.getSettingString("watchmode_api_key") or None
            bundled_player = (
                Path(__file__).resolve().parents[1]
                / "plugin.video.streambridge"
                / "bin"
                / "StreamBridge.Player.exe"
            )
            self.adapter = NetflixCatalogAdapter(
                store=self.store,
                watchmode_api_key=wm_key,
                companion_exe_path=bundled_player if bundled_player.exists() else None,
            )
        else:
            self.adapter = FixtureAdapter(self.store, fixture_path=default_fixture)
        self.region = region
        self.sync_runs: list[ReconciliationResult] = []
        self.last_sync_timestamp: float = 0.0
        self.player_monitor = StreamBridgePlayerMonitor(self.kodi)

    def _execute_sync(
        self,
        region: str | None = None,
        now: float | None = None,
    ) -> ReconciliationResult:
        """Run a full catalog + My List reconciliation pass and request targeted Kodi scan/clean."""
        target_region = region or self.region
        policy = self.kodi.load_filter_policy()
        result = self.reconciler.reconcile_provider(
            self.adapter, region=target_region, filter_policy=policy
        )
        self.sync_runs.append(result)
        self.last_sync_timestamp = now if now is not None else time.time()

        mylist_titles = [
            t for t in self.store.list_titles(provider="netflix", available_only=True)
            if t.in_my_list
        ]
        if mylist_titles:
            self.rpc.sync_mylist_to_kodi_favourites(mylist_titles)

        if result.requires_kodi_scan:
            self.rpc.request_targeted_scan()
        if result.requires_kodi_clean:
            self.rpc.request_targeted_clean()
        return result

    def run_startup_sync(self, now: float | None = None) -> ReconciliationResult:
        """Perform initial provider reconciliation on service startup."""
        return self._execute_sync(now=now)

    def run_periodic_sync_if_due(self, now: float | None = None) -> ReconciliationResult | None:
        """Run a periodic background sync if enabled, due by interval, and not blocked by playback."""
        sched = self.kodi.load_sync_schedule_settings()
        if not sched.get("periodic_sync_enabled", True):
            return None
        if sched.get("pause_sync_during_playback", True) and self.kodi.is_playing_video():
            return None

        current_time = now if now is not None else time.time()
        interval_hours = max(1, int(sched.get("periodic_sync_interval_hours", 6)))
        interval_seconds = float(interval_hours) * 3600.0
        if self.last_sync_timestamp > 0 and (current_time - self.last_sync_timestamp) < interval_seconds:
            return None

        return self._execute_sync(
            region=str(sched.get("region") or self.region),
            now=current_time,
        )

    def onSettingsChanged(self) -> None:
        """Triggered by Kodi when the user saves changes in StreamBridge Settings."""
        if self.kodi.is_playing_video():
            return
        sched = self.kodi.load_sync_schedule_settings()
        self.region = str(sched.get("region") or self.region)
        self._execute_sync(region=self.region)

    def onScanStarted(self, library: str = "video") -> None:
        """Triggered by Kodi when a library scan begins."""
        if library != "video":
            return
        is_user_initiated = self.rpc.mark_kodi_scan_started()
        if not is_user_initiated:
            # Guard against self-triggered scan loops
            return
        # Guard against external filesystem watchers (e.g. service.librarywatchdog)
        # immediately triggering UpdateLibrary right after a StreamBridge sync
        if self.last_sync_timestamp > 0 and (time.time() - self.last_sync_timestamp) < 300.0:
            return

        self._execute_sync()

    def onScanFinished(self, library: str = "video") -> None:
        """Triggered by Kodi when a library scan finishes."""
        if library != "video":
            return
        run_followup_scan, run_followup_clean = self.rpc.mark_kodi_scan_finished()
        if run_followup_scan:
            self.rpc.request_targeted_scan()
        if run_followup_clean:
            self.rpc.request_targeted_clean()

    def onCleanStarted(self, library: str = "video") -> None:
        if library != "video":
            return
        self.kodi.log("Video library clean started", level="info")

    def onCleanFinished(self, library: str = "video") -> None:
        if library != "video":
            return
        self.kodi.log("Video library clean finished", level="info")

    def run_forever(self) -> None:
        sched = self.kodi.load_sync_schedule_settings()
        if sched.get("sync_on_startup", True):
            self.run_startup_sync()
        else:
            self.last_sync_timestamp = time.time()
        while not self.abortRequested():
            if self.waitForAbort(10.0):
                break
            self.run_periodic_sync_if_due()


if __name__ == "__main__":
    if IN_KODI:
        service = StreamBridgeServiceMonitor()
        service.run_forever()
