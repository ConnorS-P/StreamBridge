"""Isolated Kodi Python API Bridge (`xbmc`, `xbmcgui`, `xbmcplugin`, `xbmcaddon`, `xbmcvfs`).

Isolates all Kodi C-extension calls in one module (addressing the 4th highest churn area
identified in `.reference/CHURN_ANALYSIS.md`: Kodi 21/22 `ListItem.setInfo` deprecations
and `InfoTagVideo` changes) while providing a deterministic in-memory fallback when run
outside Kodi (e.g. in `pytest` or CI).

Also integrates with the broader Kodi ecosystem:
- Sets `InfoTagVideo.setUniqueIDs({'tmdb': ..., 'imdb': ..., 'netflix': ..., 'streambridge': ...}, defaultuniqueid='tmdb')`
- Publishes `Window(10000)` properties (`tmdb_id`, `imdb_id`, `VideoPlayer.TMDB`, `streambridge.provider`,
  `streambridge.title_id`) so **Trakt (`script.trakt`)**, **TMDb Helper (`plugin.video.themoviedb.helper`)**,
  **Up Next**, and modern skins recognize active StreamBridge items.
- Supports optional `script.module.inputstreamhelper` readiness checks when an authorized
  DASH/HLS manifest is resolved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .filters import LibraryFilterPolicy
from .models import LaunchDescriptor, MediaType, Offer, PlaybackState, Title
from .security import redact_secrets

try:
    import xbmc  # type: ignore[import-not-found]
    import xbmcaddon  # type: ignore[import-not-found]
    import xbmcgui  # type: ignore[import-not-found]
    import xbmcplugin  # type: ignore[import-not-found]
    import xbmcvfs  # type: ignore[import-not-found]

    IN_KODI = True
except ImportError:
    xbmc = None
    xbmcaddon = None
    xbmcgui = None
    xbmcplugin = None
    xbmcvfs = None
    IN_KODI = False


class KodiBridge:
    """Unified interface for Kodi UI, InfoTagVideo, Window(10000) properties, and audio device control."""

    def __init__(self, addon_id: str = "plugin.video.streambridge") -> None:
        self.addon_id = addon_id
        self.window_properties: dict[str, str] = {}
        self.notifications: list[tuple[str, str]] = []
        self.executed_builtins: list[str] = []
        self.audio_suspended: bool = False

    def log(self, message: str, level: str = "info") -> None:
        sanitized = f"[StreamBridge] {redact_secrets(message)}"
        if IN_KODI and xbmc is not None:
            lvl_map = {
                "debug": xbmc.LOGDEBUG,
                "info": xbmc.LOGINFO,
                "warning": xbmc.LOGWARNING,
                "error": xbmc.LOGERROR,
            }
            xbmc.log(sanitized, lvl_map.get(level.lower(), xbmc.LOGINFO))

    def notify(self, heading: str, message: str, error: bool = False) -> None:
        sanitized_msg = redact_secrets(message)
        self.notifications.append((heading, sanitized_msg))
        if IN_KODI and xbmcgui is not None:
            icon = xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO
            xbmcgui.Dialog().notification(heading, sanitized_msg, icon, 4000)

    def has_addon(self, addon_id: str) -> bool:
        if IN_KODI and xbmc is not None:
            return bool(xbmc.getCondVisibility(f"System.HasAddon({addon_id})"))
        return False

    def execute_builtin(self, command: str) -> None:
        self.executed_builtins.append(command)
        if IN_KODI and xbmc is not None:
            xbmc.executebuiltin(command)

    def set_ecosystem_window_properties(self, descriptor: LaunchDescriptor) -> None:
        """Publish active playback metadata to `Window(10000)` for Trakt, TMDb Helper, and skins."""
        props = {
            "streambridge.active": "true",
            "streambridge.session_id": descriptor.session_id,
            "streambridge.title_id": descriptor.title_id,
            "streambridge.provider": descriptor.provider,
            "tmdb_id": descriptor.tmdb_id or "",
            "imdb_id": descriptor.imdb_id or "",
            "VideoPlayer.TMDB": descriptor.tmdb_id or "",
            "VideoPlayer.IMDBNumber": descriptor.imdb_id or "",
        }
        self.window_properties.update(props)
        if IN_KODI and xbmcgui is not None:
            win = xbmcgui.Window(10000)
            for key, val in props.items():
                win.setProperty(key, val)

    def clear_ecosystem_window_properties(self) -> None:
        """Clear `Window(10000)` playback properties when session finishes."""
        keys = (
            "streambridge.active",
            "streambridge.session_id",
            "streambridge.title_id",
            "streambridge.provider",
            "tmdb_id",
            "imdb_id",
            "VideoPlayer.TMDB",
            "VideoPlayer.IMDBNumber",
        )
        for key in keys:
            self.window_properties.pop(key, None)
        if IN_KODI and xbmcgui is not None:
            win = xbmcgui.Window(10000)
            for key in keys:
                win.clearProperty(key)

    def release_kodi_audio_device(self) -> None:
        """Stop any active Kodi playback and suspend Kodi's audio sink so WASAPI/AVR is free."""
        self.audio_suspended = True
        if IN_KODI and xbmc is not None:
            if xbmc.Player().isPlaying():
                xbmc.Player().stop()
            xbmc.audioSuspend()

    def restore_kodi_audio_and_focus(self) -> None:
        """Resume Kodi's audio sink after external or companion playback exits."""
        self.audio_suspended = False
        if IN_KODI and xbmc is not None:
            xbmc.audioResume()

    def build_listitem_metadata(
        self,
        title: Title,
        offer: Optional[Offer] = None,
        playback_state: Optional[PlaybackState] = None,
    ) -> dict[str, Any]:
        """Construct the Kodi 21+ `InfoTagVideo` metadata dictionary (and apply to `xbmcgui.ListItem` if in Kodi)."""
        unique_ids: dict[str, str] = {"streambridge": title.title_id}
        if title.tmdb_id:
            unique_ids["tmdb"] = str(title.tmdb_id)
        if title.imdb_id:
            unique_ids["imdb"] = str(title.imdb_id)
        if offer and offer.provider_content_id:
            unique_ids[offer.provider.lower()] = str(offer.provider_content_id)

        default_uid = "tmdb" if "tmdb" in unique_ids else ("imdb" if "imdb" in unique_ids else "streambridge")

        meta = {
            "title": title.title,
            "mediatype": title.media_type.value,
            "year": title.year,
            "plot": title.plot,
            "showtitle": title.show_title,
            "season": title.season,
            "episode": title.episode,
            "premiered": title.premiered,
            "unique_ids": unique_ids,
            "default_unique_id": default_uid,
            "resume_position": playback_state.position_seconds if playback_state else 0.0,
            "resume_duration": playback_state.duration_seconds if playback_state else 0.0,
            "play_count": playback_state.play_count if playback_state else 0,
        }

        if IN_KODI and xbmcgui is not None:
            li = xbmcgui.ListItem(label=title.title, offscreen=True)
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType(title.media_type.value)
            info_tag.setTitle(title.title)
            if title.year:
                info_tag.setYear(int(title.year))
            if title.plot:
                info_tag.setPlot(title.plot)
            if title.media_type == MediaType.EPISODE:
                if title.show_title:
                    info_tag.setTvShowTitle(title.show_title)
                if title.season is not None:
                    info_tag.setSeason(int(title.season))
                if title.episode is not None:
                    info_tag.setEpisode(int(title.episode))
            info_tag.setUniqueIDs(unique_ids, defaultuniqueid=default_uid)
            if playback_state:
                info_tag.setPlayCount(int(playback_state.play_count))
                if playback_state.position_seconds > 0 and not playback_state.completed:
                    info_tag.setResumePoint(
                        float(playback_state.position_seconds),
                        float(playback_state.duration_seconds or 0.0),
                    )
            meta["listitem"] = li

        return meta

    def check_inputstream_adaptive(self, drm_scheme: str = "com.widevine.alpha") -> bool:
        """Use `script.module.inputstreamhelper` when an adapter resolves an InputStream Adaptive stream."""
        try:
            import inputstreamhelper  # type: ignore[import-not-found]

            is_helper = inputstreamhelper.Helper("mpd", drm=drm_scheme)
            return bool(is_helper.check_inputstream())
        except ImportError:
            return False

    def get_profile_dir(self, fallback_dir: Optional[Path] = None) -> Path:
        if IN_KODI and xbmcvfs is not None and xbmcaddon is not None:
            raw_path = xbmcvfs.translatePath(xbmcaddon.Addon(self.addon_id).getAddonInfo("profile"))
            p = Path(raw_path)
            p.mkdir(parents=True, exist_ok=True)
            return p
        target = fallback_dir or (Path.home() / ".streambridge")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def load_filter_policy(self) -> LibraryFilterPolicy:
        """Load the user's configured `LibraryFilterPolicy` from Kodi settings when in Kodi."""
        if not (IN_KODI and xbmcaddon is not None):
            return LibraryFilterPolicy.permissive_default()
        try:
            addon = xbmcaddon.Addon("plugin.video.streambridge")
            settings_dict = {
                "include_movies": addon.getSettingBool("include_movies"),
                "include_tv_shows": addon.getSettingBool("include_tv_shows"),
                "allowed_original_languages": addon.getSettingString("allowed_original_languages"),
                "blocked_original_languages": addon.getSettingString("blocked_original_languages"),
                "allow_dubbed_content": addon.getSettingBool("allow_dubbed_content"),
                "max_content_rating": addon.getSettingString("max_content_rating"),
                "exclude_kids_content": addon.getSettingBool("exclude_kids_content"),
                "blocked_content_ratings": addon.getSettingString("blocked_content_ratings"),
                "exclude_reality_and_talk": addon.getSettingBool("exclude_reality_and_talk"),
                "exclude_standup": addon.getSettingBool("exclude_standup"),
                "exclude_documentaries": addon.getSettingBool("exclude_documentaries"),
                "blocked_genres": addon.getSettingString("blocked_genres"),
                "min_user_rating": addon.getSettingNumber("min_user_rating"),
                "min_release_year": addon.getSettingInt("min_release_year"),
            }
            return LibraryFilterPolicy.from_kodi_settings(settings_dict)
        except Exception:
            return LibraryFilterPolicy.permissive_default()
