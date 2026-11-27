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
from typing import Any
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

from streambridge_core.adapters.base import ProviderAdapter
from streambridge_core.adapters.fixture import FixtureAdapter
from streambridge_core.adapters.netflix_catalog import NetflixCatalogAdapter
from streambridge_core.broker import PlaybackBroker, PlaybackSessionResult
from streambridge_core.db import StreamBridgeStore
from streambridge_core.kodi_bridge import IN_KODI, KodiBridge
from streambridge_core.kodi_rpc import KodiJsonRpcClient
from streambridge_core.models import MediaType
from streambridge_core.projection import ProjectionEngine
from streambridge_core.reconciler import ReconciliationEngine, ReconciliationResult
from streambridge_core.security import (
    clear_isolated_browser_profile,
    get_isolated_browser_profile_dir,
    validate_launch_url,
)

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

    def run(self, argv: list[str] | None = None) -> Any:
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
        store: StreamBridgeStore | None = None,
        export_root: Path | None = None,
        adapter: ProviderAdapter | None = None,
        broker: PlaybackBroker | None = None,
        kodi_bridge: KodiBridge | None = None,
        kodi_rpc: KodiJsonRpcClient | None = None,
    ) -> None:
        self.kodi = kodi_bridge or KodiBridge("plugin.video.streambridge")
        self.rpc = kodi_rpc or KodiJsonRpcClient()
        profile_dir = self.kodi.get_profile_dir()
        self.store = store or StreamBridgeStore(profile_dir / "streambridge.db")
        self.export_root = export_root or self.kodi.get_export_root(profile_dir / "library")
        self.export_root.mkdir(parents=True, exist_ok=True)
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
        bundled_player = Path(__file__).resolve().parent / "bin" / "StreamBridge.Player.exe"
        self.companion_exe_path = bundled_player if bundled_player.exists() else None
        if adapter is not None:
            self.adapter = adapter
        elif IN_KODI:
            import xbmcaddon  # type: ignore[import-not-found]

            addon_inst = xbmcaddon.Addon("plugin.video.streambridge")
            cat_mode = addon_inst.getSettingString("netflix_catalog_mode") or "live_justwatch"
            wm_key = addon_inst.getSettingString("watchmode_api_key") or None
            if cat_mode == "fixture":
                self.adapter = FixtureAdapter(self.store, fixture_path=default_fixture)
            else:
                self.adapter = NetflixCatalogAdapter(
                    store=self.store,
                    watchmode_api_key=wm_key,
                    companion_exe_path=self.companion_exe_path,
                )
        else:
            self.adapter = FixtureAdapter(self.store, fixture_path=default_fixture)
        self.broker = broker or PlaybackBroker(
            store=self.store,
            kodi_bridge=self.kodi,
            kodi_rpc=self.rpc,
            companion_exe_path=self.companion_exe_path,
        )

    def build_adapter(
        self,
        mode: str = "live_justwatch",
        watchmode_api_key: str | None = None,
        tmdb_api_key: str | None = None,
    ) -> ProviderAdapter:
        if mode == "fixture":
            return self.adapter
        return NetflixCatalogAdapter(
            store=self.store,
            watchmode_api_key=watchmode_api_key,
            tmdb_api_key=tmdb_api_key,
            companion_exe_path=self.companion_exe_path,
        )

    def list_provider_titles(
        self, provider_id: str = "netflix", media_filter: str | None = None
    ) -> list[dict[str, Any]]:
        titles = self.store.list_titles(provider=provider_id, available_only=True)
        entries: list[dict[str, Any]] = []
        for title in titles:
            if media_filter == "movies" and title.media_type != MediaType.MOVIE:
                continue
            if media_filter == "tvshows" and title.media_type not in (MediaType.SHOW, MediaType.EPISODE):
                continue
            offers = self.store.get_offers_for_title(title.title_id, provider=provider_id)
            offer = offers[0] if offers else None
            pb_state = self.store.get_playback_state(title.title_id)
            meta = self.kodi.build_listitem_metadata(title, offer=offer, playback_state=pb_state)
            meta["play_url"] = f"plugin://plugin.video.streambridge/play/{title.title_id}"
            entries.append(meta)
        return entries

    def sync_provider(
        self,
        provider_id: str = "netflix",
        region: str = "US",
        sync_favourites: bool = True,
    ) -> ReconciliationResult:
        policy = self.kodi.load_filter_policy()
        res = self.reconciler.reconcile_provider(
            self.adapter, region=region, filter_policy=policy
        )
        if sync_favourites:
            mylist_titles = [
                t for t in self.store.list_titles(provider=provider_id, available_only=True)
                if t.in_my_list
            ]
            if mylist_titles:
                self.rpc.sync_mylist_to_kodi_favourites(mylist_titles)
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

    def clear_auth_session(self, base_dir: Path | None = None) -> bool:
        cleared = clear_isolated_browser_profile(base_dir=base_dir)
        self.kodi.notify(
            "StreamBridge Security",
            "Isolated browser session data cleared.",
        )
        return cleared

    def add_provider_video_sources(
        self,
        provider_id: str = "netflix",
        media_type: str = "all",
        sources_xml_path: Path | None = None,
    ) -> list[str]:
        """Sync catalog and register `<video>` sources using Kodi's native MediaSource dialog."""
        from streambridge_core.projection import format_provider_dir

        self.sync_provider(provider_id=provider_id)
        prov_dir = format_provider_dir(provider_id)
        base = self.export_root / "Streaming" / prov_dir
        added: list[str] = []

        if IN_KODI and sources_xml_path is None:
            if media_type in ("movies", "all"):
                label = f"{prov_dir} (Movies)"
                self.kodi.add_video_source_via_native_dialog(
                    base / "Movies",
                    source_name=label,
                    rpc_client=self.rpc,
                    media_type="movies",
                )
                added.append(label)
            elif media_type in ("tvshows", "tv"):
                label = f"{prov_dir} (TV Shows)"
                self.kodi.add_video_source_via_native_dialog(
                    base / "TV",
                    source_name=label,
                    rpc_client=self.rpc,
                    media_type="tvshows",
                )
                added.append(label)
            return added

        if media_type in ("movies", "all"):
            label = f"{prov_dir} (Movies)"
            self.kodi.ensure_video_source_entry(
                base / "Movies",
                source_name=label,
                sources_xml_path=sources_xml_path,
            )
            added.append(label)

        if media_type in ("tvshows", "tv", "all"):
            label = f"{prov_dir} (TV Shows)"
            self.kodi.ensure_video_source_entry(
                base / "TV",
                source_name=label,
                sources_xml_path=sources_xml_path,
            )
            added.append(label)

        return added

    def launch_provider_login(self, provider_id: str = "netflix") -> bool:
        """Open the provider's official login page inside Kodi as a centered WS_CHILD surface."""
        import subprocess

        from streambridge_core.platform_compat import detect_platform_environment

        if not self.rpc.is_fake_fullscreen_enabled():
            self.kodi.show_ok_dialog(
                "StreamBridge — Incompatible Display Mode",
                "StreamBridge renders inside Kodi's window using Windows DirectComposition, "
                "which is incompatible with legacy Exclusive Fullscreen.\n\n"
                "Please enable 'Use fullscreen window' under Settings -> System -> Display.",
            )
            return False

        login_url = validate_launch_url("https://www.netflix.com/login", provider=provider_id)
        profile_dir = get_isolated_browser_profile_dir()

        if self.companion_exe_path and self.companion_exe_path.exists():
            no_win_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
            if IN_KODI:
                import xbmc  # type: ignore[import-not-found]
                import xbmcgui  # type: ignore[import-not-found]

                was_mouse_enabled = self.rpc.get_mouse_enabled()
                if not was_mouse_enabled:
                    self.rpc.set_mouse_enabled(True)

                dlg = xbmcgui.WindowDialog()
                header = xbmcgui.ControlLabel(
                    x=0,
                    y=18,
                    width=1280,
                    height=40,
                    label="StreamBridge Netflix Sign-In — Closes automatically once signed in (or press ESC / Back)",
                    alignment=2,
                )
                dlg.addControl(header)
                dlg.show()
                try:
                    proc = subprocess.Popen(
                        [str(self.companion_exe_path), "--login", login_url],
                        shell=False,
                        close_fds=True,
                        creationflags=no_win_flag,
                    )
                    while proc.poll() is None and not xbmc.Monitor().abortRequested():
                        xbmc.sleep(150)
                finally:
                    dlg.close()
                    if not was_mouse_enabled:
                        self.rpc.set_mouse_enabled(False)
                self.kodi.notify("StreamBridge Login", "Netflix session saved.")
                return True

            subprocess.Popen(
                [str(self.companion_exe_path), "--login", login_url],
                shell=False,
                close_fds=True,
                creationflags=no_win_flag,
            )
            return True

        env = detect_platform_environment(provider_id=provider_id)
        if not env.desktop_browser_cmd:
            self.kodi.notify(
                "StreamBridge Login",
                "No playback host or desktop browser found to open login session.",
                error=True,
            )
            return False

        binary = env.desktop_browser_cmd[0]
        exe_name = Path(binary).name.lower()
        if any(b in exe_name for b in ("msedge", "chrome", "chromium")):
            args = [
                binary,
                f"--app={login_url}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
            ]
        else:
            args = [binary, login_url]

        self.kodi.notify(
            "StreamBridge Login",
            "Opening Netflix login in browser.",
        )
        subprocess.Popen(
            args,
            shell=False,
            close_fds=True,
        )
        return True


