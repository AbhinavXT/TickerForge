"""Settings you can change without SSH-ing in to edit `.env`.

An override lives in SQLite under `setting:<KEY>` and wins over `.env`, which
in turn wins over the built-in default. Three layers, and the page shows which
one is answering — that provenance is the point. A risk percentage you set in
the browser six months ago, silently overriding the `.env` you're staring at,
is exactly the kind of thing that makes you distrust the whole tool.

Overrides are deliberately a small allowlist. Anything that decides how to
reach an external service — API keys, the ntfy topic, the command PIN — stays
in `.env` only. A browser page with no authentication is not where the
credentials should live, and a settings form that can retarget the notification
channel is a settings form that can silently redirect your alerts.

Reverting is first-class: clearing an override drops back to `.env` rather
than writing `.env`'s value into the database. Otherwise the two copies drift
and neither is the truth.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config, db

PREFIX = "setting:"


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str                      # number | text | choice
    group: str
    help: str
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None


SETTINGS: tuple[Setting, ...] = (
    # ---- what's allowed to interrupt you
    Setting("EVENT_PUSH_CATEGORIES", "Event pushes", "text", "Notifications",
            "Which corporate events buzz, comma-separated: RESULTS, "
            "CORP ACTION, DIVIDEND, CONCALL. `all` for everything. Holding a "
            "stock used to subscribe you to its entire paperwork trail; this "
            "is what stops that."),
    Setting("EVENT_PUSH_HORIZON_DAYS", "Event horizon (days)", "number",
            "Notifications",
            "How far ahead a dated event may be and still buzz. 0 means "
            "today only. A board meeting three weeks out is a calendar entry, "
            "not a notification \u2014 it still appears in the morning brief "
            "the day before.", minimum=0, maximum=30, step=1),

    # ---- the self-executing paper account
    Setting("PAPER_AUTO", "Paper autopilot", "choice", "Paper",
            "Queue tomorrow's orders from today's close, automatically, and "
            "fill them at the open. Off by default \u2014 a paper account "
            "that starts trading because you updated the code is a surprise, "
            "even with play money.", choices=("0", "1")),
    Setting("PAPER_STRATEGIES", "Paper strategies", "text", "Paper",
            "Comma-separated strategy keys, e.g. rsi_revert,sma_cross. "
            "`signals` means follow whatever the signal scan is set to, which keeps the "
            "paper trades and the alerts you receive in agreement about what "
            "fired."),
    Setting("PAPER_STOP_PCT", "Paper stop (%)", "number", "Paper",
            "Initial stop below entry, as a percent. It sizes the position "
            "and it is enforced \u2014 a position that trades through it is "
            "closed on the next check, at the open if the day gapped past it.",
            minimum=0.5, maximum=30, step=0.5),
    Setting("PAPER_MAX_POSITIONS", "Paper max positions", "number", "Paper",
            "How many paper positions may be open at once. Guardrails apply "
            "to play money too: the numbers this produces are the evidence "
            "for going live, and evidence gathered without limits says "
            "nothing about a system that will have them.",
            minimum=1, maximum=50, step=1),

    Setting("PAPER_TRAIL_MULT", "Paper trailing stop (x ATR)", "number",
            "Paper",
            "Trail the stop at highest-high minus this many ATRs. 0 is off, "
            "which means a winner gives back all its open profit waiting for "
            "the strategy to flip \u2014 trend systems then look far worse on "
            "paper than they are. Use the same multiple your backtests use.",
            minimum=0, maximum=10, step=0.5),
    Setting("PAPER_TRAIL_PERIOD", "Trail ATR period", "number", "Paper",
            "Bars in the ATR the trail is measured from. 22 matches the "
            "backtester's default; changing one without the other makes the "
            "live-vs-backtest comparison compare two different systems.",
            minimum=2, maximum=100, step=1),
    Setting("PAPER_REGIME_GATE", "Regime gate", "choice", "Paper",
            "Skip new entries when the index is below its own 200-day. Long "
            "signals in a risk-off tape are where breakout systems bleed; "
            "with this on you can measure exactly how much.",
            choices=("0", "1")),

    # ---- risk model
    Setting("ACCOUNT_SIZE", "Account size (₹)", "number", "Risk",
            "What position sizing divides. Keep it honest — sizing off a "
            "figure larger than you'd actually deploy makes every position "
            "too big.", minimum=0, step=1000),
    Setting("RISK_PCT", "Risk per trade (%)", "number", "Risk",
            "The fraction of capital one trade may lose. 1% is the "
            "conventional start: it takes a long bad run to do real damage.",
            minimum=0.05, maximum=10, step=0.05),
    Setting("DAILY_LOSS_R", "Daily loss limit (R)", "number", "Risk",
            "Realised losses after which the guard tells you to stand down. "
            "Counts open risk too, so two live trades already put you close.",
            minimum=0.5, maximum=20, step=0.5),
    Setting("MAX_POSITIONS", "Max open positions", "number", "Risk",
            "The guard blocks new trades past this count.",
            minimum=1, maximum=50, step=1),
    Setting("MAX_POSITION_PCT", "Max position size (% of capital)", "number",
            "Risk",
            "Hard ceiling on one position. 100 = cash account, no margin. "
            "Raise it only if you actually trade on leverage.",
            minimum=1, maximum=500, step=5),

    # ---- backtesting defaults
    Setting("COST_BPS", "Assumed costs (bps per leg)", "number", "Backtesting",
            "Charged on every entry AND every exit, so 10 bps ≈ 0.2% per "
            "round trip. Test at 15–20 to be safe.",
            minimum=0, maximum=100, step=1),

    # ---- news
    Setting("NEWS_CURATOR", "News curation", "choice", "News",
            "How the digest is summarised. `none` needs nothing installed; "
            "`ollama` is local and private; `claude` is best but needs a key "
            "in .env.",
            choices=("none", "ollama", "claude")),

    # ---- maintenance
    Setting("BACKUP_KEEP_DAYS", "Keep backups for (days)", "number",
            "Maintenance",
            "The nightly job prunes dated backup folders older than this.",
            minimum=1, maximum=365, step=1),
)

BY_KEY = {s.key: s for s in SETTINGS}


class SettingError(ValueError):
    """A value the form should not have accepted."""


# ------------------------------------------------------------------ reading
def override(key: str) -> str | None:
    """The stored override, or None. Never raises — config.get calls this."""
    try:
        value = db.get_meta(PREFIX + key)
    except Exception:                             # noqa: BLE001
        return None                               # DB not built yet
    return value if value not in (None, "") else None


def value(key: str) -> str:
    """Override, else `.env`/default. What the app should actually use.

    Falls through to the module constant for keys whose value is legitimately
    zero — config.get's `or` chain treats 0 as absent, so ACCOUNT_SIZE=0 would
    otherwise render as an empty box rather than the zero it is.
    """
    return config.get(key, "") or _env_or_default(key)


def source(key: str) -> str:
    """Which layer is answering: override | env | default."""
    if override(key) is not None:
        return "override"
    import os
    return "env" if os.getenv(key) not in (None, "") else "default"


def effective() -> list[dict]:
    """Everything the settings page needs, in one call."""
    out = []
    for s in SETTINGS:
        out.append({
            "key": s.key,
            "label": s.label,
            "kind": s.kind,
            "group": s.group,
            "help": s.help,
            "choices": list(s.choices),
            "min": s.minimum,
            "max": s.maximum,
            "step": s.step,
            "value": value(s.key),
            "source": source(s.key),
            "env_value": _env_or_default(s.key),
        })
    return out


def _env_or_default(key: str) -> str:
    """What we'd fall back to if the override were cleared — shown next to
    the input so 'revert' isn't a leap of faith."""
    import os
    env = os.getenv(key)
    if env not in (None, ""):
        return env
    # Module constant first, then the DEFAULTS table for keys that have no
    # constant. Missing this leaves the page offering "revert to " with
    # nothing after it, which is not a choice anyone can make.
    default = config.__dict__.get(key)
    if default is None:
        default = config.DEFAULTS.get(key)
    return "" if default is None else str(default)


