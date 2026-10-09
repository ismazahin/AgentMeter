"""Phase E: who may call this backend from where (CORS, passcode, rate limits).

CORS (the static front-end is on another origin):
  * AGENTMETER_ALLOWED_ORIGINS="https://agentmeter.vercel.app,https://x.pages.dev"
    -> only those exact origins get CORS headers (credentials are never used).
  * unset, REAL mode -> no cross-origin access at all (same-origin only); a
    wildcard is never sent by a real-GPU server.
  * unset, MOCK mode -> permissive (local development, file:// dashboards).

Passcode (AGENTMETER_PASSCODE): when set, every state-changing request
(POST/PUT/PATCH/DELETE: prepare, import, benchmark jobs, resume, notify-test ...)
and the job list need header X-AgentMeter-Passcode. Read-only
pages stay open: /health, the static pages, a job's status/results/PDF by its id,
a prepared set by its id. No accounts.

Rate limits (in memory, per client IP — Cloudflare's CF-Connecting-IP when the
request came through the tunnel):
  * job creation: AGENTMETER_JOBS_PER_HOUR (default 12) per client and a queue cap
    AGENTMETER_MAX_QUEUED_JOBS (default 4) — the single-job lock is unchanged;
  * wrong passcodes: 10 per 10 minutes per client, then 429.
"""
from __future__ import annotations

import hmac
import os
import threading
import time
from collections import deque
from typing import Callable, Optional

PASSCODE_HEADER = "X-AgentMeter-Passcode"
JOB_CREATING = {("POST", "/api/prepare"), ("POST", "/api/prepared/import"), ("POST", "/api/jobs")}
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


def install(app, *, mode: str, queue_depth: Callable[[], int]) -> dict:
    """Register CORS, passcode and rate-limit hooks on the Flask app."""
    from flask import jsonify, request

    origins = allowed_origins()
    permissive = not origins and mode != "real"
    passcode = os.environ.get("AGENTMETER_PASSCODE") or ""
    jobs_limit = SlidingWindow(int(os.environ.get("AGENTMETER_JOBS_PER_HOUR", "12")), 3600)
    bad_pass = SlidingWindow(10, 600)
    max_queued = int(os.environ.get("AGENTMETER_MAX_QUEUED_JOBS", "4"))
    state = {"origins": origins, "permissive": permissive, "auth_required": bool(passcode),
             "jobs_per_hour": jobs_limit.limit, "max_queued": max_queued}
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
        needs = passcode and (request.method in _WRITE or key == ("GET", "/api/jobs"))
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
