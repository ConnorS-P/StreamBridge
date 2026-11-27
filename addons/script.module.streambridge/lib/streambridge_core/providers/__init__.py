"""Declarative Provider Descriptor loader for isolating high-churn provider rules."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass
class ProviderDescriptor:
    """Hot-reloadable provider configuration isolating URL templates, allowlists, and player hooks."""

    provider_id: str
    display_name: str
    allowed_hosts: tuple[str, ...]
    title_url_template: str
    watch_url_template: str
    android_package: str = ""
    android_deep_link_template: str = ""
    windows_app_uri_template: str = ""
    companion_kodi_addon: str = ""
    companion_kodi_uri_template: str = ""
    watchmode_source_id: int = 203
    tmdb_provider_id: int = 8
    webview_video_selector: str = "video"
    capabilities: dict[str, Any] = field(default_factory=dict)

    def format_title_url(self, content_id: str) -> str:
        return self.title_url_template.format(content_id=content_id)

    def format_watch_url(self, content_id: str) -> str:
        return self.watch_url_template.format(content_id=content_id)

    def format_deep_link(self, content_id: str) -> str:
        if self.windows_app_uri_template:
            return self.windows_app_uri_template.format(content_id=content_id)
        return self.format_watch_url(content_id)


def load_provider_descriptor(provider_id: str, custom_path: Path | None = None) -> ProviderDescriptor:
    """Load a provider descriptor from JSON."""
    if custom_path is not None:
        path = Path(custom_path)
    else:
        path = Path(__file__).parent / f"{provider_id.lower()}.json"

    data = json.loads(path.read_text(encoding="utf-8"))
    return ProviderDescriptor(
        provider_id=data["provider_id"],
        display_name=data["display_name"],
        allowed_hosts=tuple(data.get("allowed_hosts", ())),
        title_url_template=data["title_url_template"],
        watch_url_template=data["watch_url_template"],
        android_package=data.get("android_package", ""),
        android_deep_link_template=data.get("android_deep_link_template", ""),
        windows_app_uri_template=data.get("windows_app_uri_template", ""),
        companion_kodi_addon=data.get("companion_kodi_addon", ""),
        companion_kodi_uri_template=data.get("companion_kodi_uri_template", ""),
        watchmode_source_id=int(data.get("watchmode_source_id", 203)),
        tmdb_provider_id=int(data.get("tmdb_provider_id", 8)),
        webview_video_selector=data.get("webview_video_selector", "video"),
        capabilities=data.get("capabilities", {}),
    )
