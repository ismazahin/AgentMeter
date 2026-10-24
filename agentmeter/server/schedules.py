"""Scheduled re-runs: repeat a finished session's benchmark on a fixed interval, on THIS backend
(same GPU, same prepared set, same models, settings and pipeline), so History & Trends can show
the same models measured over time.

  GET  /api/schedules                  every schedule, newest first
  POST /api/schedules                  {job_id, every_hours, runs} -> 201 {schedule}
  POST /api/schedules/<id>/cancel      stop a schedule (its owner or an admin) -> {schedule}

Each firing queues an ordinary benchmark job (the FIFO queue, as any session) owned by whoever
created the schedule. A firing is postponed, never stacked: while the schedule's previous run
is still queued or running, or the queue is full, it waits and tries again later.

Limits: every 6 to 168 hours, 1 to 30 runs, at most MAX_ACTIVE_PER_USER active schedules per
user. In control-plane mode creating or cancelling needs a "schedule" run token for the
session (the Worker only issues it to someone who may start benchmarks).

Stored in <jobs dir>/schedules.json. Nothing here changes a stored session.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import runtoken
from .jobs import JobError

MIN_HOURS, MAX_HOURS = 6, 168
MAX_RUNS = 30
MAX_ACTIVE_PER_USER = 3
RETRY_S = 600                     # a postponed firing tries again after 10 minutes
TICK_S = 30.0
_BUSY = ("queued", "running")


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Schedules:
    def __init__(self, get_manager: Callable[[], Any], *, provider: Optional[str] = None,
                 base_config: Optional[str] = None, environment: Optional[Callable[[], dict]] = None,
                 queue_full: Callable[[], bool] = lambda: False, path: Optional[Path] = None,
                 clock: Callable[[], float] = time.time):
        self.get_manager = get_manager
        self.provider, self.base_config, self.environment = provider, base_config, environment
        self.queue_full = queue_full
        self._path = path
        self.clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # --- store ---------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path or Path(self.get_manager().jobs_dir) / "schedules.json"

    def _read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._view(s) for s in sorted(self._read(), key=lambda s: s["created_ts"], reverse=True)]

    @staticmethod
    def _view(s: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in s.items() if k not in ("base_config",)}
        out["next_at"] = _iso(s["next_ts"]) if s.get("active") else None
        out["owner"] = (s.get("user") or {}).get("uname") or None
        return out

    # --- create / cancel -------------------------------------------------------------
    def create(self, job_id: str, every_hours: Any, runs: Any, user: Optional[dict] = None,
               admin: bool = False) -> dict[str, Any]:
        if not isinstance(every_hours, int) or isinstance(every_hours, bool) or not MIN_HOURS <= every_hours <= MAX_HOURS:
            raise JobError(f"every_hours must be a whole number from {MIN_HOURS} to {MAX_HOURS}", "bad_schedule", 400)
        if not isinstance(runs, int) or isinstance(runs, bool) or not 1 <= runs <= MAX_RUNS:
            raise JobError(f"runs must be a whole number from 1 to {MAX_RUNS}", "bad_schedule", 400)
        job = self.get_manager().get(job_id)                   # not_found -> JobError 404
        if (job.get("kind") or "benchmark") != "benchmark" or not job.get("run_dir"):
            raise JobError("only a benchmark session can be repeated", "bad_schedule", 400)
        if job.get("status") != "done":
            raise JobError("only a finished session can be repeated (this one is " + str(job.get("status")) + ")",
                           "bad_schedule", 409)
        owner = (job.get("user") or {}).get("sub") if isinstance(job.get("user"), dict) else None
        if user and not admin and owner and owner != user.get("sub"):
            raise JobError("only the session's owner or an admin can repeat it on a schedule", "forbidden", 403)
        now = self.clock()
        with self._lock:
            rows = self._read()
            who = (user or {}).get("sub")
            mine = [s for s in rows if s.get("active") and (s.get("user") or {}).get("sub") == who]
            if len(mine) >= MAX_ACTIVE_PER_USER:
                raise JobError(f"at most {MAX_ACTIVE_PER_USER} active schedules per user — cancel one first",
                               "too_many_schedules", 409)
            s = {"schedule_id": "sch_" + secrets.token_hex(6), "source_job_id": job_id,
                 "run_name": job.get("run_name"), "models": list(job.get("models") or []),
                 "pipeline": job.get("pipeline"), "provider": job.get("provider"),
                 "base_config": job.get("base_config"), "every_hours": every_hours, "runs_total": runs,
                 "runs_done": 0, "jobs": [], "active": True, "ended": None, "last_error": None, "waiting": None,
                 "user": user, "created_at": _iso(now), "created_ts": now, "next_ts": now + every_hours * 3600}
            rows.append(s)
            self._write(rows)
            return self._view(s)

    def cancel(self, schedule_id: str, user: Optional[dict] = None, admin: bool = False,
               token_job: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            rows = self._read()
            s = next((r for r in rows if r["schedule_id"] == schedule_id), None)
            if s is None:
                raise JobError("no such schedule", "not_found", 404)
            if token_job is not None and token_job != s["source_job_id"]:
                raise JobError("the token was issued for another session", "run_token_mismatch", 403)
            owner = (s.get("user") or {}).get("sub")
            if user and not admin and owner and owner != user.get("sub"):
                raise JobError("only the schedule's owner or an admin can cancel it", "forbidden", 403)
            if s.get("active"):
                s.update(active=False, ended="cancelled", ended_by=(user or {}).get("uname") or None,
                         waiting=None)
                self._write(rows)
            return self._view(s)

    # --- firing ----------------------------------------------------------------------
    def tick(self) -> list[str]:
        """Queue every due re-run; returns the new job ids."""
        now, fired = self.clock(), []
        with self._lock:
            rows = self._read()
            changed = False
            for s in rows:
                if not s.get("active") or s["next_ts"] > now:
                    continue
                changed = True
                job_id = self._fire(s, now)
                if job_id:
                    fired.append(job_id)
            if changed:
                self._write(rows)
        return fired

    def _fire(self, s: dict[str, Any], now: float) -> Optional[str]:
        mgr = self.get_manager()
        last = s["jobs"][-1] if s["jobs"] else None
        if last:
            try:
                busy = mgr.get(last)["status"] in _BUSY
            except JobError:
                busy = False
            if busy:
                s.update(waiting="the previous scheduled run is still queued or running", next_ts=now + RETRY_S)
                return None
        if self.queue_full():
            s.update(waiting="the job queue is full", next_ts=now + RETRY_S)
            return None
        meta = {"user": s.get("user"), "grant_jti": None, "schedule_id": s["schedule_id"]}
        try:
            job = mgr.create_job(s["run_name"], s["models"], provider=s.get("provider"),
                                 base_config=self.base_config or s.get("base_config"),
                                 environment=self.environment() if self.environment else None,
                                 meta=meta, pipeline=s.get("pipeline"))
        except JobError as e:
            s.update(last_error=str(e), waiting=None)
            if e.code in ("run_missing", "bad_models", "too_many_models"):    # will never succeed
                s.update(active=False, ended="failed")
            else:
                s["next_ts"] = now + RETRY_S
            return None
        s["jobs"].append(job["job_id"])
        s["runs_done"] += 1
        s.update(waiting=None, last_error=None, last_fired_at=_iso(now))
        nxt = s["next_ts"] + s["every_hours"] * 3600
        s["next_ts"] = nxt if nxt > now else now + s["every_hours"] * 3600     # never a burst of catch-ups
        if s["runs_done"] >= s["runs_total"]:
            s.update(active=False, ended="completed")
        return job["job_id"]

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="agentmeter-schedules", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — a bad tick never kills the scheduler
                print(f"schedules: tick failed: {type(e).__name__}: {e}")


def register_schedules(app, sched: Schedules) -> None:
    from flask import jsonify, request

    def fail(e: JobError):
        return jsonify({"error": str(e), "code": e.code}), e.status

    def who():
        tok = runtoken.current()
        return (runtoken.user_of(tok), bool((tok or {}).get("admin")), tok)

    @app.route("/api/schedules", methods=["GET", "POST"])
    def schedules_collection():
        if request.method == "GET":
            return jsonify({"schedules": sched.list(), "limits": {"min_hours": MIN_HOURS, "max_hours": MAX_HOURS,
                            "max_runs": MAX_RUNS, "max_active_per_user": MAX_ACTIVE_PER_USER}})
        body = request.get_json(silent=True) or {}
        user, admin, tok = who()
        job_id = str(body.get("job_id") or "")
        if tok is not None and tok.get("job_id") != job_id:
            return fail(JobError("the token was issued for another session", "run_token_mismatch", 403))
        try:
            return jsonify(sched.create(job_id, body.get("every_hours"), body.get("runs"), user, admin)), 201
        except JobError as e:
            return fail(e)

    @app.route("/api/schedules/<schedule_id>/cancel", methods=["POST"])
    def schedule_cancel(schedule_id):
        user, admin, tok = who()
        try:
            return jsonify(sched.cancel(schedule_id, user, admin, token_job=(tok or {}).get("job_id") if tok else None))
        except JobError as e:
            return fail(e)
