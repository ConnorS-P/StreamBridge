"""Security, URL allowlisting, DPAPI credential storage, and log redaction for StreamBridge.

Enforces the design contract:
- Never accept, store, log, or transmit user streaming service passwords or cookies.
- Validate all launch URLs against HTTPS and provider domain allowlists (preventing SSRF / command injection).
- Redact API keys, tokens, cookies, and authorization parameters from all logs and diagnostics.
- Protect catalog API keys on Windows via DPAPI (CryptProtectData) when requested, and provide
  isolated browser profile cleanup so users have full control over their browser login state.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import logging
from pathlib import Path
import re
import shutil
import sys
from typing import Iterable, Optional
from urllib.parse import urlparse


DEFAULT_ALLOWED_HOSTS: dict[str, tuple[str, ...]] = {
    "netflix": ("www.netflix.com", "netflix.com"),
}

# Patterns that must never appear in logs or diagnostic dumps
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(api[_-]?key|apikey|token|access[_-]?token|secret|password|passwd|auth)([\s=:]+['\"]?)([^'\"&\s]+)"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(NetflixId|SecureNetflixId|nfvdid|flwssn)=([^;\s&]+)"), r"\1=[REDACTED]"),
)


class SecurityValidationError(ValueError):
    """Raised when a URL, path, or IPC payload fails security validation."""


def redact_secrets(text: str) -> str:
    """Strip API keys, bearer tokens, passwords, and session cookies from any string."""
    if not text:
        return text
    sanitized = text
    for pattern, replacement in _SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


class RedactingLogFilter(logging.Filter):
    """Logging filter that automatically redacts secrets from log messages and args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(a) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


def validate_launch_url(
    url: str,
    provider: str,
    allowed_hosts: Optional[Iterable[str]] = None,
) -> str:
    """Validate that a playback launch URL is strictly HTTPS and matches the provider's allowlist."""
    if not url or not isinstance(url, str):
        raise SecurityValidationError("Launch URL must be a non-empty string")

    cleaned = url.strip()
    if any(ch in cleaned for ch in ("\r", "\n", "\x00", '"', "'", "`", ";", "|", "&")):
        # Note: Netflix title/watch URLs do not require shell metacharacters; reject injection chars.
        # Allow standard query '&' only if it is part of a clean query string without shell metacharacters.
        if any(ch in cleaned for ch in ("\r", "\n", "\x00", '"', "'", "`", ";", "|")):
            raise SecurityValidationError("Launch URL contains forbidden control or shell characters")

    parsed = urlparse(cleaned)
    if parsed.scheme.lower() != "https":
        raise SecurityValidationError(f"Launch URL must use https:// scheme (got {parsed.scheme!r})")

    if parsed.username or parsed.password:
        raise SecurityValidationError("Launch URL must not embed credentials")

    host = (parsed.hostname or "").lower()
    permitted = tuple(h.lower() for h in (allowed_hosts or DEFAULT_ALLOWED_HOSTS.get(provider.lower(), ())))
    if not permitted:
        raise SecurityValidationError(f"No allowed hostnames configured for provider {provider!r}")

    if host not in permitted:
        raise SecurityValidationError(
            f"Hostname {host!r} is not in allowlist {permitted} for provider {provider!r}"
        )

    return cleaned


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def dpapi_protect(secret: str) -> str:
    """Encrypt a string using Windows DPAPI bound to the current OS user account.

    On non-Windows platforms, returns an explicitly tagged base64 representation
    (while relying on OS file permissions `0600`).
    """
    raw = secret.encode("utf-8")
    if sys.platform != "win32":
        return "b64:" + base64.b64encode(raw).decode("ascii")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buf = ctypes.create_string_buffer(raw, len(raw))
    in_blob = _DATA_BLOB(len(raw), in_buf)
    out_blob = _DATA_BLOB()

    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "StreamBridgeSecret",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        raise OSError("Windows CryptProtectData failed")

    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def dpapi_unprotect(token: str) -> str:
    """Decrypt a token created by `dpapi_protect`."""
    if not token:
        return ""
    if token.startswith("b64:"):
        return base64.b64decode(token[4:].encode("ascii")).decode("utf-8")
    if not token.startswith("dpapi:"):
        return token

    if sys.platform != "win32":
        raise OSError("Cannot decrypt Windows DPAPI token on non-Windows platform")

    raw = base64.b64decode(token[6:].encode("ascii"))
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buf = ctypes.create_string_buffer(raw, len(raw))
    in_blob = _DATA_BLOB(len(raw), in_buf)
    out_blob = _DATA_BLOB()

    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        raise OSError("Windows CryptUnprotectData failed")

    try:
        decrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return decrypted.decode("utf-8")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def get_isolated_browser_profile_dir(base_dir: Optional[Path] = None) -> Path:
    """Return the path to the isolated WebView2 / browser profile directory."""
    if base_dir is not None:
        profile_dir = Path(base_dir) / "BrowserProfile"
    elif sys.platform == "win32":
        import os

        local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        profile_dir = Path(local_app_data) / "StreamBridge" / "BrowserProfile"
    else:
        profile_dir = Path.home() / ".local" / "share" / "StreamBridge" / "BrowserProfile"

    profile_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        try:
            profile_dir.chmod(0o700)
        except OSError:
            pass
    return profile_dir


def clear_isolated_browser_profile(base_dir: Optional[Path] = None) -> bool:
    """Wipe the isolated browser profile directory so the user can sign out cleanly."""
    profile_dir = get_isolated_browser_profile_dir(base_dir)
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        return True
    return False