_ACTIVE_APP: StreamBridgePluginApp | None = None


def get_app() -> StreamBridgePluginApp:
    global _ACTIVE_APP
    if _ACTIVE_APP is None:
        _ACTIVE_APP = StreamBridgePluginApp()
    return _ACTIVE_APP


def set_app(app: StreamBridgePluginApp | None) -> None:
    global _ACTIVE_APP
    _ACTIVE_APP = app


@plugin.route("/")
def index() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {
            "label": "Netflix — Movies",
            "url": "plugin://plugin.video.streambridge/providers/netflix/movies",
            "context_menu": [
                (
                    "Add Netflix (Movies) to Kodi Video Sources & Sync",
                    "RunPlugin(plugin://plugin.video.streambridge/sources/add/netflix/movies)",
                ),
                (
                    "Add All Netflix Sources (Movies + TV) to Kodi",
                    "RunPlugin(plugin://plugin.video.streambridge/sources/add/netflix/all)",
                ),
                (
                    "Sync Netflix Catalog Now",
                    "RunPlugin(plugin://plugin.video.streambridge/sync/netflix)",
                ),
                (
                    "Sign In to Netflix",
                    "RunPlugin(plugin://plugin.video.streambridge/auth/login/netflix)",
                ),
            ],
        },
        {
            "label": "Netflix — TV Shows",
            "url": "plugin://plugin.video.streambridge/providers/netflix/tvshows",
            "context_menu": [
                (
                    "Add Netflix (TV Shows) to Kodi Video Sources & Sync",
                    "RunPlugin(plugin://plugin.video.streambridge/sources/add/netflix/tvshows)",
                ),
                (
                    "Add All Netflix Sources (Movies + TV) to Kodi",
                    "RunPlugin(plugin://plugin.video.streambridge/sources/add/netflix/all)",
                ),
                (
                    "Sync Netflix Catalog Now",
                    "RunPlugin(plugin://plugin.video.streambridge/sync/netflix)",
                ),
                (
                    "Sign In to Netflix",
                    "RunPlugin(plugin://plugin.video.streambridge/auth/login/netflix)",
                ),
            ],
        },
        {
            "label": "Add Netflix Movies & TV to Kodi Video Sources",
            "url": "plugin://plugin.video.streambridge/sources/add/netflix/all",
            "context_menu": [],
        },
        {
            "label": "Sync Netflix Catalog Now",
            "url": "plugin://plugin.video.streambridge/sync/netflix",
            "context_menu": [],
        },
        {
            "label": "Sign In to Netflix",
            "url": "plugin://plugin.video.streambridge/auth/login/netflix",
            "context_menu": [],
        },
        {
            "label": "Clear Isolated Browser Session",
            "url": "plugin://plugin.video.streambridge/auth/clear",
            "context_menu": [],
        },
    ]
    if IN_KODI:
        import xbmcgui  # type: ignore[import-not-found]
        import xbmcplugin  # type: ignore[import-not-found]

        for entry in items:
            li = xbmcgui.ListItem(label=entry["label"], offscreen=True)
            if entry["context_menu"]:
                li.addContextMenuItems(entry["context_menu"])
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


@plugin.route("/providers/<provider_id>/<media_filter>")
def browse_provider_filtered(provider_id: str, media_filter: str) -> list[dict[str, Any]]:
    app = get_app()
    entries = app.list_provider_titles(provider_id=provider_id, media_filter=media_filter)
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


@plugin.route("/sources/add/<provider_id>/<media_type>")
def add_sources_route(provider_id: str, media_type: str) -> list[str]:
    app = get_app()
    return app.add_provider_video_sources(provider_id=provider_id, media_type=media_type)


@plugin.route("/sync/<provider_id>")
def sync_route(provider_id: str) -> ReconciliationResult:
    app = get_app()
    return app.sync_provider(provider_id=provider_id)


@plugin.route("/auth/login/<provider_id>")
def login_route(provider_id: str) -> bool:
    app = get_app()
    return app.launch_provider_login(provider_id=provider_id)


@plugin.route("/auth/clear")
def clear_auth_route() -> bool:
    app = get_app()
    return app.clear_auth_session()


if __name__ == "__main__":
    plugin.run()
