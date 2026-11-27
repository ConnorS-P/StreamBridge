"""Library Content Filtering Engine for StreamBridge.

Addresses common streaming catalog frustrations (dubbed foreign-language content passed off
as native English, reality/talk-show clutter, toddler shovelware, low-rated filler, and
parental maturity caps) before `.strm` files are projected into Kodi's library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import MediaType, Title

# Canonical maturity hierarchy from least restrictive (1) to most restrictive (5)
MATURITY_RANK: dict[str, int] = {
    "TV-Y": 1,
    "TV-Y7": 1,
    "TV-Y7-FV": 1,
    "G": 1,
    "TV-G": 1,
    "PG": 2,
    "TV-PG": 2,
    "PG-13": 3,
    "TV-14": 3,
    "R": 4,
    "TV-MA": 4,
    "NC-17": 5,
}

KIDS_RATINGS = frozenset({"TV-Y", "TV-Y7", "TV-Y7-FV", "G", "TV-G"})
REALITY_AND_TALK_GENRES = frozenset(
    {"reality", "reality-tv", "reality tv", "talk show", "talk-show", "game show", "game-show"}
)
STANDUP_GENRES = frozenset({"stand-up", "standup", "stand-up comedy"})
DOCUMENTARY_GENRES = frozenset({"documentary", "docuseries"})


def _parse_csv_tokens(raw: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if isinstance(raw, (tuple, list)):
        return tuple(str(x).strip().lower() for x in raw if str(x).strip())
    return tuple(part.strip().lower() for part in (raw or "").split(",") if part.strip())


@dataclass
class LibraryFilterPolicy:
    """User-configurable whitelist/blacklist rules applied before library projection."""

    # Media type toggles
    include_movies: bool = True
    include_tv_shows: bool = True

    # Language & dubbing filters
    allowed_original_languages: tuple[str, ...] = ("en",)
    blocked_original_languages: tuple[str, ...] = ()
    allow_dubbed_content: bool = False

    # Maturity / content rating filters
    max_content_rating: str = "any"  # "any", "G", "PG", "PG-13", "R", "NC-17"
    exclude_kids_content: bool = False
    blocked_content_ratings: tuple[str, ...] = ()

    # Genre, format, and quality filters (community-requested Netflix filters)
    exclude_reality_and_talk: bool = True
    exclude_standup: bool = False
    exclude_documentaries: bool = False
    allowed_genres: tuple[str, ...] = ()
    blocked_genres: tuple[str, ...] = ()
    min_user_rating: float = 0.0
    min_release_year: int = 0

    @classmethod
    def permissive_default(cls) -> LibraryFilterPolicy:
        """Return a filter policy that allows all languages and genres (used when filtering is disabled)."""
        return cls(
            include_movies=True,
            include_tv_shows=True,
            allowed_original_languages=("*",),
            blocked_original_languages=(),
            allow_dubbed_content=True,
            max_content_rating="any",
            exclude_kids_content=False,
            blocked_content_ratings=(),
            exclude_reality_and_talk=False,
            exclude_standup=False,
            exclude_documentaries=False,
            allowed_genres=(),
            blocked_genres=(),
            min_user_rating=0.0,
            min_release_year=0,
        )

    @classmethod
    def from_kodi_settings(cls, settings_dict: dict[str, object]) -> LibraryFilterPolicy:
        """Construct a `LibraryFilterPolicy` from Kodi settings values."""
        allowed_langs = _parse_csv_tokens(str(settings_dict.get("allowed_original_languages", "en")))
        blocked_langs = _parse_csv_tokens(str(settings_dict.get("blocked_original_languages", "")))
        blocked_ratings = tuple(
            r.upper() for r in _parse_csv_tokens(str(settings_dict.get("blocked_content_ratings", "")))
        )
        allowed_genres = _parse_csv_tokens(str(settings_dict.get("allowed_genres", "")))
        blocked_genres = _parse_csv_tokens(str(settings_dict.get("blocked_genres", "")))

        return cls(
            include_movies=bool(settings_dict.get("include_movies", True)),
            include_tv_shows=bool(settings_dict.get("include_tv_shows", True)),
            allowed_original_languages=allowed_langs or ("*",),
            blocked_original_languages=blocked_langs,
            allow_dubbed_content=bool(settings_dict.get("allow_dubbed_content", False)),
            max_content_rating=str(settings_dict.get("max_content_rating", "any")).strip(),
            exclude_kids_content=bool(settings_dict.get("exclude_kids_content", False)),
            blocked_content_ratings=blocked_ratings,
            exclude_reality_and_talk=bool(settings_dict.get("exclude_reality_and_talk", True)),
            exclude_standup=bool(settings_dict.get("exclude_standup", False)),
            exclude_documentaries=bool(settings_dict.get("exclude_documentaries", False)),
            allowed_genres=allowed_genres,
            blocked_genres=blocked_genres,
            min_user_rating=float(settings_dict.get("min_user_rating", 0.0) or 0.0),
            min_release_year=int(settings_dict.get("min_release_year", 0) or 0),
        )


@dataclass
class FilterDecision:
    """Result of evaluating a `Title` against a `LibraryFilterPolicy`."""

    allowed: bool
    reason: Optional[str] = None


def evaluate_title_filter(title: Title, policy: LibraryFilterPolicy) -> FilterDecision:
    """Evaluate whether `title` passes all configured language, rating, genre, and quality filters."""
    # 1. Media type filter
    if title.media_type == MediaType.MOVIE and not policy.include_movies:
        return FilterDecision(False, "media_type_movies_disabled")
    if title.media_type in (MediaType.SHOW, MediaType.EPISODE) and not policy.include_tv_shows:
        return FilterDecision(False, "media_type_tv_disabled")

    # 2. Original language & dubbed content filter
    orig_lang = (title.original_language or "en").strip().lower()
    blocked_langs = {lang.lower() for lang in policy.blocked_original_languages}
    if orig_lang in blocked_langs:
        return FilterDecision(False, f"blocked_original_language:{orig_lang}")

    allowed_langs = {lang.lower() for lang in policy.allowed_original_languages if lang}
    wildcard_lang = "*" in allowed_langs or "any" in allowed_langs or "all" in allowed_langs

    if not policy.allow_dubbed_content:
        if title.is_dubbed:
            return FilterDecision(False, f"dubbed_content_excluded:{orig_lang}")
        if not wildcard_lang and allowed_langs and orig_lang not in allowed_langs:
            return FilterDecision(False, f"non_native_language:{orig_lang}")
    else:
        # Even if dubbed content is allowed, if the user set an explicit language whitelist
        # and the title is neither in `allowed_langs` nor marked `is_dubbed`, enforce whitelist
        if not wildcard_lang and allowed_langs and orig_lang not in allowed_langs and not title.is_dubbed:
            return FilterDecision(False, f"original_language_not_in_whitelist:{orig_lang}")

    # 3. Content / maturity rating filter
    rating_upper = (title.content_rating or "").strip().upper()
    if rating_upper:
        if policy.exclude_kids_content and rating_upper in KIDS_RATINGS:
            return FilterDecision(False, f"kids_rating_excluded:{rating_upper}")

        blocked_ratings = {r.strip().upper() for r in policy.blocked_content_ratings}
        if rating_upper in blocked_ratings:
            return FilterDecision(False, f"blocked_content_rating:{rating_upper}")

        max_cap = (policy.max_content_rating or "any").strip().upper()
        if max_cap and max_cap != "ANY" and max_cap in MATURITY_RANK and rating_upper in MATURITY_RANK:
            if MATURITY_RANK[rating_upper] > MATURITY_RANK[max_cap]:
                return FilterDecision(
                    False, f"exceeds_max_content_rating:{rating_upper}>{max_cap}"
                )

    # 4. Genre & format filters
    title_genres = {g.strip().lower() for g in (title.genres or ()) if g.strip()}
    if title_genres:
        if policy.exclude_reality_and_talk and (title_genres & REALITY_AND_TALK_GENRES):
            return FilterDecision(False, "reality_or_talk_excluded")
        if policy.exclude_standup and (title_genres & STANDUP_GENRES):
            return FilterDecision(False, "standup_excluded")
        if policy.exclude_documentaries and (title_genres & DOCUMENTARY_GENRES):
            return FilterDecision(False, "documentary_excluded")

        blocked_g = {g.strip().lower() for g in policy.blocked_genres if g.strip()}
        if title_genres & blocked_g:
            return FilterDecision(False, f"blocked_genre:{sorted(title_genres & blocked_g)[0]}")

        allowed_g = {g.strip().lower() for g in policy.allowed_genres if g.strip()}
        if allowed_g and not (title_genres & allowed_g):
            return FilterDecision(False, "genre_not_in_whitelist")

    # 5. Minimum user/IMDb rating score filter
    if policy.min_user_rating > 0.0 and title.user_rating is not None:
        if title.user_rating < policy.min_user_rating:
            return FilterDecision(
                False, f"below_min_user_rating:{title.user_rating}<{policy.min_user_rating}"
            )

    # 6. Minimum release year filter
    if policy.min_release_year > 0 and title.year is not None:
        if title.year < policy.min_release_year:
            return FilterDecision(
                False, f"below_min_release_year:{title.year}<{policy.min_release_year}"
            )

    return FilterDecision(True, None)
