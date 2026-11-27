"""Deterministic local JSON fixture adapter for StreamBridge testing and curated lists."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any
import uuid

from ..db import StreamBridgeStore
from ..models import (
    AvailabilityState,
    LaunchDescriptor,
    MediaType,
    Offer,
    SnapshotCompleteness,
    Title,
    generate_title_id,
)
from ..providers import ProviderDescriptor, load_provider_descriptor
from ..security import validate_launch_url
from .base import (
    AdapterCapabilities,
    AdapterHealth,
    CatalogBatch,
    CatalogItem,
    ProviderAdapter,
)


class FixtureAdapter(ProviderAdapter):
    """Provider adapter backed by a local JSON fixture file or in-memory catalog payload."""

    def __init__(
        self,
        store: StreamBridgeStore,
        fixture_path: Path | str | None = None,
        provider_id: str = "netflix",
        descriptor: ProviderDescriptor | None = None,
    ) -> None:
        self.store = store
        self.provider_id = provider_id
        self.descriptor = descriptor or load_provider_descriptor(provider_id)
        self.fixture_path = Path(fixture_path) if fixture_path else None
        self._override_payload: dict[str, Any] | None = None
        self._simulate_failure: str | None = None
        self._last_refresh: float | None = None

    def set_override_payload(self, payload: dict[str, Any] | None) -> None:
        """Override the JSON fixture data in memory (used by Gate G1/G2 tests)."""
        self._override_payload = payload

    def set_simulated_failure(self, error_message: str | None) -> None:
        """Simulate an offline/rate-limit/auth failure on the next `refresh_catalog` call."""
        self._simulate_failure = error_message

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            provider_id=self.provider_id,
            display_name=self.descriptor.display_name,
            supports_catalog_sync=True,
            supports_delta_sync=True,
            supports_episode_links=True,
            auth_delegated_to_browser=True,
        )

    def refresh_catalog(
        self, region: str = "US", cursor: str | None = None
    ) -> CatalogBatch:
        if self._simulate_failure:
            return CatalogBatch(
                provider=self.provider_id,
                region=region,
                revision=f"err_{int(time.time())}",
                completeness=SnapshotCompleteness.FAILED,
                status="error",
                items=[],
                error_summary=self._simulate_failure,
            )

        data = self._load_payload()
        revision = str(data.get("revision", f"rev_{int(time.time())}"))
        completeness = SnapshotCompleteness(
            data.get("completeness", SnapshotCompleteness.COMPLETE.value)
        )
        status = str(data.get("status", "ok"))

        items: list[CatalogItem] = []
        for raw in data.get("items", []):
            item_region = raw.get("region", region)
            if item_region != region:
                continue

            media_type = MediaType(raw.get("media_type", "movie"))
            content_id = str(raw["provider_content_id"])
            raw_url = raw.get("launch_url") or self.descriptor.format_watch_url(content_id)
            validated_url = validate_launch_url(
                raw_url,
                provider=self.provider_id,
                allowed_hosts=self.descriptor.allowed_hosts,
            )

            raw_genres = raw.get("genres") or ()
            title = Title(
                title_id=raw.get("title_id") or generate_title_id(),
                media_type=media_type,
                title=raw["title"],
                year=raw.get("year"),
                tmdb_id=str(raw["tmdb_id"]) if raw.get("tmdb_id") is not None else None,
                imdb_id=raw.get("imdb_id"),
                parent_show_id=raw.get("parent_show_id"),
                show_title=raw.get("show_title"),
                season=raw.get("season"),
                episode=raw.get("episode"),
                edition=raw.get("edition"),
                premiered=raw.get("premiered"),
                plot=raw.get("plot"),
                is_aired=bool(raw.get("is_aired", True)),
                original_language=str(raw.get("original_language", "en")),
                is_dubbed=bool(raw.get("is_dubbed", False)),
                content_rating=raw.get("content_rating"),
                user_rating=float(raw["user_rating"]) if raw.get("user_rating") is not None else None,
                genres=tuple(str(g) for g in raw_genres),
            )

            offer = Offer(
                title_id=title.title_id,
                provider=self.provider_id,
                provider_content_id=content_id,
                region=region,
                launch_url=validated_url,
                deep_link_url=raw.get("deep_link_url") or self.descriptor.format_deep_link(content_id),
                entitlement_class=raw.get("entitlement_class", "subscription"),
                availability=AvailabilityState(raw.get("availability", "available")),
                last_seen_revision=revision,
            )
            items.append(CatalogItem(title=title, offer=offer))

        self._last_refresh = time.time()
        return CatalogBatch(
            provider=self.provider_id,
            region=region,
            revision=revision,
            completeness=completeness,
            status=status,
            items=items,
            error_summary=data.get("error_summary"),
        )

    def resolve_offer(
        self, title_id: str, profile: str = "default", preferred_backend: str = "auto"
    ) -> LaunchDescriptor:
        title = self.store.get_title(title_id)
        if title is None:
            raise LookupError(f"Unknown StreamBridge title_id: {title_id}")

        offers = self.store.get_offers_for_title(title_id, provider=self.provider_id)
        if not offers:
            raise LookupError(f"No {self.provider_id} offer found for title_id: {title_id}")

        offer = offers[0]
        if offer.availability == AvailabilityState.ABSENT:
            raise PermissionError(
                f"Title {title.title!r} is no longer available on {self.descriptor.display_name}"
            )

        validated_url = validate_launch_url(
            offer.launch_url,
            provider=self.provider_id,
            allowed_hosts=self.descriptor.allowed_hosts,
        )
        playback_state = self.store.get_playback_state(title_id, profile=profile)
        resume_sec = (
            playback_state.position_seconds
            if playback_state and not playback_state.completed
            else 0.0
        )

        display_name = (
            f"{title.show_title} S{title.season:02d}E{title.episode:02d} - {title.title}"
            if title.media_type == MediaType.EPISODE and title.show_title
            else f"{title.title} ({title.year})"
            if title.year
            else title.title
        )

        return LaunchDescriptor(
            session_id=f"ses_{uuid.uuid4().hex[:12]}",
            title_id=title.title_id,
            provider=self.provider_id,
            provider_content_id=offer.provider_content_id,
            launch_url=validated_url,
            media_type=title.media_type,
            title_display=display_name,
            preferred_backend=preferred_backend,
            resume_position_seconds=resume_sec,
            tmdb_id=title.tmdb_id,
            imdb_id=title.imdb_id,
            deep_link_url=offer.deep_link_url,
            extra_capabilities=dict(self.descriptor.capabilities),
        )

    def get_availability(
        self, title_id: str, region: str = "US"
    ) -> AvailabilityState:
        offers = self.store.get_offers_for_title(title_id, provider=self.provider_id)
        for offer in offers:
            if offer.region == region:
                return offer.availability
        return AvailabilityState.UNKNOWN

    def health(self) -> AdapterHealth:
        if self._simulate_failure:
            return AdapterHealth(
                provider_id=self.provider_id,
                status="degraded",
                auth_required=False,
                last_successful_refresh=self._last_refresh,
                error_message=self._simulate_failure,
            )
        return AdapterHealth(
            provider_id=self.provider_id,
            status="healthy",
            auth_required=False,
            last_successful_refresh=self._last_refresh,
        )

    def _load_payload(self) -> dict[str, Any]:
        if self._override_payload is not None:
            return self._override_payload
        if self.fixture_path and self.fixture_path.exists():
            return json.loads(self.fixture_path.read_text(encoding="utf-8"))
        raise FileNotFoundError("No fixture payload or fixture_path configured")
