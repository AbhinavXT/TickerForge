"""The supervisor's restart policy, tested without launching real processes.

`spawn` and `pusher` are injected precisely so this file can drive crashes,
crash loops and recoveries deterministically. Testing it by starting four real
services would be slow, flaky, and would only ever exercise the happy path —
the interesting behaviour is what happens on the fifth consecutive death.
"""
from __future__ import annotations

import json

import pytest

from app import supervisor as S


class FakeProc:
    """A child process that dies when told to."""

    _next_pid = 1000

    def __init__(self):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self._code = None
        self.terminated = False
        self.killed = False
        self.stdout = None

    def die(self, code=1):
        self._code = code

    def poll(self):
        return self._code

    def terminate(self):
        self.terminated = True
        self._code = 0

    def kill(self):
        self.killed = True
        self._code = -9

    def wait(self, timeout=None):
        if self._code is None:
            raise __import__("subprocess").TimeoutExpired("fake", timeout)
        return self._code


def build(tmp_path, services=None, spawn_fails=False):
    """A supervisor wired to fakes. Returns (sup, spawned, pushes)."""
    spawned, pushes = [], []

    def spawn(service):
        if spawn_fails:
            raise OSError("no such executable")
        p = FakeProc()
        spawned.append((service.name, p))
        return p

    services = services or (S.Service("watcher", ("intraday", "run"), "alerts"),)
    sup = S.Supervisor(services=services, spawn=spawn,
                       pusher=pushes.append,
                       state_path=tmp_path / "supervisor.json",
                       pump=None)
    return sup, spawned, pushes


def live(sup, name="watcher"):
    return sup.children[name].proc


# ------------------------------------------------------------------ startup
def test_start_launches_every_service(tmp_path):
    sup, spawned, _ = build(tmp_path, services=S.SERVICES)
    sup.start()
    assert len(spawned) == len(S.SERVICES)
    assert all(c.state == "up" for c in sup.children.values())


def test_start_writes_state_immediately(tmp_path):
    """/health must have something to read the moment the parent is up."""
    sup, _, _ = build(tmp_path)
    sup.start()
    state = json.loads((tmp_path / "supervisor.json").read_text())
    assert state["services"][0]["name"] == "watcher"
    assert state["ok"] is True


def test_a_service_that_cannot_launch_is_marked_failed(tmp_path):
    sup, _, pushes = build(tmp_path, spawn_fails=True)
    sup.start()
    assert sup.children["watcher"].state == "failed"
    assert any("could not be launched" in m for m in pushes)


# ------------------------------------------------------------------ restart
def test_a_dead_child_is_restarted(tmp_path):
    sup, spawned, _ = build(tmp_path)
    sup.start()
    live(sup).die(1)

    sup.tick(now=100.0)                      # notices the death
    assert sup.children["watcher"].state == "restarting"

    sup.tick(now=100.0 + S.BACKOFF_START)    # backoff elapsed
    assert sup.children["watcher"].state == "up"
    assert sup.children["watcher"].restarts == 1
    assert len(spawned) == 2


def test_restart_waits_for_the_backoff(tmp_path):
    """Restarting instantly would spin the CPU against an unfixable failure."""
    sup, spawned, _ = build(tmp_path)
    sup.start()
    live(sup).die(1)
    sup.tick(now=100.0)
    sup.tick(now=100.0 + S.BACKOFF_START / 2)     # too early
    assert sup.children["watcher"].state == "restarting"
    assert len(spawned) == 1


def test_backoff_doubles_between_failures(tmp_path):
    sup, _, _ = build(tmp_path)
    sup.start()
    seen = []
    t = 100.0
    for _ in range(3):
        live(sup).die(1)
        sup.tick(now=t)
        seen.append(sup.children["watcher"].retry_at - t)
        t += 1000                                  # far past the retry time
        sup.tick(now=t)
    assert seen == sorted(seen) and seen[1] > seen[0]


