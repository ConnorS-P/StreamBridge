"""Licensed Catalog Adapter for Netflix using Watchmode API + TMDB Watch Providers.

Uses `requests.Session` (provided by `script.module.requests` in Kodi) with timeouts,
redactable API key handling, and fallback to local fixture/curated JSON if no API key is set.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional
import uuid

import requests

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
from ..security import RedactingLogFilter, redact_secrets, validate_launch_url
from .base import (
    AdapterCapabilities,
    AdapterHealth,
    CatalogBatch,
    CatalogItem,
    ProviderAdapter,
)

logger = logging.getLogger("streambridge.netflix_catalog")
logger.addFilter(RedactingLogFilter())


class NetflixCatalogAdapter(ProviderAdapter):
    """Ingests Netflix availability and verified launch links via Watchmode and/or TMDB APIs."""

    provider_id = "netflix"
    WATCHMODE_BASE_URL = "https://api.watchmode.com/v1"
    TMDB_BASE_URL = "https://api.themoviedb.org/3"

    def __init__(
        self,
        store: StreamBridgeStore,
        watchmode_api_key: Optional[str] = None,
        tmdb_api_key: Optional[str] = None,
        descriptor: Optional[ProviderDescriptor] = None,
        session: Optional[requests.Session] = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.store = store
        self.watchmode_api_key = (watchmode_api_key or "").strip() or None
        self.tmdb_api_key = (tmdb_api_key or "").strip() or None
        self.descriptor = descriptor or load_provider_descriptor("netflix")
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "StreamBridge-Kodi/0.1.0",
            }
        )
        self.timeout_seconds = timeout_seconds
        self._last_refresh: Optional[float] = None
        self._last_error: Optional[str] = None

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            provider_id=self.provider_id,
            display_name=self.descriptor.display_name,
            supports_catalog_sync=True,
            supports_delta_sync=False,
            supports_episode_links=True,
            auth_delegated_to_browser=True,
        )

    def refresh_catalog(
        self, region: str = "US", cursor: Optional[str] = None
    ) -> CatalogBatch:
        if self.watchmode_api_key:
            return self._refresh_from_watchmode(region=region, page=int(cursor or "1"))
        if self.tmdb_api_key:
            return self._refresh_from_tmdb(region=region, page=int(cursor or "1"))

        self._last_error = "No Watchmode or TMDB API key configured"
        return CatalogBatch(
            provider=self.provider_id,
            region=region,
            revision=f"no_key_{int(time.time())}",
            completeness=SnapshotCompleteness.FAILED,
            status="auth_required",
            items=[],
            error_summary=self._last_error,
        )

    def _refresh_from_watchmode(self, region: str, page: int = 1) -> CatalogBatch:
        """Fetch Netflix titles and deep links from Watchmode `/list-titles/` endpoint."""
        try:
            resp = self.session.get(
                f"{self.WATCHMODE_BASE_URL}/list-titles/",
                params={
                    "apiKey": self.watchmode_api_key,
                    "source_ids": str(self.descriptor.watchmode_source_id),
                    "regions": region,
                    "types": "movie,tv_series",
                    "page": str(page),
                    "limit": "250",
                },
                timeout=self.timeout_seconds,
            )
            if resp.status_code in (401, 403, 429):
                self._last_error = f"Watchmode API HTTP {resp.status_code}"
                return CatalogBatch(
                    provider=self.provider_id,
                    region=region,
                    revision=f"wm_err_{int(time.time())}",
                    completeness=SnapshotCompleteness.FAILED,
                    status="rate_limited" if resp.status_code == 429 else "auth_error",
                    items=[],
                    error_summary=self._last_error,
                )
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
        except Exception as exc:
            sanitized_err = redact_secrets(str(exc))
            self._last_error = f"Watchmode request failed: {sanitized_err}"
            logger.warning("Watchmode catalog fetch failed: %s", sanitized_err)
            return CatalogBatch(
                provider=self.provider_id,
                region=region,
                revision=f"wm_exc_{int(time.time())}",
                completeness=SnapshotCompleteness.FAILED,
                status="network_error",
                items=[],
                error_summary=self._last_error,
            )

        raw_titles = payload.get("titles", [])
        total_pages = int(payload.get("total_pages", 1))
        current_page = int(payload.get("page", page))

        items: list[CatalogItem] = []
        for entry in raw_titles:
            wm_id = str(entry.get("id", ""))
            content_id = str(entry.get("netflix_id") or entry.get("source_id") or wm_id)
            raw_url = entry.get("web_url") or self.descriptor.format_title_url(content_id)
            validated_url = validate_launch_url(
                raw_url,
                provider=self.provider_id,
                allowed_hosts=self.descriptor.allowed_hosts,
            )
            media_type = (
                MediaType.MOVIE
                if entry.get("type") in ("movie", "short_film")
                else MediaType.SHOW
            )
            orig_lang = str(entry.get("original_language", "en")).lower()
            is_dubbed = bool(entry.get("is_dubbed", orig_lang != "en" and region.upper() in ("US", "GB", "CA", "AU", "NZ")))
            raw_genres = entry.get("genre_names") or entry.get("genres") or ()
            title = Title(
                title_id=generate_title_id(),
                media_type=media_type,
                title=entry.get("title", "Untitled"),
                year=entry.get("year"),
                tmdb_id=str(entry["tmdb_id"]) if entry.get("tmdb_id") else None,
                imdb_id=entry.get("imdb_id"),
                original_language=orig_lang,
                is_dubbed=is_dubbed,
                content_rating=entry.get("us_rating") or entry.get("content_rating"),
                user_rating=float(entry["user_rating"]) if entry.get("user_rating") is not None else None,
                genres=tuple(str(g) for g in raw_genres),
            )
            offer = Offer(
                title_id=title.title_id,
                provider=self.provider_id,
                provider_content_id=content_id,
                region=region,
                launch_url=validated_url,
                deep_link_url=self.descriptor.format_deep_link(content_id),
                availability=AvailabilityState.AVAILABLE,
            )
            items.append(CatalogItem(title=title, offer=offer))

        completeness = (
            SnapshotCompleteness.COMPLETE
            if current_page >= total_pages
            else SnapshotCompleteness.PARTIAL
        )
        self._last_refresh = time.time()
        self._last_error = None
        return CatalogBatch(
            provider=self.provider_id,
            region=region,
            revision=f"wm_{int(self._last_refresh)}_p{current_page}",
            completeness=completeness,
            status="ok",
            items=items,
            next_cursor=str(current_page + 1) if current_page < total_pages else None,
        )

    def _refresh_from_tmdb(self, region: str, page: int = 1) -> CatalogBatch:
        """Fetch Netflix subscription catalog from TMDB `/discover/movie` with watch_provider=8."""
        try:
            resp = self.session.get(
                f"{self.TMDB_BASE_URL}/discover/movie",
                params={
                    "api_key": self.tmdb_api_key,
                    "with_watch_providers": str(self.descriptor.tmdb_provider_id),
                    "watch_region": region,
                    "with_watch_monetization_types": "flatrate",
                    "page": str(page),
                },
                timeout=self.timeout_seconds,
            )
            if resp.status_code in (401, 403, 429):
                self._last_error = f"TMDB API HTTP {resp.status_code}"
                return CatalogBatch(
                    provider=self.provider_id,
                    region=region,
                    revision=f"tmdb_err_{int(time.time())}",
                    completeness=SnapshotCompleteness.FAILED,
                    status="rate_limited" if resp.status_code == 429 else "auth_error",
                    items=[],
                    error_summary=self._last_error,
                )
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
        except Exception as exc:
            sanitized_err = redact_secrets(str(exc))
            self._last_error = f"TMDB request failed: {sanitized_err}"
            return CatalogBatch(
                provider=self.provider_id,
                region=region,
                revision=f"tmdb_exc_{int(time.time())}",
                completeness=SnapshotCompleteness.FAILED,
                status="network_error",
                items=[],
                error_summary=self._last_error,
            )

        results = payload.get("results", [])
        total_pages = int(payload.get("total_pages", 1))
        current_page = int(payload.get("page", page))
        items: list[CatalogItem] = []

        for entry in results:
            tmdb_id = str(entry["id"])
            release_date = entry.get("release_date") or ""
            year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None
            content_id = str(entry.get("netflix_id") or tmdb_id)
            launch_url = validate_launch_url(
                self.descriptor.format_title_url(content_id),
                provider=self.provider_id,
                allowed_hosts=self.descriptor.allowed_hosts,
            )
            title = Title(
                title_id=generate_title_id(),
                media_type=MediaType.MOVIE,
                title=entry.get("title", "Untitled"),
                year=year,
                tmdb_id=tmdb_id,
                premiered=release_date or None,
                plot=entry.get("overview"),
            )
            offer = Offer(
                title_id=title.title_id,
                provider=self.provider_id,
                provider_content_id=content_id,
                region=region,
                launch_url=launch_url,
                deep_link_url=self.descriptor.format_deep_link(content_id),
                availability=AvailabilityState.AVAILABLE,
            )
            items.append(CatalogItem(title=title, offer=offer))

        completeness = (
            SnapshotCompleteness.COMPLETE
            if current_page >= total_pages
            else SnapshotCompleteness.PARTIAL
        )
        self._last_refresh = time.time()
        self._last_error = None
        return CatalogBatch(
            provider=self.provider_id,
            region=region,
            revision=f"tmdb_{int(self._last_refresh)}_p{current_page}",
            completeness=completeness,
            status="ok",
            items=items,
            next_cursor=str(current_page + 1) if current_page < total_pages else None,
        )

    def resolve_offer(
        self, title_id: str, profile: str = "default", preferred_backend: str = "auto"
    ) -> LaunchDescriptor:
        title = self.store.get_title(title_id)
        if title is None:
            raise LookupError(f"Unknown StreamBridge title_id: {title_id}")

        offers = self.store.get_offers_for_title(title_id, provider=self.provider_id)
        if not offers:
            raise LookupError(f"No Netflix offer found for title_id: {title_id}")

        offer = offers[0]
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
        if not self.watchmode_api_key and not self.tmdb_api_key:
            return AdapterHealth(
                provider_id=self.provider_id,
                status="unconfigured",
                auth_required=True,
                last_successful_refresh=self._last_refresh,
                error_message="Configure a Watchmode or TMDB API key, or enable Fixture mode.",
            )
        if self._last_error:
            return AdapterHealth(
                provider_id=self.provider_id,
                status="degraded",
                auth_required=False,
                last_successful_refresh=self._last_refresh,
                error_message=self._last_error,
            )
        return AdapterHealth(
            provider_id=self.provider_id,
            status="healthy",
            auth_required=False,
            last_successful_refresh=self._last_refresh,
        )
