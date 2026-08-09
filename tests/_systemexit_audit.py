"""Audit: `try` blocks that can raise SystemExit but only catch `Exception`.

`config.require()` raises SystemExit when a key is missing, and it sits behind
`kite_auth.get_kite()` and `notify.send()` — the two things nearly every
long-running path touches. SystemExit inherits from BaseException, so
`except Exception` does not catch it.

The failure mode is nasty because it's inverted: these blocks were written
defensively *specifically so they wouldn't crash*, and they crash only in the
exact conditions they were meant to survive — an expired token in the morning,
or an unconfigured ntfy topic.

A block counts as safe if any handler names SystemExit or BaseException (the
server routes use that to convert it into a 401), or if it's a bare `except:`.
"""
from __future__ import annotations

import ast
import pathlib

RISKY = (
    "get_kite", "load_token",
    "notify.send", "notify.send_image", "send_image(",
    "config.require",
    "fetch_and_store", "fetch_all",
    "signals.scan", "summary_text",
    "backtest.run", "instruments.sync", "poll_once",
    "maybe_eod_scan", "notify_if_breached",
)

SAFE_NAMES = {"SystemExit", "BaseException"}


def _handler_names(handler: ast.ExceptHandler) -> list[str]:
    t = handler.type
    if t is None:
        return ["BaseException"]          # bare `except:` catches everything
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, ast.Tuple):
        return [e.id for e in t.elts if isinstance(e, ast.Name)]
    return []


def audit(root: str = "app") -> list[tuple[str, int, str]]:
    """-> [(file, lineno, which risky call)] for each unsafe block."""
    out = []
    for path in sorted(pathlib.Path(root).glob("*.py")):
        src = path.read_text()
        lines = src.splitlines()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Try) or not node.handlers:
                continue
            body = "\n".join(
                lines[node.body[0].lineno - 1:node.body[-1].end_lineno])
            call = next((r for r in RISKY if r in body), None)
            if not call:
                continue
            names = [n for h in node.handlers for n in _handler_names(h)]
            if SAFE_NAMES & set(names):
                continue
            if "Exception" in names:
                out.append((str(path), node.handlers[0].lineno,
                            call.rstrip("(")))
    return out


if __name__ == "__main__":
    hits = audit()
    for f, ln, c in hits:
        print(f"{f}:{ln}  wraps {c}")
    print(f"\n{len(hits)} unsafe site(s)")
