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
from typing import Any

from .filters import (
    GENRE_TOGGLE_MAP,
    LANGUAGE_TOGGLE_MAP,
    RATING_TOGGLE_MAP,
    LibraryFilterPolicy,
)
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

    def show_ok_dialog(self, heading: str, message: str) -> None:
        """Display a modal Kodi OK dialog (used for incompatible display mode errors)."""
        sanitized_msg = redact_secrets(message)
        self.notifications.append((heading, sanitized_msg))
        if IN_KODI and xbmcgui is not None:
            xbmcgui.Dialog().ok(heading, sanitized_msg)

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
        offer: Offer | None = None,
        playback_state: PlaybackState | None = None,
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

    def get_profile_dir(self, fallback_dir: Path | None = None) -> Path:
        if IN_KODI and xbmcvfs is not None and xbmcaddon is not None:
            raw_path = xbmcvfs.translatePath(xbmcaddon.Addon(self.addon_id).getAddonInfo("profile"))
            p = Path(raw_path)
            p.mkdir(parents=True, exist_ok=True)
            return p
        target = fallback_dir or (Path.home() / ".streambridge")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def get_export_root(self, fallback_dir: Path | None = None) -> Path:
        """Resolve and pre-create the configured `export_root` and provider Movies/TV folders."""
        target: Path | None = None
        if IN_KODI and xbmcvfs is not None and xbmcaddon is not None:
            try:
                addon = xbmcaddon.Addon("plugin.video.streambridge")
                configured = addon.getSettingString("export_root") or "special://profile/addon_data/plugin.video.streambridge/library"
                translated = xbmcvfs.translatePath(configured)
                target = Path(translated)
            except Exception as exc:
                self.log(f"Failed to resolve export_root setting: {exc}", level="warning")

        if target is None:
            target = fallback_dir or (self.get_profile_dir() / "library")

        # Always pre-create the Movies and TV directories so they exist before the user browses for sources
        (target / "Streaming" / "Netflix" / "Movies").mkdir(parents=True, exist_ok=True)
        (target / "Streaming" / "Netflix" / "TV").mkdir(parents=True, exist_ok=True)
        self.ensure_browseable_source_entry(target / "Streaming", sources_xml_path=None)
        return target

    @staticmethod
    def _normalize_source_path(p: str) -> str:
        cleaned = (p or "").strip().replace("/", "\\").rstrip("\\")
        return cleaned.lower()

    def _upsert_xml_source_idempotent(
        self,
        xml_path: Path,
        section_tag: str,
        source_name: str,
        target_dir: Path,
    ) -> bool:
        """Idempotently ensure `(source_name, target_dir)` exists under `<section_tag>` in `sources.xml`.

        Returns `True` if `sources.xml` was modified on disk, or `False` if already up-to-date.
        Matches existing `<source>` entries by either `<name>` (case-insensitive) OR normalized `<path>`,
        and removes any accidental duplicates.
        """
        import os
        import xml.etree.ElementTree as ET

        if not xml_path.exists():
            root = ET.Element("sources")
            for sec in ("programs", "video", "music", "pictures", "files", "games"):
                sec_el = ET.SubElement(root, sec)
                ET.SubElement(sec_el, "default", attrib={"pathversion": "1"})
            changed = True
        else:
            try:
                tree = ET.parse(xml_path)
                root = tree.getroot()
                changed = False
            except Exception as exc:
                self.log(f"Could not parse sources.xml: {exc}", level="warning")
                return False

        found_sec = root.find(section_tag)
        if found_sec is None:
            sec_el = ET.SubElement(root, section_tag)
            ET.SubElement(sec_el, "default", attrib={"pathversion": "1"})
            changed = True
        else:
            sec_el = found_sec

        target_path_str = str(target_dir.resolve())
        if not target_path_str.endswith(("\\", "/")):
            target_path_str += "\\" if "\\" in target_path_str else "/"
        norm_target = self._normalize_source_path(target_path_str)
        norm_name = source_name.strip().lower()

        matched_sources: list[ET.Element] = []
        for src in list(sec_el.findall("source")):
            name_el = src.find("name")
            path_el = src.find("path")
            existing_name = (name_el.text or "").strip().lower() if name_el is not None else ""
            existing_paths = [
                self._normalize_source_path(p.text or "")
                for p in src.findall("path")
                if p.text
            ]
            if existing_name == norm_name or norm_target in existing_paths:
                matched_sources.append(src)

        if matched_sources:
            primary = matched_sources[0]
            # Remove any duplicate <source> entries for the same name/path
            for dup in matched_sources[1:]:
                sec_el.remove(dup)
                changed = True

            name_el = primary.find("name")
            path_el = primary.find("path")
            if name_el is not None and name_el.text != source_name:
                name_el.text = source_name
                changed = True
            if path_el is not None and path_el.text != target_path_str:
                path_el.text = target_path_str
                changed = True
        else:
            new_src = ET.SubElement(sec_el, "source")
            name_el = ET.SubElement(new_src, "name")
            name_el.text = source_name
            path_el = ET.SubElement(new_src, "path", attrib={"pathversion": "1"})
            path_el.text = target_path_str
            allow_el = ET.SubElement(new_src, "allowsharing")
            allow_el.text = "true"
            changed = True

        if not changed:
            return False

        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = xml_path.with_name(f".{xml_path.name}.{os.getpid()}.tmp")
        xml_content = ET.tostring(root, encoding="unicode") + "\n"
        tmp_path.write_text(xml_content, encoding="utf-8")
        os.replace(tmp_path, xml_path)
        return True

    def ensure_browseable_source_entry(
        self,
        streaming_dir: Path,
        sources_xml_path: Path | None = None,
        source_name: str = "StreamBridge Library",
    ) -> bool:
        """Idempotently register `StreamBridge Library` under `<files>` in Kodi's `sources.xml`."""
        xml_path = sources_xml_path
        if xml_path is None:
            if IN_KODI and xbmcvfs is not None:
                xml_path = Path(xbmcvfs.translatePath("special://profile/sources.xml"))
            else:
                return False
        streaming_dir.mkdir(parents=True, exist_ok=True)
        self._upsert_xml_source_idempotent(xml_path, "files", source_name, streaming_dir)
        return True

    def ensure_video_source_entry(
        self,
        folder_path: Path,
        source_name: str,
        sources_xml_path: Path | None = None,
    ) -> bool:
        """Idempotently register a Movie or TV directory under `<video>` in Kodi's `sources.xml`.

        Returns `True` if `sources.xml` was modified, or `False` if the source was already present.
        """
        xml_path = sources_xml_path
        if xml_path is None:
            if IN_KODI and xbmcvfs is not None:
                xml_path = Path(xbmcvfs.translatePath("special://profile/sources.xml"))
            else:
                return False
        folder_path.mkdir(parents=True, exist_ok=True)
        return self._upsert_xml_source_idempotent(xml_path, "video", source_name, folder_path)

    def load_filter_policy(self) -> LibraryFilterPolicy:
        """Load the user's configured radio-button `LibraryFilterPolicy` from Kodi settings."""
        if not (IN_KODI and xbmcaddon is not None):
            return LibraryFilterPolicy.permissive_default()
        try:
            addon = xbmcaddon.Addon("plugin.video.streambridge")
            settings_dict: dict[str, Any] = {
                "include_movies": addon.getSettingBool("include_movies"),
                "include_tv_shows": addon.getSettingBool("include_tv_shows"),
                "allow_dubbed_content": addon.getSettingBool("allow_dubbed_content"),
                "filter_by_language": addon.getSettingBool("filter_by_language"),
                "lang_other": addon.getSettingBool("lang_other"),
                "filter_by_rating": addon.getSettingBool("filter_by_rating"),
                "rating_unrated": addon.getSettingBool("rating_unrated"),
                "min_user_rating": addon.getSettingNumber("min_user_rating"),
                "min_release_year": addon.getSettingInt("min_release_year"),
                "mylist_only": addon.getSettingBool("mylist_only"),
                "mylist_bypass_filters": addon.getSettingBool("mylist_bypass_filters"),
            }
            for lang_key in LANGUAGE_TOGGLE_MAP:
                settings_dict[lang_key] = addon.getSettingBool(lang_key)
            for rating_key in RATING_TOGGLE_MAP:
                settings_dict[rating_key] = addon.getSettingBool(rating_key)
            for genre_key in GENRE_TOGGLE_MAP:
                settings_dict[genre_key] = addon.getSettingBool(genre_key)
            return LibraryFilterPolicy.from_kodi_settings(settings_dict)
        except Exception:
            return LibraryFilterPolicy.permissive_default()
