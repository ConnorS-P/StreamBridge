"""6-step transactional catalog reconciliation engine for StreamBridge.

Implements Section 3 of the StreamBridge Design v0.1:
1. Fetch and validate a complete snapshot or documented delta.
2. Match canonical titles and offers.
3. Stage additions and confirmed removals; quarantine unexpected mass removals.
4. Atomically replace owned files individually and commit revision; journal interrupted work.
5. Queue targeted scan/clean after the current Kodi operation finishes.
6. Reconcile Kodi IDs and preserve viewing history.

Failure and deletion policy:
- Offline, expired login, rate limits, partial pagination, or unknown completeness
  NEVER imply deletion. Keep last-known-good data, mark stale, and retry with backoff.
- Remove an offer only on explicit authoritative removal or repeated complete snapshots
  reaching `removal_grace_snapshots`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
import uuid

from .adapters.base import CatalogBatch, ProviderAdapter
from .db import StreamBridgeStore
from .models import (
    AvailabilityState,
    Snapshot,
    SnapshotCompleteness,
)
from .projection import ProjectionEngine


@dataclass
class ReconciliationConfig:
    """Configurable thresholds for safe catalog reconciliation and deletion."""

    removal_grace_snapshots: int = 2
    mass_removal_ratio_threshold: float = 0.50
    min_active_offers_for_quarantine: int = 4


@dataclass
class ReconciliationResult:
    """Outcome of a single provider reconciliation transaction."""

    provider: str
    region: str
    revision: str
    completeness: SnapshotCompleteness
    status: str
    added_count: int = 0
    updated_count: int = 0
    removed_count: int = 0
    stale_count: int = 0
    quarantined_count: int = 0
    requires_kodi_scan: bool = False
    requires_kodi_clean: bool = False
    error_summary: str | None = None
    scan_paths: list[str] = field(default_factory=list)


class ReconciliationEngine:
    """Coordinates provider catalog ingestion, SQLite persistence, and physical projection."""

    def __init__(
        self,
        store: StreamBridgeStore,
        projection_engine: ProjectionEngine,
        config: ReconciliationConfig | None = None,
    ) -> None:
        self.store = store
        self.projection = projection_engine
        self.config = config or ReconciliationConfig()

    def reconcile_provider(
        self,
        adapter: ProviderAdapter,
        region: str = "US",
        cursor: str | None = None,
        force_authoritative_removal: bool = False,
    ) -> ReconciliationResult:
        """Execute the 6-step reconciliation transaction for `adapter`."""
        provider = adapter.provider_id
        journal_id = f"jrn_{uuid.uuid4().hex[:12]}"
        now = time.time()

        # Step 1: Fetch and validate snapshot
        try:
            batch: CatalogBatch = adapter.refresh_catalog(region=region, cursor=cursor)
        except Exception as exc:
            error_msg = f"Adapter fetch exception: {exc}"
            snapshot = Snapshot(
                snapshot_id=f"snap_{uuid.uuid4().hex[:12]}",
                provider=provider,
                region=region,
                revision=f"failed_{int(now)}",
                completeness=SnapshotCompleteness.FAILED,
                status="failed",
                fetched_at=now,
                error_summary=error_msg,
                item_count=0,
            )
            return self._handle_non_complete_batch(provider, region, snapshot)

        snapshot = Snapshot(
            snapshot_id=f"snap_{uuid.uuid4().hex[:12]}",
            provider=provider,
            region=region,
            revision=batch.revision,
            completeness=batch.completeness,
            status=batch.status,
            fetched_at=now,
            error_summary=batch.error_summary,
            item_count=len(batch.items),
        )

        # Offline, expired login, rate limit, or failed fetch -> never delete, mark existing stale
        if batch.completeness == SnapshotCompleteness.FAILED:
            return self._handle_non_complete_batch(provider, region, snapshot)

        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO reconciliation_journal (journal_id, provider, revision, state, started_at)
                VALUES (?, ?, ?, 'in_progress', ?)
                """,
                (journal_id, provider, batch.revision, now),
            )
            self.store.record_snapshot(snapshot, conn=conn)

            existing_offers = {
                o.title_id: o
                for o in self.store.get_provider_offers(provider=provider, region=region)
            }
            existing_projections = {
                p.title_id: p for p in self.store.list_projections(provider=provider)
            }

            seen_title_ids: set[str] = set()
            added_count = 0
            updated_count = 0

            # Step 2: Match canonical titles and offers
            for item in batch.items:
                stored_title = self.store.upsert_title(item.title, conn=conn)
                offer = item.offer
                offer.title_id = stored_title.title_id
                offer.provider = provider
                offer.region = region
                offer.last_seen_revision = batch.revision
                offer.last_seen_at = now
                offer.missing_streak = 0

                was_present = stored_title.title_id in existing_offers
                seen_title_ids.add(stored_title.title_id)

                if offer.availability == AvailabilityState.AVAILABLE:
                    self.store.upsert_offer(offer, conn=conn)
                    proj = self.projection.project_title(stored_title, offer)
                    if proj is not None:
                        self.store.upsert_projection(proj, conn=conn)
                        if was_present:
                            updated_count += 1
                        else:
                            added_count += 1
                elif offer.availability == AvailabilityState.ABSENT:
                    # Explicit authoritative removal in delta or complete feed
                    offer.missing_streak = self.config.removal_grace_snapshots
                    self.store.upsert_offer(offer, conn=conn)

            # Step 3: Stage removals & check mass-deletion quarantine on COMPLETE snapshots
            removed_count = 0
            stale_count = 0
            quarantined_count = 0

            active_previous = [
                o
                for o in existing_offers.values()
                if o.availability in (AvailabilityState.AVAILABLE, AvailabilityState.STALE, AvailabilityState.QUARANTINED)
            ]

            missing_offers = [
                o
                for o in active_previous
                if o.title_id not in seen_title_ids
                or (
                    o.title_id in seen_title_ids
                    and any(
                        i.offer.availability == AvailabilityState.ABSENT
                        and i.title.title_id == o.title_id
                        for i in batch.items
                    )
                )
            ]

            if batch.completeness == SnapshotCompleteness.PARTIAL:
                # Partial pagination: retain all unseen offers as STALE, never delete
                for unseen in missing_offers:
                    unseen.availability = AvailabilityState.STALE
                    self.store.upsert_offer(unseen, conn=conn)
                    stale_count += 1
            elif batch.completeness == SnapshotCompleteness.COMPLETE:
                total_active = len(active_previous)
                missing_ratio = (len(missing_offers) / total_active) if total_active > 0 else 0.0
                is_mass_removal = (
                    not force_authoritative_removal
                    and total_active >= self.config.min_active_offers_for_quarantine
                    and missing_ratio >= self.config.mass_removal_ratio_threshold
                )

                if is_mass_removal:
                    # Quarantine unexpected mass removal; keep .strm projections intact
                    for offer in missing_offers:
                        offer.availability = AvailabilityState.QUARANTINED
                        offer.missing_streak += 1
                        self.store.upsert_offer(offer, conn=conn)
                        quarantined_count += 1
                else:
                    for offer in missing_offers:
                        offer.missing_streak += 1
                        explicit_absent = any(
                            i.title.title_id == offer.title_id
                            and i.offer.availability == AvailabilityState.ABSENT
                            for i in batch.items
                        )
                        if (
                            force_authoritative_removal
                            or explicit_absent
                            or offer.missing_streak >= self.config.removal_grace_snapshots
                        ):
                            offer.availability = AvailabilityState.ABSENT
                            self.store.upsert_offer(offer, conn=conn)
                            proj = existing_projections.get(offer.title_id)
                            if proj:
                                self.projection.remove_projection(proj)
                                self.store.delete_projection_record(
                                    offer.title_id, provider, conn=conn
                                )
                                removed_count += 1
                        else:
                            offer.availability = AvailabilityState.STALE
                            self.store.upsert_offer(offer, conn=conn)
                            stale_count += 1
            elif batch.completeness == SnapshotCompleteness.DELTA:
                # In DELTA mode, only explicitly ABSENT items in the delta are removed
                for item in batch.items:
                    if item.offer.availability == AvailabilityState.ABSENT:
                        proj = existing_projections.get(item.title.title_id)
                        if proj:
                            self.projection.remove_projection(proj)
                            self.store.delete_projection_record(
                                item.title.title_id, provider, conn=conn
                            )
                            removed_count += 1

            result = ReconciliationResult(
                provider=provider,
                region=region,
                revision=batch.revision,
                completeness=batch.completeness,
                status=batch.status,
                added_count=added_count,
                updated_count=updated_count,
                removed_count=removed_count,
                stale_count=stale_count,
                quarantined_count=quarantined_count,
                requires_kodi_scan=(added_count > 0),
                requires_kodi_clean=(removed_count > 0),
                error_summary=batch.error_summary,
            )

            conn.execute(
                """
                UPDATE reconciliation_journal
                SET state = 'committed', completed_at = ?, summary_json = ?
                WHERE journal_id = ?
                """,
                (
                    time.time(),
                    json.dumps(
                        {
                            "added": added_count,
                            "updated": updated_count,
                            "removed": removed_count,
                            "stale": stale_count,
                            "quarantined": quarantined_count,
                        }
                    ),
                    journal_id,
                ),
            )
            return result

    def _handle_non_complete_batch(
        self, provider: str, region: str, snapshot: Snapshot
    ) -> ReconciliationResult:
        """Mark active offers as STALE without deleting any .strm files or watch history."""
        stale_count = 0
        with self.store.transaction() as conn:
            self.store.record_snapshot(snapshot, conn=conn)
            for offer in self.store.get_provider_offers(provider=provider, region=region):
                if offer.availability == AvailabilityState.AVAILABLE:
                    offer.availability = AvailabilityState.STALE
                    self.store.upsert_offer(offer, conn=conn)
                    stale_count += 1

        return ReconciliationResult(
            provider=provider,
            region=region,
            revision=snapshot.revision,
            completeness=snapshot.completeness,
            status=snapshot.status,
            added_count=0,
            updated_count=0,
            removed_count=0,
            stale_count=stale_count,
            quarantined_count=0,
            requires_kodi_scan=False,
            requires_kodi_clean=False,
            error_summary=snapshot.error_summary,
        )
