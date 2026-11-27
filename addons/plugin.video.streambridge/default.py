"""StreamBridge Video Plugin Entry Point (`plugin.video.streambridge`).

Uses `script.module.routing` when running inside Kodi (with a lightweight compatible
router fallback for standalone CLI / unit tests).

Supported routes:
- `/` -> Root menu (Providers, Sync Now, Clear Browser Session)
- `/providers/<provider_id>` -> Browse projected titles for a provider
- `/play/<title_id>` -> Resolve and play a title via PlaybackBroker
- `/play` (with `?id=<title_id>`) -> Query-string compatibility route for `.strm` files
- `/sync/<provider_id>` -> Trigger immediate catalog reconciliation and projection
- `/auth/clear` -> Securely clear the isolated DPAPI browser profile directory
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from streambridge_core.adapters.base import ProviderAdapter
from streambridge_core.adapters.fixture import FixtureAdapter
from streambridge_core.adapters.netflix_catalog import NetflixCatalogAdapter
from streambridge_core.broker import PlaybackBroker, PlaybackSessionResult
from streambridge_core.db import StreamBridgeStore
from streambridge_core.kodi_bridge import IN_KODI, KodiBridge
from streambridge_core.kodi_rpc import KodiJsonRpcClient
from streambridge_core.projection import ProjectionEngine
from streambridge_core.reconciler import ReconciliationEngine, ReconciliationResult
from streambridge_core.security import clear_isolated_browser_profile

try:
    import routing  # type: ignore[import-not-found]
except ImportError:
    routing = None


class _FallbackPluginRouter:
    """Minimal `script.module.routing`-compatible router for offline/pytest environments."""

    def __init__(self, base_url: str = "plugin://plugin.video.streambridge") -> None:
        self.base_url = base_url.rstrip("/")
        self.handle = -1
        self.args: dict[str, list[str]] = {}
        self._routes: list[tuple[ list[str], Callable[..., Any]]] = []

    def route(self, pattern: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        parts = [p for p in pattern.strip("/").split("/") if p]

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._routes.append((parts, func))
            return func

        return decorator

    def url_for(self, func: Callable[..., Any], **kwargs: Any) -> str:
        for parts, target in self._routes:
            if target is func:
                built: list[str] = []
                for p in parts:
                    if p.startswith("<") and p.endswith(">"):
                        key = p[1:-1]
                        built.append(str(kwargs.get(key, "")))
                    else:
                        built.append(p)
                return f"{self.base_url}/{'/'.join(built)}" if built else f"{self.base_url}/"
        return f"{self.base_url}/"

    def run(self, argv: Optional[list[str]] = None) -> Any:
        args = argv if argv is not None else sys.argv
        raw_url = args[0] if args else f"{self.base_url}/"
        self.handle = int(args[1]) if len(args) > 1 and str(args[1]).lstrip("-").isdigit() else -1
        query_str = args[2].lstrip("?") if len(args) > 2 else ""

        parsed = urlparse(raw_url)
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        if parsed.query and not query_str:
            query_str = parsed.query
        self.args = parse_qs(query_str)

        for route_parts, func in self._routes:
            if len(route_parts) != len(path_parts):
                continue
            kwargs: dict[str, str] = {}
            matched = True
            for rp, pp in zip(route_parts, path_parts):
                if rp.startswith("<") and rp.endswith(">"):
                    kwargs[rp[1:-1]] = pp
                elif rp != pp:
                    matched = False
                    break
            if matched:
                return func(**kwargs)
        raise LookupError(f"No plugin route matched URL: {raw_url!r}")


plugin = routing.Plugin() if routing is not None else _FallbackPluginRouter()


class StreamBridgePluginApp:
    """Application container wiring store, adapter, reconciler, and playback broker for plugin routes."""

    def __init__(
        self,
        store: Optional[StreamBridgeStore] = None,
        export_root: Optional[Path] = None,
        adapter: Optional[ProviderAdapter] = None,
        broker: Optional[PlaybackBroker] = None,
        kodi_bridge: Optional[KodiBridge] = None,
        kodi_rpc: Optional[KodiJsonRpcClient] = None,
    ) -> None:
        self.kodi = kodi_bridge or KodiBridge("plugin.video.streambridge")
        self.rpc = kodi_rpc or KodiJsonRpcClient()
        profile_dir = self.kodi.get_profile_dir()
        self.store = store or StreamBridgeStore(profile_dir / "streambridge.db")
        self.export_root = export_root or (profile_dir / "library")
        self.projection = ProjectionEngine(self.export_root, write_nfo=False)
        self.reconciler = ReconciliationEngine(self.store, self.projection)

        default_fixture = (
            Path(__file__).resolve().parents[1]
            / "script.module.streambridge"
            / "lib"
            / "streambridge_core"
            / "providers"
            / "netflix_sample_catalog.json"
        )
        self.adapter = adapter or FixtureAdapter(self.store, fixture_path=default_fixture)
        self.broker = broker or PlaybackBroker(
            store=self.store,
            kodi_bridge=self.kodi,
            kodi_rpc=self.rpc,
        )

    def build_adapter(
        self,
        mode: str = "fixture",
        watchmode_api_key: Optional[str] = None,
        tmdb_api_key: Optional[str] = None,
    ) -> ProviderAdapter:
        if mode in ("watchmode", "tmdb") and (watchmode_api_key or tmdb_api_key):
            return NetflixCatalogAdapter(
                store=self.store,
                watchmode_api_key=watchmode_api_key,
                tmdb_api_key=tmdb_api_key,
            )
        return self.adapter

    def list_provider_titles(self, provider_id: str = "netflix") -> list[dict[str, Any]]:
        titles = self.store.list_titles(provider=provider_id, available_only=True)
        entries: list[dict[str, Any]] = []
        for title in titles:
            offers = self.store.get_offers_for_title(title.title_id, provider=provider_id)
            offer = offers[0] if offers else None
            pb_state = self.store.get_playback_state(title.title_id)
            meta = self.kodi.build_listitem_metadata(title, offer=offer, playback_state=pb_state)
            meta["play_url"] = f"plugin://plugin.video.streambridge/play/{title.title_id}"
            entries.append(meta)
        return entries

    def sync_provider(self, provider_id: str = "netflix", region: str = "US") -> ReconciliationResult:
        policy = self.kodi.load_filter_policy()
        res = self.reconciler.reconcile_provider(
            self.adapter, region=region, filter_policy=policy
        )
        if res.requires_kodi_scan:
            self.rpc.request_targeted_scan()
        if res.requires_kodi_clean:
            self.rpc.request_targeted_clean()
        self.kodi.notify(
            "StreamBridge Sync Complete",
            f"Added: {res.added_count}, Filtered: {res.filtered_out_count}, Removed: {res.removed_count}",
        )
        return res

    def play(
        self,
        title_id: str,
        profile: str = "default",
        preferred_backend: str = "auto",
    ) -> PlaybackSessionResult:
        return self.broker.play_title(
            adapter=self.adapter,
            title_id=title_id,
            profile=profile,
            preferred_backend=preferred_backend,
        )

    def clear_auth_session(self, base_dir: Optional[Path] = None) -> bool:
        cleared = clear_isolated_browser_profile(base_dir=base_dir)
        self.kodi.notify(
            "StreamBridge Security",
            "Isolated browser session data cleared.",
        )
        return cleared


_ACTIVE_APP: Optional[StreamBridgePluginApp] = None


def get_app() -> StreamBridgePluginApp:
    global _ACTIVE_APP
    if _ACTIVE_APP is None:
        _ACTIVE_APP = StreamBridgePluginApp()
    return _ACTIVE_APP


def set_app(app: Optional[StreamBridgePluginApp]) -> None:
    global _ACTIVE_APP
    _ACTIVE_APP = app


@plugin.route("/")
def index() -> list[dict[str, str]]:
    items = [
        {"label": "Netflix Library", "url": "plugin://plugin.video.streambridge/providers/netflix"},
        {"label": "Sync Netflix Catalog Now", "url": "plugin://plugin.video.streambridge/sync/netflix"},
        {"label": "Clear Isolated Browser Session", "url": "plugin://plugin.video.streambridge/auth/clear"},
    ]
    if IN_KODI:
        import xbmcgui  # type: ignore[import-not-found]
        import xbmcplugin  # type: ignore[import-not-found]

        for entry in items:
            li = xbmcgui.ListItem(label=entry["label"], offscreen=True)
            xbmcplugin.addDirectoryItem(plugin.handle, entry["url"], li, isFolder=True)
        xbmcplugin.endOfDirectory(plugin.handle)
    return items


@plugin.route("/providers/<provider_id>")
def browse_provider(provider_id: str) -> list[dict[str, Any]]:
    app = get_app()
    entries = app.list_provider_titles(provider_id=provider_id)
    if IN_KODI:
        import xbmcplugin  # type: ignore[import-not-found]

        for entry in entries:
            li = entry.get("listitem")
            if li is not None:
                li.setProperty("IsPlayable", "true")
                xbmcplugin.addDirectoryItem(plugin.handle, entry["play_url"], li, isFolder=False)
        xbmcplugin.endOfDirectory(plugin.handle)
    return entries


@plugin.route("/play/<title_id>")
def play_route(title_id: str) -> PlaybackSessionResult:
    app = get_app()
    return app.play(title_id=title_id)


@plugin.route("/play")
def play_query_route() -> PlaybackSessionResult:
    id_list = plugin.args.get("id", [])
    if not id_list:
        raise ValueError("Missing required 'id' query parameter on /play")
    return play_route(id_list[0])


@plugin.route("/sync/<provider_id>")
def sync_route(provider_id: str) -> ReconciliationResult:
    app = get_app()
    return app.sync_provider(provider_id=provider_id)


@plugin.route("/auth/clear")
def clear_auth_route() -> bool:
    app = get_app()
    return app.clear_auth_session()


if __name__ == "__main__":
    plugin.run()
