"""Phase 47: run tokens from the control plane (Cloudflare Worker) — they replace the passcode.

The Worker signs a short-lived token for a logged-in user (worker/src/runs.ts); this backend
verifies it with the shared secret AGENTMETER_RUN_TOKEN_SECRET before it prepares, imports,
benchmarks, resumes, lists sessions or serves a prepared set's files.

Format (same as worker/src/crypto.ts):  v1.<b64url(JSON payload)>.<b64url(HMAC-SHA256(secret, "v1.<payload>"))>
Payload: typ "run", aud "agentmeter-backend", jti (32 hex), sub (user id), uname, kind, iat, exp
(5 minutes), and per kind: after_prepare / run / prepare_jti (benchmark), job_id (resume), set (files).

Kinds that START work (prepare, import, benchmark, resume) are single-use: a jti is accepted
once (kept in memory until it expires — a token outlives a backend restart by at most 5 minutes).
read / files tokens may be reused until they expire.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from typing import Any, Optional

SECRET_ENV = "AGENTMETER_RUN_TOKEN_SECRET"
AUDIENCE = "agentmeter-backend"
SINGLE_USE = ("prepare", "import", "benchmark", "resume")
KINDS = SINGLE_USE + ("read", "files")
MAX_TTL_S = 15 * 60                     # the Worker issues 5 min; refuse anything claiming longer
_JTI = re.compile(r"^[0-9a-f]{32}$")


class RunTokenError(ValueError):
    def __init__(self, message: str, code: str = "run_token_invalid", status: int = 401):
        super().__init__(message)
        self.code, self.status = code, status


def secret() -> str:
    return os.environ.get(SECRET_ENV, "")


def enabled() -> bool:
    """Control-plane mode: the backend only works for requests carrying a Worker-issued token."""
    return bool(secret())


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sign(payload: dict[str, Any], key: Optional[str] = None) -> str:
    """Tests / tools only (the Worker is the issuer in production)."""
    body = "v1." + _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = hmac.new((key or secret()).encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + _b64(mac)


def verify(token: str, key: Optional[str] = None, now: Optional[float] = None) -> dict[str, Any]:
    """Signature, type, audience, expiry and shape. Returns the payload or raises RunTokenError."""
    key = key if key is not None else secret()
    if not key:
        raise RunTokenError("this backend has no run-token secret", "misconfigured", 500)
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise RunTokenError("run token missing or malformed")
    body = parts[0] + "." + parts[1]
    want = hmac.new(key.encode(), body.encode(), hashlib.sha256).digest()
    try:
        got = _unb64(parts[2])
        payload = json.loads(_unb64(parts[1]))
    except (ValueError, TypeError) as e:
        raise RunTokenError("run token malformed") from e
    if not hmac.compare_digest(want, got):
        raise RunTokenError("run token signature invalid")
    if not isinstance(payload, dict) or payload.get("typ") != "run" or payload.get("aud") != AUDIENCE:
        raise RunTokenError("not a run token for this backend")
    t = time.time() if now is None else now
    exp, iat = payload.get("exp"), payload.get("iat")
    if not isinstance(exp, (int, float)) or not isinstance(iat, (int, float)) or exp - iat > MAX_TTL_S:
        raise RunTokenError("run token has no valid lifetime")
    if t >= exp:
        raise RunTokenError("run token expired — ask the control plane for a new one", "run_token_expired")
    if payload.get("kind") not in KINDS or not _JTI.match(str(payload.get("jti") or "")) \
            or not payload.get("sub"):
        raise RunTokenError("run token payload incomplete")
    return payload


class JtiStore:
    """Single-use jtis, in memory, forgotten once the token would have expired anyway."""

    def __init__(self):
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def use(self, jti: str, exp: float, now: Optional[float] = None) -> bool:
        t = time.time() if now is None else now
        with self._lock:
            for k in [k for k, e in self._seen.items() if e <= t]:
                del self._seen[k]
            if jti in self._seen:
                return False
            self._seen[jti] = float(exp)
            return True

    def release(self, jti: str) -> None:
        with self._lock:
            self._seen.pop(jti, None)


def bearer(headers) -> str:
    m = re.match(r"^Bearer\s+(\S+)$", headers.get("Authorization") or "")
    return m.group(1) if m else ""


def user_of(payload: Optional[dict[str, Any]]) -> Optional[dict[str, str]]:
    """What a job records about who started it (never more than id + username)."""
    if not payload:
        return None
    return {"sub": str(payload.get("sub")), "uname": str(payload.get("uname") or "")}


def current() -> Optional[dict[str, Any]]:
    """The verified run token of the current Flask request (control-plane mode), else None."""
    try:
        from flask import g
        return g.get("run_token")
    except Exception:  # noqa: BLE001 — outside a request
        return None


def job_meta() -> dict[str, Any]:
    """Fields a new job records about who started it: {user: {sub, uname}, grant_jti}."""
    p = current()
    return {"user": user_of(p), "grant_jti": p.get("jti")} if p else {}
