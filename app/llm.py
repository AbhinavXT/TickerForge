"""Shared LLM completion helper (backend: NEWS_CURATOR in .env).

Used by the news digest and the market-events digest. Best-effort by
design: any failure returns None and callers fall back to plain formatting.
"""
import requests

from . import config


def complete(prompt: str, timeout: int = 120) -> str | None:
    backend = config.NEWS_CURATOR
    try:
        if backend == "ollama":
            r = requests.post(
                f"{config.OLLAMA_URL}/api/chat",
                json={"model": config.OLLAMA_MODEL, "stream": False,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout)
            r.raise_for_status()
            return r.json()["message"]["content"].strip() or None
        if backend == "claude":
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": config.ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01"},
                json={"model": config.ANTHROPIC_MODEL, "max_tokens": 1500,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=60)
            r.raise_for_status()
            return ("".join(b.get("text", "")
                            for b in r.json()["content"]).strip() or None)
    except Exception:
        return None
    return None
