"""One parent process for the four services that must stay alive.

The problem this solves isn't crashes — it's *quiet* crashes. Only `serve`
fails loudly, because you notice when pages stop loading. If the intraday
watcher dies, alerts simply never fire again, and "no alert fired" is
indistinguishable from "nothing triggered". You find out the next time you
check a chart manually and see the setup you were waiting for, three hours
gone. The token-expiry heartbeat exists for exactly this reason and only
covers one failure mode.

So: supervise all four, restart what dies, and — the part that actually
matters — **say so on the phone**. A restart you don't know about is only
marginally better than a death you don't know about, because a service that
restarts every ninety seconds is not working either.

Design
------
* **Backoff** doubles from 1s to a cap, so a service that can't start (bad
  token, port in use) doesn't spin the CPU retrying forever.
* **Stability resets it.** A child that stays up past `STABLE_AFTER` is
  considered recovered and its backoff and failure count reset. Without this,
  a service that dies once a day would eventually be treated as crash-looping.
* **Crash loops give up.** After `MAX_FAILURES` restarts without stabilising,
  the service is marked failed and left alone. Restarting forever hides the
  problem; stopping makes it visible, and `/health` says which one.
* **State is written to disk** every cycle so the `/health` phone command can
  read it from the remote listener, which is itself one of the children.

Run:  python -m app.cli up
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config, notify

STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "supervisor.json"

BACKOFF_START = 1.0          # seconds before the first restart
BACKOFF_CAP = 60.0
STABLE_AFTER = 60.0          # up this long = recovered, reset the counters
MAX_FAILURES = 5             # restarts without stabilising before giving up
POLL = 1.0                   # how often the parent checks its children
STOP_GRACE = 10.0            # seconds to wait for a clean exit before killing


@dataclass(frozen=True)
class Service:
    """One supervised child. `args` are appended to `python -m app.cli`."""
    name: str
    args: tuple[str, ...]
    blurb: str
    critical: bool = True        # False = its death is worth a line, not a push


SERVICES: tuple[Service, ...] = (
    Service("server", ("serve",), "web UI on :5000", critical=False),
    Service("watcher", ("intraday", "run"), "intraday alert engine"),
    Service("events", ("events", "run"), "corporate events + scheduled pushes"),
    Service("remote", ("remote",), "phone command listener"),
)


@dataclass
class Child:
    """Live state for one supervised service."""
    service: Service
    proc: object | None = None
    started_at: float = 0.0
    restarts: int = 0
    failures: int = 0            # consecutive, since the last stable run
    backoff: float = BACKOFF_START
    retry_at: float = 0.0
    last_exit: int | None = None
    state: str = "starting"      # starting | up | restarting | failed | stopped
    notified_down: bool = False

    def uptime(self, now: float) -> float:
        return (now - self.started_at) if self.state == "up" else 0.0


def _spawn(service: Service):
    """Launch one child as its own `python -m app.cli ...` process.

    stderr is folded into stdout so the pump below sees one ordered stream;
    interleaving two pipes gives you lines out of order at the worst moment.
    """
    return subprocess.Popen(
        [sys.executable, "-m", "app.cli", *service.args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def _pump(name: str, stream, out=None) -> threading.Thread:
    """Forward a child's output to our own, tagged.

    Four services sharing one terminal is unreadable without this. The thread
    is a daemon: when the parent goes, it goes.
    """
    out = out or sys.stdout

    def run():
        try:
            for line in iter(stream.readline, ""):
                out.write(f"[{name}] {line.rstrip()}\n")
                out.flush()
        except (ValueError, OSError):
            pass                                  # stream closed under us

    t = threading.Thread(target=run, name=f"pump-{name}", daemon=True)
    t.start()
    return t


class Supervisor:
    """Keeps the children alive and the phone informed.

    `spawn` and `pusher` are injected so the whole restart policy can be
    tested without launching real processes or sending real notifications.
    """

    def __init__(self, services=SERVICES, spawn=_spawn, pusher=None,
                 state_path: Path | None = None, pump=_pump):
        self.children = {s.name: Child(service=s) for s in services}
        self._spawn = spawn
        self._pump = pump
        self._push = pusher if pusher is not None else self._default_push
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self.stopping = False
        self.started_at = time.time()

    # ------------------------------------------------------------- pushing
    @staticmethod
    def _default_push(message: str) -> None:
        if not config.get("NTFY_TOPIC", ""):
            return
        try:
            notify.send(message, title="TickerForge supervisor")
        except (Exception, SystemExit) as e:      # noqa: BLE001
            # SystemExit is deliberate. notify.send exits the process when the
            # topic is unusable, which is correct for a one-shot CLI command
            # and catastrophic here: the supervisor is the parent of
            # everything, and a failed push must never take the services down
            # with it. A notification we couldn't send is a line of output,
            # not an outage.
            print(f"[supervisor] could not push: {e}")

    def _say(self, message: str, push: bool = False) -> None:
        print(f"[supervisor] {message}")
        if push:
            self._push(message)

    # -------------------------------------------------------------- start
    def start_child(self, child: Child, now: float | None = None) -> None:
        # `now` is threaded through rather than read here so the whole
        # supervisor works off ONE clock. Mixing time.time() with a caller's
        # notion of now means started_at and the stability check disagree,
        # and a restarted service never counts as recovered.
        now = time.time() if now is None else now
        try:
            child.proc = self._spawn(child.service)
        except OSError as e:
            child.state = "failed"
            child.last_exit = None
            self._say(f"{child.service.name} could not be launched: {e}",
                      push=child.service.critical)
            return
        child.started_at = now
        child.state = "up"
        stream = getattr(child.proc, "stdout", None)
        if stream is not None and self._pump is not None:
            self._pump(child.service.name, stream)

    def start(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        # The parent's own clock starts here too, for the same reason the
        # children's does: one notion of now, or the uptime it reports is
        # measured against a different one than everything else.
        self.started_at = now
        for child in self.children.values():
            self.start_child(child, now)
            self._say(f"{child.service.name} started — {child.service.blurb}")
        self.write_state()

    # --------------------------------------------------------------- tick
    def tick(self, now: float | None = None) -> None:
        """One supervision cycle. Pure enough to call directly from a test."""
        now = time.time() if now is None else now
        for child in self.children.values():
            if child.state == "failed" or child.state == "stopped":
                continue

            if child.state == "restarting":
                if now >= child.retry_at:
                    self.start_child(child, now)
                    child.restarts += 1
                    if child.state == "up":
                        self._say(f"{child.service.name} restarted "
                                  f"(attempt {child.failures})")
                continue

            code = child.proc.poll() if child.proc is not None else -1
            if code is None:
                # still running — has it earned a clean slate?
                if (child.failures and
                        now - child.started_at >= STABLE_AFTER):
                    child.failures = 0
                    child.backoff = BACKOFF_START
                    if child.notified_down:
                        child.notified_down = False
                        self._say(f"{child.service.name} is stable again",
                                  push=child.service.critical)
                continue

            # it died
            child.last_exit = code
            child.failures += 1
            if child.failures >= MAX_FAILURES:
                child.state = "failed"
                self._say(
                    f"{child.service.name} has died {child.failures} times "
                    f"(last exit {code}) — giving up. Alerts from this service "
                    f"are OFF until you restart it.",
                    push=True)
                continue

            child.state = "restarting"
            child.retry_at = now + child.backoff
            # Only the FIRST death in a run gets a push. A crash loop already
            # gets its own message when it gives up; announcing every attempt
            # would turn a broken service into a broken phone.
            first = not child.notified_down
            child.notified_down = True
            self._say(
                f"{child.service.name} exited ({code}) — restarting in "
                f"{child.backoff:.0f}s",
                push=first and child.service.critical)
            child.backoff = min(child.backoff * 2, BACKOFF_CAP)
        self.write_state()

    # -------------------------------------------------------------- state
    def health(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {
            "updated": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "uptime_s": round(now - self.started_at, 1),
            "ok": all(c.state == "up" for c in self.children.values()),
            "services": [
                {
                    "name": c.service.name,
                    "blurb": c.service.blurb,
                    "state": c.state,
                    "pid": getattr(c.proc, "pid", None),
                    "uptime_s": round(c.uptime(now), 1),
                    "restarts": c.restarts,
                    "last_exit": c.last_exit,
                }
                for c in self.children.values()
            ],
        }

    def write_state(self) -> None:
        """Atomically, because /health may read it mid-write.

        The temp name carries our pid. With a shared name, two supervisors
        writing at once each clobber the other's temp file and then one of the
        renames finds nothing left to rename — which surfaces as a confusing
        "No such file" on a path you can see with your own eyes. Per-process
        names make the write safe; `claim()` is what stops there being two
        supervisors in the first place.
        """
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(
                f"{self.state_path.stem}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.health(), indent=2))
            tmp.replace(self.state_path)
        except OSError as e:
            print(f"[supervisor] could not write state: {e}")

    # --------------------------------------------------------------- stop
    def stop(self) -> None:
        self.stopping = True
        self._say("stopping children…")
        for child in self.children.values():
            proc = child.proc
            if proc is None or proc.poll() is not None:
                child.state = "stopped"
                continue
            try:
                proc.terminate()
            except OSError:
                pass
        deadline = time.time() + STOP_GRACE
        for child in self.children.values():
            proc = child.proc
            if proc is None:
                continue
            remaining = max(0.0, deadline - time.time())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._say(f"{child.service.name} ignored SIGTERM — killing")
                try:
                    proc.kill()
                except OSError:
                    pass
            except OSError:
                pass
            child.state = "stopped"
        self.write_state()
        self._say("all stopped")

    # ---------------------------------------------------------------- run
    def claim(self) -> int | None:
        """Refuse to be the second supervisor.

        Two supervisors means two of every service: two watchers double-firing
        every alert, two remote listeners each answering the same phone
        command, two events pollers racing on the same dedupe table. The
        state-file collision that gives this away is the least of it.
        """
        other = running_elsewhere(self.state_path)
        if other:
            from .service import UP_LABEL
            self._say(
                f"another supervisor is already running (pid {other}).\n"
                f"  Two would double every alert and answer every phone "
                f"command twice.\n"
                f"  Stop that one first, or if it's a launchd agent:\n"
                f"    launchctl unload -w "
                f"~/Library/LaunchAgents/{UP_LABEL}.plist")
        return other

    def run(self) -> int:
        if self.claim():
            return 1

        def handler(signum, frame):               # noqa: ARG001
            self.stopping = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass                              # not the main thread

        self._say(f"starting {len(self.children)} services "
                  f"(pid {os.getpid()}) — Ctrl-C to stop")
        self.start()
        try:
            while not self.stopping:
                self.tick()
                time.sleep(POLL)
        finally:
            self.stop()
        return 0


# ------------------------------------------------------------- readers
def alive(pid: int | None) -> bool:
    """Is that pid a running process?

    Signal 0 checks for existence without delivering anything. A permission
    error still means it exists — owned by someone else — so that counts.
    """
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def running_elsewhere(path: Path | None = None) -> int | None:
    """The pid of another live supervisor, or None."""
    state = read_state(path)
    if not state:
        return None
    pid = state.get("pid")
    if pid == os.getpid() or not alive(pid):
        return None
    return pid


def read_state(path: Path | None = None) -> dict | None:
    """What `/health` and `status` read. None means the supervisor isn't up."""
    path = Path(path) if path else STATE_PATH
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def format_health(state: dict | None, stale_after: float = 30.0) -> str:
    """Phone-readable. Leads with the verdict, because that's the question."""
    if state is None:
        return ("Supervisor not running — services are unmanaged.\n"
                "Start it with: python -m app.cli up")

    lines = []
    try:
        age = (datetime.now()
               - datetime.fromisoformat(state["updated"])).total_seconds()
    except (KeyError, ValueError):
        age = None

    if age is not None and age > stale_after:
        # A stale file is the supervisor itself having died, which is the one
        # failure nothing else can report.
        lines.append(f"⚠ supervisor state is {int(age)}s old — it may be dead.")

    services = state.get("services", [])
    down = [s for s in services if s["state"] != "up"]
    lines.append("All services up." if not down
                 else f"{len(down)} of {len(services)} services not running.")

    for s in services:
        mark = {"up": "•", "restarting": "↻", "failed": "✕"}.get(s["state"], "?")
        bit = f"{mark} {s['name']}: {s['state']}"
        if s["state"] == "up":
            bit += f" ({_dur(s['uptime_s'])})"
        if s.get("restarts"):
            bit += f", {s['restarts']} restart" + ("s" if s["restarts"] > 1 else "")
        if s.get("last_exit") is not None and s["state"] != "up":
            bit += f", last exit {s['last_exit']}"
        lines.append(bit)

    lines.append(f"supervisor up {_dur(state.get('uptime_s', 0))}")
    return "\n".join(lines)


def _dur(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


def main() -> int:
    return Supervisor().run()
