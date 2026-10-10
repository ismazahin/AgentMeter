"""Phase E: who may call this backend from where (CORS, passcode, rate limits).

CORS (the static front-end is on another origin):
  * AGENTMETER_ALLOWED_ORIGINS="https://agentmeter.vercel.app,https://x.pages.dev"
    -> only those exact origins get CORS headers (credentials are never used).
  * unset, REAL mode -> no cross-origin access at all (same-origin only); a
    wildcard is never sent by a real-GPU server.
  * unset, MOCK mode -> permissive (local development, file:// dashboards).

Passcode (AGENTMETER_PASSCODE): when set, every state-changing request
(POST/PUT/PATCH/DELETE: prepare, import, benchmark jobs, resume, notify-test ...)
and the job / session / leaderboard listings need header X-AgentMeter-Passcode. Read-only
pages stay open: /health, the static pages, a job's status/results/PDF by its id,
a prepared set by its id. No accounts.

Rate limits (in memory, per client IP — Cloudflare's CF-Connecting-IP when the
request came through the tunnel):
  * sessions started per client per hour: config.yaml service.sessions_per_hour, env
    AGENTMETER_JOBS_PER_HOUR (default 12). A wizard session — its data preparation plus
    the one benchmark queued behind it (after_prepare) — counts ONCE; reusing a prepared
    set or re-uploading one counts as a session; resume counts;
  * queue cap: service.max_queued_sessions / AGENTMETER_MAX_QUEUED_JOBS (default 4),
    counted in sessions (a benchmark waiting for its own preparation is not a second
    one) — the single-job lock is unchanged;
  * wrong passcodes: 10 per 10 minutes per client, then 429.
"""
from __future__ import annotations

import hmac
import os
import re
import threading
import time
from collections import deque
from typing import Callable, Optional

PASSCODE_HEADER = "X-AgentMeter-Passcode"
JOB_CREATING = {("POST", "/api/prepare"), ("POST", "/api/prepared/import"), ("POST", "/api/jobs")}
# Listings that enumerate every job/session need the passcode too (Phase 46 adds
# sessions + leaderboard); a single job, session or comparison by its ids stays open.
LISTINGS = {("GET", "/api/jobs"), ("GET", "/api/sessions"), ("GET", "/api/leaderboard")}
_WRITE = {"POST", "PUT", "PATCH", "DELETE"}


def allowed_origins() -> list[str]:
    raw = os.environ.get("AGENTMETER_ALLOWED_ORIGINS", "")
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


