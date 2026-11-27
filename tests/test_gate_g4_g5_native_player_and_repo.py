"""Gate G4 & G5 Tests — Native C++ StreamBridge.Player Diagnostics, Live Named Pipe IPC, Watchmode/TMDB Adapters, and Kodi Repository Builder."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
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
    assert diag["player_version"] == "0.1.0"
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
