"""Central configuration. Reads .env from the project root.

Required keys (see .env.example):
    KITE_API_KEY, KITE_API_SECRET  - from https://developers.kite.trade
    NTFY_TOPIC                     - your private ntfy.sh topic name
"""
from pathlib import Path
import os

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(PROJECT_ROOT / ".env")

KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
# Optional ntfy access token. On a server that supports auth (self-hosted, or
# ntfy.sh with a reserved topic) this is what actually protects the channel —
# the topic name alone is only a secret until someone reads it over a shoulder.
NTFY_TOKEN = os.getenv("NTFY_TOKEN", "")

# Biggest message the ntfy server will accept, in bytes.
#
# ntfy.sh caps this at 4 KB; a self-hosted instance sets whatever
# `message-size-limit` says. It matters because a price history is split to fit
# — at 4 KB that is five messages for a year, and at 64 KB it is one. Kept as a
# figure rather than a batch size so the split follows the server rather than a
# constant somebody has to remember to change.
NTFY_MESSAGE_LIMIT = int(os.getenv("NTFY_MESSAGE_LIMIT", "4096"))
# Optional PIN required by state-changing phone commands (/alert, /unalert).
# Weak by design — see the security note in commands.py — but better than the
# topic name being the only thing between a stranger and your armed alerts.
NTFY_CMD_PIN = os.getenv("NTFY_CMD_PIN", "")

# ---- risk model -----------------------------------------------------------
# Trading capital and the fraction of it you're willing to lose on one trade.
# ACCOUNT_SIZE is the number position sizing divides; keep it honest — sizing
# off a figure larger than you'd actually deploy makes every position too big.
# 1% is the conventional starting point: it takes a long, bad run to do real
# damage, and it's small enough that any single trade is genuinely survivable.
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "0") or 0)
RISK_PCT = float(os.getenv("RISK_PCT", "1") or 1)
# Realised losses (in R) after which the guard tells you to stand down.
DAILY_LOSS_R = float(os.getenv("DAILY_LOSS_R", "2") or 2)
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5") or 5)
# Hard ceiling on one position as a % of capital. 100 = cash account, no
# margin. Raise it only if you actually trade on leverage.
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "100") or 100)

# News digest curation: none | ollama | claude
NEWS_CURATOR = os.getenv("NEWS_CURATOR", "none")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")

DB_PATH = DATA_DIR / "tickerforge.db"
TOKEN_PATH = DATA_DIR / "kite_token.json"

# Exchanges whose instruments we keep in the local search index.
EXCHANGES = ("NSE", "BSE")


def require(*names: str) -> None:
    """Fail fast with a helpful message if a config key is missing."""
    missing = [n for n in names if not globals().get(n)]
    if missing:
        raise SystemExit(
            f"Missing config: {', '.join(missing)}. "
            f"Copy .env.example to .env and fill these in."
        )


# Settings the browser may override, stored in SQLite. Deliberately excludes
# anything that decides how to reach an external service — API keys, the ntfy
# topic, the command PIN. A page with no authentication is not where the
# credentials belong, and a form that can retarget the notification channel is
# a form that can silently redirect your alerts.
OVERRIDABLE = frozenset({
    "ACCOUNT_SIZE", "RISK_PCT", "DAILY_LOSS_R", "MAX_POSITIONS",
    "MAX_POSITION_PCT", "COST_BPS", "NEWS_CURATOR", "BACKUP_KEEP_DAYS",
    "EVENT_PUSH_CATEGORIES", "EVENT_PUSH_HORIZON_DAYS",
    "PAPER_AUTO", "PAPER_STRATEGIES", "PAPER_STOP_PCT",
    "PAPER_MAX_POSITIONS", "PAPER_TRAIL_MULT", "PAPER_TRAIL_PERIOD",
    "PAPER_REGIME_GATE",
    "INSIDER_MIN_VALUE",
    "PREOPEN_GAP_ATR",
    "RESULTS_WARN_DAYS", "MIN_RR", "CORR_WARN",
})

# Built-in defaults for keys that have no module constant above, so `get`
# answers the same way whether or not .env mentions them.
DEFAULTS = {"COST_BPS": "10", "BACKUP_KEEP_DAYS": "14",
            # Narrow on purpose. Holding a stock should not subscribe
            # you to its entire paperwork trail; CONCALL is left out
            # because an analyst-meet notice is rarely worth a buzz.
            "EVENT_PUSH_CATEGORIES": "RESULTS,CORP ACTION,DIVIDEND",
            "EVENT_PUSH_HORIZON_DAYS": "0",
            # Off by default. A paper account that starts trading because
            # you updated the code is a surprise, even with play money.
            "PAPER_AUTO": "0",
            "PAPER_STRATEGIES": "signals",
            "PAPER_STOP_PCT": "5",
            "PAPER_MAX_POSITIONS": "8",
            # 0 = off. Matches the backtester's 22-bar ATR default, because
            # paper trailing on a different period from the backtest it is
            # compared against measures a different system.
            "PAPER_TRAIL_MULT": "0",
            "PAPER_TRAIL_PERIOD": "22",
            "PAPER_REGIME_GATE": "0",
            # ₹1 Cr. Market-wide insider buys below this are noise; on
            # names you follow every disclosure pushes regardless.
            "INSIDER_MIN_VALUE": "10000000",
            "PREOPEN_GAP_ATR": "1",
            "RESULTS_WARN_DAYS": "5",
            "MIN_RR": "1.5",
            "CORR_WARN": "0.7",
            }


def _override(name: str) -> str | None:
    """A stored override, or None. Must never raise: `get` is called during
    startup, before the database necessarily exists."""
    try:
        from . import db
        value = db.get_meta("setting:" + name)
    except Exception:                             # noqa: BLE001
        return None
    return value if value not in (None, "") else None


def get(name: str, default: str = "") -> str:
    """Read a config value: override → module constant → env → default."""
    if name in OVERRIDABLE:
        stored = _override(name)
        if stored is not None:
            return stored
    return (globals().get(name)
            or os.getenv(name)
            or DEFAULTS.get(name)
            or default)
