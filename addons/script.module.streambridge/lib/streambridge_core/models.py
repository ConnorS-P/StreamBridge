"""Domain models for StreamBridge catalog, projection, reconciliation, and playback."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import time
import uuid


class MediaType(str, Enum):
    MOVIE = "movie"
    SHOW = "tvshow"
    EPISODE = "episode"


class AvailabilityState(str, Enum):
    AVAILABLE = "available"
    ABSENT = "absent"
    STALE = "stale"
    QUARANTINED = "quarantined"
    UNKNOWN = "unknown"


class SnapshotCompleteness(str, Enum):
    COMPLETE = "complete"
    DELTA = "delta"
    PARTIAL = "partial"
    FAILED = "failed"


class PlaybackSessionState(str, Enum):
    IDLE = "idle"
    LAUNCHING = "launching"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"


def generate_title_id() -> str:
    """Generate a deterministic-format opaque StreamBridge internal title ID."""
    return f"sb_{uuid.uuid4().hex[:16]}"


@dataclass
class Title:
    """Canonical media title independent of individual streaming providers."""

    title_id: str
    media_type: MediaType
    title: str
    year: int | None = None
    tmdb_id: str | None = None
    imdb_id: str | None = None
    parent_show_id: str | None = None
    show_title: str | None = None
    season: int | None = None
    episode: int | None = None
    edition: str | None = None
    premiered: str | None = None
    plot: str | None = None
    is_aired: bool = True
    original_language: str = "en"
    is_dubbed: bool = False
    content_rating: str | None = None
    user_rating: float | None = None
    genres: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def canonical_key(self) -> str:
        """Return a stable lookup key based on canonical external IDs when present."""
        if self.media_type == MediaType.EPISODE:
            parent_key = self.parent_show_id or (f"tmdb:{self.tmdb_id}" if self.tmdb_id else self.show_title or "")
            return f"episode:{parent_key}:S{self.season or 0:02d}E{self.episode or 0:02d}"
        if self.tmdb_id:
            return f"{self.media_type.value}:tmdb:{self.tmdb_id}"
        if self.imdb_id:
            return f"{self.media_type.value}:imdb:{self.imdb_id}"
        return f"{self.media_type.value}:title:{self.title.lower().strip()}:{self.year or 0}"


@dataclass
class Offer:
    """Provider-specific availability and verified launch link for a canonical Title."""

    title_id: str
    provider: str
    provider_content_id: str
    region: str
    launch_url: str
    entitlement_class: str = "subscription"
    availability: AvailabilityState = AvailabilityState.AVAILABLE
    last_seen_revision: str | None = None
    last_seen_at: float = field(default_factory=time.time)
    missing_streak: int = 0
    deep_link_url: str | None = None


@dataclass
class Projection:
    """Physical .strm and optional .nfo projection record owned by StreamBridge."""

    title_id: str
    provider: str
    relative_path: str
    nfo_relative_path: str | None = None
    kodi_db_id: int | None = None
    generation: int = 1
    owned_by_streambridge: bool = True
    updated_at: float = field(default_factory=time.time)


@dataclass
class Snapshot:
    """Record of a catalog fetch operation from a provider adapter."""

    snapshot_id: str
    provider: str
    region: str
    revision: str
    completeness: SnapshotCompleteness
    status: str
    fetched_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    error_summary: str | None = None
    item_count: int = 0


@dataclass
class PlaybackState:
    """Recoverable local playback progress and telemetry state."""

    title_id: str
    profile: str = "default"
    position_seconds: float = 0.0
    duration_seconds: float = 0.0
    completed: bool = False
    play_count: int = 0
    event_sequence: int = 0
    last_state: PlaybackSessionState = PlaybackSessionState.IDLE
    last_error: str | None = None
    updated_at: float = field(default_factory=time.time)


@dataclass
class LaunchDescriptor:
    """Validated, secret-free launch request passed from Resolver to Playback Broker."""

    session_id: str
    title_id: str
    provider: str
    provider_content_id: str
    launch_url: str
    media_type: MediaType
    title_display: str
    preferred_backend: str = "auto"
    resume_position_seconds: float = 0.0
    tmdb_id: str | None = None
    imdb_id: str | None = None
    deep_link_url: str | None = None
    extra_capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionEvent:
    """Telemetry event emitted by a playback backend or companion player."""

    session_id: str
    title_id: str
    state: PlaybackSessionState
    sequence: int
    position_seconds: float | None = None
    duration_seconds: float | None = None
    backend_used: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    telemetry_verified: bool = False
    timestamp: float = field(default_factory=time.time)
