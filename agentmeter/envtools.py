"""Phase 11 — .env token loading + safe missing-token messaging.

Fill tokens ONCE in a local .env (copied from .env.example); the server and
tooling read them from the environment automatically. NEVER commit .env, NEVER
hard-code a token, and NEVER log a token VALUE — only its name and set/not-set.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("agentmeter.envtools")

# Token env vars this project understands (NAMES + purpose only — never values).
TOKENS: dict[str, str] = {
    "HF_TOKEN": "pull GATED/PRIVATE Hugging Face models (public models need none)",
    "VAST_API_KEY": "Vast.ai self-destroy (--auto-destroy / idle timeout)",
    "VAST_INSTANCE_ID": "identify this Vast.ai instance to destroy",
    "GITHUB_TOKEN": "clone a PRIVATE repo on the box (manual; not auto-wired)",
}


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (fallback when python-dotenv is not installed)."""
    out: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


def load_env(paths: Iterable[str] = (".env", ".env.local")) -> list[str]:
    """Load .env file(s) into os.environ if present. Existing environment values
    WIN (are never overridden), matching standard dotenv behavior. Uses
    python-dotenv when available, else a builtin parser (no hard dependency).

    Returns the NAMES of variables actually set from the files (never values).
    """
    loaded: list[str] = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            continue
        try:
            from dotenv import dotenv_values

            values = dict(dotenv_values(str(path)))
            src = "python-dotenv"
        except Exception:  # noqa: BLE001 — python-dotenv optional
            values = _parse_env_file(path)
            src = "builtin parser (python-dotenv not installed)"
        from_this: list[str] = []
        for k, v in values.items():
            if v is None:
                continue
            if os.environ.get(k):
                continue  # existing env wins; do not override
            os.environ[k] = v
            from_this.append(k)
        if from_this:
            loaded.extend(from_this)
            # log NAMES only, never values
            log.info("Loaded %d var(s) from %s via %s: %s", len(from_this), path,
                     src, ", ".join(sorted(from_this)))
    return loaded


def token_present(var: str) -> bool:
    return bool(os.environ.get(var))


def log_token_status(logger: logging.Logger = log) -> None:
    """Log which known tokens are set — NAMES and set/not-set only, never values."""
    logger.info("Token status (from environment / .env):")
    for var, purpose in TOKENS.items():
        logger.info("  %-16s : %-7s (%s)", var,
                    "set" if token_present(var) else "not set", purpose)


def hf_token_hint(var: str = "HF_TOKEN", logger: logging.Logger = log) -> Optional[str]:
    """On the gated/private pull path: if the HF token is missing, log + return a
    clear message naming the env var to set. Returns None when it is present.
    NEVER logs the token value.
    """
    if token_present(var):
        return None
    msg = (f"{var} not set — GATED/PRIVATE Hugging Face models will fail to pull. "
           f"Set {var} in your .env (copy .env.example). PUBLIC models (e.g. the "
           "canonical 5) need no token.")
    logger.warning(msg)
    return msg