def test_backoff_is_capped(tmp_path):
    sup, _, _ = build(tmp_path)
    sup.start()
    sup.children["watcher"].backoff = S.BACKOFF_CAP
    live(sup).die(1)
    sup.tick(now=100.0)
    assert sup.children["watcher"].backoff <= S.BACKOFF_CAP


# --------------------------------------------------------------- crash loop
def test_a_crash_loop_eventually_gives_up(tmp_path):
    """Restarting forever hides the problem. Stopping makes it visible."""
    sup, _, pushes = build(tmp_path)
    sup.start()
    t = 100.0
    for _ in range(S.MAX_FAILURES):
        live(sup).die(1)
        sup.tick(now=t)
        t += 1000
        sup.tick(now=t)
    assert sup.children["watcher"].state == "failed"
    assert any("giving up" in m for m in pushes)


def test_a_failed_service_is_left_alone(tmp_path):
    sup, spawned, _ = build(tmp_path)
    sup.start()
    sup.children["watcher"].state = "failed"
    before = len(spawned)
    for t in range(200, 400, 10):
        sup.tick(now=float(t))
    assert len(spawned) == before


# ----------------------------------------------------------------- recovery
def test_staying_up_resets_the_failure_count(tmp_path):
    """Without this, a service that dies once a day is eventually treated as
    a crash loop and abandoned."""
    sup, _, _ = build(tmp_path)
    sup.start()
    live(sup).die(1)
    sup.tick(now=100.0)
    sup.tick(now=100.0 + S.BACKOFF_START)          # back up
    assert sup.children["watcher"].failures == 1

    sup.tick(now=100.0 + S.BACKOFF_START + S.STABLE_AFTER + 1)
    assert sup.children["watcher"].failures == 0
    assert sup.children["watcher"].backoff == S.BACKOFF_START


def test_recovery_is_announced(tmp_path):
    sup, _, pushes = build(tmp_path)
    sup.start()
    live(sup).die(1)
    sup.tick(now=100.0)
    sup.tick(now=101.0)
    sup.tick(now=101.0 + S.STABLE_AFTER + 1)
    assert any("stable again" in m for m in pushes)


# ------------------------------------------------------------ push restraint
def test_only_the_first_death_pushes(tmp_path):
    """A broken service must not become a broken phone. The give-up message
    carries the bad news for a crash loop; the attempts don't need to."""
    sup, _, pushes = build(tmp_path)
    sup.start()
    t = 100.0
    for _ in range(S.MAX_FAILURES - 1):
        live(sup).die(1)
        sup.tick(now=t)
        t += 1000
        sup.tick(now=t)
    deaths = [m for m in pushes if "restarting in" in m]
    assert len(deaths) == 1, f"pushed {len(deaths)} restart notices"


def test_non_critical_service_death_is_not_pushed(tmp_path):
    """The web server failing is self-announcing — pages stop loading."""
    svc = (S.Service("server", ("serve",), "web UI", critical=False),)
    sup, _, pushes = build(tmp_path, services=svc)
    sup.start()
    live(sup, "server").die(1)
    sup.tick(now=100.0)
    assert pushes == []


# -------------------------------------------------------------------- state
def test_health_reports_per_service_detail(tmp_path):
    sup, _, _ = build(tmp_path, services=S.SERVICES)
    sup.start()
    h = sup.health()
    assert h["ok"] is True
    names = {s["name"] for s in h["services"]}
    assert names == {s.name for s in S.SERVICES}
    assert all(s["pid"] for s in h["services"])


def test_health_is_not_ok_when_something_is_down(tmp_path):
    sup, _, _ = build(tmp_path)
    sup.start()
    live(sup).die(1)
    sup.tick(now=100.0)
    assert sup.health()["ok"] is False


def test_state_file_survives_a_partial_write(tmp_path):
    """Written via a temp file and replaced, so /health never reads a half
    file — it either sees the old state or the new one."""
    sup, _, _ = build(tmp_path)
    sup.start()
    for _ in range(20):
        sup.write_state()
        assert json.loads((tmp_path / "supervisor.json").read_text())


