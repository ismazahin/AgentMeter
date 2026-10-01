"""Phase 30 — Telegram notifications for new analyses.

When a new analysis.json appears under results/ (e.g. after run-full auto-analyzes,
Phase 27, or after a remote run copies results back, Phase 29), send a short push to
the owner's phone via the Telegram Bot API so they know there is a new session to
load in the dashboard.

Config lives in .env (single source of truth, read LIVE):
    TELEGRAM_BOT_TOKEN   # from @BotFather
    TELEGRAM_CHAT_ID     # your chat id (message the bot, then /getUpdates)

Both must be set or notifications are a silent no-op. The token VALUE is never
logged; only presence is ever reported. Sending is best-effort — a failure never
affects a run or the server.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from ..util import envtools

log = logging.getLogger("agentmeter.notify")

TELEGRAM_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
_API = "https://api.telegram.org"


def telegram_config(env: Optional[dict[str, str]] = None) -> dict[str, Any]:
    env = env if env is not None else envtools.read_dotenv_live()
    get = lambda k: (env.get(k) or "").strip()
    token, chat = get("TELEGRAM_BOT_TOKEN"), get("TELEGRAM_CHAT_ID")
    missing = [k for k in TELEGRAM_KEYS if not get(k)]
    return {"token": token, "chat_id": chat, "enabled": not missing, "missing": missing}


def notify_status(env: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Presence only — safe to return to the browser (never the token value)."""
    c = telegram_config(env)
    return {"enabled": c["enabled"], "missing": c["missing"],
            "present": {k: bool((env or envtools.read_dotenv_live()).get(k, "").strip())
                        for k in TELEGRAM_KEYS}}


def send_telegram(text: str, env: Optional[dict[str, str]] = None,
                  poster: Optional[Callable] = None, timeout: int = 10) -> dict[str, Any]:
    """Send a message. No-op (ok=False, disabled) if not configured. Best-effort:
    never raises, never logs the token. `poster` overrides the HTTP call for tests."""
    c = telegram_config(env)
    if not c["enabled"]:
        return {"ok": False, "disabled": True, "missing": c["missing"]}
    url = f"{_API}/bot{c['token']}/sendMessage"
    payload = {"chat_id": c["chat_id"], "text": text, "disable_web_page_preview": True}
    try:
        if poster is None:
            import requests
            r = requests.post(url, data=payload, timeout=timeout)
            ok = r.status_code == 200
            return {"ok": ok, "status": r.status_code} if ok else {
                "ok": False, "status": r.status_code, "error": "Telegram API returned non-200"}
        return poster(url, payload)
    except ImportError:
        log.warning("Telegram notify skipped: `requests` not installed.")
        return {"ok": False, "error": "requests not installed"}
    except Exception as e:  # noqa: BLE001 — notifications must never break a run
        log.warning("Telegram notify failed: %s", e)
        return {"ok": False, "error": str(e)}


class NewResultsWatcher:
    """Tracks analysis.json files under results/ and notifies once per NEW file.

    Seeded with whatever already exists, so only analyses that appear AFTER start
    trigger a push. Dedup by relative path. Safe to poll repeatedly / concurrently
    enough for a daemon thread (single-threaded poller)."""

    def __init__(self, results_dir: str | Path,
                 env_reader: Optional[Callable] = None, poster: Optional[Callable] = None):
        self.root = Path(results_dir)
        self._env_reader = env_reader or envtools.read_dotenv_live
        self._poster = poster
        self._seen: set[str] = set()

    def _current(self) -> list[str]:
        if not self.root.exists():
            return []
        out = []
        for p in self.root.rglob("analysis.json"):
            try:
                out.append(str(p.resolve().relative_to(self.root.resolve())).replace("\\", "/"))
            except (ValueError, OSError):
                continue
        return out

    def seed(self) -> None:
        """Mark everything present now as already seen (no notify on startup)."""
        self._seen = set(self._current())

    def poll_once(self) -> list[str]:
        """Return the newly-appeared analyses and notify for each (if Telegram is on).
        Always updates the seen set, so enabling Telegram later never floods."""
        env = self._env_reader()
        enabled = telegram_config(env)["enabled"]
        new = [f for f in self._current() if f not in self._seen]
        for rel in sorted(new):
            self._seen.add(rel)
            if enabled:
                send_telegram(
                    f"AgentMeter: new analysis ready to load — {rel}. "
                    "Open the dashboard's Local results tab to view it.",
                    env=env, poster=self._poster)
        return new