# ------------------------------------------------------------------ writing
def validate(key: str, raw: str) -> str:
    spec = BY_KEY.get(key)
    if spec is None:
        raise SettingError(f"{key} is not an editable setting")
    raw = (raw or "").strip()
    if raw == "":
        raise SettingError(f"{spec.label} cannot be blank — clear it instead")

    if spec.kind == "number":
        try:
            num = float(raw)
        except ValueError:
            raise SettingError(f"{spec.label} must be a number") from None
        if spec.minimum is not None and num < spec.minimum:
            raise SettingError(f"{spec.label} must be at least {spec.minimum}")
        if spec.maximum is not None and num > spec.maximum:
            raise SettingError(f"{spec.label} must be at most {spec.maximum}")
        # keep integers looking like integers, so the .env and the page agree
        return str(int(num)) if num == int(num) else str(num)

    if spec.kind == "choice" and raw not in spec.choices:
        raise SettingError(
            f"{spec.label} must be one of: {', '.join(spec.choices)}")
    return raw


def set_value(key: str, raw: str) -> str:
    cleaned = validate(key, raw)
    db.set_meta(PREFIX + key, cleaned)
    return cleaned


def clear(key: str) -> None:
    """Drop back to `.env`. Storing .env's current value instead would let the
    two copies drift the next time .env changes."""
    if key not in BY_KEY:
        raise SettingError(f"{key} is not an editable setting")
    db.set_meta(PREFIX + key, "")


def apply_all(values: dict[str, str]) -> dict:
    """Validate everything BEFORE writing anything.

    A form that saves the first three fields and then rejects the fourth
    leaves you with a config you didn't ask for and no clear way back.
    """
    cleaned = {}
    errors = {}
    for key, raw in values.items():
        try:
            cleaned[key] = validate(key, raw)
        except SettingError as e:
            errors[key] = str(e)
    if errors:
        return {"ok": False, "errors": errors, "saved": []}
    for key, val in cleaned.items():
        db.set_meta(PREFIX + key, val)
    return {"ok": True, "errors": {}, "saved": sorted(cleaned)}


def as_text() -> str:
    """CLI / phone rendering."""
    lines = []
    group = None
    for s in effective():
        if s["group"] != group:
            group = s["group"]
            lines.append(f"\n{group}")
        mark = {"override": "*", "env": " ", "default": "·"}[s["source"]]
        lines.append(f" {mark} {s['label']}: {s['value']}")
    lines.append("\n* set here · from .env · built-in default")
    return "\n".join(lines).strip()