class SlidingWindow:
    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = int(limit), float(window_s)
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, now: Optional[float] = None) -> bool:
        """Record a hit; False when the key is over its limit (the hit is not recorded)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def retry_after(self, key: str) -> int:
        q = self._hits.get(key)
        return int(self.window - (time.monotonic() - q[0])) + 1 if q else 1


def client_ip(request) -> str:
    return (request.headers.get("CF-Connecting-IP") or request.remote_addr or "?").strip()


_JOB_ID = re.compile(r"^job_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$")
DEFAULT_SESSIONS_PER_HOUR = 12
DEFAULT_MAX_QUEUED_SESSIONS = 4


def limits() -> dict:
    """Session rate limits: config.yaml service.sessions_per_hour / service.max_queued_sessions,
    overridden by env AGENTMETER_JOBS_PER_HOUR / AGENTMETER_MAX_QUEUED_JOBS. Defaults 12 / 4."""
    cfg = {}
    try:
        from ..config import load_config
        cfg = load_config(None).get("service") or {}
    except Exception:  # noqa: BLE001 — a missing/odd config falls back to the defaults
        cfg = {}

    def pick(env: str, key: str, default: int) -> tuple[int, str]:
        for src, v in ((f"env {env}", os.environ.get(env)), (f"config service.{key}", cfg.get(key))):
            if v not in (None, ""):
                try:
                    return max(1, int(v)), src
                except (TypeError, ValueError):
                    continue
        return default, "default"
    sph, s1 = pick("AGENTMETER_JOBS_PER_HOUR", "sessions_per_hour", DEFAULT_SESSIONS_PER_HOUR)
    mq, s2 = pick("AGENTMETER_MAX_QUEUED_JOBS", "max_queued_sessions", DEFAULT_MAX_QUEUED_SESSIONS)
    return {"sessions_per_hour": sph, "max_queued_sessions": mq, "source": {"sessions_per_hour": s1, "max_queued_sessions": s2}}


def install(app, *, mode: str, queue_depth: Callable[[], int]) -> dict:
    """Register CORS, passcode and rate-limit hooks on the Flask app."""
    from flask import jsonify, request

    origins = allowed_origins()
    permissive = not origins and mode != "real"
    passcode = os.environ.get("AGENTMETER_PASSCODE") or ""
    lim = limits()
    jobs_limit = SlidingWindow(lim["sessions_per_hour"], 3600)
    bad_pass = SlidingWindow(10, 600)
    max_queued = lim["max_queued_sessions"]
    chained: set[str] = set()       # prepare jobs whose ONE follow-up benchmark was already free
    state = {"origins": origins, "permissive": permissive, "auth_required": bool(passcode),
             "jobs_per_hour": jobs_limit.limit, "max_queued": max_queued, "limits_source": lim["source"]}
    app.config["ACCESS"] = state

    def cors_headers(resp):
        origin = request.headers.get("Origin")
        if not origin:
            return resp
        if permissive:
            resp.headers["Access-Control-Allow-Origin"] = "*" if origin == "null" else origin
        elif origin.rstrip("/") in origins:
            resp.headers["Access-Control-Allow-Origin"] = origin
        else:
            return resp                                   # no CORS headers: the browser blocks it
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = f"Content-Type, {PASSCODE_HEADER}"
        resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Retry-After"
        resp.headers["Access-Control-Max-Age"] = "600"
        return resp

    def deny(code: str, msg: str, status: int, retry: Optional[int] = None):
        r = jsonify({"error": msg, "code": code})
        r.status_code = status
        if retry:
            r.headers["Retry-After"] = str(retry)
        return cors_headers(r)

    @app.before_request
    def _access_gate():
        if request.method == "OPTIONS":                  # CORS preflight: answer here, no auth
            return cors_headers(app.make_response(("", 204)))
        key = (request.method, request.path.rstrip("/") or "/")
        needs = passcode and (request.method in _WRITE or key in LISTINGS)
        ip = client_ip(request)
        if needs:
            given = request.headers.get(PASSCODE_HEADER, "")
            if not hmac.compare_digest(given.encode(), passcode.encode()):
                if not bad_pass.hit(ip):
                    return deny("too_many_attempts", "too many wrong passcodes — wait a few minutes",
                                429, bad_pass.retry_after(ip))
                return deny("passcode_required" if not given else "passcode_invalid",
                            "this server needs its access passcode to prepare or benchmark"
                            if not given else "wrong passcode", 401)
        creates = key in JOB_CREATING or (request.method == "POST" and request.path.endswith("/resume"))
        if creates and key == ("POST", "/api/jobs"):
            # One wizard session = its data preparation + ONE benchmark queued behind it
            # (after_prepare): the preparation was already counted, so that benchmark is
            # free — once per prepare job (a second benchmark on it counts as a new session).
            body = request.get_json(silent=True) or {}
            prep = body.get("after_prepare") if isinstance(body, dict) else None
            if isinstance(prep, str) and _JOB_ID.match(prep) and prep not in chained:
                chained.add(prep)
                creates = False
        if creates:
            if queue_depth() >= max_queued:
                return deny("queue_full", f"the job queue is full ({max_queued} waiting) — try again "
                            "when the current jobs finish", 429, 60)
            if not jobs_limit.hit(ip):
                return deny("rate_limited", f"at most {jobs_limit.limit} jobs per hour from one client",
                            429, jobs_limit.retry_after(ip))
        return None

    app.after_request(cors_headers)
    return state
