"""Gate G3 Tests — Playback Session Broker, Honest Telemetry, Non-Windows Fallbacks, and Security."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import socket
import threading

import pytest

from streambridge_core.adapters.fixture import FixtureAdapter
from streambridge_core.broker import PlaybackBroker
from streambridge_core.db import StreamBridgeStore
from streambridge_core.ipc import NamedPipeClient, validate_ipc_payload
from streambridge_core.kodi_bridge import KodiBridge
from streambridge_core.kodi_rpc import KodiJsonRpcClient
from streambridge_core.models import PlaybackSessionState
from streambridge_core.projection import ProjectionEngine
from streambridge_core.reconciler import ReconciliationEngine
from streambridge_core.security import (
    RedactingLogFilter,
    SecurityValidationError,
    clear_isolated_browser_profile,
    dpapi_protect,
    dpapi_unprotect,
    get_isolated_browser_profile_dir,
    redact_secrets,
    validate_launch_url,
)


def test_g3_verified_ipc_telemetry_persists_progress_and_syncs_kodi(tmp_path: Path) -> None:
    store = StreamBridgeStore(tmp_path / "sb.db")
    export_root = tmp_path / "library"
    projection = ProjectionEngine(export_root)
    reconciler = ReconciliationEngine(store, projection)
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "netflix_sample_catalog.json"
    adapter = FixtureAdapter(store, fixture_path=fixture_path)
    reconciler.reconcile_provider(adapter)

    # Link a Kodi movieid to the projection to verify JSON-RPC state sync
    proj = store.get_projection("sb_nf_movie_glassonion", "netflix")
    assert proj is not None
    proj.kodi_db_id = 42
    store.upsert_projection(proj)

    rpc_calls: list[dict[str, object]] = []

    def fake_rpc(req_str: str) -> str:
        rpc_calls.append(json.loads(req_str))
        return '{"jsonrpc": "2.0", "id": 1, "result": "OK"}'

    kodi_bridge = KodiBridge()
    kodi_rpc = KodiJsonRpcClient(rpc_Transport=fake_rpc)

    # Spin up a local loopback IPC server simulating StreamBridge.Player emitting verified telemetry
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    def run_mock_player() -> None:
        conn, _ = server_sock.accept()
        with conn:
            req_line = conn.makefile("r", encoding="utf-8").readline()
            req = json.loads(req_line)["payload"]
            sid = req["session_id"]
            tid = req["title_id"]
            events = [
                {"session_id": sid, "title_id": tid, "state": "buffering", "sequence": 1, "position_seconds": 0.0, "duration_seconds": 7200.0, "backend_used": "webview2", "telemetry_verified": True},
                {"session_id": sid, "title_id": tid, "state": "playing", "sequence": 2, "position_seconds": 1800.0, "duration_seconds": 7200.0, "backend_used": "webview2", "telemetry_verified": True},
                {"session_id": sid, "title_id": tid, "state": "paused", "sequence": 3, "position_seconds": 3600.0, "duration_seconds": 7200.0, "backend_used": "webview2", "telemetry_verified": True},
                {"session_id": sid, "title_id": tid, "state": "stopped", "sequence": 4, "position_seconds": 6600.0, "duration_seconds": 7200.0, "backend_used": "webview2", "telemetry_verified": True},
            ]
            for ev in events:
                conn.sendall((json.dumps({"type": "event", "payload": ev}) + "\n").encode("utf-8"))
        server_sock.close()

    t = threading.Thread(target=run_mock_player, daemon=True)
    t.start()

    ipc_client = NamedPipeClient(tcp_fallback=("127.0.0.1", port))
    broker = PlaybackBroker(
        store=store,
        kodi_bridge=kodi_bridge,
        kodi_rpc=kodi_rpc,
        ipc_client=ipc_client,
    )

    result = broker.play_title(adapter, "sb_nf_movie_glassonion")
    t.join(timeout=5)

    assert result.final_state == PlaybackSessionState.STOPPED
    assert result.telemetry_verified is True
    assert result.persisted_state is not None
    # 6600 / 7200 = 91.6% >= 90% completion threshold -> marked completed and play_count incremented
    assert result.persisted_state.completed is True
    assert result.persisted_state.play_count == 1
    # Audio must be restored and Window(10000) properties cleared after exit
    assert kodi_bridge.audio_suspended is False
    assert "streambridge.active" not in kodi_bridge.window_properties

    # Verify JSON-RPC VideoLibrary.SetMovieDetails was called with playcount=1
    assert any(c.get("method") == "VideoLibrary.SetMovieDetails" for c in rpc_calls)
    store.close()


def test_g3_unverified_browser_launch_does_not_fabricate_progress(tmp_path: Path) -> None:
    store = StreamBridgeStore(tmp_path / "sb.db")
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "netflix_sample_catalog.json"
    adapter = FixtureAdapter(store, fixture_path=fixture_path)
    ReconciliationEngine(store, ProjectionEngine(tmp_path / "lib")).reconcile_provider(adapter)

    launched: list[tuple[str, str]] = []
    broker = PlaybackBroker(
        store=store,
        browser_launcher=lambda b, u: launched.append((b, u)),
    )

    res = broker.play_title(adapter, "sb_nf_movie_allquiet")
    assert res.telemetry_verified is False
    assert res.persisted_state is None
    assert store.get_playback_state("sb_nf_movie_allquiet") is None
    store.close()


def test_g3_graceful_non_windows_fallbacks(tmp_path: Path) -> None:
    store = StreamBridgeStore(tmp_path / "sb.db")
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "netflix_sample_catalog.json"
    adapter = FixtureAdapter(store, fixture_path=fixture_path)
    ReconciliationEngine(store, ProjectionEngine(tmp_path / "lib")).reconcile_provider(adapter)

    # Case 1: Android -> emits StartAndroidActivity builtin for com.netflix.ninja
    kodi_android = KodiBridge()
    broker_android = PlaybackBroker(store=store, kodi_bridge=kodi_android)
    res_android = broker_android.play_title(
        adapter, "sb_nf_movie_nimona", platform_override="android"
    )
    assert res_android.backend_used == "android_intent"
    assert any("StartAndroidActivity" in cmd and "com.netflix.ninja" in cmd for cmd in kodi_android.executed_builtins)

    # Case 2: Linux / LibreELEC with companion Kodi add-on `plugin.video.netflix` installed
    class CompanionKodiBridge(KodiBridge):
        def has_addon(self, addon_id: str) -> bool:
            return addon_id == "plugin.video.netflix"

    kodi_elec = CompanionKodiBridge()
    broker_elec = PlaybackBroker(store=store, kodi_bridge=kodi_elec)
    res_elec = broker_elec.play_title(
        adapter, "sb_nf_movie_nimona", platform_override="linux"
    )
    assert res_elec.backend_used == "kodi_addon:plugin.video.netflix"
    assert any("PlayMedia(plugin://plugin.video.netflix/" in cmd for cmd in kodi_elec.executed_builtins)
    store.close()


def test_g3_security_url_allowlist_dpapi_and_log_redaction(tmp_path: Path) -> None:
    # 1. Valid HTTPS Netflix URLs pass
    assert validate_launch_url("https://www.netflix.com/watch/81458416", "netflix") == "https://www.netflix.com/watch/81458416"

    # 2. HTTP, untrusted domains, embedded credentials, and command injection chars are blocked
    for bad_url in (
        "http://www.netflix.com/watch/81458416",
        "https://evil-phishing.com/watch/81458416",
        "https://user:pass@www.netflix.com/watch/81458416",
        'https://www.netflix.com/watch/81458416" --remote-debugging-port=9222',
        "file:///C:/Windows/System32/calc.exe",
    ):
        with pytest.raises(SecurityValidationError):
            validate_launch_url(bad_url, "netflix")

    # 3. IPC payload with password/cookies is rejected
    with pytest.raises(SecurityValidationError):
        validate_ipc_payload({"type": "launch", "payload": {"cookie": "SecureNetflixId=abc"}})

    # 4. Log redaction strips API keys, tokens, and Netflix cookies
    raw_log = "Request failed for apiKey=WM_SECRET_12345 and Cookie: SecureNetflixId=NF_COOKIE_999; Bearer eyJhbGciOi"
    redacted = redact_secrets(raw_log)
    assert "WM_SECRET_12345" not in redacted
    assert "NF_COOKIE_999" not in redacted
    assert "eyJhbGciOi" not in redacted

    record = logging.LogRecord("test", logging.WARNING, "test.py", 10, raw_log, None, None)
    RedactingLogFilter().filter(record)
    assert "WM_SECRET_12345" not in record.getMessage()

    # 5. DPAPI / OS credential protection round-trip
    protected = dpapi_protect("super_secret_watchmode_key")
    assert "super_secret_watchmode_key" not in protected
    assert dpapi_unprotect(protected) == "super_secret_watchmode_key"

    # 6. Isolated browser profile creation and sign-out wipe
    profile_dir = get_isolated_browser_profile_dir(tmp_path)
    dummy_cookie_file = profile_dir / "Default" / "Network" / "Cookies"
    dummy_cookie_file.parent.mkdir(parents=True, exist_ok=True)
    dummy_cookie_file.write_bytes(b"dpapi_encrypted_blob")
    assert dummy_cookie_file.exists()

    assert clear_isolated_browser_profile(tmp_path) is True
    assert not dummy_cookie_file.exists()
    assert profile_dir.exists()