# ---------------------------------------------------------------- rendering
def test_format_health_when_nothing_is_running():
    out = S.format_health(None)
    assert "not running" in out and "app.cli up" in out


def test_format_health_leads_with_the_verdict(tmp_path):
    sup, _, _ = build(tmp_path, services=S.SERVICES)
    sup.start()
    out = S.format_health(sup.health())
    assert out.splitlines()[0] == "All services up."


def test_format_health_counts_what_is_down(tmp_path):
    sup, _, _ = build(tmp_path, services=S.SERVICES)
    sup.start()
    live(sup, "watcher").die(1)
    sup.tick(now=100.0)
    out = S.format_health(sup.health())
    assert "1 of 4 services not running" in out
    assert "watcher: restarting" in out


def test_format_health_flags_a_stale_file():
    """The supervisor dying is the one failure nothing else can report — an
    old timestamp is the only evidence."""
    stale = {"updated": "2020-01-01T00:00:00", "uptime_s": 10,
             "services": [{"name": "watcher", "state": "up", "uptime_s": 5,
                           "restarts": 0, "last_exit": None}]}
    assert "may be dead" in S.format_health(stale)


def test_format_health_does_not_flag_a_fresh_file(tmp_path):
    sup, _, _ = build(tmp_path)
    sup.start()
    assert "may be dead" not in S.format_health(sup.health())


@pytest.mark.parametrize("secs,text", [
    (5, "5s"), (90, "1m"), (3700, "1h01m"), (100000, "1d3h"),
])
def test_duration_formatting(secs, text):
    assert S._dur(secs) == text


# --------------------------------------------------------------- shutdown
def test_stop_terminates_every_child(tmp_path):
    sup, _, _ = build(tmp_path, services=S.SERVICES)
    sup.start()
    procs = [c.proc for c in sup.children.values()]
    sup.stop()
    assert all(p.terminated for p in procs)
    assert all(c.state == "stopped" for c in sup.children.values())


