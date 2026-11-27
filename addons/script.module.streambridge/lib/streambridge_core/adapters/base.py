"""Provider adapter contract for StreamBridge (Section 4 of Design v0.1).

Contract methods:
- capabilities() -> catalog, links, auth, playback modes
- refresh_catalog(region, cursor) -> snapshot/delta + completeness
- resolve_offer(title_id, profile) -> verified launch descriptor
- get_availability(title_id) -> available / absent / unknown
- health() -> status, auth required, last successful refresh
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..models import (
    AvailabilityState,
    LaunchDescriptor,
    Offer,
    SnapshotCompleteness,
    Title,
)


@dataclass
class CatalogItem:
    """Pair of canonical Title and provider Offer produced during catalog ingestion."""

    title: Title
    offer: Offer


@dataclass
class CatalogBatch:
    """Snapshot or delta batch returned by `ProviderAdapter.refresh_catalog`."""

    provider: str
    region: str
    revision: str
    completeness: SnapshotCompleteness
    status: str
    items: list[CatalogItem] = field(default_factory=list)
    next_cursor: str | None = None
    error_summary: str | None = None


@dataclass
class AdapterCapabilities:
    """Capabilities declared by a provider adapter."""

    provider_id: str
    display_name: str
    supports_catalog_sync: bool = True
    supports_delta_sync: bool = False
    supports_episode_links: bool = True
    auth_delegated_to_browser: bool = True
    playback_modes: tuple[str, ...] = ("webview2", "official_edge", "netflix_app", "platform_fallback")


@dataclass
class AdapterHealth:
    """Health and readiness report for a provider adapter."""

    provider_id: str
    status: str
    auth_required: bool = False
    last_successful_refresh: float | None = None
    error_message: str | None = None


class ProviderAdapter(ABC):
    """Abstract base class for all StreamBridge provider adapters."""

    provider_id: str

    @abstractmethod
    def capabilities(self) -> AdapterCapabilities:
        """Return static and configured capabilities of this provider adapter."""

    @abstractmethod
    def refresh_catalog(
        self, region: str = "US", cursor: str | None = None
    ) -> CatalogBatch:
        """Fetch a catalog snapshot or delta for `region`."""

    @abstractmethod
    def resolve_offer(
        self, title_id: str, profile: str = "default", preferred_backend: str = "auto"
    ) -> LaunchDescriptor:
        """Resolve an internal `title_id` into a validated, secret-free `LaunchDescriptor`."""

    @abstractmethod
    def get_availability(
        self, title_id: str, region: str = "US"
    ) -> AvailabilityState:
        """Return current known availability for `title_id`."""

    @abstractmethod
    def health(self) -> AdapterHealth:
        """Return adapter operational status and last refresh timestamp."""
