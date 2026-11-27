"""StreamBridge Background Service (`service.streambridge`).

Responsibilities (Section 1 & Section 3 of Design v0.1):
- Queue due provider refresh on startup and user-initiated library scans.
- Coalesce concurrent scan events and guard against self-generated scan loops.
- After reconciliation commit, request one targeted follow-up scan/clean if changes arrived
  while Kodi was already scanning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from streambridge_core.adapters.base import ProviderAdapter
from streambridge_core.adapters.fixture import FixtureAdapter
from streambridge_core.db import StreamBridgeStore
from streambridge_core.kodi_bridge import IN_KODI, KodiBridge
from streambridge_core.kodi_rpc import KodiJsonRpcClient
from streambridge_core.projection import ProjectionEngine
from streambridge_core.reconciler import ReconciliationEngine, ReconciliationResult

try:
    import xbmc  # type: ignore[import-not-found]

    _BaseMonitor = xbmc.Monitor
except ImportError:

    class _BaseMonitor:  # type: ignore[no-redef]
        def waitForAbort(self, timeout: float = 1.0) -> bool:
            return True

        def abortRequested(self) -> bool:
            return True


class StreamBridgeServiceMonitor(_BaseMonitor):
    """Kodi `xbmc.Monitor` subclass managing non-blocking catalog sync and scan/clean hooks."""

    def __init__(
        self,
        store: Optional[StreamBridgeStore] = None,
        export_root: Optional[Path] = None,
        adapter: Optional[ProviderAdapter] = None,
        kodi_bridge: Optional[KodiBridge] = None,
        kodi_rpc: Optional[KodiJsonRpcClient] = None,
        region: str = "US",
    ) -> None:
        super().__init__()
        self.kodi = kodi_bridge or KodiBridge("service.streambridge")
        self.rpc = kodi_rpc or KodiJsonRpcClient()
        profile_dir = self.kodi.get_profile_dir()
        self.store = store or StreamBridgeStore(profile_dir / "streambridge.db")
        self.export_root = export_root or (profile_dir / "library")
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
        self.adapter = adapter or FixtureAdapter(self.store, fixture_path=default_fixture)
        self.region = region
        self.sync_runs: list[ReconciliationResult] = []

    def run_startup_sync(self) -> ReconciliationResult:
        """Perform initial provider reconciliation on service startup."""
        policy = self.kodi.load_filter_policy()
        result = self.reconciler.reconcile_provider(
            self.adapter, region=self.region, filter_policy=policy
        )
        self.sync_runs.append(result)
        if result.requires_kodi_scan:
            self.rpc.request_targeted_scan()
        if result.requires_kodi_clean:
            self.rpc.request_targeted_clean()
        return result

    def onScanStarted(self, library: str = "video") -> None:
        """Triggered by Kodi when a library scan begins."""
        if library != "video":
            return
        is_user_initiated = self.rpc.mark_kodi_scan_started()
        if not is_user_initiated:
            # Guard against self-triggered scan loops
            return

        policy = self.kodi.load_filter_policy()
        result = self.reconciler.reconcile_provider(
            self.adapter, region=self.region, filter_policy=policy
        )
        self.sync_runs.append(result)
        if result.requires_kodi_scan:
            # Since `_kodi_scanning` is True, this queues `_pending_followup_scan`
            self.rpc.request_targeted_scan()
        if result.requires_kodi_clean:
            self.rpc.request_targeted_clean()

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
        self.run_startup_sync()
        while not self.abortRequested():
            if self.waitForAbort(10.0):
                break


if __name__ == "__main__":
    if IN_KODI:
        service = StreamBridgeServiceMonitor()
        service.run_forever()
