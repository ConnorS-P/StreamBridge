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
from streambridge_core.models import PlaybackSessionState, PlaybackState, Title
from streambridge_core.osd_overlay import (
    disable_kodi_osd_cutout,
    read_live_telemetry,
    send_player_control,
)
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
        self.holding_for_buffer: bool = False
        self.last_programmatic_seek_ts: float = 0.0
        self.last_programmatic_seek_target: float = -100.0
        self.last_user_seek_ts: float = 0.0
        self.last_user_pause_ts: float = 0.0
        self.last_known_netflix_pos: float = 0.0
        self.last_known_netflix_dur: float = 0.0
        self.saw_fullscreen_video: bool = False

    def onPlayBackStarted(self) -> None:
        self.saw_fullscreen_video = False

    def onPlayBackPaused(self) -> None:
        self.last_action = "pause"
        if self.holding_for_buffer:
            return
        self.last_user_pause_ts = time.time()
        send_player_control("pause")

    def onPlayBackResumed(self) -> None:
        self.last_action = "resume"
        self.holding_for_buffer = False
        send_player_control("resume")

    def onPlayBackSeek(self, time_ms: int, seek_offset: int) -> None:
        now = time.time()
        target_sec = float(time_ms) / 1000.0
        if (now - self.last_programmatic_seek_ts) < 6.0 or abs(target_sec - self.last_programmatic_seek_target) < 8.0:
            return
        self.last_action = "seek"
        self.last_user_seek_ts = now
        self.last_seek_seconds = target_sec

        # Intercept chapterless '>>' (SkipNext to end) and '<<' (SkipPrevious to 0:00) on Kodi's M OSD bar
        if (
            self.last_known_netflix_dur > 60.0
            and target_sec >= (self.last_known_netflix_dur - 15.0)
            and seek_offset > 120000
        ):
            new_pos = min(self.last_known_netflix_dur - 5.0, self.last_known_netflix_pos + 30.0)
            self.last_known_netflix_pos = new_pos
            send_player_control("seek_by", 30)
            if IN_KODI and xbmc is not None:
                try:
                    self.last_programmatic_seek_ts = time.time()
                    self.last_programmatic_seek_target = new_pos
                    xbmc.Player().seekTime(new_pos)
                except Exception as exc:
                    self.kodi.log(f"SkipNext clamp seekTime skipped: {exc}", level="debug")
            return

        if target_sec <= 2.0 and seek_offset < -60000 and self.last_known_netflix_pos > 15.0:
            new_pos = max(0.0, self.last_known_netflix_pos - 30.0)
            self.last_known_netflix_pos = new_pos
            send_player_control("seek_by", -30)
            if IN_KODI and xbmc is not None:
                try:
                    self.last_programmatic_seek_ts = time.time()
                    self.last_programmatic_seek_target = new_pos
                    xbmc.Player().seekTime(new_pos)
                except Exception as exc:
                    self.kodi.log(f"SkipPrev clamp seekTime skipped: {exc}", level="debug")
            return

        if seek_offset != 0:
            send_player_control("seek_by", int(round(float(seek_offset) / 1000.0)))
        else:
            send_player_control("seek", int(target_sec))

    def onPlayBackStopped(self) -> None:
        self.last_action = "stop"
        self.holding_for_buffer = False
        self.saw_fullscreen_video = False
        send_player_control("stop")
        disable_kodi_osd_cutout()
        self.kodi.clear_ecosystem_window_properties()

    def onPlayBackEnded(self) -> None:
        self.last_action = "ended"
        self.holding_for_buffer = False
        self.saw_fullscreen_video = False
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
        self._last_telemetry_poll: float = 0.0
        self._last_bookmark_sync_ts: float = 0.0
        self._last_osd_mode: int = -1
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

        for proj in self.store.list_projections(provider="netflix"):
            pb = self.store.get_playback_state(proj.title_id)
            if pb and (pb.position_seconds > 5 or pb.play_count > 0):
                title_obj = self.store.get_title(proj.title_id)
                if title_obj:
                    self._sync_title_bookmark_to_kodi(title_obj, pb)

        if result.requires_kodi_scan:
            self.rpc.request_targeted_scan()
        if result.requires_kodi_clean:
            self.rpc.request_targeted_clean()
        return result

    def _sync_title_bookmark_to_kodi(self, title: Title, pb: PlaybackState) -> None:
        """Resolve `kodi_db_id` if needed and push `PlaybackState` resume/playcount into Kodi's library."""
        proj = self.store.get_projection(title.title_id, provider="netflix")
        if proj is None:
            return
        db_id = proj.kodi_db_id
        if db_id is None:
            db_id = self.rpc.resolve_kodi_db_id(title, relative_path=proj.relative_path)
            if db_id is not None:
                proj.kodi_db_id = db_id
                self.store.upsert_projection(proj)
        if db_id is not None:
            self.rpc.sync_playback_state_to_kodi(title, pb, kodi_db_id=db_id)

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

    def poll_live_telemetry(self) -> None:
        """Read live HTML5 `<video>` telemetry from `StreamBridge.Player` and keep Kodi in real-time parity."""
        telemetry = read_live_telemetry()
        if not telemetry:
            return
        active_title_id = ""
        if IN_KODI and xbmc is not None:
            active_title_id = xbmc.getInfoLabel("Window(10000).Property(streambridge.title_id)") or ""
        else:
            active_title_id = self.kodi.window_properties.get("streambridge.title_id", "")
        if not active_title_id:
            return

        now = time.time()
        pos = float(telemetry.get("position_seconds") or 0.0)
        dur = float(telemetry.get("duration_seconds") or 0.0)
        updated_at = float(telemetry.get("updated_at") or 0.0)
        raw_state = str(telemetry.get("state") or "playing").lower()

        if pos > 0:
            self.player_monitor.last_known_netflix_pos = pos
        if dur > 0:
            self.player_monitor.last_known_netflix_dur = dur

        is_hung_or_buffering = (raw_state in ("launching", "buffering")) or (
            updated_at > 0.0 and (now - updated_at) > 2.5
        )
        if IN_KODI and xbmc is not None:
            try:
                if (
                    raw_state == "playing"
                    and pos > 1.0
                    and (now - self.player_monitor.last_user_seek_ts) > 10.0
                    and (now - self.player_monitor.last_programmatic_seek_ts) > 10.0
                ):
                    kodi_pos = float(xbmc.Player().getTime())
                    if abs(kodi_pos - pos) > 5.0:
                        self.player_monitor.last_programmatic_seek_ts = now
                        self.player_monitor.last_programmatic_seek_target = pos
                        xbmc.Player().seekTime(pos)
            except Exception as exc:
                self.kodi.log(f"Player parity sync skipped: {exc}", level="debug")

        if dur <= 0:
            return

        state_enum = (
            PlaybackSessionState.BUFFERING
            if is_hung_or_buffering
            else PlaybackSessionState.PAUSED
            if raw_state == "paused"
            else PlaybackSessionState.STOPPED
            if raw_state == "stopped"
            else PlaybackSessionState.PLAYING
        )
        pb = self.store.get_playback_state(active_title_id) or PlaybackState(title_id=active_title_id)
        pb.position_seconds = pos
        pb.duration_seconds = dur
        pb.last_state = state_enum
        if dur > 60 and (pos / dur) >= 0.90 and not pb.completed:
            pb.completed = True
            pb.play_count += 1
            pb.position_seconds = 0.0
        self.store.upsert_playback_state(pb)

        title = self.store.get_title(active_title_id)
        if title:
            if dur > 60 and title.runtime_seconds != int(dur):
                title.runtime_seconds = int(dur)
                self.store.upsert_title(title)
            if (now - self._last_bookmark_sync_ts) >= 15.0 or state_enum == PlaybackSessionState.STOPPED:
                self._last_bookmark_sync_ts = now
                self._sync_title_bookmark_to_kodi(title, pb)

    def sync_osd_cutout_mode(self) -> None:
        """Dynamically adjust `StreamBridge.Player` alpha blend (`WM_APP + 4`) and bridge Subtitle/Audio OSD menus."""
        if not (IN_KODI and xbmc is not None):
            return

        active_title_id = xbmc.getInfoLabel("Window(10000).Property(streambridge.title_id)") or ""
        if active_title_id:
            is_fs = bool(xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"))
            if is_fs:
                self.player_monitor.saw_fullscreen_video = True
            elif self.player_monitor.saw_fullscreen_video:
                self.player_monitor.saw_fullscreen_video = False
                send_player_control("stop")
                try:
                    xbmc.Player().stop()
                except Exception as exc:
                    self.kodi.log(f"Fullscreen exit stop skipped: {exc}", level="debug")
                return

        if xbmc.getCondVisibility("Window.IsVisible(osdsubtitlesettings)"):
            xbmc.executebuiltin("Dialog.Close(osdsubtitlesettings,true)")
            self._open_cadmium_track_picker(track_key="subtitles", control_action="set_subtitle", heading="StreamBridge — Select Subtitles")
            return

        if xbmc.getCondVisibility("Window.IsVisible(osdaudiosettings)"):
            xbmc.executebuiltin("Dialog.Close(osdaudiosettings,true)")
            self._open_cadmium_track_picker(track_key="audio_tracks", control_action="set_audio", heading="StreamBridge — Select Audio Track")
            return

        full_modal = bool(
            xbmc.getCondVisibility(
                "Window.IsVisible(osdvideosettings) | Window.IsVisible(videobookmarks) | "
                "Window.IsVisible(fullscreeninfo) | Window.IsVisible(selectdialog) | "
                "Window.IsVisible(subtitlesearch) | Window.IsVisible(sliderdialog) | "
                "Window.IsVisible(playerprocessinfo) | Window.IsVisible(contextmenu) | "
                "Window.IsVisible(yesnodialog) | Window.IsVisible(okdialog)"
            )
        )
        osd_bar = bool(xbmc.getCondVisibility("Window.IsVisible(videoosd) | Window.IsVisible(seekbar)"))
        mode = 2 if full_modal else (1 if osd_bar else 0)
        if mode != self._last_osd_mode:
            self._last_osd_mode = mode
            send_player_control("osd_mode", mode)

    def _open_cadmium_track_picker(self, track_key: str, control_action: str, heading: str) -> None:
        """Present a native Kodi `Dialog().select(...)` listing live Netflix Cadmium subtitle or audio tracks."""
        if not (IN_KODI and xbmc is not None):
            return
        try:
            import xbmcgui  # type: ignore[import-not-found]

            telemetry = read_live_telemetry() or {}
            tracks = telemetry.get(track_key) or []
            if not isinstance(tracks, list) or not tracks:
                self.kodi.notify(heading, "No alternate tracks reported by stream yet.")
                return
            labels: list[str] = []
            preselect = 0
            for i, trk in enumerate(tracks):
                if not isinstance(trk, dict):
                    continue
                lbl = str(trk.get("label") or f"Track {i + 1}")
                is_act = bool(trk.get("active"))
                if is_act:
                    preselect = i
                    labels.append(f"[B]* {lbl} (Active)[/B]")
                else:
                    labels.append(lbl)
            self._last_osd_mode = 2
            send_player_control("osd_mode", 2)
            chosen = xbmcgui.Dialog().select(heading, labels, preselect=preselect)
            if chosen is not None and chosen >= 0 and chosen < len(tracks):
                target_idx = int(tracks[chosen].get("index", chosen))
                send_player_control(control_action, target_idx)
        except Exception as exc:
            self.kodi.log(f"Track picker error ({track_key}): {exc}", level="warning")

    def run_forever(self) -> None:
        sched = self.kodi.load_sync_schedule_settings()
        if sched.get("sync_on_startup", True):
            self.run_startup_sync()
        else:
            self.last_sync_timestamp = time.time()
        while not self.abortRequested():
            playing = self.kodi.is_playing_video()
            wait_sec = 0.15 if playing else 10.0
            if self.waitForAbort(wait_sec):
                break
            if playing:
                self.sync_osd_cutout_mode()
                now = time.time()
                if (now - self._last_telemetry_poll) >= 0.25:
                    self._last_telemetry_poll = now
                    self.poll_live_telemetry()
            else:
                self._last_osd_mode = -1
                self.run_periodic_sync_if_due()


if __name__ == "__main__":
    if IN_KODI:
        service = StreamBridgeServiceMonitor()
        service.run_forever()
