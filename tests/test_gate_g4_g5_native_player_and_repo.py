"""Gate G4 & G5 Tests — Native C++ StreamBridge.Player Diagnostics, Live Named Pipe IPC, Watchmode/TMDB Adapters, and Kodi Repository Builder."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest

from scripts.build_kodi_repo import build_repository
from streambridge_core.adapters.fixture import FixtureAdapter
from streambridge_core.adapters.netflix_catalog import NetflixCatalogAdapter
from streambridge_core.broker import PlaybackBroker
from streambridge_core.db import StreamBridgeStore
from streambridge_core.ipc import NamedPipeClient
from streambridge_core.models import PlaybackSessionState, SnapshotCompleteness
from streambridge_core.projection import ProjectionEngine
from streambridge_core.reconciler import ReconciliationEngine

PLAYER_EXE = Path(__file__).resolve().parents[1] / "player" / "build" / "Release" / "StreamBridge.Player.exe"


@pytest.mark.skipif(sys.platform != "win32" or not PLAYER_EXE.exists(), reason="Windows StreamBridge.Player.exe required")
def test_g4_g5_native_player_diagnostics_and_wasapi_probe() -> None:
    proc = subprocess.run(
        [str(PLAYER_EXE), "--self-test"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    diag = json.loads(proc.stdout)
    assert diag["player_version"] in ("0.1.0", "0.1.5")
    assert "wasapi_audio" in diag
    assert "media_foundation_and_display" in diag

    wasapi = diag["wasapi_audio"]
    format_names = {f["format_name"] for f in wasapi.get("tested_formats", [])}
    if wasapi["endpoint_found"]:
        assert "PCM_2.0_48kHz_16bit" in format_names
        assert "PCM_5.1_48kHz_16bit" in format_names
        assert "IEC61937_AC3_DolbyDigital" in format_names
        assert "IEC61937_EAC3_DolbyDigitalPlus_AtmosJOC" in format_names

    mf = diag["media_foundation_and_display"]
    assert mf["mf_initialized"] is True


@pytest.mark.skipif(sys.platform != "win32" or not PLAYER_EXE.exists(), reason="Windows StreamBridge.Player.exe required")
def test_g3_g5_live_named_pipe_ipc_with_cpp_companion(tmp_path: Path) -> None:
    store = StreamBridgeStore(tmp_path / "sb.db")
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "netflix_sample_catalog.json"
    adapter = FixtureAdapter(store, fixture_path=fixture_path)
    ReconciliationEngine(store, ProjectionEngine(tmp_path / "lib")).reconcile_provider(adapter)

    pipe_name = rf"\\.\pipe\StreamBridgeTest_{os.getpid()}_{int(time.time() * 1000)}"

    player_proc = subprocess.Popen(
        [str(PLAYER_EXE), "--pipe", pipe_name, "--dry-run"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Give the C++ Named Pipe server a brief moment to call CreateNamedPipeW
    time.sleep(0.25)

    try:
        ipc_client = NamedPipeClient(pipe_name=pipe_name)
        broker = PlaybackBroker(store=store, ipc_client=ipc_client)
        result = broker.play_title(adapter, "sb_nf_movie_glassonion")

        player_proc.wait(timeout=10)
        assert player_proc.returncode == 0
        assert result.final_state == PlaybackSessionState.STOPPED
        assert result.telemetry_verified is True
        assert [e.state for e in result.events] == [
            PlaybackSessionState.BUFFERING,
            PlaybackSessionState.PLAYING,
            PlaybackSessionState.PAUSED,
            PlaybackSessionState.STOPPED,
        ]
        assert result.persisted_state is not None
        assert result.persisted_state.position_seconds == 600.0
    finally:
        if player_proc.poll() is None:
            player_proc.kill()
        store.close()


def test_watchmode_and_tmdb_catalog_adapters(tmp_path: Path) -> None:
    store = StreamBridgeStore(tmp_path / "sb.db")
    mock_session = MagicMock()

    # 1. Watchmode response
    wm_resp = MagicMock()
    wm_resp.status_code = 200
    wm_resp.json.return_value = {
        "page": 1,
        "total_pages": 1,
        "titles": [
            {
                "id": 12345,
                "title": "The Irishman",
                "year": 2019,
                "imdb_id": "tt1302006",
                "tmdb_id": 398978,
                "type": "movie",
                "netflix_id": "80175798",
                "web_url": "https://www.netflix.com/title/80175798",
            }
        ],
    }
    mock_session.get.return_value = wm_resp

    wm_adapter = NetflixCatalogAdapter(
        store=store,
        watchmode_api_key="test_wm_key",
        session=mock_session,
    )
    batch = wm_adapter.refresh_catalog(region="US")
    assert batch.completeness == SnapshotCompleteness.COMPLETE
    assert len(batch.items) == 1
    assert batch.items[0].title.tmdb_id == "398978"
    assert batch.items[0].offer.launch_url == "https://www.netflix.com/title/80175798"

    # 2. TMDB Watch Providers response
    tmdb_resp = MagicMock()
    tmdb_resp.status_code = 200
    tmdb_resp.json.return_value = {
        "page": 1,
        "total_pages": 2,
        "results": [
            {
                "id": 550,
                "title": "roma",
                "release_date": "2018-12-14",
                "overview": "Cleo is one of two domestic workers...",
            }
        ],
    }
    mock_session.get.return_value = tmdb_resp

    tmdb_adapter = NetflixCatalogAdapter(
        store=store,
        tmdb_api_key="test_tmdb_key",
        session=mock_session,
    )
    batch_tmdb = tmdb_adapter.refresh_catalog(region="US")
    assert batch_tmdb.completeness == SnapshotCompleteness.PARTIAL
    assert batch_tmdb.next_cursor == "2"
    assert len(batch_tmdb.items) == 1
    assert batch_tmdb.items[0].title.year == 2018

    # 3. Zero-API-key JustWatch GraphQL response (Movies + Shows + Seasons/Episodes + My List)
    jw_resp = MagicMock()
    jw_resp.status_code = 200
    jw_resp.json.return_value = {
        "data": {
            "popularTitles": {
                "edges": [
                    {
                        "node": {
                            "objectId": 1001,
                            "objectType": "MOVIE",
                            "content": {
                                "title": "Rebel Ridge",
                                "originalReleaseYear": 2024,
                                "ageCertification": "R",
                                "shortDescription": "An ex-Marine grapples his way through a web of small-town corruption.",
                                "productionCountries": ["US"],
                                "externalIds": {"imdbId": "tt11301886", "tmdbId": "646097"},
                                "scoring": {"imdbScore": 6.8},
                                "genres": [{"shortName": "act"}, {"shortName": "trl"}],
                            },
                            "watchNowOffer": {
                                "standardWebURL": "https://www.netflix.com/title/81157729"
                            },
                        }
                    },
                    {
                        "node": {
                            "objectId": 2002,
                            "objectType": "SHOW",
                            "content": {
                                "title": "Dark",
                                "originalReleaseYear": 2017,
                                "ageCertification": "TV-MA",
                                "shortDescription": "A missing child sets four families on a frantic hunt for answers.",
                                "productionCountries": ["DE"],
                                "externalIds": {"imdbId": "tt5753856", "tmdbId": "70523"},
                                "scoring": {"imdbScore": 8.7},
                                "genres": [{"shortName": "drm"}, {"shortName": "scf"}],
                            },
                            "watchNowOffer": {
                                "standardWebURL": "https://www.netflix.com/title/80100172"
                            },
                            "seasons": [
                                {
                                    "content": {"seasonNumber": 1},
                                    "episodes": [
                                        {
                                            "content": {"title": "Secrets", "episodeNumber": 1},
                                            "watchNowOffer": {
                                                "standardWebURL": "https://www.netflix.com/watch/80114790"
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    },
                ]
            }
        }
    }
    mock_session.post.return_value = jw_resp

    jw_adapter = NetflixCatalogAdapter(
        store=store,
        session=mock_session,
        my_list_content_ids={"80100172"},
    )
    batch_jw = jw_adapter.refresh_catalog(region="US")
    assert batch_jw.completeness == SnapshotCompleteness.COMPLETE
    assert len(batch_jw.items) == 2
    movie_item = batch_jw.items[0]
    ep_item = batch_jw.items[1]
    assert movie_item.title.title == "Rebel Ridge"
    assert movie_item.title.original_language == "en"
    assert movie_item.title.is_dubbed is False
    assert movie_item.title.in_my_list is False
    assert ep_item.title.show_title == "Dark"
    assert ep_item.title.original_language == "de"
    assert ep_item.title.is_dubbed is True
    assert ep_item.title.in_my_list is True

    # Verify My List filter bypass & My List Only filter rules + Kodi Favourites & Tag RPC
    from streambridge_core.filters import LibraryFilterPolicy, evaluate_title_filter
    from streambridge_core.kodi_rpc import KodiJsonRpcClient

    # Even though 'Dark' is German/dubbed, `mylist_bypass_filters=True` allows it because `in_my_list=True`!
    strict_en_policy = LibraryFilterPolicy(
        allowed_original_languages=("en",),
        allow_dubbed_content=False,
        mylist_bypass_filters=True,
        mylist_only=False,
    )
    assert evaluate_title_filter(ep_item.title, strict_en_policy).allowed is True

    mylist_only_policy = LibraryFilterPolicy(mylist_only=True)
    assert evaluate_title_filter(movie_item.title, mylist_only_policy).allowed is False
    assert evaluate_title_filter(ep_item.title, mylist_only_policy).allowed is True

    rpc_calls: list[dict[str, object]] = []
    rpc = KodiJsonRpcClient(
        rpc_Transport=lambda s: (rpc_calls.append(json.loads(s)) or '{"jsonrpc":"2.0","id":1,"result":"OK"}')
    )
    assert rpc.sync_mylist_to_kodi_favourites([movie_item.title, ep_item.title]) == 1
    assert any(c.get("method") == "Favourites.AddFavourite" for c in rpc_calls)
    assert rpc.apply_mylist_tag_to_kodi_item(
        movie_item.title.__class__(
            title_id="sb_ml_1",
            media_type=movie_item.title.media_type,
            title="My List Movie",
            in_my_list=True,
        ),
        kodi_db_id=99,
    ) is True
    store.close()


def test_kodi_repository_builder_generates_valid_index_and_hashes() -> None:
    packaged = build_repository()
    assert set(packaged.keys()) == {
        "repository.streambridge",
        "script.module.streambridge",
        "plugin.video.streambridge",
        "service.streambridge",
    }

    root_dir = Path(__file__).resolve().parents[1]
    repo_dir = root_dir / "dist" / "repo"
    addons_xml = repo_dir / "addons.xml"
    addons_md5 = repo_dir / "addons.xml.md5"
    assert addons_xml.exists()
    assert addons_md5.exists()
    assert (root_dir / "dist" / "repository.streambridge.zip").exists()

    expected_md5 = hashlib.md5(addons_xml.read_bytes()).hexdigest()
    assert addons_md5.read_text(encoding="utf-8").strip() == expected_md5

    for addon_id, version in packaged.items():
        zip_path = repo_dir / addon_id / f"{addon_id}-{version}.zip"
        sha_path = repo_dir / addon_id / f"{addon_id}-{version}.zip.sha256"
        assert zip_path.exists()
        assert sha_path.exists()
        expected_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        assert sha_path.read_text(encoding="utf-8").strip() == expected_sha


def test_kodi_settings_xml_labels_match_strings_po() -> None:
    import re
    import xml.etree.ElementTree as ET

    root_dir = Path(__file__).resolve().parents[1]
    addon_dir = root_dir / "addons" / "plugin.video.streambridge"
    settings_xml = addon_dir / "resources" / "settings.xml"
    strings_po = addon_dir / "resources" / "language" / "resource.language.en_gb" / "strings.po"

    assert settings_xml.exists()
    assert strings_po.exists()

    po_text = strings_po.read_text(encoding="utf-8")
    defined_ids = set(re.findall(r'msgctxt\s+"#(\d+)"', po_text))
    assert len(defined_ids) >= 20

    tree = ET.parse(settings_xml)
    for elem in tree.iter():
        for attr in ("label", "help"):
            val = elem.attrib.get(attr)
            if val:
                assert val.isdigit(), f"<{elem.tag} {attr}={val!r}> must be a numeric string ID"
                assert val in defined_ids, f"String ID #{val} in <{elem.tag}> not found in strings.po"
        if elem.tag == "heading" and elem.text:
            val = elem.text.strip()
            assert val.isdigit(), f"<heading>{val}</heading> must be a numeric string ID"
            assert val in defined_ids, f"Heading ID #{val} not found in strings.po"


@pytest.mark.skipif(sys.platform != "win32" or not PLAYER_EXE.exists(), reason="Windows StreamBridge.Player.exe required")
def test_cadmium_webview2_win32_controls_and_postplay_regression(tmp_path: Path) -> None:
    from streambridge_core.osd_overlay import read_live_telemetry, send_player_control

    root_dir = Path(__file__).resolve().parents[1]
    fixture_html = root_dir / "fixtures" / "mock_cadmium_player.html"
    assert fixture_html.exists()

    # 1. Verify --probe-cadmium contract checker against the Mock Cadmium fixture
    probe_out = tmp_path / "probe_report.json"
    probe_proc = subprocess.run(
        [str(PLAYER_EXE), "--probe-cadmium", str(fixture_html), str(probe_out)],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert probe_proc.returncode == 0
    report = json.loads(probe_out.read_text(encoding="utf-8"))
    assert report["contract_valid"] is True
    assert report["has_netflix_global"] is True
    assert report["has_cadmium_video_player_api"] is True
    assert report["has_falcor_videos_cache"] is True
    assert report["missing_methods"] == []
    # Secondary promo trailer <video> must have been automatically removed (leaving 1 <video>)
    assert report["video_element_count"] == 1
    assert report["watch_credits_clicked"] is True
    assert report["raw_current_time_mutated"] is False
    assert report["subtitle_track_count"] == 3
    assert report["audio_track_count"] == 2

    # 2. Verify live Win32 PostMessageW (WM_APP + 1..7) against --test-fixture
    player_proc = subprocess.Popen(
        [str(PLAYER_EXE), "--test-fixture", str(fixture_html)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait for WebView2 to initialize and auto-play the Mock Cadmium stream
        tel: dict[str, object] | None = None
        for _ in range(30):
            time.sleep(0.25)
            tel = read_live_telemetry()
            if tel and tel.get("state") == "playing" and float(tel.get("duration_seconds") or 0) == 6760.0:
                break
        assert tel is not None
        assert tel.get("state") == "playing"
        assert float(tel.get("duration_seconds") or 0.0) == 6760.0

        # Absolute seek (WM_APP + 3) to 1200s
        assert send_player_control("seek", 1200) is True
        for _ in range(15):
            time.sleep(0.2)
            tel = read_live_telemetry()
            if tel and abs(float(tel.get("position_seconds") or 0.0) - 1200.0) < 5.0:
                break
        assert tel is not None
        assert abs(float(tel.get("position_seconds") or 0.0) - 1200.0) < 5.0

        # Negative relative seek_by -10s (WM_APP + 5) — regression guard for 64-bit LPARAM sign extension!
        assert send_player_control("seek_by", -10) is True
        for _ in range(15):
            time.sleep(0.2)
            tel = read_live_telemetry()
            if tel and abs(float(tel.get("position_seconds") or 0.0) - 1190.0) < 5.0:
                break
        assert tel is not None
        assert abs(float(tel.get("position_seconds") or 0.0) - 1190.0) < 5.0

        # Switch subtitle track to index 2 ('Spanish') via WM_APP + 6 and audio track to index 1 ('Japanese') via WM_APP + 7
        assert send_player_control("set_subtitle", 2) is True
        assert send_player_control("set_audio", 1) is True
        assert send_player_control("osd_mode", 1) is True
        for _ in range(15):
            time.sleep(0.2)
            tel = read_live_telemetry()
            subs = (tel or {}).get("subtitles")
            if isinstance(subs, list) and len(subs) == 3 and subs[2].get("active") is True and (tel or {}).get("osd_visible") is True:
                break
        assert tel is not None
        assert tel.get("osd_visible") is True
        subs_list = tel.get("subtitles")
        auds_list = tel.get("audio_tracks")
        assert isinstance(subs_list, list) and subs_list[2]["active"] is True
        assert isinstance(auds_list, list) and auds_list[1]["active"] is True

        assert send_player_control("osd_mode", 0) is True

        # Pause (WM_APP + 1)
        assert send_player_control("pause") is True
        for _ in range(15):
            time.sleep(0.2)
            tel = read_live_telemetry()
            if tel and tel.get("state") == "paused":
                break
        assert tel is not None and tel.get("state") == "paused"

        # Stop (WM_CLOSE)
        assert send_player_control("stop") is True
        player_proc.wait(timeout=10)
        assert player_proc.returncode == 0
    finally:
        if player_proc.poll() is None:
            player_proc.kill()


@pytest.mark.skipif(sys.platform != "win32" or not PLAYER_EXE.exists(), reason="Windows StreamBridge.Player.exe required")
def test_d3d11_flip_swapchain_osd_capture_self_test(tmp_path: Path) -> None:
    out_json = tmp_path / "wgc_report.json"
    proc = subprocess.run(
        [str(PLAYER_EXE), "--test-wgc", str(out_json)],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert proc.returncode == 0
    report = json.loads(out_json.read_text(encoding="utf-8"))
    assert report["pass"] is True
    assert report["bg_alpha"] == 0
    assert 90 <= int(report["panel_alpha"]) <= 170
    assert report["text_alpha"] == 255

