"""launchd integration — start at boot, run the nightly job on schedule.

macOS only, because that's where the box lives. On anything else this prints
the equivalent systemd/cron advice rather than pretending to work.

Two agents, deliberately separate:

  * **tickerforge.up** — `KeepAlive`, so launchd restarts the supervisor if it
    dies, and the supervisor restarts the services if they do. Two layers
    sounds redundant until you remember the supervisor itself is a process,
    and nothing was watching it.

  * **tickerforge.maintenance** — `StartCalendarInterval` at 03:00. A separate
    agent rather than a thread inside the supervisor, because a nightly job
    that can wedge shouldn't be able to take the alert engine with it.

The plists are written but NOT loaded. Handing you the exact `launchctl` line
to run is better than silently installing a background service: it's your
machine, and you should see what's being registered.
"""
from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = Path.home() / "Library" / "LaunchAgents"

UP_LABEL = "com.tickerforge.up"
MAINT_LABEL = "com.tickerforge.maintenance"
MAINT_HOUR = 3


def _python() -> str:
    """The interpreter running this — which is the venv's, if you're in one.
    launchd has no shell profile, so an unqualified `python` finds the system
    one and none of the dependencies."""
    return sys.executable


def _base(label: str, args: list[str]) -> dict:
    return {
        "Label": label,
        "ProgramArguments": [_python(), "-m", "app.cli", *args],
        "WorkingDirectory": str(ROOT),
        # launchd starts with almost no environment. PATH matters for anything
        # shelling out; HOME matters because the Kite token lives under it.
        "EnvironmentVariables": {
            "PATH": os.environ.get(
                "PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
            "HOME": str(Path.home()),
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(ROOT / "data" / f"{label}.log"),
        "StandardErrorPath": str(ROOT / "data" / f"{label}.err.log"),
    }


def up_plist() -> dict:
    d = _base(UP_LABEL, ["up"])
    d["RunAtLoad"] = True
    d["KeepAlive"] = True
    # Don't hammer a supervisor that can't start — usually a missing .env,
    # which no amount of retrying will fix.
    d["ThrottleInterval"] = 30
    return d


def maintenance_plist(hour: int = MAINT_HOUR) -> dict:
    d = _base(MAINT_LABEL, ["maintain"])
    d["RunAtLoad"] = False
    d["StartCalendarInterval"] = {"Hour": hour, "Minute": 0}
    return d


def write(plist: dict, directory: Path | None = None) -> Path:
    directory = directory or AGENT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{plist['Label']}.plist"
    with path.open("wb") as fh:
        plistlib.dump(plist, fh)
    return path


def install(directory: Path | None = None, hour: int = MAINT_HOUR) -> dict:
    """Write both agents. Returns the paths and the commands to activate."""
    if sys.platform != "darwin" and directory is None:
        return {"supported": False, "paths": [], "commands": [],
                "note": _other_platform_note()}
    paths = [write(up_plist(), directory),
             write(maintenance_plist(hour), directory)]
    return {
        "supported": True,
        "paths": [str(p) for p in paths],
        "commands": [f"launchctl load -w {p}" for p in paths],
        "note": "",
    }


def _other_platform_note() -> str:
    return (
        f"launchd is macOS-only and this is {sys.platform}.\n"
        "The equivalent elsewhere:\n"
        "  systemd  — a unit with Restart=always running "
        "`python -m app.cli up`,\n"
        "             plus a timer for `maintain`.\n"
        "  cron     — @reboot for `up`, and `0 3 * * *` for `maintain`.\n"
        "Note that cron also starts with an empty environment, so use "
        "absolute paths."
    )


def format_install(result: dict) -> str:
    if not result["supported"]:
        return result["note"]
    lines = ["Wrote:"]
    lines += [f"  {p}" for p in result["paths"]]
    lines.append("\nNothing is running yet. To activate:")
    lines += [f"  {c}" for c in result["commands"]]
    lines.append("\nTo stop and remove:")
    lines += [f"  launchctl unload -w {p}" for p in result["paths"]]
    lines.append(f"\nLogs land in {ROOT / 'data'}/com.tickerforge.*.log")
    lines.append("The Kite token still expires daily — launchd starts the "
                 "services, it can't log you in.")
    return "\n".join(lines)
