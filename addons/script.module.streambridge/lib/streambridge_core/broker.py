"""Playback Session Broker for StreamBridge (Section 4 of Design v0.1).

Session lifecycle:
Resolve title and choose source -> capture Kodi focus and resume state -> release Kodi audio device ->
start backend -> receive verified session events -> handle pause, seek, stop and failure ->
persist progress -> release player resources -> restore focus and display.

Honest telemetry contract:
- Explicitly distinguish `buffering`, `playing`, `paused`, `stopped`, and `failed`.
- A browser launch is NOT evidence of playback (`telemetry_verified=False`).
- Do NOT report or persist invented watch progress or completion when `telemetry_verified=False`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from .adapters.base import ProviderAdapter
from .db import StreamBridgeStore
from .ipc import NamedPipeClient
from .kodi_bridge import KodiBridge
from .kodi_rpc import KodiJsonRpcClient
from .models import (
    LaunchDescriptor,
    PlaybackSessionState,
    PlaybackState,
    SessionEvent,
)
from .osd_overlay import (
    disable_kodi_osd_cutout,
    enable_kodi_osd_cutout,
    generate_chroma_key_avi,
)
from .platform_compat import (
    PlatformEnvironment,
    build_non_windows_fallback_plan,
    detect_platform_environment,
    launch_browser_process,
    make_unverified_browser_events,
)


@dataclass
class PlaybackSessionResult:
    """Summary of a completed or failed playback session."""

    session_id: str
    title_id: str
    backend_used: str
    final_state: PlaybackSessionState
    telemetry_verified: bool
    events: list[SessionEvent] = field(default_factory=list)
    persisted_state: PlaybackState | None = None
    error_message: str | None = None


class PlaybackBroker:
    """Orchestrates authorized playback across `StreamBridge.Player` and cross-platform fallbacks."""

    COMPLETION_THRESHOLD_RATIO = 0.90

    def __init__(
        self,
        store: StreamBridgeStore,
        kodi_bridge: KodiBridge | None = None,
        kodi_rpc: KodiJsonRpcClient | None = None,
        ipc_client: NamedPipeClient | None = None,
        companion_exe_path: Path | None = None,
        browser_launcher: Callable[[str, str], object] | None = None,
    ) -> None:
        self.store = store
        self.kodi = kodi_bridge or KodiBridge()
        self.rpc = kodi_rpc or KodiJsonRpcClient()
        self.ipc_client = ipc_client
        self.companion_exe_path = companion_exe_path
        self.browser_launcher = browser_launcher

    def play_title(
        self,
        adapter: ProviderAdapter,
        title_id: str,
        profile: str = "default",
        preferred_backend: str = "auto",
        platform_override: str | None = None,
        wait_for_exit: bool = True,
        start_from_beginning: bool = False,
    ) -> PlaybackSessionResult:
        """Resolve `title_id` and execute a complete playback session lifecycle."""
        descriptor = adapter.resolve_offer(
            title_id=title_id,
            profile=profile,
            preferred_backend=preferred_backend,
        )

        env = detect_platform_environment(
            companion_exe_path=self.companion_exe_path,
            has_kodi_addon_check=self.kodi.has_addon,
            provider_id=descriptor.provider,
            platform_override=platform_override,
        )

        if env.is_windows and not self.rpc.is_fake_fullscreen_enabled():
            incompat_msg = (
                "StreamBridge renders inside Kodi's window using Windows DirectComposition, "
                "which is incompatible with legacy Exclusive Fullscreen.\n\n"
                "Please enable 'Use fullscreen window' under Settings -> System -> Display."
            )
            self.kodi.show_ok_dialog("StreamBridge — Incompatible Display Mode", incompat_msg)
            return PlaybackSessionResult(
                session_id=descriptor.session_id,
                title_id=descriptor.title_id,
                backend_used="incompatible_display_mode",
                final_state=PlaybackSessionState.FAILED,
                telemetry_verified=False,
                events=[],
                persisted_state=None,
                error_message="incompatible_display_mode_exclusive_fullscreen",
            )

        # Capture Kodi state, set Window(10000) Trakt/TMDb Helper properties, and release audio
        self.kodi.set_ecosystem_window_properties(descriptor)
        if wait_for_exit:
            self.kodi.release_kodi_audio_device()

        try:
            if self.ipc_client is not None and (env.is_windows or self.ipc_client.tcp_fallback is not None):
                return self._run_ipc_session(descriptor, profile=profile)

            if env.has_native_companion_exe and env.companion_exe_path is not None:
                import subprocess

                proxy_path = self.kodi.get_profile_dir() / "osd_proxy_key.avi"
                content_dur = int(descriptor.duration_seconds) if (descriptor.duration_seconds and descriptor.duration_seconds > 60) else 7200
                target_dur = (content_dur + 30) if not wait_for_exit else 120
                proxy_avi = generate_chroma_key_avi(proxy_path, duration_seconds=target_dur)
                self.kodi.window_properties["streambridge.osd_proxy_path"] = str(proxy_avi)
                enable_kodi_osd_cutout()
                no_win_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if env.is_windows else 0
                cmd = [str(env.companion_exe_path), "--watch", descriptor.launch_url]
                if start_from_beginning:
                    cmd.append("--start-from-beginning")
                proc = subprocess.Popen(
                    cmd,
                    shell=False,
                    close_fds=True,
                    creationflags=no_win_flag,
                )
                if wait_for_exit:
                    try:
                        proc.wait()
                    finally:
                        disable_kodi_osd_cutout()
                events = make_unverified_browser_events(descriptor, backend_name="webview2_native_host")
                return PlaybackSessionResult(
                    session_id=descriptor.session_id,
                    title_id=descriptor.title_id,
                    backend_used="webview2_native_host",
                    final_state=PlaybackSessionState.STOPPED,
                    telemetry_verified=False,
                    events=events,
                    persisted_state=None,
                )

            return self._run_fallback_session(descriptor, env=env, profile=profile)
        finally:
            if wait_for_exit:
                self.kodi.restore_kodi_audio_and_focus()
                self.kodi.clear_ecosystem_window_properties()

    def _run_ipc_session(
        self, descriptor: LaunchDescriptor, profile: str = "default"
    ) -> PlaybackSessionResult:
        """Communicate with `StreamBridge.Player` over Named Pipe / IPC and process real telemetry."""
        assert self.ipc_client is not None
        collected_events: list[SessionEvent] = []
        latest_state = self.store.get_playback_state(descriptor.title_id, profile=profile) or PlaybackState(
            title_id=descriptor.title_id,
            profile=profile,
            position_seconds=descriptor.resume_position_seconds,
        )

        def handle_event(ev: SessionEvent) -> None:
            collected_events.append(ev)
            if ev.sequence <= latest_state.event_sequence and ev.sequence != 0:
                return
            latest_state.event_sequence = ev.sequence
            latest_state.last_state = ev.state
            if ev.error_message:
                latest_state.last_error = ev.error_message

            # Only update position/duration/completion when backend telemetry is verified
            if ev.telemetry_verified:
                if ev.position_seconds is not None and ev.position_seconds >= 0:
                    latest_state.position_seconds = ev.position_seconds
                if ev.duration_seconds is not None and ev.duration_seconds > 0:
                    latest_state.duration_seconds = ev.duration_seconds
                if (
                    latest_state.duration_seconds > 0
                    and (latest_state.position_seconds / latest_state.duration_seconds)
                    >= self.COMPLETION_THRESHOLD_RATIO
                ):
                    if not latest_state.completed:
                        latest_state.completed = True
                        latest_state.play_count += 1
                        latest_state.position_seconds = 0.0

            self.store.upsert_playback_state(latest_state)

        try:
            self.ipc_client.send_launch_and_stream_events(descriptor, on_event=handle_event)
        except Exception as exc:
            err_msg = f"IPC playback session failed: {exc}"
            self.kodi.notify("StreamBridge Playback Error", err_msg, error=True)
            return PlaybackSessionResult(
                session_id=descriptor.session_id,
                title_id=descriptor.title_id,
                backend_used="ipc_error",
                final_state=PlaybackSessionState.FAILED,
                telemetry_verified=False,
                events=collected_events,
                error_message=err_msg,
            )

        final_ev = collected_events[-1] if collected_events else None
        final_state = final_ev.state if final_ev else PlaybackSessionState.STOPPED
        verified = any(e.telemetry_verified for e in collected_events)
        backend_used = (final_ev.backend_used if final_ev and final_ev.backend_used else "streambridge_player")

        if verified:
            title = self.store.get_title(descriptor.title_id)
            proj = self.store.get_projection(descriptor.title_id, descriptor.provider)
            if title and proj and proj.kodi_db_id is not None:
                self.rpc.sync_playback_state_to_kodi(title, latest_state, kodi_db_id=proj.kodi_db_id)

        if final_state == PlaybackSessionState.FAILED:
            fail_msg: str | None = final_ev.error_message if final_ev else "Player reported failure"
            self.kodi.notify("StreamBridge Playback Failed", fail_msg or "Playback failed", error=True)
            return PlaybackSessionResult(
                session_id=descriptor.session_id,
                title_id=descriptor.title_id,
                backend_used=backend_used,
                final_state=final_state,
                telemetry_verified=verified,
                events=collected_events,
                persisted_state=latest_state if verified else None,
                error_message=fail_msg,
            )

        return PlaybackSessionResult(
            session_id=descriptor.session_id,
            title_id=descriptor.title_id,
            backend_used=backend_used,
            final_state=final_state,
            telemetry_verified=verified,
            events=collected_events,
            persisted_state=latest_state if verified else None,
        )

    def _run_fallback_session(
        self,
        descriptor: LaunchDescriptor,
        env: PlatformEnvironment,
        profile: str = "default",
    ) -> PlaybackSessionResult:
        """Execute a graceful fallback on non-Windows or when `StreamBridge.Player` is absent."""
        plan = build_non_windows_fallback_plan(descriptor, env=env)
        mode = plan["mode"]

        if mode == "kodi_addon":
            plugin_uri = plan["target"]
            self.kodi.execute_builtin(f"PlayMedia({plugin_uri})")
            events = make_unverified_browser_events(descriptor, backend_name=f"kodi_addon:{plan['addon_id']}")
            return PlaybackSessionResult(
                session_id=descriptor.session_id,
                title_id=descriptor.title_id,
                backend_used=f"kodi_addon:{plan['addon_id']}",
                final_state=PlaybackSessionState.STOPPED,
                telemetry_verified=False,
                events=events,
                persisted_state=None,
            )

        if mode == "android_intent":
            self.kodi.execute_builtin(plan["target"])
            events = make_unverified_browser_events(descriptor, backend_name="android_intent")
            return PlaybackSessionResult(
                session_id=descriptor.session_id,
                title_id=descriptor.title_id,
                backend_used="android_intent",
                final_state=PlaybackSessionState.STOPPED,
                telemetry_verified=False,
                events=events,
                persisted_state=None,
            )

        if mode == "desktop_browser":
            binary = plan["binary"]
            target_url = plan["target"]
            if self.browser_launcher is not None:
                self.browser_launcher(binary, target_url)
            else:
                proc = launch_browser_process(binary, target_url, app_mode=True)
                proc.wait(timeout=5)
            events = make_unverified_browser_events(descriptor, backend_name="official_browser")
            return PlaybackSessionResult(
                session_id=descriptor.session_id,
                title_id=descriptor.title_id,
                backend_used="official_browser",
                final_state=PlaybackSessionState.STOPPED,
                telemetry_verified=False,
                events=events,
                persisted_state=None,
            )

        msg = plan["target"]
        self.kodi.notify("StreamBridge Platform Notice", msg, error=True)
        return PlaybackSessionResult(
            session_id=descriptor.session_id,
            title_id=descriptor.title_id,
            backend_used="unsupported",
            final_state=PlaybackSessionState.FAILED,
            telemetry_verified=False,
            events=[],
            persisted_state=None,
            error_message=msg,
        )
