"""Library Content Filtering Engine for StreamBridge.

Supports both programmatic whitelist/blacklist rules and Kodi's radio-button toggle
submenu settings (Languages & Dubbing, Content Ratings, and Genres & Quality).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

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

# Mapping from radio-button language setting IDs to ISO-639-1 language codes
LANGUAGE_TOGGLE_MAP: dict[str, tuple[str, ...]] = {
    "lang_en": ("en",),
    "lang_ja": ("ja",),
    "lang_ko": ("ko",),
    "lang_es": ("es",),
    "lang_fr": ("fr",),
    "lang_de": ("de",),
    "lang_it": ("it",),
    "lang_pt": ("pt",),
    "lang_zh": ("zh", "cn", "yue"),
    "lang_hi": ("hi", "ta", "te", "ml", "bn", "pa", "mr"),
    "lang_tr": ("tr",),
    "lang_pl": ("pl",),
    "lang_scand": ("sv", "da", "no", "nb", "nn", "fi", "is"),
}

ALL_KNOWN_LANGUAGE_CODES: frozenset[str] = frozenset(
    code for codes in LANGUAGE_TOGGLE_MAP.values() for code in codes
)

# Mapping from radio-button rating setting IDs to rating strings
RATING_TOGGLE_MAP: dict[str, tuple[str, ...]] = {
    "rating_tv_y": ("TV-Y",),
    "rating_tv_y7": ("TV-Y7", "TV-Y7-FV"),
    "rating_g": ("G", "TV-G"),
    "rating_pg": ("PG", "TV-PG"),
    "rating_pg13": ("PG-13", "TV-14"),
    "rating_r": ("R", "TV-MA"),
    "rating_nc17": ("NC-17",),
}

# Mapping from radio-button genre setting IDs to canonical genre names
GENRE_TOGGLE_MAP: dict[str, tuple[str, ...]] = {
    "genre_action": ("action", "adventure", "action & adventure"),
    "genre_animation": ("animation", "animated"),
    "genre_anime": ("anime",),
    "genre_comedy": ("comedy",),
    "genre_crime": ("crime",),
    "genre_documentary": ("documentary", "docuseries"),
    "genre_drama": ("drama",),
    "genre_family": ("family", "kids", "children"),
    "genre_fantasy_scifi": ("fantasy", "sci-fi", "science fiction", "sci-fi & fantasy"),
    "genre_history": ("history", "biography"),
    "genre_horror": ("horror",),
    "genre_musical": ("music", "musical"),
    "genre_mystery_thriller": ("mystery", "thriller"),
    "genre_reality": ("reality", "reality-tv", "reality tv"),
    "genre_romance": ("romance",),
    "genre_soap": ("soap", "soap opera"),
    "genre_sports": ("sport", "sports"),
    "genre_standup": ("stand-up", "standup", "stand-up comedy"),
    "genre_talk_game": ("talk show", "talk-show", "game show", "game-show"),
    "genre_war": ("war", "war & politics"),
    "genre_western": ("western",),
}


@dataclass
class LibraryFilterPolicy:
    """User-configurable filter rules applied before library projection."""

    # Media type toggles
    include_movies: bool = True
    include_tv_shows: bool = True

    # Language & dubbing filters
    filter_by_language: bool = True
    allowed_original_languages: tuple[str, ...] = ("en",)
    blocked_original_languages: tuple[str, ...] = ()
    allow_other_languages: bool = False
    allow_dubbed_content: bool = False

    # Maturity / content rating filters
    filter_by_rating: bool = True
    max_content_rating: str = "any"
    exclude_kids_content: bool = False
    blocked_content_ratings: tuple[str, ...] = ()
    allow_unrated: bool = True

    # Genre, format, and quality filters
    exclude_reality_and_talk: bool = True
    exclude_standup: bool = False
    exclude_documentaries: bool = False
    allowed_genres: tuple[str, ...] = ()
    blocked_genres: tuple[str, ...] = ()
    min_user_rating: float = 0.0
    min_release_year: int = 0

    @classmethod
    def permissive_default(cls) -> LibraryFilterPolicy:
        """Return a filter policy that allows all languages, ratings, and genres."""
        return cls(
            include_movies=True,
            include_tv_shows=True,
            filter_by_language=False,
            allowed_original_languages=("*",),
            blocked_original_languages=(),
            allow_other_languages=True,
            allow_dubbed_content=True,
            filter_by_rating=False,
            max_content_rating="any",
            exclude_kids_content=False,
            blocked_content_ratings=(),
            allow_unrated=True,
            exclude_reality_and_talk=False,
            exclude_standup=False,
            exclude_documentaries=False,
            allowed_genres=(),
            blocked_genres=(),
            min_user_rating=0.0,
            min_release_year=0,
        )

    @classmethod
    def from_kodi_settings(cls, settings_dict: dict[str, Any]) -> LibraryFilterPolicy:
        """Construct a `LibraryFilterPolicy` from Kodi radio-button toggle settings."""
        filter_by_language = bool(settings_dict.get("filter_by_language", True))
        allowed_langs: list[str] = []
        blocked_langs: list[str] = []
        for setting_id, iso_codes in LANGUAGE_TOGGLE_MAP.items():
            default_on = setting_id == "lang_en"
            is_enabled = bool(settings_dict.get(setting_id, default_on))
            if is_enabled:
                allowed_langs.extend(iso_codes)
            else:
                blocked_langs.extend(iso_codes)

        allow_other_langs = bool(settings_dict.get("lang_other", False))

        filter_by_rating = bool(settings_dict.get("filter_by_rating", True))
        blocked_ratings: list[str] = []
        if filter_by_rating:
            for setting_id, ratings in RATING_TOGGLE_MAP.items():
                default_on = setting_id not in ("rating_tv_y", "rating_tv_y7")
                is_enabled = bool(settings_dict.get(setting_id, default_on))
                if not is_enabled:
                    blocked_ratings.extend(ratings)
        allow_unrated = bool(settings_dict.get("rating_unrated", True))

        blocked_genres: list[str] = []
        for setting_id, genre_names in GENRE_TOGGLE_MAP.items():
            default_on = setting_id not in (
                "genre_reality",
                "genre_soap",
                "genre_standup",
                "genre_talk_game",
            )
            is_enabled = bool(settings_dict.get(setting_id, default_on))
            if not is_enabled:
                blocked_genres.extend(genre_names)

        return cls(
            include_movies=bool(settings_dict.get("include_movies", True)),
            include_tv_shows=bool(settings_dict.get("include_tv_shows", True)),
            filter_by_language=filter_by_language,
            allowed_original_languages=tuple(allowed_langs) if filter_by_language else ("*",),
            blocked_original_languages=tuple(blocked_langs) if filter_by_language else (),
            allow_other_languages=allow_other_langs,
            allow_dubbed_content=bool(settings_dict.get("allow_dubbed_content", False)),
            filter_by_rating=filter_by_rating,
            max_content_rating="any",
            exclude_kids_content=False,
            blocked_content_ratings=tuple(blocked_ratings),
            allow_unrated=allow_unrated,
            exclude_reality_and_talk=not bool(settings_dict.get("genre_reality", False))
            and not bool(settings_dict.get("genre_talk_game", False)),
            exclude_standup=not bool(settings_dict.get("genre_standup", False)),
            exclude_documentaries=not bool(settings_dict.get("genre_documentary", True)),
            allowed_genres=(),
            blocked_genres=tuple(blocked_genres),
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
    if not policy.allow_dubbed_content and title.is_dubbed:
        return FilterDecision(False, f"dubbed_content_excluded:{orig_lang}")

    if policy.filter_by_language:
        blocked_langs = {lang.lower() for lang in policy.blocked_original_languages}
        if orig_lang in blocked_langs:
            return FilterDecision(False, f"blocked_original_language:{orig_lang}")

        allowed_langs = {lang.lower() for lang in policy.allowed_original_languages if lang}
        wildcard_lang = "*" in allowed_langs or "any" in allowed_langs or "all" in allowed_langs

        if not wildcard_lang:
            is_in_allowed = orig_lang in allowed_langs or (
                policy.allow_other_languages and orig_lang not in ALL_KNOWN_LANGUAGE_CODES
            )
            if not is_in_allowed:
                if not (policy.allow_dubbed_content and title.is_dubbed):
                    return FilterDecision(False, f"non_native_language:{orig_lang}")

    # 3. Content / maturity rating filter
    rating_upper = (title.content_rating or "").strip().upper()
    if policy.filter_by_rating:
        if not rating_upper:
            if not policy.allow_unrated:
                return FilterDecision(False, "unrated_excluded")
        else:
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
