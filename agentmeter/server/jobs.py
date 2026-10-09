"""Benchmark JOBS (web service, Phase 40): run a benchmark session in the
background, persistently, so it outlives the browser and the server process.

The job layer only WRAPS the existing session code: a job's work is exactly
`session.benchmark.run_session(run_dir, models, ...)` — the Phase 38 runner
(sequential subprocess-per-model `run_full`, instrumentation, SAW, comparison).
Nothing about how a benchmark is computed changes here.

Phase 43b adds a second job KIND on the same layer: "prepare" (download or take
an uploaded raw file, then prepare.py turns it into a prepared set). Both kinds
share the one worker thread, so the single-job lock covers ingestion too.
Jobs without a "kind" are benchmark jobs (records from before Phase 43b).

Design (kept deliberately simple):
  * Store: one JSON file per job in results/jobs/<job_id>.json, written
    atomically (temp file + os.replace), so a crash never leaves a torn record.
  * Single-job lock: ONE background worker thread runs jobs one at a time from
    a FIFO queue (GPU/VRAM safety). A create while busy is accepted as "queued".
  * Status: queued -> running -> done | failed | interrupted.
  * Restart: a new JobManager re-reads the store. A job left "running" by a dead
    process becomes "interrupted" (resumable); "queued" jobs are re-queued in
    creation order.
  * Resume: re-queues an interrupted/failed job. run_full resumes the incomplete
    run in that session.db per scenario (completed flows are skipped).
  * Progress: derived live from the run's own session.db (completed scenarios
    per model vs flows x models), so it is correct even after a restart.
  * No GPU: a job whose provider needs the GPU (hf with require_gpu) is
    rejected at create time with a clear error, never silently run on CPU.
"""
from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import PROJECT_ROOT, load_config

DEFAULT_JOBS_DIR = PROJECT_ROOT / "results" / "jobs"
DEFAULT_RESULTS_ROOT = PROJECT_ROOT / "results"
RUN_ROOTS = ("csv_runs", "pcap_runs")
CONFIGS_DIR = PROJECT_ROOT / "configs"

