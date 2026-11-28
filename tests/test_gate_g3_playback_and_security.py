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

    # Case 2: Exclusive fullscreen (videoscreen.fakefullscreen = false) on Windows is blocked with a clear dialog
    def fake_rpc_exclusive_fullscreen(req_str: str) -> str:
        req = json.loads(req_str)
        if req.get("method") == "Settings.GetSettingValue":
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"value": False}})
        return '{"jsonrpc": "2.0", "id": 1, "result": "OK"}'

    kodi_excl = KodiBridge()
    rpc_excl = KodiJsonRpcClient(rpc_Transport=fake_rpc_exclusive_fullscreen)
    broker_excl = PlaybackBroker(store=store, kodi_bridge=kodi_excl, kodi_rpc=rpc_excl)
    res_excl = broker_excl.play_title(
        adapter, "sb_nf_movie_nimona", platform_override="win32"
    )
    assert res_excl.final_state == PlaybackSessionState.FAILED
    assert res_excl.backend_used == "incompatible_display_mode"
    assert any("Incompatible Display Mode" in heading for heading, _ in kodi_excl.notifications)

    # Case 3: Verify silent chroma-key AVI generator for Native Skin OSD Overlay
    from streambridge_core.osd_overlay import generate_chroma_key_avi

    avi_path = generate_chroma_key_avi(tmp_path / "osd_proxy_key.avi", duration_seconds=5, fps=1)
    assert avi_path.exists()
    avi_bytes = avi_path.read_bytes()
    assert avi_bytes[:4] == b"RIFF"
    assert avi_bytes[8:12] == b"AVI "
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


def test_virtual_rle8_http_proxy_and_kodi_streamdetails(tmp_path: Path) -> None:
    import sqlite3
    import urllib.request

    from streambridge_core.models import MediaType, Title
    from streambridge_core.osd_overlay import OsdProxyHttpServer, VirtualRle8AviStream

    # 1. Verify O(1) procedural 1920x1080 24fps BI_RLE8 stream & byte-offset-to-timestamp mapping
    vstream = VirtualRle8AviStream(duration_seconds=3600, fps=24, width=1920, height=1080)
    assert vstream.total_size > 50_000_000
    assert vstream.byte_offset_to_second(0) == -1.0
    assert vstream.byte_offset_to_second(vstream.idx1_start) == -1.0
    sec_500_offset = vstream.movi_data_start + 500 * vstream.sec_stride
    assert abs(vstream.byte_offset_to_second(sec_500_offset) - 500.0) < 0.01

    hdr = vstream.read_range(0, 256)
    assert hdr[:4] == b"RIFF"
    assert b"MRLE" in hdr

    # 2. Verify Loopback HTTP Range Server (206 Partial Content)
    srv = OsdProxyHttpServer(should_pace_callback=lambda: False)
    port = srv.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/proxy/sb_test_movie.avi",
            headers={"Range": "bytes=0-511"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            assert resp.status == 206
            chunk = resp.read()
            assert len(chunk) == 512
            assert chunk[:4] == b"RIFF"
    finally:
        srv.stop()

    # 3. Verify persisting real video/audio/subtitle streamdetails into MyVideos*.db
    db_file = tmp_path / "MyVideos131.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE files (idFile INTEGER PRIMARY KEY, strFilename TEXT)")
    conn.execute(
        "CREATE TABLE streamdetails ("
        "idFile INTEGER, iStreamType INTEGER, strVideoCodec TEXT, fVideoAspect FLOAT, "
        "iVideoWidth INTEGER, iVideoHeight INTEGER, iVideoDuration INTEGER, "
        "strAudioCodec TEXT, iAudioChannels INTEGER, strAudioLanguage TEXT, strSubtitleLanguage TEXT)"
    )
    conn.execute("INSERT INTO files (idFile, strFilename) VALUES (42, 'Psycho II (1983).strm')")
    conn.commit()
    conn.close()

    kodi = KodiBridge()
    title = Title(
        title_id="sb_nf_movie_psycho2",
        media_type=MediaType.MOVIE,
        title="Psycho II",
        year=1983,
        runtime_seconds=6780,
    )
    updated = kodi.persist_kodi_streamdetails_for_title(
        title,
        video_width=1920,
        video_height=1080,
        duration_seconds=6780,
        audio_tracks=[{"lang": "eng", "channels": 6}, {"lang": "jpn", "channels": 2}],
        subtitles=[{"lang": "eng"}, {"lang": "spa"}],
        db_dir_override=tmp_path,
    )
    assert updated == 1

    conn2 = sqlite3.connect(str(db_file))
    rows = conn2.execute(
        "SELECT iStreamType, strVideoCodec, iVideoWidth, iVideoHeight, strAudioCodec, iAudioChannels, strAudioLanguage, strSubtitleLanguage "
        "FROM streamdetails WHERE idFile = 42 ORDER BY iStreamType"
    ).fetchall()
    conn2.close()
    assert len(rows) == 5
    assert rows[0][:4] == (0, "h264", 1920, 1080)
    assert (1, None, None, None, "eac3", 6, "eng", None) in rows
    assert (2, None, None, None, None, None, None, "spa") in rows

