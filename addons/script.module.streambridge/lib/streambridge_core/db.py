"""Versioned, thread-safe SQLite store for StreamBridge.

Architecture rules:
- Never write directly to Kodi's MyVideos*.db.
- Use WAL mode and foreign key enforcement.
- Provide a context-managed transaction scope that automatically rolls back on error
  (patterned after the thread-safe SQLite session fixes in plugin.video.themoviedb.helper).
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
import time
from typing import Iterator, Optional

from .models import (
    AvailabilityState,
    MediaType,
    Offer,
    PlaybackSessionState,
    PlaybackState,
    Projection,
    Snapshot,
    SnapshotCompleteness,
    Title,
)

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS titles (
    title_id TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    tmdb_id TEXT,
    imdb_id TEXT,
    parent_show_id TEXT,
    show_title TEXT,
    season INTEGER,
    episode INTEGER,
    edition TEXT,
    premiered TEXT,
    plot TEXT,
    is_aired INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_titles_tmdb ON titles(tmdb_id);
CREATE INDEX IF NOT EXISTS idx_titles_imdb ON titles(imdb_id);
CREATE INDEX IF NOT EXISTS idx_titles_parent ON titles(parent_show_id);

CREATE TABLE IF NOT EXISTS offers (
    title_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_content_id TEXT NOT NULL,
    region TEXT NOT NULL,
    launch_url TEXT NOT NULL,
    deep_link_url TEXT,
    entitlement_class TEXT NOT NULL DEFAULT 'subscription',
    availability TEXT NOT NULL DEFAULT 'available',
    last_seen_revision TEXT,
    last_seen_at REAL NOT NULL,
    missing_streak INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (title_id, provider, region),
    FOREIGN KEY (title_id) REFERENCES titles(title_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_offers_provider_avail ON offers(provider, availability);

CREATE TABLE IF NOT EXISTS projections (
    title_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    nfo_relative_path TEXT,
    kodi_db_id INTEGER,
    generation INTEGER NOT NULL DEFAULT 1,
    owned_by_streambridge INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    PRIMARY KEY (title_id, provider),
    FOREIGN KEY (title_id) REFERENCES titles(title_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    region TEXT NOT NULL,
    revision TEXT NOT NULL,
    completeness TEXT NOT NULL,
    status TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    expires_at REAL,
    error_summary TEXT,
    item_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_snapshots_provider_time ON snapshots(provider, fetched_at DESC);

CREATE TABLE IF NOT EXISTS playback_state (
    title_id TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT 'default',
    position_seconds REAL NOT NULL DEFAULT 0.0,
    duration_seconds REAL NOT NULL DEFAULT 0.0,
    completed INTEGER NOT NULL DEFAULT 0,
    play_count INTEGER NOT NULL DEFAULT 0,
    event_sequence INTEGER NOT NULL DEFAULT 0,
    last_state TEXT NOT NULL DEFAULT 'idle',
    last_error TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (title_id, profile),
    FOREIGN KEY (title_id) REFERENCES titles(title_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reconciliation_journal (
    journal_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    revision TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at REAL NOT NULL,
    completed_at REAL,
    summary_json TEXT
);
"""