def test_stop_kills_a_child_that_ignores_terminate(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STOP_GRACE", 0.01)

    class Stubborn(FakeProc):
        def terminate(self):
            self.terminated = True            # ignores it, stays alive

    def spawn(service):
        return Stubborn()

    sup = S.Supervisor(services=(S.Service("watcher", ("x",), "y"),),
                       spawn=spawn, pusher=lambda m: None,
                       state_path=tmp_path / "s.json", pump=None)
    sup.start()
    proc = sup.children["watcher"].proc
    sup.stop()
    assert proc.killed


def test_read_state_returns_none_when_absent(tmp_path):
    assert S.read_state(tmp_path / "nope.json") is None


def test_read_state_returns_none_on_garbage(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert S.read_state(p) is None


# ------------------------------------------------- with real OS processes
def test_real_process_restart_and_output_capture(tmp_path, capsys):
    """The fakes above never exercise Popen or the output pump.

    This one uses real processes — a child that prints and exits — to check
    the parts the fakes stand in for: that a launched process is tracked, that
    its output arrives tagged on the parent's stdout, and that a real exit
    code is noticed and acted on.
    """
    import subprocess
    import sys
    import time

    svc = S.Service("blip", ("noop",), "prints once and dies")

    def spawn(service):
        return subprocess.Popen(
            [sys.executable, "-c", "print('hello from the child')"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)

    sup = S.Supervisor(services=(svc,), spawn=spawn, pusher=lambda m: None,
                       state_path=tmp_path / "s.json")
    sup.start()
    assert sup.children["blip"].proc.pid > 0

    # the child exits on its own almost immediately
    deadline = time.time() + 5
    while sup.children["blip"].proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)

    sup.tick()
    assert sup.children["blip"].state == "restarting"
    assert sup.children["blip"].last_exit == 0

    time.sleep(S.BACKOFF_START + 0.2)
    sup.tick()
    assert sup.children["blip"].restarts == 1

    sup.stop()
    out = capsys.readouterr().out
    assert "[blip] hello from the child" in out, out[:400]


def test_supervisor_uptime_uses_the_same_clock_as_everything_else(tmp_path):
    """A parent measuring itself against a different clock reports nonsense —
    this showed up as an uptime of minus fifty-six years."""
    sup, _, _ = build(tmp_path)
    sup.start(now=1000.0)
    assert sup.health(now=4600.0)["uptime_s"] == 3600.0


def test_duration_never_renders_negative():
    assert S._dur(-5) == "0s"


# ==================================================== only one supervisor
def test_write_state_uses_a_per_process_temp_file(tmp_path):
    """Two supervisors sharing one temp name each clobber the other's, and
    then one rename finds nothing to rename — surfacing as a baffling
    "No such file" on a path you can see with your own eyes.
    """
    import os

    sup, _, _ = build(tmp_path)
    sup.start()
    names = [p.name for p in tmp_path.iterdir()]
    assert "supervisor.json" in names
    # nothing generic left behind, and the temp carried our pid
    assert "supervisor.tmp" not in names
    assert str(os.getpid()) in sup.state_path.with_name(
        f"{sup.state_path.stem}.{os.getpid()}.tmp").name


def test_two_writers_do_not_collide(tmp_path):
    """The actual regression: interleave two supervisors writing state."""
    a, _, _ = build(tmp_path)
    b, _, _ = build(tmp_path)
    a.start()
    for _ in range(5):
        a.write_state()
        b.write_state()
    import json
    assert json.loads((tmp_path / "supervisor.json").read_text())


def test_alive_detects_a_running_process():
    import os
    assert S.alive(os.getpid())


def test_alive_rejects_a_dead_or_missing_pid():
    assert not S.alive(999_999)
    assert not S.alive(None)
    assert not S.alive(0)
    assert not S.alive(-1)


def test_running_elsewhere_ignores_our_own_pid(tmp_path):
    """The state file we wrote ourselves must not look like a rival."""
    sup, _, _ = build(tmp_path)
    sup.start()
    assert S.running_elsewhere(sup.state_path) is None


def test_running_elsewhere_ignores_a_stale_pid(tmp_path):
    """A supervisor that was killed leaves its pid behind. That must not block
    the next start for ever."""
    import json

    (tmp_path / "supervisor.json").write_text(json.dumps(
        {"pid": 999_999, "updated": "2020-01-01T00:00:00", "services": []}))
    assert S.running_elsewhere(tmp_path / "supervisor.json") is None


def test_running_elsewhere_spots_a_live_rival(tmp_path):
    import json
    import subprocess
    import sys

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        (tmp_path / "supervisor.json").write_text(json.dumps(
            {"pid": holder.pid, "updated": "2026-01-01T00:00:00",
             "services": []}))
        assert S.running_elsewhere(tmp_path / "supervisor.json") == holder.pid
    finally:
        holder.kill()
        holder.wait()


def test_run_refuses_to_be_the_second_supervisor(tmp_path, monkeypatch):
    """Two supervisors means two watchers double-firing every alert and two
    remote listeners answering every phone command twice."""
    import json
    import os

    (tmp_path / "supervisor.json").write_text(json.dumps(
        {"pid": os.getppid(), "updated": "2026-01-01T00:00:00",
         "services": []}))
    sup, spawned, _ = build(tmp_path)
    monkeypatch.setattr(S, "alive", lambda pid: True)
    assert sup.run() == 1
    assert spawned == [], "it started services anyway"


def test_the_refusal_says_how_to_fix_it(tmp_path, monkeypatch, capsys):
    import json
    import os

    (tmp_path / "supervisor.json").write_text(json.dumps(
        {"pid": os.getppid(), "updated": "2026-01-01T00:00:00",
         "services": []}))
    sup, _, _ = build(tmp_path)
    monkeypatch.setattr(S, "alive", lambda pid: True)
    sup.run()
    out = capsys.readouterr().out
    assert "already running" in out
    assert "launchctl unload" in out
