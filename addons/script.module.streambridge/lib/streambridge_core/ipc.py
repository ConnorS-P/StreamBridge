"""User-scoped IPC channel between StreamBridge Python broker and StreamBridge.Player.

Supports:
- Windows Named Pipes (`\\\\.\\pipe\\StreamBridgePlayer`) for native Win32 companion communication.
- Local loopback (`127.0.0.1`) and in-memory transport for cross-platform testing and non-Windows environments.

Security contract:
- Pass opaque session IDs and validated launch requests only.
- Reject payloads containing credentials, cookies, tokens, or DRM keys.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import socket
import sys
from typing import Any, Callable, Optional

from .models import LaunchDescriptor, PlaybackSessionState, SessionEvent
from .security import SecurityValidationError, validate_launch_url

DEFAULT_PIPE_NAME = r"\\.\pipe\StreamBridgePlayer"
FORBIDDEN_IPC_KEYS = frozenset(
    {"password", "passwd", "cookie", "cookies", "drm_key", "license_key", "api_key", "apikey", "secret"}
)


def validate_ipc_payload(payload: dict[str, Any]) -> None:
    """Ensure an IPC dictionary contains no forbidden credential/cookie/key fields."""
    for key, val in payload.items():
        if key.lower() in FORBIDDEN_IPC_KEYS:
            raise SecurityValidationError(f"Forbidden secret field {key!r} in IPC payload")
        if isinstance(val, dict):
            validate_ipc_payload(val)


def serialize_launch_request(descriptor: LaunchDescriptor) -> str:
    """Serialize a validated `LaunchDescriptor` to a JSON-line IPC command."""
    validate_launch_url(descriptor.launch_url, provider=descriptor.provider)
    data = asdict(descriptor)
    data["media_type"] = descriptor.media_type.value
    envelope = {
        "type": "launch",
        "payload": data,
    }
    validate_ipc_payload(envelope)
    return json.dumps(envelope, separators=(",", ":")) + "\n"


def deserialize_session_event(line: str) -> SessionEvent:
    """Parse a JSON-line session telemetry event from `StreamBridge.Player`."""
    raw = json.loads(line.strip())
    validate_ipc_payload(raw)
    payload = raw.get("payload", raw)
    return SessionEvent(
        session_id=str(payload["session_id"]),
        title_id=str(payload["title_id"]),
        state=PlaybackSessionState(payload["state"]),
        sequence=int(payload.get("sequence", 0)),
        position_seconds=(
            float(payload["position_seconds"])
            if payload.get("position_seconds") is not None
            else None
        ),
        duration_seconds=(
            float(payload["duration_seconds"])
            if payload.get("duration_seconds") is not None
            else None
        ),
        backend_used=payload.get("backend_used"),
        error_code=payload.get("error_code"),
        error_message=payload.get("error_message"),
        telemetry_verified=bool(payload.get("telemetry_verified", False)),
    )


class NamedPipeClient:
    """Client for communicating with `StreamBridge.Player` over a Windows Named Pipe or loopback socket."""

    def __init__(
        self,
        pipe_name: str = DEFAULT_PIPE_NAME,
        tcp_fallback: Optional[tuple[str, int]] = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.pipe_name = pipe_name
        self.tcp_fallback = tcp_fallback
        self.timeout_seconds = timeout_seconds

    def send_launch_and_stream_events(
        self,
        descriptor: LaunchDescriptor,
        on_event: Callable[[SessionEvent], None],
    ) -> list[SessionEvent]:
        """Send a launch request and invoke `on_event` for each telemetry line until session ends."""
        request_line = serialize_launch_request(descriptor)
        events: list[SessionEvent] = []

        if self.tcp_fallback is not None:
            return self._communicate_tcp(request_line, on_event, events)

        if sys.platform == "win32":
            return self._communicate_windows_pipe(request_line, on_event, events)

        raise OSError("Windows Named Pipe IPC is only available on win32 unless tcp_fallback is set")

    def _communicate_windows_pipe(
        self,
        request_line: str,
        on_event: Callable[[SessionEvent], None],
        events: list[SessionEvent],
    ) -> list[SessionEvent]:
        with open(self.pipe_name, "r+b", buffering=0) as pipe:
            pipe.write(request_line.encode("utf-8"))
            buffer = b""
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    line_str = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    ev = deserialize_session_event(line_str)
                    events.append(ev)
                    on_event(ev)
                    if ev.state in (PlaybackSessionState.STOPPED, PlaybackSessionState.FAILED):
                        return events
        return events

    def _communicate_tcp(
        self,
        request_line: str,
        on_event: Callable[[SessionEvent], None],
        events: list[SessionEvent],
    ) -> list[SessionEvent]:
        assert self.tcp_fallback is not None
        with socket.create_connection(self.tcp_fallback, timeout=self.timeout_seconds) as sock:
            sock.sendall(request_line.encode("utf-8"))
            sock_file = sock.makefile("r", encoding="utf-8")
            for line in sock_file:
                line_str = line.strip()
                if not line_str:
                    continue
                ev = deserialize_session_event(line_str)
                events.append(ev)
                on_event(ev)
                if ev.state in (PlaybackSessionState.STOPPED, PlaybackSessionState.FAILED):
                    break
        return events
