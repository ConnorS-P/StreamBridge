"""Interactive Player Sandbox & Cadmium Contract Probe CLI for StreamBridge.

Usage examples:
  1. Run automated contract probe against the local Mock Cadmium fixture:
     python scripts/sandbox_player.py --probe-mock

  2. Run automated contract probe against a live Netflix watch URL (uses DPAPI profile):
     python scripts/sandbox_player.py --probe https://www.netflix.com/watch/70037598

  3. Launch interactive sandbox against the local Mock Cadmium fixture:
     python scripts/sandbox_player.py --mock

  4. Launch interactive sandbox against a live Netflix watch URL:
     python scripts/sandbox_player.py --url https://www.netflix.com/watch/70037598
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "addons" / "script.module.streambridge" / "lib"))

from streambridge_core.osd_overlay import read_live_telemetry, send_player_control  # noqa: E402

PLAYER_EXE = ROOT_DIR / "player" / "build" / "Release" / "StreamBridge.Player.exe"
MOCK_FIXTURE = ROOT_DIR / "fixtures" / "mock_cadmium_player.html"


def run_probe(target: str) -> int:
    if not PLAYER_EXE.exists():
        print(f"Error: {PLAYER_EXE} not found. Build with: cmake --build player/build --config Release")
        return 1

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        out_path = Path(tmp.name)

    try:
        proc = subprocess.run(
            [str(PLAYER_EXE), "--probe-cadmium", target, str(out_path)],
            shell=False,
            check=False,
            timeout=20,
        )
        if out_path.exists():
            report = json.loads(out_path.read_text(encoding="utf-8", errors="ignore"))
            print(json.dumps(report, indent=2))
            return 0 if report.get("contract_valid") and proc.returncode == 0 else 1
        return proc.returncode or 1
    finally:
        out_path.unlink(missing_ok=True)


def print_telemetry_status() -> None:
    tel = read_live_telemetry()
    if not tel:
        print("[Telemetry] No live_telemetry.json data yet.")
        return
    state = tel.get("state")
    pos = float(tel.get("position_seconds") or 0.0)
    dur = float(tel.get("duration_seconds") or 0.0)
    subs = tel.get("subtitles") or []
    auds = tel.get("audio_tracks") or []
    active_sub = next((s.get("label") for s in subs if isinstance(s, dict) and s.get("active")), "None")
    active_aud = next((a.get("label") for a in auds if isinstance(a, dict) and a.get("active")), "None")
    print(
        f"[Telemetry] state={state!r} | pos={pos:.1f}s / {dur:.1f}s | "
        f"sub={active_sub!r} ({len(subs)} tracks) | audio={active_aud!r} ({len(auds)} tracks)"
    )


def run_interactive_sandbox(target: str, is_mock: bool) -> int:
    if not PLAYER_EXE.exists():
        print(f"Error: {PLAYER_EXE} not found. Build with: cmake --build player/build --config Release")
        return 1

    flag = "--test-fixture" if is_mock else "--watch"
    proc = subprocess.Popen(
        [str(PLAYER_EXE), flag, target],
        shell=False,
    )
    print(f"Started StreamBridge.Player ({flag} {target}) [PID {proc.pid}]")
    print("Commands: status | pause | resume | seek <sec> | seek_by <±sec> | sub <idx> | audio <idx> | osd <0|1|2> | quit")
    time.sleep(1.2)
    print_telemetry_status()

    try:
        while proc.poll() is None:
            raw = input("sandbox> ").strip()
            if not raw:
                print_telemetry_status()
                continue
            parts = raw.split()
            cmd = parts[0].lower()
            arg = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("+-").isdigit() else 0

            if cmd in ("q", "quit", "exit", "stop"):
                send_player_control("stop")
                break
            if cmd == "status":
                print_telemetry_status()
            elif cmd in ("pause", "resume"):
                send_player_control(cmd)
                time.sleep(0.35)
                print_telemetry_status()
            elif cmd in ("seek", "seek_by"):
                send_player_control(cmd, arg)
                time.sleep(0.35)
                print_telemetry_status()
            elif cmd == "sub":
                send_player_control("set_subtitle", arg)
                time.sleep(0.35)
                print_telemetry_status()
            elif cmd == "audio":
                send_player_control("set_audio", arg)
                time.sleep(0.35)
                print_telemetry_status()
            elif cmd == "osd":
                send_player_control("osd_mode", arg)
                print(f"Sent osd_mode={arg}")
            else:
                print(f"Unknown command: {cmd!r}")
    except (KeyboardInterrupt, EOFError):
        send_player_control("stop")
    finally:
        if proc.poll() is None:
            send_player_control("stop")
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="StreamBridge Player Sandbox & Cadmium Contract Probe")
    parser.add_argument("--probe-mock", action="store_true", help="Run Cadmium contract probe on mock fixture")
    parser.add_argument("--probe", type=str, help="Run Cadmium contract probe on a Netflix watch URL")
    parser.add_argument("--mock", action="store_true", help="Run interactive sandbox on mock Cadmium fixture")
    parser.add_argument("--url", type=str, help="Run interactive sandbox on a live Netflix watch URL")
    args = parser.parse_args()

    if args.probe_mock:
        return run_probe(str(MOCK_FIXTURE))
    if args.probe:
        return run_probe(args.probe)
    if args.url:
        return run_interactive_sandbox(args.url, is_mock=False)
    return run_interactive_sandbox(str(MOCK_FIXTURE), is_mock=True)


if __name__ == "__main__":
    raise SystemExit(main())
