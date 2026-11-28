"""Zero-API-Key Live Netflix Catalog Adapter (JustWatch Public GraphQL + WebView2 My List + Watchmode/TMDB).

Default mode (`refresh_catalog`) uses JustWatch's public GraphQL endpoint (`https://apis.justwatch.com/graphql`)
which requires ZERO API keys and returns regional Netflix Movies, TV Shows (with Seasons and Episodes),
canonical TMDB/IMDb IDs, age ratings, IMDb scores, genres, production countries (mapped to original
language and dubbing status), and verified `https://www.netflix.com/title/<id>` launch URLs.

Also merges the user's personal Netflix "My List" items extracted via `StreamBridge.Player.exe --export-mylist`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

import requests

from ..db import StreamBridgeStore
from ..models import (
    AvailabilityState,
    LaunchDescriptor,
    MediaType,
    Offer,
    PlaybackSessionState,
    PlaybackState,
    SnapshotCompleteness,
    Title,
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

JUSTWATCH_GRAPHQL_URL = "https://apis.justwatch.com/graphql"

# Map JustWatch 3-letter genre shortNames to canonical StreamBridge genre names
_JW_GENRE_MAP: dict[str, str] = {
    "act": "Action",
    "ani": "Animation",
    "cmy": "Comedy",
    "crm": "Crime",
    "doc": "Documentary",
    "drm": "Drama",
    "fml": "Family",
    "fnt": "Fantasy",
    "hst": "History",
    "hrr": "Horror",
    "msc": "Musical",
    "trl": "Thriller",
    "rly": "Reality",
    "rma": "Romance",
    "scf": "Sci-Fi",
    "spt": "Sports",
    "war": "War",
    "wsn": "Western",
}

# Map primary ISO-3166-1 production country codes to ISO-639-1 original languages
_COUNTRY_TO_LANG: dict[str, str] = {
    "US": "en",
    "GB": "en",
    "CA": "en",
    "AU": "en",
    "NZ": "en",
    "IE": "en",
    "JP": "ja",
    "KR": "ko",
    "ES": "es",
    "MX": "es",
    "AR": "es",
    "CO": "es",
    "CL": "es",
    "FR": "fr",
    "BE": "fr",
    "DE": "de",
    "AT": "de",
    "IT": "it",
    "BR": "pt",
    "PT": "pt",
    "CN": "zh",
    "TW": "zh",
    "HK": "zh",
    "IN": "hi",
    "TR": "tr",
    "PL": "pl",
    "SE": "sv",
    "DK": "da",
    "NO": "no",
    "FI": "fi",
    "IS": "is",
}

_NETFLIX_ID_RE = re.compile(r"/(?:title|watch)/(\d+)")

_JUSTWATCH_NETFLIX_MOVIES_QUERY = """
query GetPopularNetflixMovies($country: Country!, $first: Int!, $after: String) {
  popularTitles(country: $country, first: $first, after: $after, filter: {packages: ["nfx"], objectTypes: [MOVIE]}) {
    pageInfo {
      endCursor
      hasNextPage
    }
    edges {
      node {
        objectId
        objectType
        content(country: $country, language: "en") {
          title
          originalReleaseYear
          runtime
          ageCertification
          shortDescription
          productionCountries
          externalIds {
            imdbId
            tmdbId
          }
          scoring {
            imdbScore
          }
          genres {
            shortName
          }
        }
        watchNowOffer(country: $country, platform: WEB, filter: {packages: ["nfx"]}) {
          standardWebURL
        }
      }
    }
  }
}
"""

_JUSTWATCH_NETFLIX_SHOWS_QUERY = """
query GetPopularNetflixShows($country: Country!, $first: Int!, $after: String) {
  popularTitles(country: $country, first: $first, after: $after, filter: {packages: ["nfx"], objectTypes: [SHOW]}) {
    pageInfo {
      endCursor
      hasNextPage
    }
    edges {
      node {
        objectId
        objectType
        content(country: $country, language: "en") {
          title
          originalReleaseYear
          runtime
          ageCertification
          shortDescription
          productionCountries
          externalIds {
            imdbId
            tmdbId
          }
          scoring {
            imdbScore
          }
          genres {
            shortName
          }
        }
        watchNowOffer(country: $country, platform: WEB, filter: {packages: ["nfx"]}) {
          standardWebURL
        }
        ... on Show {
          seasons {
            content(country: $country, language: "en") {
              seasonNumber
            }
            episodes {
              content(country: $country, language: "en") {
                title
                episodeNumber
                runtime
              }
              watchNowOffer(country: $country, platform: WEB, filter: {packages: ["nfx"]}) {
                standardWebURL
              }
            }
          }
        }
      }
    }
  }
}
"""

# Backward-compatible alias for unit tests
_JUSTWATCH_NETFLIX_QUERY = _JUSTWATCH_NETFLIX_SHOWS_QUERY


def _extract_netflix_id(url: str, fallback: str) -> str:
    m = _NETFLIX_ID_RE.search(url or "")
    return m.group(1) if m else fallback


def _infer_language_from_countries(countries: list[str] | tuple[str, ...]) -> str:
    if not countries:
        return "en"
    for country in countries:
        c_up = str(country).strip().upper()
        if c_up in ("US", "GB", "AU", "NZ", "IE"):
            return "en"
    first = str(countries[0]).strip().upper()
    return _COUNTRY_TO_LANG.get(first, "other")


class NetflixCatalogAdapter(ProviderAdapter):
    """Ingests live Netflix availability via JustWatch GraphQL (zero API key), Watchmode, or TMDB."""

    provider_id = "netflix"
    WATCHMODE_BASE_URL = "https://api.watchmode.com/v1"
    TMDB_BASE_URL = "https://api.themoviedb.org/3"

    def __init__(
        self,
        store: StreamBridgeStore,
        watchmode_api_key: str | None = None,
        tmdb_api_key: str | None = None,
        descriptor: ProviderDescriptor | None = None,
        session: requests.Session | None = None,
        timeout_seconds: float = 15.0,
        companion_exe_path: Path | None = None,
        my_list_content_ids: set[str] | None = None,
        catalog_limit: int = 100,
    ) -> None:
        self.store = store
        self.watchmode_api_key = (watchmode_api_key or "").strip() or None
        self.tmdb_api_key = (tmdb_api_key or "").strip() or None
        self.descriptor = descriptor or load_provider_descriptor("netflix")
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "StreamBridge-Kodi/0.1.9",
            }
        )
        self.timeout_seconds = timeout_seconds
        self.companion_exe_path = companion_exe_path
        self.my_list_content_ids: set[str] = set(my_list_content_ids or ())
        self.server_bookmarks: dict[str, tuple[float, float]] = {}
        self.catalog_limit = catalog_limit
        self._last_refresh: float | None = None
        self._last_error: str | None = None

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            provider_id=self.provider_id,
            display_name=self.descriptor.display_name,
            supports_catalog_sync=True,
            supports_delta_sync=False,
            supports_episode_links=True,
            auth_delegated_to_browser=True,
        )

    def fetch_my_list_via_companion(self) -> list[dict[str, str]]:
        """Run `StreamBridge.Player.exe --export-mylist` to read the user's signed-in Netflix My List."""
        if not (self.companion_exe_path and self.companion_exe_path.exists()):
            return []
        try:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            no_win_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
            subprocess.run(
                [str(self.companion_exe_path), "--export-mylist", str(tmp_path)],
                shell=False,
                close_fds=True,
                creationflags=no_win_flag,
                timeout=15,
                check=False,
            )
            if tmp_path.exists():
                raw_text = tmp_path.read_text(encoding="utf-8", errors="ignore")
                tmp_path.unlink(missing_ok=True)
                data = json.loads(raw_text) if raw_text.strip() else {}
                items = data.get("items", [])
                for item in items:
                    cid = str(item.get("provider_content_id", "")).strip()
                    if cid:
                        self.my_list_content_ids.add(cid)
                for bm in data.get("bookmarks", []):
                    bm_cid = str(bm.get("provider_content_id", "")).strip()
                    pos_s = float(bm.get("position_seconds") or 0.0)
                    dur_s = float(bm.get("duration_seconds") or 0.0)
                    if bm_cid and pos_s > 5.0:
                        self.server_bookmarks[bm_cid] = (pos_s, dur_s)
                return items
        except Exception as exc:
            logger.warning("My List companion extraction skipped: %s", redact_secrets(str(exc)))
        return []

    def refresh_catalog(
        self, region: str = "US", cursor: str | None = None
    ) -> CatalogBatch:
        if self.watchmode_api_key:
            return self._refresh_from_watchmode(region=region, page=int(cursor or "1"))
        if self.tmdb_api_key:
            return self._refresh_from_tmdb(region=region, page=int(cursor or "1"))

        return self._refresh_from_justwatch_graphql(region=region, first=self.catalog_limit)

    def _refresh_from_justwatch_graphql(self, region: str = "US", first: int = 100) -> CatalogBatch:
        """Fetch live regional Netflix Movies & TV Shows from JustWatch public GraphQL (zero API key).

        Splits Movie and Show GraphQL queries so nested `seasons { episodes }` on Show nodes never
        exceeds JustWatch's 350,000 complexity cap while fetching hundreds of Movies per batch.
        """
        my_list_entries = self.fetch_my_list_via_companion()
        edges: list[dict[str, Any]] = []
        country_code = region.upper()

        # 1. Fetch Movie pages (low GraphQL complexity -> 100 movies per request, up to 3 pages = 300 movies)
        movie_cursor: str | None = None
        movie_page_size = min(max(first, 20), 100)
        for _ in range(3):
            try:
                resp = self.session.post(
                    JUSTWATCH_GRAPHQL_URL,
                    json={
                        "query": _JUSTWATCH_NETFLIX_MOVIES_QUERY,
                        "variables": {
                            "country": country_code,
                            "first": movie_page_size,
                            "after": movie_cursor,
                        },
                    },
                    timeout=self.timeout_seconds,
                )
                resp.raise_for_status()
                payload: dict[str, Any] = resp.json()
            except Exception as exc:
                if not edges:
                    sanitized_err = redact_secrets(str(exc))
                    self._last_error = f"JustWatch GraphQL request failed: {sanitized_err}"
                    logger.warning("JustWatch catalog fetch failed: %s", sanitized_err)
                    return CatalogBatch(
                        provider=self.provider_id,
                        region=region,
                        revision=f"jw_exc_{int(time.time())}",
                        completeness=SnapshotCompleteness.FAILED,
                        status="network_error",
                        items=[],
                        error_summary=self._last_error,
                    )
                break

            pop_titles = (payload.get("data") or {}).get("popularTitles") or {}
            page_edges = pop_titles.get("edges") or []
            edges.extend(page_edges)
            page_info = pop_titles.get("pageInfo") or {}
            if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
                break
            movie_cursor = str(page_info["endCursor"])

        # 2. Fetch TV Show pages (`first: 6` per page stays below the 350,000 complexity limit with seasons/episodes)
        show_cursor: str | None = None
        for _ in range(4):
            try:
                resp = self.session.post(
                    JUSTWATCH_GRAPHQL_URL,
                    json={
                        "query": _JUSTWATCH_NETFLIX_SHOWS_QUERY,
                        "variables": {
                            "country": country_code,
                            "first": 6,
                            "after": show_cursor,
                        },
                    },
                    timeout=self.timeout_seconds,
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception:
                break

            pop_shows = (payload.get("data") or {}).get("popularTitles") or {}
            show_edges = pop_shows.get("edges") or []
            edges.extend(show_edges)
            page_info = pop_shows.get("pageInfo") or {}
            if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
                break
            show_cursor = str(page_info["endCursor"])

        items: list[CatalogItem] = []
        seen_content_ids: set[str] = set()

        for edge in edges:
            node = edge.get("node") or {}
            obj_type = str(node.get("objectType", "MOVIE")).upper()
            content = node.get("content") or {}
            offer_node = node.get("watchNowOffer") or {}
            raw_web_url = str(offer_node.get("standardWebURL") or "")
            if not raw_web_url:
                continue

            obj_id = str(node.get("objectId") or "")
            nf_content_id = _extract_netflix_id(raw_web_url, fallback=obj_id)
            if not nf_content_id or nf_content_id in seen_content_ids:
                continue

            try:
                validated_url = validate_launch_url(
                    raw_web_url,
                    provider=self.provider_id,
                    allowed_hosts=self.descriptor.allowed_hosts,
                )
            except Exception:
                validated_url = self.descriptor.format_title_url(nf_content_id)

            ext_ids = content.get("externalIds") or {}
            tmdb_id = str(ext_ids["tmdbId"]) if ext_ids.get("tmdbId") else None
            imdb_id = str(ext_ids["imdbId"]) if ext_ids.get("imdbId") else None
            scoring = content.get("scoring") or {}
            imdb_score = (
                float(scoring["imdbScore"])
                if scoring.get("imdbScore") is not None
                else None
            )
            age_cert = content.get("ageCertification")
            year = content.get("originalReleaseYear")
            raw_runtime_min = content.get("runtime")
            runtime_sec = int(raw_runtime_min) * 60 if raw_runtime_min and int(raw_runtime_min) > 0 else None
            plot = content.get("shortDescription")
            prod_countries = content.get("productionCountries") or []
            orig_lang = _infer_language_from_countries(prod_countries)
            is_dubbed = orig_lang != "en" and region.upper() in ("US", "GB", "CA", "AU", "NZ")

            raw_genres = content.get("genres") or []
            genres = tuple(
                _JW_GENRE_MAP.get(str(g.get("shortName", "")).lower(), str(g.get("shortName", "")))
                for g in raw_genres
                if isinstance(g, dict) and g.get("shortName")
            )

            in_my_list = nf_content_id in self.my_list_content_ids
            seen_content_ids.add(nf_content_id)

            if obj_type == "MOVIE":
                title_obj = Title(
                    title_id=f"sb_nf_movie_{nf_content_id}",
                    media_type=MediaType.MOVIE,
                    title=str(content.get("title") or "Untitled"),
                    year=int(year) if year else None,
                    tmdb_id=tmdb_id,
                    imdb_id=imdb_id,
                    plot=plot,
                    original_language=orig_lang,
                    is_dubbed=is_dubbed,
                    content_rating=age_cert,
                    user_rating=imdb_score,
                    genres=genres,
                    in_my_list=in_my_list,
                    runtime_seconds=runtime_sec,
                )
                offer_obj = Offer(
                    title_id=title_obj.title_id,
                    provider=self.provider_id,
                    provider_content_id=nf_content_id,
                    region=region,
                    launch_url=validated_url,
                    deep_link_url=self.descriptor.format_deep_link(nf_content_id),
                    availability=AvailabilityState.AVAILABLE,
                )
                items.append(CatalogItem(title=title_obj, offer=offer_obj))
            elif obj_type == "SHOW":
                show_title = str(content.get("title") or "Untitled Show")
                parent_key = f"tmdb:{tmdb_id}" if tmdb_id else f"nf:{nf_content_id}"
                seasons = node.get("seasons") or []
                for s_node in seasons[:3]:  # Cap initial seasons per show for fast library sync
                    s_content = s_node.get("content") or {}
                    season_num = int(s_content.get("seasonNumber") or 1)
                    episodes = s_node.get("episodes") or []
                    for ep_node in episodes[:25]:
                        ep_content = ep_node.get("content") or {}
                        ep_num = int(ep_content.get("episodeNumber") or 1)
                        ep_title = str(ep_content.get("title") or f"Episode {ep_num}")
                        ep_rt_min = ep_content.get("runtime")
                        ep_rt_sec = (
                            int(ep_rt_min) * 60
                            if ep_rt_min and int(ep_rt_min) > 0
                            else runtime_sec
                        )
                        ep_offer_node = ep_node.get("watchNowOffer") or {}
                        ep_raw_url = str(ep_offer_node.get("standardWebURL") or validated_url)
                        ep_nf_id = _extract_netflix_id(ep_raw_url, fallback=nf_content_id)
                        try:
                            ep_valid_url = validate_launch_url(
                                ep_raw_url,
                                provider=self.provider_id,
                                allowed_hosts=self.descriptor.allowed_hosts,
                            )
                        except Exception:
                            ep_valid_url = validated_url

                        ep_title_obj = Title(
                            title_id=f"sb_nf_ep_{nf_content_id}_s{season_num:02d}e{ep_num:02d}",
                            media_type=MediaType.EPISODE,
                            title=ep_title,
                            show_title=show_title,
                            parent_show_id=parent_key,
                            season=season_num,
                            episode=ep_num,
                            year=int(year) if year else None,
                            tmdb_id=tmdb_id,
                            imdb_id=imdb_id,
                            plot=plot,
                            original_language=orig_lang,
                            is_dubbed=is_dubbed,
                            content_rating=age_cert,
                            user_rating=imdb_score,
                            genres=genres,
                            in_my_list=in_my_list,
                            runtime_seconds=ep_rt_sec,
                        )
                        ep_offer_obj = Offer(
                            title_id=ep_title_obj.title_id,
                            provider=self.provider_id,
                            provider_content_id=ep_nf_id,
                            region=region,
                            launch_url=ep_valid_url,
                            deep_link_url=self.descriptor.format_deep_link(ep_nf_id),
                            availability=AvailabilityState.AVAILABLE,
                        )
                        items.append(CatalogItem(title=ep_title_obj, offer=ep_offer_obj))

        # Include any additional My List items discovered directly from the user's Netflix session
        for ml_item in my_list_entries:
            cid = str(ml_item.get("provider_content_id", "")).strip()
            if not cid or cid in seen_content_ids:
                continue
            seen_content_ids.add(cid)
            ml_url = self.descriptor.format_watch_url(cid)
            ml_title = Title(
                title_id=f"sb_nf_ml_{cid}",
                media_type=MediaType.MOVIE,
                title=str(ml_item.get("title") or f"Netflix #{cid}"),
                in_my_list=True,
            )
            ml_offer = Offer(
                title_id=ml_title.title_id,
                provider=self.provider_id,
                provider_content_id=cid,
                region=region,
                launch_url=ml_url,
                deep_link_url=self.descriptor.format_deep_link(cid),
                availability=AvailabilityState.AVAILABLE,
            )
            items.append(CatalogItem(title=ml_title, offer=ml_offer))

        for item in items:
            bm = self.server_bookmarks.get(item.offer.provider_content_id)
            if bm:
                pos_s, dur_s = bm
                pb = self.store.get_playback_state(item.title.title_id) or PlaybackState(
                    title_id=item.title.title_id
                )
                pb.position_seconds = pos_s
                if dur_s > 60:
                    pb.duration_seconds = dur_s
                elif item.title.runtime_seconds and item.title.runtime_seconds > 60:
                    pb.duration_seconds = float(item.title.runtime_seconds)
                pb.last_state = PlaybackSessionState.PAUSED
                self.store.upsert_playback_state(pb)

        self._last_refresh = time.time()
        self._last_error = None
        return CatalogBatch(
            provider=self.provider_id,
            region=region,
            revision=f"jw_{int(self._last_refresh)}",
            completeness=SnapshotCompleteness.COMPLETE,
            status="ok",
            items=items,
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
            is_dubbed = bool(
                entry.get(
                    "is_dubbed",
                    orig_lang != "en" and region.upper() in ("US", "GB", "CA", "AU", "NZ"),
                )
            )
            raw_genres = entry.get("genre_names") or entry.get("genres") or ()
            title = Title(
                title_id=f"sb_nf_wm_{content_id}",
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
                in_my_list=(content_id in self.my_list_content_ids),
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
                title_id=f"sb_nf_tmdb_{content_id}",
                media_type=MediaType.MOVIE,
                title=entry.get("title", "Untitled"),
                year=year,
                tmdb_id=tmdb_id,
                premiered=release_date or None,
                plot=entry.get("overview"),
                in_my_list=(content_id in self.my_list_content_ids),
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
        raw_launch = offer.launch_url
        if "netflix.com/title/" in raw_launch:
            raw_launch = raw_launch.replace("netflix.com/title/", "netflix.com/watch/")
        validated_url = validate_launch_url(
            raw_launch,
            provider=self.provider_id,
            allowed_hosts=self.descriptor.allowed_hosts,
        )
        playback_state = self.store.get_playback_state(title_id, profile=profile)
        resume_sec = (
            playback_state.position_seconds
            if playback_state and not playback_state.completed
            else 0.0
        )
        effective_dur = (
            int(playback_state.duration_seconds)
            if playback_state and playback_state.duration_seconds > 60
            else title.runtime_seconds
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
            duration_seconds=effective_dur,
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
