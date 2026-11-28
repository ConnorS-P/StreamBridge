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
        self.playing_video: bool = False
        self.sync_schedule_overrides: dict[str, Any] = {}

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
            dur_sec = (
                int(playback_state.duration_seconds)
                if playback_state and playback_state.duration_seconds > 60
                else title.runtime_seconds
            )
            if dur_sec and dur_sec > 0:
                info_tag.setDuration(int(dur_sec))
                meta["duration"] = int(dur_sec)
            if hasattr(xbmc, "VideoStreamDetail") and hasattr(info_tag, "addVideoStream"):
                try:
                    info_tag.addVideoStream(xbmc.VideoStreamDetail(1920, 1080, 1.7778, int(dur_sec or 0), "h264", ""))
                except Exception as exc:
                    self.log(f"addVideoStream skipped: {exc}", level="debug")
            if hasattr(xbmc, "AudioStreamDetail") and hasattr(info_tag, "addAudioStream"):
                try:
                    info_tag.addAudioStream(xbmc.AudioStreamDetail(6, "eac3", "eng"))
                except Exception as exc:
                    self.log(f"addAudioStream skipped: {exc}", level="debug")
            if playback_state:
                if hasattr(info_tag, "setPlaycount"):
                    info_tag.setPlaycount(int(playback_state.play_count))
                elif hasattr(info_tag, "setPlayCount"):
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

    def build_source_setup_instructions(
        self,
        source_name: str,
        media_type: str = "movies",
    ) -> tuple[str, str]:
        """Build the user-facing step-by-step scraper configuration instructions for a video source."""
        heading = f"StreamBridge — Setup Guide: {source_name}"
        if media_type.lower() in ("tvshows", "tv", "show", "episode"):
            message = (
                "When you click OK, StreamBridge will automatically fill in the folder path and name "
                "in Kodi's 'Add video source' dialog and open the 'Set content' window.\n\n"
                "In 'Set content', configure these settings:\n"
                "• This directory contains: TV shows\n"
                "• Information provider: TMDb TV Shows\n"
                "• Selected folder contains a single TV show: OFF (each show has its own folder)\n"
                "Then click OK, and select Yes to scan items into your library."
            )
        else:
            message = (
                "When you click OK, StreamBridge will automatically fill in the folder path and name "
                "in Kodi's 'Add video source' dialog and open the 'Set content' window.\n\n"
                "In 'Set content', configure these settings:\n"
                "• This directory contains: Movies\n"
                "• Information provider: The Movie Database Python\n"
                "• Movies are in separate folders that match the movie title: ON\n"
                "• Scan recursively: ON\n"
                "Then click OK, and select Yes to scan items into your library."
            )
        return heading, message

    def add_video_source_via_native_dialog(
        self,
        folder_path: Path,
        source_name: str,
        rpc_client: Any,
        media_type: str = "movies",
    ) -> bool:
        """Drive Kodi's native `CGUIDialogMediaSource` (`Window 10129`) so `CMediaSourceSettings::AddShare`
        registers the source in C++ memory, saves `sources.xml`, and opens the `Set content` scraper dialog.
        """
        if not (IN_KODI and xbmc is not None and xbmcgui is not None):
            return self.ensure_video_source_entry(folder_path, source_name)

        folder_path.mkdir(parents=True, exist_ok=True)
        target_path_str = str(folder_path.resolve())
        if not target_path_str.endswith(("\\", "/")):
            target_path_str += "\\" if "\\" in target_path_str else "/"

        # Idempotency check against Kodi's live in-memory CMediaSourceSettings via Files.GetSources
        if rpc_client is not None and rpc_client.is_video_source_loaded_in_kodi(folder_path):
            self.show_ok_dialog(
                f"StreamBridge — {source_name}",
                f"'{source_name}' is already registered in your Kodi Video Sources.\n\n"
                f"To review or update its scraper settings, highlight '{source_name}' under "
                "Videos -> Files, open the Context Menu (C), and select 'Change content'.",
            )
            return False

        # Show modal instructions explaining what will happen next and how to configure 'Set content'
        heading, instructions = self.build_source_setup_instructions(source_name, media_type=media_type)
        self.show_ok_dialog(heading, instructions)

        # 1. Navigate to `sources://video/` inside MyVideoNav (Window 10025)
        if xbmcgui.getCurrentWindowId() == 10025:
            xbmc.executebuiltin("Container.Update(sources://video/,replace)")
        else:
            xbmc.executebuiltin("ActivateWindow(videos,sources://video/,return)")

        # Wait for `sources://video/` container to finish loading
        for _ in range(40):
            xbmc.sleep(100)
            folder = xbmc.getInfoLabel("Container.FolderPath")
            updating = xbmc.getCondVisibility("Container.IsUpdating")
            if folder == "sources://video/" and not updating:
                break

        # 2. Move focus in the `sources://video/` list to the 'Add videos...' item (`ListItem.FolderPath == 'add'`)
        win = xbmcgui.Window(10025)
        focus_id = win.getFocusId() or 50
        found_add_item = False
        for _ in range(25):
            cur_path = xbmc.getInfoLabel("ListItem.FolderPath")
            if cur_path == "add":
                found_add_item = True
                break
            xbmc.executebuiltin(f"Control.Move({focus_id},1)")
            xbmc.sleep(60)

        if not found_add_item:
            # Fallback to XML registration if custom skin hid the 'Add videos...' item
            return self.ensure_video_source_entry(folder_path, source_name)

        # 3. Select 'Add videos...' to open `Window 10129` (`DialogMediaSource.xml`)
        xbmc.executebuiltin("Action(Select)")
        for _ in range(30):
            xbmc.sleep(100)
            if xbmc.getCondVisibility("Window.IsActive(mediasource)"):
                break
        else:
            return self.ensure_video_source_entry(folder_path, source_name)

        # 4. Focus Control 10 (Path list) and send Action(Select) (ACTION_SELECT_ITEM = 7)
        #    so CGUIDialogMediaSource::OnMessage opens the VirtualKeyboard (Window 10103)
        xbmc.executebuiltin("Control.SetFocus(10)")
        xbmc.sleep(150)
        xbmc.executebuiltin("Action(Select)")
        for _ in range(30):
            xbmc.sleep(80)
            if xbmc.getCondVisibility("Window.IsActive(virtualkeyboard)"):
                break
        rpc_client.call("Input.SendText", {"text": target_path_str, "done": True})
        xbmc.sleep(250)

        # 5. Focus Control 12 (Source Name edit control) and send Action(Select) -> Input.SendText
        xbmc.executebuiltin("Control.SetFocus(12)")
        xbmc.sleep(150)
        xbmc.executebuiltin("Action(Select)")
        for _ in range(15):
            xbmc.sleep(80)
            if xbmc.getCondVisibility("Window.IsActive(virtualkeyboard)"):
                break
        rpc_client.call("Input.SendText", {"text": source_name, "done": True})
        xbmc.sleep(250)

        # 6. Click Control 18 (OK button on DialogMediaSource.xml)
        #    This executes CMediaSourceSettings::AddShare() in C++ memory, saves sources.xml,
        #    and automatically opens the native 'Set content' (Scraper picker) dialog!
        xbmc.executebuiltin("SendClick(10129,18)")
        return True

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

    def is_playing_video(self) -> bool:
        """Return `True` if Kodi is currently playing video (used to defer background catalog syncs)."""
        if IN_KODI and xbmc is not None:
            try:
                return bool(xbmc.Player().isPlayingVideo() or xbmc.getCondVisibility("Player.HasVideo"))
            except Exception:
                return False
        return self.playing_video

    def load_sync_schedule_settings(self) -> dict[str, Any]:
        """Load startup & periodic catalog sync schedule settings from Kodi add-on settings."""
        defaults: dict[str, Any] = {
            "sync_on_startup": True,
            "periodic_sync_enabled": True,
            "periodic_sync_interval_hours": 6,
            "pause_sync_during_playback": True,
            "region": "US",
        }
        if self.sync_schedule_overrides:
            merged = dict(defaults)
            merged.update(self.sync_schedule_overrides)
            return merged
        if not (IN_KODI and xbmcaddon is not None):
            return defaults
        try:
            addon = xbmcaddon.Addon("plugin.video.streambridge")
            interval_hours = addon.getSettingInt("periodic_sync_interval_hours")
            if interval_hours <= 0:
                interval_hours = 6
            return {
                "sync_on_startup": addon.getSettingBool("sync_on_startup"),
                "periodic_sync_enabled": addon.getSettingBool("periodic_sync_enabled"),
                "periodic_sync_interval_hours": interval_hours,
                "pause_sync_during_playback": addon.getSettingBool("pause_sync_during_playback"),
                "region": addon.getSettingString("region") or "US",
            }
        except Exception:
            return defaults
