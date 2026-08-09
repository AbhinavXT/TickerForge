"""The two surfaces nothing else exercises: the CLI and the route table.

Both duplicate-registration bugs this file guards against shipped in a repo
with 481 passing tests, because every one of those tests imports a *module*.
Nobody ever ran `main()`, and nobody ever asked FastAPI what it had registered.

The CLI failure was total — a duplicate `walkforward` subparser made argparse
raise before it parsed a single argument, so `login`, `serve`, `watch` and
everything else died on startup. The server one was silent: FastAPI keeps the
first route registered for a path and ignores the second, so edits to the
wrong copy simply had no effect.

Neither needs a Kite session, a network, or a database. `--help` walks the
whole parser tree, which also catches the other argparse trap: help strings
are %-formatted, so a literal `%` raises ValueError unless written `%%`.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest


# ------------------------------------------------------------------- CLI
def _run_cli(argv: list[str]) -> None:
    """Invoke the CLI as the shell would. `--help` always exits 0."""
    import sys

    from app import cli

    old = sys.argv
    sys.argv = ["tickerforge", *argv]
    try:
        cli.main()
    finally:
        sys.argv = old


def _subcommands(capsys) -> list[str]:
    """Read the subcommand list out of the top-level usage line.

    Derived rather than hardcoded so a newly added subcommand is covered the
    day it lands, which is the day its help string is most likely to be wrong.
    """
    with pytest.raises(SystemExit) as e:
        _run_cli(["--help"])
    assert e.value.code == 0
    text = capsys.readouterr().out
    m = re.search(r"\{([a-z0-9,\-]+)\}", text)
    assert m, f"no subcommand list found in usage:\n{text}"
    return m.group(1).split(",")


def test_cli_help_runs(capsys):
    """`python -m app.cli --help` must not crash. It did."""
    with pytest.raises(SystemExit) as e:
        _run_cli(["--help"])
    assert e.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_every_subcommand_help_runs(capsys):
    subs = _subcommands(capsys)
    assert len(subs) > 5, f"suspiciously few subcommands: {subs}"

    broken = []
    for name in subs:
        try:
            with pytest.raises(SystemExit) as e:
                _run_cli([name, "--help"])
            if e.value.code != 0:
                broken.append(f"{name}: exit {e.value.code}")
        except Exception as exc:                      # noqa: BLE001
            broken.append(f"{name}: {type(exc).__name__}: {exc}")
        capsys.readouterr()                           # drain

    assert not broken, "subcommand --help failed:\n  " + "\n  ".join(broken)


def test_no_duplicate_subparsers():
    """A repeated `add_parser(name)` raises at build time — catch it directly.

    The `--help` tests above already fail on this, but they fail with an
    argparse traceback that doesn't name the culprit. This one does.
    """
    import argparse
    import sys

    # Keyed per PARENT, not globally: `list`, `run`, `show` and `digest` are
    # all legitimately reused under different commands (`watch list` and
    # `journal list` are different things). Only a collision within the same
    # subparser group is the bug.
    seen: list[tuple[int, str]] = []
    original = argparse._SubParsersAction.add_parser

    def spy(self, name, **kw):
        seen.append((id(self), name))
        return original(self, name, **kw)

    from app import cli

    argparse._SubParsersAction.add_parser = spy
    old = sys.argv
    sys.argv = ["tickerforge", "--help"]
    try:
        with pytest.raises(SystemExit):
            cli.main()
    finally:
        argparse._SubParsersAction.add_parser = original
        sys.argv = old

    dupes = sorted(name for key, count in Counter(seen).items() if count > 1
                   for name in [key[1]])
    assert not dupes, (f"subcommand registered more than once under the same "
                       f"parent: {dupes}")


# ----------------------------------------------------------------- routes
def test_no_duplicate_routes():
    """FastAPI keeps the FIRST handler for a path and silently drops the rest.

    So a duplicated route isn't an error, it's a trap: the dead copy looks
    live in the file and ignores every edit you make to it.
    """
    from app.server import app

    paths = [(r.path, tuple(sorted(r.methods)))
             for r in app.routes if hasattr(r, "methods")]
    dupes = [p for p, count in Counter(paths).items() if count > 1]
    assert not dupes, f"route registered more than once: {dupes}"


def test_routes_registered():
    """Guard the guard: if the app failed to import routes, the test above
    would pass vacuously on an empty list."""
    from app.server import app

    paths = {r.path for r in app.routes if hasattr(r, "methods")}
    assert len(paths) > 40
    for expected in ("/api/candles", "/api/journal", "/api/backtest",
                     "/api/backtest/walkforward", "/api/scanner"):
        assert expected in paths, f"{expected} is missing from the route table"


# ------------------------------------------------------------ undefined names
def test_no_undefined_names_anywhere():
    """A name that is never imported is a crash waiting for the one branch
    that reaches it.

    `--help` walks the whole parser tree but executes none of the dispatch
    bodies, so the tests above could pass while `intraday run` died instantly
    on a missing import — which is exactly what happened. Static analysis
    reaches the code paths that only run on a live market with a real session.

    This is the same idea as _systemexit_audit.py: some bugs are cheaper to
    prove absent than to test for.
    """
    import shutil
    import subprocess
    import sys

    if not shutil.which(sys.executable):
        pytest.skip("no interpreter to run pyflakes with")
    probe = subprocess.run([sys.executable, "-m", "pyflakes", "--version"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("pyflakes not installed (pip install -r requirements-dev.txt)")

    app = Path(__file__).resolve().parents[1] / "app"
    r = subprocess.run(
        [sys.executable, "-m", "pyflakes", *sorted(str(p) for p in app.glob("*.py"))],
        capture_output=True, text=True)
    bad = [ln for ln in r.stdout.splitlines() if "undefined name" in ln]
    assert not bad, "undefined names:\n  " + "\n  ".join(bad)


def test_every_dispatch_branch_names_a_real_module():
    """Cheap belt-and-braces if pyflakes isn't installed.

    Checks that every `X.y(...)` call in cli.main() refers to something the
    module actually binds — the specific shape of the bug that shipped.
    """
    import ast

    cli_path = Path(__file__).resolve().parents[1] / "app" / "cli.py"
    tree = ast.parse(cli_path.read_text())

    import builtins
    bound = set(dir(builtins)) | {"self"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)

    missing = set()
    for node in ast.walk(tree):
        # the `module.function(...)` shape specifically
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id not in bound):
            missing.add(node.func.value.id)

    assert not missing, f"cli.py calls into names it never binds: {sorted(missing)}"


# ------------------------------------------------------------ documentation
def test_commands_doc_names_no_command_that_does_not_exist():
    """A reference that lists a command you can't run is worse than no
    reference — you'd conclude the tool is broken rather than the doc."""
    import re

    from app import commands

    doc = (Path(__file__).resolve().parents[1] / "COMMANDS.md").read_text()
    cited = set(re.findall(r"`/([a-z?]+)", doc))
    real = set(commands.HANDLERS) | set(commands.MULTI_ARG) | {
        "help", "commands", "?"}
    assert cited <= real, f"documented but missing: {sorted(cited - real)}"


def test_every_phone_command_is_documented():
    """The reverse gap: a command nobody knows about may as well not exist."""
    import re

    from app import commands

    doc = (Path(__file__).resolve().parents[1] / "COMMANDS.md").read_text()
    cited = set(re.findall(r"`/([a-z?]+)", doc))
    real = set(commands.HANDLERS) | set(commands.MULTI_ARG)
    assert real <= cited | {"?"}, f"undocumented: {sorted(real - cited)}"


def test_commands_doc_lists_every_strategy_and_condition():
    from app import backtest, scanner

    doc = (Path(__file__).resolve().parents[1] / "COMMANDS.md").read_text()
    for key in backtest.STRATEGIES:
        assert key in doc, f"strategy {key} is not in COMMANDS.md"
    for key in scanner.CONDITIONS:
        assert key in doc, f"scan condition {key} is not in COMMANDS.md"
