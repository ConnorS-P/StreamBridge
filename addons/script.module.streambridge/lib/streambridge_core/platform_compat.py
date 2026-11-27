"""Cross-platform compatibility & graceful non-Windows playback fallbacks for StreamBridge.

While StreamBridge's primary HTPC player (`StreamBridge.Player.exe`) targets Windows x64,
library projection, SQLite catalog sync, NFO generation, and Kodi library integration work
on all Kodi platforms (Linux, LibreELEC/CoreELEC, macOS, Android, Windows).

On non-Windows systems (or when the native companion player is not installed), this module
gracefully routes playback through:
1. An installed companion Kodi add-on if available and enabled (e.g. `plugin.video.netflix`
   or `inputstream.adaptive` via `script.module.inputstreamhelper`).
2. Android native app intent (`StartAndroidActivity` -> `com.netflix.ninja`).
3. Desktop browser app/kiosk mode (`chromium --app=...`, `google-chrome --app=...`, `xdg-open`, `open`).
4. A clean, non-crashing notification explaining the platform status on headless/embedded setups.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable

from .models import LaunchDescriptor, PlaybackSessionState, SessionEvent
from .providers import ProviderDescriptor, load_provider_descriptor
from .security import get_isolated_browser_profile_dir, validate_launch_url


@dataclass
class PlatformEnvironment:
    """Detected operating system and available playback capabilities."""

    os_family: str  # "windows", "linux", "macos", "android", "libreelec"
    is_windows: bool
    has_native_companion_exe: bool
    companion_exe_path: Path | None
    has_companion_kodi_addon: bool
    desktop_browser_cmd: list[str] | None


def detect_platform_environment(
    companion_exe_path: Path | None = None,
    has_kodi_addon_check: Callable[[str], bool] | None = None,
    provider_id: str = "netflix",
    platform_override: str | None = None,
) -> PlatformEnvironment:
    """Detect the current platform and available playback pathways."""
    raw_platform = (platform_override or sys.platform).lower()
    descriptor = load_provider_descriptor(provider_id)

    is_windows = raw_platform.startswith("win")
    if is_windows:
        os_family = "windows"
    elif "android" in raw_platform or "ANDROID_ROOT" in __import__("os").environ:
        os_family = "android"
    elif raw_platform == "darwin":
        os_family = "macos"
    else:
        uname_release = platform. release().lower()
        if "libreelec" in uname_release or "coreelec" in uname_release or Path("/etc/os-release").exists() and "libreelec" in Path("/etc/os-release").read_text(errors="ignore").lower():
            os_family = "libreelec"
        else:
            os_family = "linux"

    resolved_exe: Path | None = None
    if is_windows and companion_exe_path and Path(companion_exe_path).exists():
        resolved_exe = Path(companion_exe_path)

    has_companion_addon = False
    if has_kodi_addon_check and descriptor.companion_kodi_addon:
        try:
            has_companion_addon = bool(has_kodi_addon_check(descriptor.companion_kodi_addon))
        except Exception:
            has_companion_addon = False

    browser_cmd = _find_desktop_browser(os_family)

    return PlatformEnvironment(
        os_family=os_family,
        is_windows=is_windows,
        has_native_companion_exe=(resolved_exe is not None),
        companion_exe_path=resolved_exe,
        has_companion_kodi_addon=has_companion_addon,
        desktop_browser_cmd=browser_cmd,
    )


def _find_desktop_browser(os_family: str) -> list[str] | None:
    """Locate an installed browser binary without using `shell=True`.

    On Windows, prefers `msedge_IFEO.exe` if MSEdgeRedirect is installed (which otherwise
    intercepts `msedge.exe` via Image File Execution Options and drops `--app` arguments),
    or falls back to `msedge.exe` / `chrome.exe`.
    """
    if os_family == "windows":
        candidates = [
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge_IFEO.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge_IFEO.exe"),
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ]
        for cand in candidates:
            if cand.exists():
                return [str(cand)]
        for name in ("chrome", "msedge"):
            found_bin = shutil.which(name)
            if found_bin:
                return [found_bin]
        return None

    if os_family == "macos":
        if shutil.which("open"):
            return ["open"]
        return None

    if os_family == "linux":
        for binary in ("chromium", "chromium-browser", "google-chrome", "microsoft-edge", "xdg-open"):
            found = shutil.which(binary)
            if found:
                return [found]
    return None


def build_non_windows_fallback_plan(
    descriptor: LaunchDescriptor,
    env: PlatformEnvironment,
    provider_descriptor: ProviderDescriptor | None = None,
) -> dict[str, str]:
    """Determine the safest non-Windows playback action for `descriptor`.

    Returns a dictionary with:
    - `mode`: `"kodi_addon"`, `"android_intent"`, `"desktop_browser"`, or `"unsupported"`
    - `target`: URI, command, or user-facing message
    """
    prov = provider_descriptor or load_provider_descriptor(descriptor.provider)
    validated_url = validate_launch_url(
        descriptor.launch_url,
        provider=descriptor.provider,
        allowed_hosts=prov.allowed_hosts,
    )

    if env.has_companion_kodi_addon and prov.companion_kodi_uri_template:
        uri = prov.companion_kodi_uri_template.format(content_id=descriptor.provider_content_id)
        return {
            "mode": "kodi_addon",
            "target": uri,
            "addon_id": prov.companion_kodi_addon,
        }

    if env.os_family == "android" and prov.android_package:
        deep_link = prov.android_deep_link_template.format(content_id=descriptor.provider_content_id)
        intent = f'StartAndroidActivity("{prov.android_package}", "android.intent.action.VIEW", "", "{deep_link}")'
        return {
            "mode": "android_intent",
            "target": intent,
            "url": validated_url,
        }

    if env.desktop_browser_cmd:
        cmd0 = env.desktop_browser_cmd[0]
        return {
            "mode": "desktop_browser",
            "target": validated_url,
            "binary": cmd0,
        }

    return {
        "mode": "unsupported",
        "target": (
            f"StreamBridge library sync is active on {env.os_family}, but no playback backend "
            f"(StreamBridge.Player, {prov.companion_kodi_addon}, or desktop browser) was found."
        ),
    }


def launch_browser_process(
    binary: str,
    validated_url: str,
    app_mode: bool = True,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> subprocess.Popen[bytes]:
    """Launch a browser subprocess safely without `shell=True`.

    Note: As required by Section 4 of the Design Doc, a browser launch is NOT
    evidence of verified playback telemetry (`telemetry_verified=False`).
    """
    exe_name = Path(binary).name.lower()
    if app_mode and any(b in exe_name for b in ("msedge", "chrome", "chromium")):
        profile_dir = get_isolated_browser_profile_dir()
        args = [
            binary,
            f"--app={validated_url}",
            f"--user-data-dir={profile_dir}",
            "--start-fullscreen",
            "--no-first-run",
        ]
    else:
        args = [binary, validated_url]

    return popen_factory(
        args,
        shell=False,
        close_fds=True,
    )


def make_unverified_browser_events(descriptor: LaunchDescriptor, backend_name: str) -> list[SessionEvent]:
    """Produce lifecycle events for an external browser/app launch where telemetry is not verified.

    Explicitly sets `telemetry_verified=False` so the playback broker never fabricates watch progress.
    """
    return [
        SessionEvent(
            session_id=descriptor.session_id,
            title_id=descriptor.title_id,
            state=PlaybackSessionState.LAUNCHING,
            sequence=1,
            backend_used=backend_name,
            telemetry_verified=False,
        ),
        SessionEvent(
            session_id=descriptor.session_id,
            title_id=descriptor.title_id,
            state=PlaybackSessionState.STOPPED,
            sequence=2,
            backend_used=backend_name,
            telemetry_verified=False,
        ),
    ]
