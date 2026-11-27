"""Physical .strm and .nfo library projection engine for StreamBridge.

Design rules:
- Generate ordinary .strm files in per-provider Movie and TV directories.
- Each .strm file contains a stable internal plugin URL:
  plugin://plugin.video.streambridge/play/<title_id>
- Optional generated NFOs include an explicit ownership marker
  `<!-- streambridge:owned:v1 -->` and verified unique IDs (TMDB, IMDb, provider).
- Never overwrite user-edited NFOs or delete user-created files.
- Handle missing release years/dates and unaired episodes gracefully.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import time
from typing import Optional
import xml.etree.ElementTree as ET

from .models import MediaType, Offer, Projection, Title

NFO_OWNERSHIP_MARKER = "<!-- streambridge:owned:v1 -->"
STRM_URI_PREFIX = "plugin://plugin.video.streambridge/play/"

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """Sanitize a title or show name for safe cross-platform filesystem paths."""
    cleaned = _INVALID_FILENAME_CHARS.sub("", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).rstrip(". ")
    return cleaned or "Untitled"


def format_provider_dir(provider: str) -> str:
    """Format a provider identifier as a clean folder name (e.g. 'netflix' -> 'Netflix')."""
    mapping = {
        "netflix": "Netflix",
        "prime": "PrimeVideo",
        "disney": "DisneyPlus",
        "max": "Max",
    }
    return mapping.get(provider.lower(), sanitize_filename(provider.title()))


class ProjectionEngine:
    """Manages atomic creation, update, and safe removal of StreamBridge .strm and .nfo files."""

    def __init__(
        self,
        export_root: Path | str,
        write_nfo: bool = True,
        export_unaired: bool = False,
    ) -> None:
        self.export_root = Path(export_root)
        self.write_nfo = write_nfo
        self.export_unaired = export_unaired

    def compute_relative_paths(self, title: Title, provider: str) -> tuple[str, str]:
        """Return `(strm_relative_path, nfo_relative_path)` in POSIX-normalized relative form."""
        provider_dir = format_provider_dir(provider)
        if title.media_type == MediaType.MOVIE:
            safe_title = sanitize_filename(title.title)
            folder_name = f"{safe_title} ({title.year})" if title.year else safe_title
            base = f"Streaming/{provider_dir}/Movies/{folder_name}/{folder_name}"
            return f"{base}.strm", f"{base}.nfo"

        if title.media_type == MediaType.EPISODE:
            safe_show = sanitize_filename(title.show_title or title.title)
            season_num = title.season if title.season is not None else 1
            episode_num = title.episode if title.episode is not None else 1
            season_folder = f"Season {season_num:02d}" if season_num > 0 else "Specials"
            ep_stem = f"{safe_show} S{season_num:02d}E{episode_num:02d}"
            base = f"Streaming/{provider_dir}/TV/{safe_show}/{season_folder}/{ep_stem}"
            return f"{base}.strm", f"{base}.nfo"

        safe_title = sanitize_filename(title.title)
        base = f"Streaming/{provider_dir}/TV/{safe_title}/tvshow"
        return f"{base}.strm", f"{base}.nfo"

    def build_strm_content(self, title: Title) -> str:
        """Return the stable internal plugin URI for a title."""
        return f"{STRM_URI_PREFIX}{title.title_id}\n"

    def build_nfo_content(self, title: Title, offer: Optional[Offer] = None) -> str:
        """Build a minimal, standards-compliant Kodi XML NFO with an ownership comment."""
        root_tag = "episodedetails" if title.media_type == MediaType.EPISODE else "movie"
        root = ET.Element(root_tag)

        title_el = ET.SubElement(root, "title")
        title_el.text = title.title

        if title.media_type == MediaType.EPISODE:
            if title.show_title:
                show_el = ET.SubElement(root, "showtitle")
                show_el.text = title.show_title
            if title.season is not None:
                season_el = ET.SubElement(root, "season")
                season_el.text = str(title.season)
            if title.episode is not None:
                ep_el = ET.SubElement(root, "episode")
                ep_el.text = str(title.episode)
            if title.premiered:
                aired_el = ET.SubElement(root, "aired")
                aired_el.text = str(title.premiered)
        else:
            if title.year is not None:
                year_el = ET.SubElement(root, "year")
                year_el.text = str(title.year)
            if title.premiered:
                prem_el = ET.SubElement(root, "premiered")
                prem_el.text = str(title.premiered)

        if title.plot:
            plot_el = ET.SubElement(root, "plot")
            plot_el.text = title.plot

        # Unique IDs for Kodi scrapers, TMDb Helper, and Trakt
        default_set = False
        if title.tmdb_id:
            uid_tmdb = ET.SubElement(root, "uniqueid", attrib={"type": "tmdb", "default": "true"})
            uid_tmdb.text = str(title.tmdb_id)
            default_set = True

        if title.imdb_id:
            attrib = {"type": "imdb"}
            if not default_set:
                attrib["default"] = "true"
                default_set = True
            uid_imdb = ET.SubElement(root, "uniqueid", attrib=attrib)
            uid_imdb.text = str(title.imdb_id)

        if offer and offer.provider_content_id:
            uid_prov = ET.SubElement(
                root,
                "uniqueid",
                attrib={"type": offer.provider.lower()},
            )
            uid_prov.text = str(offer.provider_content_id)

        uid_sb = ET.SubElement(root, "uniqueid", attrib={"type": "streambridge"})
        uid_sb.text = title.title_id

        ET.indent(root, space="  ")
        xml_body = ET.tostring(root, encoding="unicode")
        return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n{NFO_OWNERSHIP_MARKER}\n{xml_body}\n'

    def is_owned_strm(self, path: Path) -> bool:
        """Verify that an existing .strm file was generated by StreamBridge."""
        if not path.exists() or not path.is_file():
            return False
        try:
            content = path.read_text(encoding="utf-8", errors="ignore").strip()
            return content.startswith("plugin://plugin.video.streambridge/")
        except OSError:
            return False

    def is_owned_nfo(self, path: Path) -> bool:
        """Verify that an existing .nfo file still carries the StreamBridge ownership marker."""
        if not path.exists() or not path.is_file():
            return False
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:512]
            return NFO_OWNERSHIP_MARKER in head
        except OSError:
            return False

    def project_title(self, title: Title, offer: Offer) -> Optional[Projection]:
        """Atomically write the .strm and optional .nfo file for an available offer."""
        if not title.is_aired and not self.export_unaired:
            return None

        strm_rel, nfo_rel = self.compute_relative_paths(title, offer.provider)
        strm_path = self.export_root / Path(strm_rel)
        nfo_path = self.export_root / Path(nfo_rel)

        # Never overwrite a non-StreamBridge .strm file
        if strm_path.exists() and not self.is_owned_strm(strm_path):
            raise FileExistsError(
                f"Refusing to overwrite user-owned file at {strm_path}"
            )

        self._atomic_write_text(strm_path, self.build_strm_content(title))

        final_nfo_rel: Optional[str] = None
        if self.write_nfo:
            # Only write or update NFO if it does not exist or is still owned by StreamBridge
            if not nfo_path.exists() or self.is_owned_nfo(nfo_path):
                self._atomic_write_text(nfo_path, self.build_nfo_content(title, offer))
                final_nfo_rel = nfo_rel
            else:
                # User edited the NFO; preserve it untouched
                final_nfo_rel = None

        return Projection(
            title_id=title.title_id,
            provider=offer.provider,
            relative_path=strm_rel,
            nfo_relative_path=final_nfo_rel,
            owned_by_streambridge=True,
            updated_at=time.time(),
        )

    def remove_projection(self, projection: Projection) -> bool:
        """Remove a projected .strm and .nfo only if they are confirmed StreamBridge-owned."""
        if not projection.owned_by_streambridge:
            return False

        strm_path = self.export_root / Path(projection.relative_path)
        removed_any = False

        if strm_path.exists() and self.is_owned_strm(strm_path):
            strm_path.unlink()
            removed_any = True

        if projection.nfo_relative_path:
            nfo_path = self.export_root / Path(projection.nfo_relative_path)
            if nfo_path.exists() and self.is_owned_nfo(nfo_path):
                nfo_path.unlink()

        # Prune empty leaf folder only if completely empty
        parent = strm_path.parent
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

        return removed_any

    @staticmethod
    def _atomic_write_text(target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, target)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