class StreamBridgeStore:
    """Thread-safe SQLite store managing canonical titles, offers, projections, and playback state."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._local = threading.local()
        self._initialize_schema()

    def _get_connection(self) -> sqlite3.Connection:
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close the current thread's SQLite connection if open."""
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Single-writer transactional context that commits on success and rolls back on exception."""
        with self._write_lock:
            conn = self._get_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _initialize_schema(self) -> None:
        with self.transaction() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def get_schema_version(self) -> int:
        conn = self._get_connection()
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0

    def upsert_title(self, title: Title, conn: Optional[sqlite3.Connection] = None) -> Title:
        """Insert or update a canonical title, preserving its existing `title_id` if matched by `canonical_key`."""
        if conn is None:
            with self.transaction() as tx_conn:
                return self.upsert_title(title, conn=tx_conn)

        canonical_key = title.canonical_key()
        existing = conn.execute(
            "SELECT title_id, created_at FROM titles WHERE canonical_key = ? OR title_id = ?",
            (canonical_key, title.title_id),
        ).fetchone()

        now = time.time()
        if existing:
            title.title_id = existing["title_id"]
            title.created_at = existing["created_at"]
            title.updated_at = now
            conn.execute(
                """
                UPDATE titles SET
                    canonical_key = ?,
                    media_type = ?,
                    title = ?,
                    year = ?,
                    tmdb_id = COALESCE(?, tmdb_id),
                    imdb_id = COALESCE(?, imdb_id),
                    parent_show_id = COALESCE(?, parent_show_id),
                    show_title = COALESCE(?, show_title),
                    season = ?,
                    episode = ?,
                    edition = ?,
                    premiered = COALESCE(?, premiered),
                    plot = COALESCE(?, plot),
                    is_aired = ?,
                    updated_at = ?
                WHERE title_id = ?
                """,
                (
                    canonical_key,
                    title.media_type.value,
                    title.title,
                    title.year,
                    title.tmdb_id,
                    title.imdb_id,
                    title.parent_show_id,
                    title.show_title,
                    title.season,
                    title.episode,
                    title.edition,
                    title.premiered,
                    title.plot,
                    1 if title.is_aired else 0,
                    now,
                    title.title_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO titles (
                    title_id, canonical_key, media_type, title, year,
                    tmdb_id, imdb_id, parent_show_id, show_title, season, episode,
                    edition, premiered, plot, is_aired, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title.title_id,
                    canonical_key,
                    title.media_type.value,
                    title.title,
                    title.year,
                    title.tmdb_id,
                    title.imdb_id,
                    title.parent_show_id,
                    title.show_title,
                    title.season,
                    title.episode,
                    title.edition,
                    title.premiered,
                    title.plot,
                    1 if title.is_aired else 0,
                    title.created_at,
                    now,
                ),
            )
        return title

    def get_title(self, title_id: str) -> Optional[Title]:
        conn = self._get_connection()
        row = conn.execute("SELECT * FROM titles WHERE title_id = ?", (title_id,)).fetchone()
        return self._row_to_title(row) if row else None

    def list_titles(self, provider: Optional[str] = None, available_only: bool = True) -> list[Title]:
        conn = self._get_connection()
        if provider:
            query = """
                SELECT DISTINCT t.* FROM titles t
                JOIN offers o ON o.title_id = t.title_id
                WHERE o.provider = ?
            """
            params: list[object] = [provider]
            if available_only:
                query += " AND o.availability IN ('available', 'stale', 'quarantined')"
            query += " ORDER BY t.title ASC, t.season ASC, t.episode ASC"
            rows = conn.execute(query, params).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM titles ORDER BY title ASC, season ASC, episode ASC"
            ).fetchall()
        return [self._row_to_title(r) for r in rows]

    def upsert_offer(self, offer: Offer, conn: Optional[sqlite3.Connection] = None) -> None:
        if conn is None:
            with self.transaction() as tx_conn:
                self.upsert_offer(offer, conn=tx_conn)
                return

        conn.execute(
            """
            INSERT INTO offers (
                title_id, provider, provider_content_id, region, launch_url,
                deep_link_url, entitlement_class, availability, last_seen_revision,
                last_seen_at, missing_streak
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(title_id, provider, region) DO UPDATE SET
                provider_content_id = excluded.provider_content_id,
                launch_url = excluded.launch_url,
                deep_link_url = excluded.deep_link_url,
                entitlement_class = excluded.entitlement_class,
                availability = excluded.availability,
                last_seen_revision = excluded.last_seen_revision,
                last_seen_at = excluded.last_seen_at,
                missing_streak = excluded.missing_streak
            """,
            (
                offer.title_id,
                offer.provider,
                offer.provider_content_id,
                offer.region,
                offer.launch_url,
                offer.deep_link_url,
                offer.entitlement_class,
                offer.availability.value,
                offer.last_seen_revision,
                offer.last_seen_at,
                offer.missing_streak,
            ),
        )

    def get_offers_for_title(self, title_id: str, provider: Optional[str] = None) -> list[Offer]:
        conn = self._get_connection()
        if provider:
            rows = conn.execute(
                "SELECT * FROM offers WHERE title_id = ? AND provider = ?",
                (title_id, provider),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM offers WHERE title_id = ? ORDER BY last_seen_at DESC",
                (title_id,),
            ).fetchall()
        return [self._row_to_offer(r) for r in rows]

    def get_availability(
        self, title_id: str, provider: str = "netflix", region: str = "US"
    ) -> AvailabilityState:
        for offer in self.get_offers_for_title(title_id, provider=provider):
            if offer.region == region:
                return offer.availability
        return AvailabilityState.UNKNOWN

    def get_provider_offers(self, provider: str, region: Optional[str] = None) -> list[Offer]:
        conn = self._get_connection()
        if region:
            rows = conn.execute(
                "SELECT * FROM offers WHERE provider = ? AND region = ?",
                (provider, region),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM offers WHERE provider = ?",
                (provider,),
            ).fetchall()
        return [self._row_to_offer(r) for r in rows]

    def upsert_projection(self, projection: Projection, conn: Optional[sqlite3.Connection] = None) -> None:
        if conn is None:
            with self.transaction() as tx_conn:
                self.upsert_projection(projection, conn=tx_conn)
                return

        conn.execute(
            """
            INSERT INTO projections (
                title_id, provider, relative_path, nfo_relative_path,
                kodi_db_id, generation, owned_by_streambridge, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(title_id, provider) DO UPDATE SET
                relative_path = excluded.relative_path,
                nfo_relative_path = excluded.nfo_relative_path,
                kodi_db_id = COALESCE(excluded.kodi_db_id, projections.kodi_db_id),
                generation = projections.generation + 1,
                owned_by_streambridge = excluded.owned_by_streambridge,
                updated_at = excluded.updated_at
            """,
            (
                projection.title_id,
                projection.provider,
                projection.relative_path,
                projection.nfo_relative_path,
                projection.kodi_db_id,
                projection.generation,
                1 if projection.owned_by_streambridge else 0,
                projection.updated_at,
            ),
        )

    def get_projection(self, title_id: str, provider: str) -> Optional[Projection]:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM projections WHERE title_id = ? AND provider = ?",
            (title_id, provider),
        ).fetchone()
        return self._row_to_projection(row) if row else None

    def list_projections(self, provider: Optional[str] = None) -> list[Projection]:
        conn = self._get_connection()
        if provider:
            rows = conn.execute(
                "SELECT * FROM projections WHERE provider = ?", (provider,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM projections").fetchall()
        return [self._row_to_projection(r) for r in rows]

    def delete_projection_record(
        self, title_id: str, provider: str, conn: Optional[sqlite3.Connection] = None
    ) -> None:
        if conn is None:
            with self.transaction() as tx_conn:
                self.delete_projection_record(title_id, provider, conn=tx_conn)
                return
        conn.execute(
            "DELETE FROM projections WHERE title_id = ? AND provider = ?",
            (title_id, provider),
        )

    def record_snapshot(self, snapshot: Snapshot, conn: Optional[sqlite3.Connection] = None) -> None:
        if conn is None:
            with self.transaction() as tx_conn:
                self.record_snapshot(snapshot, conn=tx_conn)
                return
        conn.execute(
            """
            INSERT OR REPLACE INTO snapshots (
                snapshot_id, provider, region, revision, completeness,
                status, fetched_at, expires_at, error_summary, item_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id,
                snapshot.provider,
                snapshot.region,
                snapshot.revision,
                snapshot.completeness.value,
                snapshot.status,
                snapshot.fetched_at,
                snapshot.expires_at,
                snapshot.error_summary,
                snapshot.item_count,
            ),
        )

    def get_latest_snapshot(self, provider: str, region: Optional[str] = None) -> Optional[Snapshot]:
        conn = self._get_connection()
        if region:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE provider = ? AND region = ? ORDER BY fetched_at DESC LIMIT 1",
                (provider, region),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE provider = ? ORDER BY fetched_at DESC LIMIT 1",
                (provider,),
            ).fetchone()
        return self._row_to_snapshot(row) if row else None

    def upsert_playback_state(
        self, state: PlaybackState, conn: Optional[sqlite3.Connection] = None
    ) -> None:
        if conn is None:
            with self.transaction() as tx_conn:
                self.upsert_playback_state(state, conn=tx_conn)
                return
        conn.execute(
            """
            INSERT INTO playback_state (
                title_id, profile, position_seconds, duration_seconds,
                completed, play_count, event_sequence, last_state, last_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(title_id, profile) DO UPDATE SET
                position_seconds = excluded.position_seconds,
                duration_seconds = excluded.duration_seconds,
                completed = excluded.completed,
                play_count = excluded.play_count,
                event_sequence = excluded.event_sequence,
                last_state = excluded.last_state,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (
                state.title_id,
                state.profile,
                state.position_seconds,
                state.duration_seconds,
                1 if state.completed else 0,
                state.play_count,
                state.event_sequence,
                state.last_state.value,
                state.last_error,
                state.updated_at,
            ),
        )

    def get_playback_state(self, title_id: str, profile: str = "default") -> Optional[PlaybackState]:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM playback_state WHERE title_id = ? AND profile = ?",
            (title_id, profile),
        ).fetchone()
        return self._row_to_playback_state(row) if row else None

    @staticmethod
    def _row_to_title(row: sqlite3.Row) -> Title:
        return Title(
            title_id=row["title_id"],
            media_type=MediaType(row["media_type"]),
            title=row["title"],
            year=row["year"],
            tmdb_id=row["tmdb_id"],
            imdb_id=row["imdb_id"],
            parent_show_id=row["parent_show_id"],
            show_title=row["show_title"],
            season=row["season"],
            episode=row["episode"],
            edition=row["edition"],
            premiered=row["premiered"],
            plot=row["plot"],
            is_aired=bool(row["is_aired"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_offer(row: sqlite3.Row) -> Offer:
        return Offer(
            title_id=row["title_id"],
            provider=row["provider"],
            provider_content_id=row["provider_content_id"],
            region=row["region"],
            launch_url=row["launch_url"],
            deep_link_url=row["deep_link_url"],
            entitlement_class=row["entitlement_class"],
            availability=AvailabilityState(row["availability"]),
            last_seen_revision=row["last_seen_revision"],
            last_seen_at=row["last_seen_at"],
            missing_streak=row["missing_streak"],
        )

    @staticmethod
    def _row_to_projection(row: sqlite3.Row) -> Projection:
        return Projection(
            title_id=row["title_id"],
            provider=row["provider"],
            relative_path=row["relative_path"],
            nfo_relative_path=row["nfo_relative_path"],
            kodi_db_id=row["kodi_db_id"],
            generation=row["generation"],
            owned_by_streambridge=bool(row["owned_by_streambridge"]),
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
        return Snapshot(
            snapshot_id=row["snapshot_id"],
            provider=row["provider"],
            region=row["region"],
            revision=row["revision"],
            completeness=SnapshotCompleteness(row["completeness"]),
            status=row["status"],
            fetched_at=row["fetched_at"],
            expires_at=row["expires_at"],
            error_summary=row["error_summary"],
            item_count=row["item_count"],
        )

    @staticmethod
    def _row_to_playback_state(row: sqlite3.Row) -> PlaybackState:
        return PlaybackState(
            title_id=row["title_id"],
            profile=row["profile"],
            position_seconds=row["position_seconds"],
            duration_seconds=row["duration_seconds"],
            completed=bool(row["completed"]),
            play_count=row["play_count"],
            event_sequence=row["event_sequence"],
            last_state=PlaybackSessionState(row["last_state"]),
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )
