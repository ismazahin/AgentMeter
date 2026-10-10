"""Who may call this backend from where (CORS, run tokens, rate limits).

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product.

CORS (the static front-end is on another origin):
  * AGENTMETER_ALLOWED_ORIGINS="https://agentmeter.pages.dev,..." -> only those exact origins
    get CORS headers (credentials/cookies are never used; tokens travel in Authorization).
  * unset, REAL mode -> no cross-origin access at all (same-origin only).
  * unset, MOCK mode -> permissive (local development, file:// dashboards).

Phase 47 — control-plane mode (AGENTMETER_RUN_TOKEN_SECRET set; REAL mode requires it):
every request that starts work or lists/reads stored work needs a run token the control plane
(Cloudflare Worker) issued to a logged-in user — `Authorization: Bearer <token>` (runtoken.py):

  POST /api/prepare                       kind prepare     (single use)
  POST /api/prepared/import               kind import      (single use)
  POST /api/jobs                          kind benchmark   (single use; body run/after_prepare = the token's)
  POST /api/jobs/<id>/resume              kind resume      (single use; job_id = the token's)
  GET  /api/jobs, /api/sessions, /api/sessions/<id>, /api/compare, /api/leaderboard,
       /api/jobs/<id>/result|report.pdf|constraints, /api/prepared/<kind>/<name>     kind read
  GET  /api/prepared/<kind>/<name>/<file> kind files       (set = <kind>/<name>)
  GET  /api/jobs/<id>                     open for 128-bit job ids (status/progress only);
                                          a pre-Phase-47 id (24 random bits) needs a read token
  any other POST/PUT/PATCH/DELETE         refused (notifications are sent by the control plane)

The per-user hourly session limit is enforced by the control plane (persistent, in D1); this
backend keeps the queue cap (limits pushed by the control plane on every heartbeat) as a safety
net. A single-use token whose request fails (4xx/5xx) is released, so a typo does not burn it.

Standalone mode (no secret — local development / mock only): no token; the in-memory per-IP
limits below apply. The old shared passcode (AGENTMETER_PASSCODE) is gone.

Rate limits in standalone mode (in memory, per client IP — CF-Connecting-IP behind the tunnel):
  * sessions started per client per hour: config.yaml service.sessions_per_hour, env
    AGENTMETER_JOBS_PER_HOUR (default 12). A wizard session — its data preparation plus
    the one benchmark queued behind it (after_prepare) — counts ONCE;
  * queue cap: service.max_queued_sessions / AGENTMETER_MAX_QUEUED_JOBS (default 4),
    counted in sessions.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from typing import Callable, Optional

from . import runtoken
from .jobs import _JOB_ID, strong_job_id

JOB_CREATING = {("POST", "/api/prepare"), ("POST", "/api/prepared/import"), ("POST", "/api/jobs")}
_WRITE = {"POST", "PUT", "PATCH", "DELETE"}
_WRITE_KIND = {"/api/prepare": "prepare", "/api/prepared/import": "import", "/api/jobs": "benchmark"}
_READ_EXACT = {"/api/jobs", "/api/sessions", "/api/compare", "/api/leaderboard"}
_JOB_SUB = re.compile(r"^/api/jobs/([^/]+)(?:/(result|report\.pdf|constraints|resume))?$")
_SESSION = re.compile(r"^/api/sessions/[^/]+$")
_PREPARED = re.compile(r"^/api/prepared/([^/]+)/([^/]+)(?:/([^/]+))?$")


def required(method: str, path: str) -> tuple[Optional[str], dict]:
    """(kind of run token needed or None, constraints) for a request in control-plane mode.
    kind "deny" = not available in control-plane mode."""
    if method in _WRITE:
        if path in _WRITE_KIND:
            return _WRITE_KIND[path], {}
        m = _JOB_SUB.match(path)
        if m and m.group(2) == "resume":
            return "resume", {"job_id": m.group(1)}
        return "deny", {}
    if method != "GET":
        return None, {}
    if path in _READ_EXACT or _SESSION.match(path):
        return "read", {}
    m = _JOB_SUB.match(path)
    if m:
        if m.group(2):
            return "read", {}
        return (None if strong_job_id(m.group(1)) else "read"), {}
    m = _PREPARED.match(path)
    if m and m.group(1) != "import":
        if m.group(3):
            return "files", {"set": f"{m.group(1)}/{m.group(2)}"}
        return "read", {}
    return None, {}


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
    """Register CORS, run-token and rate-limit hooks on the Flask app."""
    from flask import g, jsonify, request

    origins = allowed_origins()
    permissive = not origins and mode != "real"
    cp = runtoken.enabled()
    lim = limits()
    jobs_limit = SlidingWindow(lim["sessions_per_hour"], 3600)
    jtis = runtoken.JtiStore()
    chained: set[str] = set()       # standalone: prepare jobs whose ONE follow-up benchmark was free
    state = {"origins": origins, "permissive": permissive, "auth_required": cp,
             "control_plane": cp, "jobs_per_hour": jobs_limit.limit,
             "max_queued": lim["max_queued_sessions"], "limits_source": lim["source"]}
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
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Retry-After"
        resp.headers["Access-Control-Max-Age"] = "600"
        return resp

    def deny(code: str, msg: str, status: int, retry: Optional[int] = None):
        r = jsonify({"error": msg, "code": code})
        r.status_code = status
        if retry:
            r.headers["Retry-After"] = str(retry)
        return cors_headers(r)

    def token_gate(key: tuple[str, str]):
        kind, need = required(*key)
        if kind is None:
            return None
        if kind == "deny":
            return deny("control_plane_only", "not available on a backend run by the control plane", 403)
        try:
            p = runtoken.verify(runtoken.bearer(request.headers))
        except runtoken.RunTokenError as e:
            return deny(e.code if request.headers.get("Authorization") else "run_token_required",
                        str(e) if request.headers.get("Authorization") else
                        "log in to AgentMeter: this backend only accepts requests authorised by the control plane",
                        e.status)
        if p["kind"] != kind:
            return deny("run_token_wrong_kind", f"this request needs a {kind!r} token, not {p['kind']!r}", 403)
        if kind == "resume" and p.get("job_id") != need["job_id"]:
            return deny("run_token_mismatch", "the token was issued for another job", 403)
        if kind == "files" and p.get("set") != need["set"]:
            return deny("run_token_mismatch", "the token was issued for another prepared set", 403)
        if kind in runtoken.SINGLE_USE:
            if not jtis.use(p["jti"], p["exp"]):
                return deny("run_token_used", "this run token was already used — start again", 409)
            g.reserved_jti = p["jti"]
        g.run_token = p
        return None

    @app.before_request
    def _access_gate():
        if request.method == "OPTIONS":                  # CORS preflight: answer here, no auth
            return cors_headers(app.make_response(("", 204)))
        key = (request.method, request.path.rstrip("/") or "/")
        g.run_token = None
        if cp:
            r = token_gate(key)
            if r is not None:
                return r
        creates = key in JOB_CREATING or (request.method == "POST" and request.path.endswith("/resume"))
        if creates and key == ("POST", "/api/jobs"):
            body = request.get_json(silent=True) or {}
            prep = body.get("after_prepare") if isinstance(body, dict) else None
            if cp:
                # free follow-up of a wizard session: the control plane already counted it
                creates = not (g.run_token or {}).get("prepare_jti")
            elif isinstance(prep, str) and _JOB_ID.match(prep) and prep not in chained:
                # One wizard session = its data preparation + ONE benchmark queued behind it
                # (after_prepare): the preparation was already counted, so that benchmark is
                # free — once per prepare job (a second benchmark on it counts as a new session).
                chained.add(prep)
                creates = False
        if creates:
            if queue_depth() >= state["max_queued"]:
                return deny("queue_full", f"the job queue is full ({state['max_queued']} waiting) — try again "
                            "when the current jobs finish", 429, 60)
            if not cp and not jobs_limit.hit(client_ip(request)):
                return deny("rate_limited", f"at most {jobs_limit.limit} jobs per hour from one client",
                            429, jobs_limit.retry_after(client_ip(request)))
        return None

    @app.after_request
    def _release_failed(resp):
        jti = g.pop("reserved_jti", None)
        if jti and resp.status_code >= 400:
            jtis.release(jti)                             # a refused request does not burn the token
        return cors_headers(resp)

    def apply_limits(new: dict) -> None:
        """Limits pushed by the control plane (register/heartbeat)."""
        try:
            state["max_queued"] = max(1, int(new.get("max_queued_sessions", state["max_queued"])))
        except (TypeError, ValueError):
            pass
    state["apply_limits"] = apply_limits
    return state
