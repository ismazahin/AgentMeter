"""Structured logs and the admin's "problems in the last 24 hours".

  * AGENTMETER_LOG_JSON=1  -> every log line is one JSON object (ts, level, logger, msg, job_id when
                              known), for log collectors; otherwise the usual text lines.
  * every WARNING and ERROR is also kept in memory (the last MAX records) so an admin can see what
    went wrong recently without shell access to the rented GPU box: GET /api/logs/recent.
Nothing here leaves the server; the records hold log messages only (no request bodies, no tokens).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Optional

MAX = 500
_LOCK = threading.Lock()
_RECENT: deque = deque(maxlen=MAX)


def _job_id(record: logging.LogRecord) -> Optional[str]:
    jid = getattr(record, "job_id", None)
    if jid:
        return str(jid)
    msg = record.getMessage()
    i = msg.find("job_")
    if i >= 0:
        tok = msg[i:].split()[0].strip(".,:;()[]'\"")
        return tok if 20 <= len(tok) <= 64 else None
    return None


def as_dict(record: logging.LogRecord) -> dict[str, Any]:
    out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)), "t": record.created,
           "level": record.levelname, "logger": record.name, "msg": record.getMessage()[:2000]}
    jid = _job_id(record)
    if jid:
        out["job_id"] = jid
    if record.exc_info:
        out["exc"] = logging.Formatter().formatException(record.exc_info).splitlines()[-1][:500]
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = as_dict(record)
        d.pop("t", None)
        return json.dumps(d, ensure_ascii=False)


class RecentHandler(logging.Handler):
    """Keeps WARNING+ records in memory for /api/logs/recent."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with _LOCK:
                _RECENT.append(as_dict(record))
        except Exception:  # noqa: BLE001 — logging never raises
            pass


def install(json_logs: Optional[bool] = None) -> None:
    root = logging.getLogger()
    if not any(isinstance(h, RecentHandler) for h in root.handlers):
        root.addHandler(RecentHandler())
    if json_logs is None:
        json_logs = os.environ.get("AGENTMETER_LOG_JSON", "").strip().lower() in ("1", "true", "yes")
    if json_logs:
        for h in root.handlers:
            if not isinstance(h, RecentHandler):
                h.setFormatter(JsonFormatter())


def recent(hours: float = 24.0) -> list[dict[str, Any]]:
    cut = time.time() - hours * 3600
    with _LOCK:
        rows = [dict(r) for r in _RECENT if r["t"] >= cut]
    rows.reverse()                                       # newest first
    for r in rows:
        r.pop("t", None)
    return rows


def clear() -> None:
    with _LOCK:
        _RECENT.clear()


def register_logs(app, get_manager) -> None:
    from flask import jsonify, request
    from . import runtoken

    @app.route("/api/logs/recent", methods=["GET"])
    def logs_recent():
        tok = runtoken.current()
        if tok is not None and not tok.get("admin"):           # control plane: admins only
            return jsonify({"error": "only an admin can read the server's problems", "code": "forbidden"}), 403
        try:
            hours = max(1.0, min(168.0, float(request.args.get("hours", 24))))
        except ValueError:
            hours = 24.0
        cut = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - hours * 3600))
        failed = []
        for j in get_manager().list_jobs(limit=500):
            if j.get("status") in ("failed", "interrupted") and str(j.get("finished_at") or j.get("created_at") or "") >= cut:
                full = get_manager().get(j["job_id"])
                failed.append({"job_id": j["job_id"], "kind": j.get("kind"), "status": j["status"], "models": j.get("models"),
                               "finished_at": j.get("finished_at"), "error": (full.get("error") or full.get("message") or "")[:500],
                               "error_code": full.get("error_code")})
        problems = recent(hours)
        return jsonify({"hours": hours, "problems": problems, "failed_jobs": failed,
                        "counts": {"errors": sum(1 for p in problems if p["level"] in ("ERROR", "CRITICAL")),
                                   "warnings": sum(1 for p in problems if p["level"] == "WARNING"), "failed_jobs": len(failed)}})
