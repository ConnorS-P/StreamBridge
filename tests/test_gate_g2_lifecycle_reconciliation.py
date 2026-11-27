"""Gate G2 Tests — Lifecycle, Fault Tolerance, Quarantine, Grace Periods, and Loop Prevention."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from service import StreamBridgeServiceMonitor
from streambridge_core.adapters.fixture import FixtureAdapter
from streambridge_core.db import StreamBridgeStore
from streambridge_core.kodi_rpc import KodiJsonRpcClient
from streambridge_core.models import AvailabilityState, SnapshotCompleteness
from streambridge_core.projection import ProjectionEngine
from streambridge_core.reconciler import ReconciliationConfig, ReconciliationEngine


def _make_catalog_payload(revision: str, count: int, completeness: str = "complete") -> dict[str, Any]:
    items = []
    for i in range(1, count + 1):
        items.append(
            {
                "title_id": f"sb_movie_{i:03d}",
                "media_type": "movie",
                "title": f"Test Movie {i}",
                "year": 2020 + (i % 5),
                "tmdb_id": str(10000 + i),
                "imdb_id": f"tt00{10000 + i}",
                "provider_content_id": str(80000000 + i),
                "launch_url": f"https://www.netflix.com/watch/{80000000 + i}",
                "availability": "available",
            }
        )
    return {
        "provider": "netflix",
        "region": "US",
        "revision": revision,
        "completeness": completeness,
        "status": "ok",
        "items": items,
    }


def test_g2_idempotent_scans_and_offline_partial_preservation(tmp_path: Path) -> None:
    store = StreamBridgeStore(tmp_path / "sb.db")
    export_root = tmp_path / "library"
    projection = ProjectionEngine(export_root, write_nfo=True)
    reconciler = ReconciliationEngine(
        store,
        projection,
        config=ReconciliationConfig(
            removal_grace_snapshots=2,
            mass_removal_ratio_threshold=0.50,
            min_active_offers_for_quarantine=4,
        ),
    )
    adapter = FixtureAdapter(store)

    # 1. Initial complete snapshot of 6 movies
    adapter.set_override_payload(_make_catalog_payload("rev_1", 6))
    r1 = reconciler.reconcile_provider(adapter)
    assert r1.added_count == 6
    assert r1.removed_count == 0
    assert len(list(export_root.rglob("*.strm"))) == 6

    # 2. Repeated scan with identical snapshot is idempotent (0 added, 0 removed)
    r2 = reconciler.reconcile_provider(adapter)
    assert r2.added_count == 0
    assert r2.updated_count == 6
    assert r2.removed_count == 0
    assert len(list(export_root.rglob("*.strm"))) == 6

    # 3. Simulated offline / rate limit / expired auth failure NEVER deletes files
    adapter.set_simulated_failure("HTTP 429 Too Many Requests")
    r_fail = reconciler.reconcile_provider(adapter)
    assert r_fail.completeness == SnapshotCompleteness.FAILED
    assert r_fail.removed_count == 0
    assert r_fail.stale_count == 6
    assert len(list(export_root.rglob("*.strm"))) == 6
    adapter.set_simulated_failure(None)

    # 4. Partial pagination snapshot (only 2 of 6 items returned) marks unseen as STALE, never deletes
    adapter.set_override_payload(_make_catalog_payload("rev_partial", 2, completeness="partial"))
    r_partial = reconciler.reconcile_provider(adapter)
    assert r_partial.completeness == SnapshotCompleteness.PARTIAL
    assert r_partial.removed_count == 0
    assert r_partial.stale_count == 4
    assert len(list(export_root.rglob("*.strm"))) == 6

    # 5. Unexpected mass removal (5 of 6 items disappear in a "complete" feed = 83% drop >= 50%)
    # Triggers quarantine; keeps all 6 .strm files intact!
    adapter.set_override_payload(_make_catalog_payload("rev_mass_drop", 1, completeness="complete"))
    r_quarantine = reconciler.reconcile_provider(adapter)
    assert r_quarantine.quarantined_count == 5
    assert r_quarantine.removed_count == 0
    assert len(list(export_root.rglob("*.strm"))) == 6

    # 6. Restore full 6-item catalog, then remove 1 item cleanly across 2 grace snapshots
    adapter.set_override_payload(_make_catalog_payload("rev_restore", 6, completeness="complete"))
    reconciler.reconcile_provider(adapter)

    adapter.set_override_payload(_make_catalog_payload("rev_drop_one_1", 5, completeness="complete"))
    r_grace1 = reconciler.reconcile_provider(adapter)
    assert r_grace1.stale_count == 1
    assert r_grace1.removed_count == 0
    assert len(list(export_root.rglob("*.strm"))) == 6

    adapter.set_override_payload(_make_catalog_payload("rev_drop_one_2", 5, completeness="complete"))
    r_grace2 = reconciler.reconcile_provider(adapter)
    assert r_grace2.removed_count == 1
    assert r_grace2.requires_kodi_clean is True
    assert len(list(export_root.rglob("*.strm"))) == 5
    assert store.get_availability("sb_movie_006") == AvailabilityState.ABSENT
    store.close()


def test_g2_service_monitor_coalesces_scans_and_prevents_self_trigger_loops(tmp_path: Path) -> None:
    rpc_calls: list[str] = []

    def fake_rpc_transport(req_json: str) -> str:
        rpc_calls.append(req_json)
        return '{"jsonrpc": "2.0", "id": 1, "result": "OK"}'

    store = StreamBridgeStore(tmp_path / "sb.db")
    export_root = tmp_path / "library"
    rpc = KodiJsonRpcClient(rpc_Transport=fake_rpc_transport)
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "netflix_sample_catalog.json"
    adapter = FixtureAdapter(store, fixture_path=fixture_path)

    service = StreamBridgeServiceMonitor(
        store=store,
        export_root=export_root,
        adapter=adapter,
        kodi_rpc=rpc,
    )

    # User initiates a library scan in Kodi -> onScanStarted fires
    service.onScanStarted("video")
    assert len(service.sync_runs) == 1
    assert service.sync_runs[0].added_count == 6
    # Because Kodi was in the middle of scanning, the follow-up scan is queued until onScanFinished
    assert len(rpc_calls) == 0

    # Kodi finishes its initial scan -> StreamBridge fires exactly ONE targeted follow-up scan
    service.onScanFinished("video")
    assert len(rpc_calls) == 1
    assert "VideoLibrary.Scan" in rpc_calls[0]

    # When Kodi starts that follow-up scan, onScanStarted fires again,
    # but StreamBridge recognizes it as self-triggered and does NOT loop!
    service.onScanStarted("video")
    assert len(service.sync_runs) == 1
    service.onScanFinished("video")
    assert len(rpc_calls) == 1

    # Verify periodic background sync interval & playback deferral
    service.last_sync_timestamp = 1000.0
    service.kodi.sync_schedule_overrides = {
        "periodic_sync_enabled": True,
        "periodic_sync_interval_hours": 6,
        "pause_sync_during_playback": True,
    }
    # Not due yet (only 1 hour elapsed out of 6 hours)
    assert service.run_periodic_sync_if_due(now=1000.0 + 3600.0) is None
    assert len(service.sync_runs) == 1

    # Due (6 hours elapsed), but video playback is active -> deferred!
    service.kodi.playing_video = True
    assert service.run_periodic_sync_if_due(now=1000.0 + 6 * 3600.0 + 10.0) is None
    assert len(service.sync_runs) == 1

    # Playback stops -> periodic sync executes immediately on next tick!
    service.kodi.playing_video = False
    res_periodic = service.run_periodic_sync_if_due(now=1000.0 + 6 * 3600.0 + 20.0)
    assert res_periodic is not None
    assert len(service.sync_runs) == 2

    # User updates StreamBridge Settings -> onSettingsChanged triggers immediate reconciliation
    service.onSettingsChanged()
    assert len(service.sync_runs) == 3
    store.close()