TERMINAL = ("done", "failed", "interrupted")
RESUMABLE = ("interrupted", "failed")
_JOB_ID = re.compile(r"^job_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$")
_RUN_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class JobError(ValueError):
    """A job request is invalid. `code` is a stable machine-readable reason."""

    def __init__(self, message: str, code: str = "bad_request", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _order(job: dict[str, Any]) -> tuple:
    """Creation order: nanosecond stamp (same-second jobs stay FIFO), then id."""
    return (int(job.get("created_ns") or 0), job.get("created_at", ""), job["job_id"])


def _new_job_id() -> str:
    return f"job_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


def default_gpu_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Default work + progress: thin wrappers over the existing session code
# ---------------------------------------------------------------------------
def run_benchmark_job(job: dict[str, Any]) -> str:
    """A job's work: the Phase 38 session, unchanged. Returns the results path."""
    from ..session.benchmark import RESULTS_JSON, run_session

    run_session(job["run_dir"], job["models"], base_config=job.get("base_config"),
                provider=job.get("provider"), fresh=False)
    return str(Path(job["run_dir"]) / RESULTS_JSON)


def run_prepare_job(job: dict[str, Any], report: Callable[..., None]) -> str:
    """A prepare job's work: download (URL source) if not done yet, then the
    existing ingestion via prepare.prepare_file. Returns the prepared-set id."""
    from .prepare import prepare_file
    from .urlfetch import download

    src = dict(job["source"])
    lim = job["limits"]
    path = Path(src["path"]) if src.get("path") else None
    if src["kind"] == "url" and (path is None or not path.exists()):
        report(phase="downloading", bytes=0, total=None, message="connecting")
        d = download(src["url"], Path(job["uploads_dir"]), max_bytes=lim["max_url_download_bytes"],
                     timeout_s=lim["url_timeout_s"], progress=report, stem=job["name"])
        src.update(path=str(d.path), filename=d.filename, size_bytes=d.size_bytes, sha256=d.sha256,
                   final_url=d.final_url, content_type=d.content_type, redirects=d.redirects,
                   detected=d.detected)
        report(source=src)                       # persisted: a resume does not re-download
        path = d.path
    public = {k: src.get(k) for k in ("kind", "filename", "url", "final_url", "size_bytes", "sha256",
                                       "content_type", "redirects") if src.get(k) is not None}
    return prepare_file(path, source=public, results_root=Path(job["results_root"]), name=job["name"],
                        max_flows=int(job["max_flows"]), other_attack=bool(job["other_attack"]),
                        limits=lim, progress=report)


def session_progress(job: dict[str, Any]) -> dict[str, Any]:
    """Completed scenarios per model, read-only from the run's session.db (latest run)."""
    from ..session.benchmark import SESSION_DB

    models = list(job["models"])
    total_flows = int(job.get("n_flows") or 0)
    per_model = {m: 0 for m in models}
    db = Path(job["run_dir"]) / SESSION_DB
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                row = con.execute("SELECT run_id FROM runs ORDER BY started_at DESC, rowid DESC "
                                  "LIMIT 1").fetchone()
                if row:
                    for model, n in con.execute(
                            "SELECT model, COUNT(*) FROM scenario_results WHERE run_id=? AND "
                            "status='complete' GROUP BY model", (row[0],)):
                        for m in models:            # DB label is the model id, or mock:<id>
                            if model in (m, f"mock:{m}"):
                                per_model[m] = int(n)
            finally:
                con.close()
        except sqlite3.Error:
            pass                                    # DB being created/locked: report what we have
    completed = sum(per_model.values())
    total = total_flows * len(models)
    current = next((m for m in models if per_model[m] < total_flows), None)
    return {"completed": completed, "total": total, "per_model": per_model,
            "current_model": current if job.get("status") == "running" else None,
            "percent": round(100.0 * completed / total, 1) if total else 0.0}


# ---------------------------------------------------------------------------
# Job manager
# ---------------------------------------------------------------------------
class JobManager:
    def __init__(self, jobs_dir: str | Path = DEFAULT_JOBS_DIR,
                 results_root: str | Path = DEFAULT_RESULTS_ROOT,
                 runner: Callable[[dict], str] = run_benchmark_job,
                 progress_fn: Callable[[dict], dict] = session_progress,
                 prepare_runner: Callable[[dict, Callable], str] = run_prepare_job,
                 gpu_available: Callable[[], bool] = default_gpu_available,
                 autostart: bool = True):
        self.jobs_dir = Path(jobs_dir)
        self.results_root = Path(results_root)
        self.runner = runner
        self.progress_fn = progress_fn
        self.prepare_runner = prepare_runner
        self.gpu_available = gpu_available
        self._lock = threading.RLock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._autostart = autostart
        self._recover()

    # --- store -------------------------------------------------------------------
    def _path(self, job_id: str) -> Path:
        if not _JOB_ID.match(job_id or ""):
            raise JobError(f"no such job: {job_id!r}", "not_found", 404)
        return self.jobs_dir / f"{job_id}.json"

    def _save(self, job: dict[str, Any]) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        job["updated_at"] = _now()
        path = self._path(job["job_id"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
        os.replace(tmp, path)                       # atomic on POSIX/NTFS

    def _load(self, job_id: str) -> dict[str, Any]:
        path = self._path(job_id)
        if not path.exists():
            raise JobError(f"no such job: {job_id}", "not_found", 404)
        return json.loads(path.read_text(encoding="utf-8"))

    def _update(self, job_id: str, **fields) -> dict[str, Any]:
        with self._lock:
            job = self._load(job_id)
            job.update(fields)
            self._save(job)
            return job

    def _all(self) -> list[dict[str, Any]]:
        if not self.jobs_dir.exists():
            return []
        jobs = []
        for p in self.jobs_dir.glob("job_*.json"):
            try:
                jobs.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(jobs, key=_order)

    def _recover(self) -> None:
        """Startup: running -> interrupted (process died); re-queue queued jobs (FIFO)."""
        for job in self._all():
            if job["status"] == "running":
                self._update(job["job_id"], status="interrupted", finished_at=_now(),
                             message="the server stopped while this job was running; "
                                     "resume to continue from the last completed flow")
            elif job["status"] == "queued":
                self._enqueue(job["job_id"])

    # --- validation ----------------------------------------------------------------
    def resolve_run(self, run: str | Path) -> Path:
        """A run name ('csv_runs/<name>', 'pcap_runs/<name>' or '<name>') under the
        results root, or an existing directory path (Python API)."""
        s = str(run or "").strip().strip("/")
        if not s:
            raise JobError("missing 'run' (e.g. csv_runs/<name>)", "run_missing", 400)
        parts = s.split("/")
        if len(parts) == 2 and parts[0] in RUN_ROOTS and _RUN_NAME.match(parts[1]):
            cands = [self.results_root / parts[0] / parts[1]]
        elif len(parts) == 1 and _RUN_NAME.match(parts[0]):
            cands = [self.results_root / r / parts[0] for r in RUN_ROOTS]
        else:
            raise JobError(f"invalid run name {s!r} (use csv_runs/<name> or pcap_runs/<name>)",
                           "bad_request", 400)
        for c in cands:
            if (c / "input.json").exists():
                return c.resolve()
        raise JobError(f"prepared run not found: {s} (ingest the file first)", "run_missing", 404)

    def _base_config(self, name: Optional[str]) -> Optional[str]:
        """None (config.yaml), a file name in configs/ ('run_full_l4.yaml' or
        'configs/run_full_l4.yaml'), or an absolute path (Python API only)."""
        if not name:
            return None
        p = Path(name)
        if not p.is_absolute():
            p = PROJECT_ROOT / "config.yaml" if p.name == "config.yaml" else CONFIGS_DIR / p.name
        if not p.is_file() or p.suffix not in (".yaml", ".yml"):
            raise JobError(f"unknown base config {name!r} (use a file in configs/)", "bad_request", 400)
        return str(p)

    def validate(self, run_dir: Path, models: list[str], provider: Optional[str],
                 base_config: Optional[str]) -> dict[str, Any]:
        from ..ingest.unified import InputContractError, load_input_run
        from ..session.benchmark import SessionError, validate_models

        try:
            models = validate_models(models or [])
        except SessionError as e:
            code = "too_many_models" if "at most" in str(e) else "bad_models"
            raise JobError(str(e), code, 400) from e
        if provider not in (None, "mock", "hf"):
            raise JobError(f"unknown provider {provider!r} (mock | hf)", "bad_request", 400)
        try:
            meta = load_input_run(run_dir).metadata
        except (InputContractError, FileNotFoundError, OSError, ValueError) as e:
            raise JobError(f"prepared run is not usable: {e}", "run_missing", 404) from e
        cfg = load_config(base_config)
        eff_provider = provider or cfg.get("model.provider")
        if eff_provider == "hf" and not self.gpu_available():
            raise JobError("no GPU available: real models need a CUDA GPU (require_gpu) — this job "
                           "will not fall back to CPU. Run on a GPU host (e.g. Colab A100).",
                           "no_gpu", 503)
        return {"models": models, "provider": eff_provider,
                "n_flows": int(meta.get("rows_selected") or 0),
                "evaluation_mode": meta.get("evaluation_mode"),
                "class_scheme": (meta.get("class_scheme") or {}).get("name")}

    # --- public API ------------------------------------------------------------------
    def create_job(self, run: str | Path, models: list[str], provider: Optional[str] = None,
                   base_config: Optional[str] = None) -> dict[str, Any]:
        """Validate, persist as 'queued', enqueue; returns immediately."""
        run_dir = Path(run).resolve() if Path(str(run)).is_absolute() else self.resolve_run(run)
        if not (run_dir / "input.json").exists():
            raise JobError(f"prepared run not found: {run_dir}", "run_missing", 404)
        cfg_path = self._base_config(base_config)
        v = self.validate(run_dir, models, provider, cfg_path)
        job = {
            "job_id": _new_job_id(), "status": "queued",
            "run_dir": str(run_dir), "run_name": f"{run_dir.parent.name}/{run_dir.name}",
            "models": v["models"], "provider": provider, "effective_provider": v["provider"],
            "base_config": cfg_path, "n_flows": v["n_flows"],
            "evaluation_mode": v["evaluation_mode"], "class_scheme": v["class_scheme"],
            "created_at": _now(), "created_ns": time.time_ns(),   # FIFO order key
            "started_at": None, "finished_at": None,
            "attempts": 0, "message": "queued", "error": None, "result_path": None,
            "non_validated": True,
        }
        with self._lock:
            self._save(job)
            self._enqueue(job["job_id"])
        return self.get(job["job_id"])

    def create_prepare_job(self, source: dict[str, Any], *, name: str, max_flows: int,
                           other_attack: bool, limits: dict[str, Any]) -> dict[str, Any]:
        """Queue a Prepare job. `source` is {"kind": "upload", "path", "filename",
        "size_bytes", "sha256"} (already saved) or {"kind": "url", "url"}."""
        if source.get("kind") not in ("upload", "url"):
            raise JobError("source must be an upload or a URL", "bad_request", 400)
        if source["kind"] == "upload" and not Path(source.get("path") or "").is_file():
            raise JobError("the uploaded file was not saved", "invalid_file", 400)
        if source["kind"] == "url":
            from .urlfetch import UrlImportError, check_url, test_loopback_allowed
            try:
                check_url(source.get("url"), test_loopback_allowed())
            except UrlImportError as e:
                raise JobError(str(e), e.code, e.status) from e
        if not (isinstance(max_flows, int) and 1 <= max_flows <= 500):
            raise JobError("max_flows must be between 1 and 500", "bad_request", 400)
        job = {
            "job_id": _new_job_id(), "kind": "prepare", "status": "queued",
            "name": name, "source": source, "max_flows": max_flows,
            "other_attack": bool(other_attack), "limits": limits,
            "results_root": str(self.results_root), "uploads_dir": str(self.results_root / "uploads"),
            "prepared": None, "prep_progress": {"phase": "queued"},
            "run_name": source.get("filename") or source.get("url"), "models": [],
            "created_at": _now(), "created_ns": time.time_ns(),
            "started_at": None, "finished_at": None,
            "attempts": 0, "message": "queued", "error": None, "result_path": None,
        }
        with self._lock:
            self._save(job)
            self._enqueue(job["job_id"])
        return self.get(job["job_id"])

    def _reporter(self, job_id: str) -> Callable[..., None]:
        """Progress callback for a prepare job: merged into prep_progress (source=
        updates the job's source). Writes at most every 0.5 s unless the phase changes."""
        state = {"t": 0.0, "phase": None}

        def report(**kw) -> None:
            src = kw.pop("source", None)
            now = time.monotonic()
            phase = kw.get("phase", state["phase"])
            if src is None and phase == state["phase"] and now - state["t"] < 0.5:
                return
            state.update(t=now, phase=phase)
            with self._lock:
                job = self._load(job_id)
                if src is not None:
                    job["source"] = src
                job["prep_progress"] = {**(job.get("prep_progress") or {}), **kw}
                if kw.get("message"):
                    job["message"] = kw["message"]
                self._save(job)
        return report

    def resume(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._load(job_id)
            if job["status"] not in RESUMABLE:
                raise JobError(f"job {job_id} is {job['status']}; only interrupted or failed jobs "
                               "can be resumed", "not_resumable", 409)
            self._update(job_id, status="queued", error=None, finished_at=None,
                         message=("queued to retry the prepare step" if job.get("kind") == "prepare"
                                  else "queued to resume (completed flows will be skipped)"))
            self._enqueue(job_id)
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        job = self._load(job_id)
        if job.get("kind") == "prepare":
            job["progress"] = job.get("prep_progress") or {}
        else:
            job["progress"] = job.get("final_progress") or self.progress_fn(job)
        job["result_ready"] = job["status"] == "done" and bool(job.get("result_path")) \
            and Path(job["result_path"]).exists()
        if job["status"] == "queued":
            ahead = [j for j in self._all() if j["status"] in ("queued", "running")
                     and _order(j) < _order(job)]
            job["queue_position"] = len(ahead)
        return job

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        keys = ("job_id", "kind", "status", "run_name", "models", "effective_provider", "prepared",
                "created_at", "started_at", "finished_at", "message")
        return [{**{k: j.get(k) for k in keys}, "kind": j.get("kind") or "benchmark"}
                for j in reversed(self._all())][:max(1, int(limit))]

    def result(self, job_id: str) -> dict[str, Any]:
        job = self._load(job_id)
        if job["status"] != "done":
            raise JobError(f"job {job_id} is {job['status']}; results are available when it is done",
                           "not_ready", 409)
        path = Path(job.get("result_path") or "")
        if not path.exists():
            raise JobError(f"results file missing for job {job_id}", "result_missing", 404)
        if job.get("kind") == "prepare":
            from .prepare import summarize
            return {"prepared": job["prepared"], "summary": summarize(path.parent)}
        return json.loads(path.read_text(encoding="utf-8"))

    def running_job(self) -> Optional[str]:
        return next((j["job_id"] for j in self._all() if j["status"] == "running"), None)

    def wait(self, job_id: str, timeout: float = 120.0) -> dict[str, Any]:
        """Block until the job reaches a terminal state (tests / CLI)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            job = self._load(job_id)
            if job["status"] in TERMINAL:
                return self.get(job_id)
            time.sleep(0.05)
        raise TimeoutError(f"job {job_id} still {self._load(job_id)['status']} after {timeout}s")

    # --- worker (single thread = single-job lock) ---------------------------------------------
    def _enqueue(self, job_id: str) -> None:
        self._queue.put(job_id)
        if self._autostart:
            self.start()

    def start(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="agentmeter-jobs", daemon=True)
                self._worker.start()

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run_one(job_id)
            finally:
                self._queue.task_done()

    def _run_one(self, job_id: str) -> None:
        try:
            job = self._load(job_id)
        except JobError:
            return
        if job["status"] != "queued":             # e.g. duplicate queue entry
            return
        prep = job.get("kind") == "prepare"
        job = self._update(job_id, status="running", started_at=_now(),
                           attempts=int(job.get("attempts") or 0) + 1,
                           message=("preparing the input" if prep else
                                    f"running {len(job['models'])} model(s) sequentially"))
        if prep:
            self._run_prepare(job)
            return
        try:
            result_path = self.runner(job)
        except BaseException as e:               # noqa: BLE001 — a job must never kill the worker
            self._update(job_id, status="failed", finished_at=_now(),
                         error=f"{type(e).__name__}: {e}",
                         error_trace=traceback.format_exc(limit=5),
                         final_progress=self.progress_fn(self._load(job_id)),
                         message="failed — resume to retry from the last completed flow")
            return
        done = self._load(job_id)
        done["status"] = "done"
        self._update(job_id, status="done", finished_at=_now(), result_path=str(result_path),
                     final_progress=self.progress_fn(done), message="done — results ready")

    def _run_prepare(self, job: dict[str, Any]) -> None:
        job_id = job["job_id"]
        report = self._reporter(job_id)
        try:
            prepared = self.prepare_runner(job, report)
        except BaseException as e:               # noqa: BLE001 — a job must never kill the worker
            msg = str(e) if getattr(e, "code", None) else f"{type(e).__name__}: {e}"
            self._update(job_id, status="failed", finished_at=_now(), error=msg,
                         error_code=getattr(e, "code", "prepare_failed"),
                         error_trace=traceback.format_exc(limit=5),
                         message="failed — fix the input or resume to retry")
            return
        kind, name = prepared.split("/")
        self._update(job_id, status="done", finished_at=_now(), prepared=prepared,
                     result_path=str(self.results_root / kind / name / "manifest.json"),
                     prep_progress={**(self._load(job_id).get("prep_progress") or {}), "phase": "done"},
                     message="done — prepared set ready")
