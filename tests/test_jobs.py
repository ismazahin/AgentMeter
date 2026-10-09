"""Phase 40: persistent background benchmark JOBS around the existing session runner.

CPU only, MOCK provider. Jobs run the real Phase 38 session (run_full -> one
worker subprocess per model) unless a test injects a gated runner to observe
queueing/locking deterministically.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
import yaml

from agentmeter.config import PROJECT_ROOT, load_config
from agentmeter.ingest.run import process_csv
from agentmeter.server.jobs import JobError, JobManager, run_benchmark_job

SAMPLE = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
DATASET = PROJECT_ROOT / "data" / "cicids_full_300.csv"
DATASET_SHA256 = "ef1b13787e2c87602033f54d985355515408b4f930c9648cd1fb29cb540392b5"
RUN = "csv_runs/cicids2017_sample"
FLOWS = 6


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _db_hashes():
    return {str(p): _sha(p) for p in (PROJECT_ROOT / "results").glob("*.db")}


@pytest.fixture
def root(tmp_path):
    process_csv(SAMPLE, out_root=tmp_path / "results" / "csv_runs", max_flows=FLOWS)
    return tmp_path


def manager(root, **kw):
    return JobManager(jobs_dir=root / "jobs", results_root=root / "results", **kw)


def until(pred, timeout=60.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    raise TimeoutError("condition not met")


class Gate:
    """A runner that waits for release() before doing the real work; counts concurrency."""

    def __init__(self, real=run_benchmark_job):
        self.real = real
        self.release_evt = threading.Event()
        self.active = 0
        self.max_active = 0
        self.order: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, job):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.order.append(job["job_id"])
        try:
            self.release_evt.wait(30)
            return self.real(job)
        finally:
            with self._lock:
                self.active -= 1

    def release(self):
        self.release_evt.set()


# --- lifecycle --------------------------------------------------------------------------
def test_create_returns_immediately_and_job_runs_to_done(root):
    before = _db_hashes()
    gate = Gate()
    mgr = manager(root, runner=gate)
    t0 = time.monotonic()
    job = mgr.create_job(RUN, ["mock-a", "mock-b"], provider="mock")
    assert time.monotonic() - t0 < 2.0                       # returned before any model ran
    assert job["status"] in ("queued", "running") and job["job_id"].startswith("job_")
    assert job["progress"]["completed"] == 0 and job["progress"]["total"] == FLOWS * 2
    until(lambda: mgr.get(job["job_id"])["status"] == "running")
    running = mgr.get(job["job_id"])
    assert running["started_at"] and running["progress"]["current_model"] == "mock-a"
    gate.release()
    done = mgr.wait(job["job_id"])
    assert done["status"] == "done" and done["result_ready"] and done["finished_at"]
    assert done["progress"]["completed"] == done["progress"]["total"] == FLOWS * 2
    assert done["progress"]["per_model"] == {"mock-a": FLOWS, "mock-b": FLOWS}
    res = mgr.result(job["job_id"])
    assert res["schema"] == "agentmeter.session.v1"
    assert res["comparison"] and res["relative_comparison"]["models"] == ["mock-a", "mock-b"]
    assert res["provenance"]["non_validated"] is True
    # persisted on disk, and the locked study is untouched
    rec = json.loads((root / "jobs" / f"{job['job_id']}.json").read_text())
    assert rec["status"] == "done" and rec["result_path"].endswith("session_results.json")
    assert _db_hashes() == before and _sha(DATASET) == DATASET_SHA256


def test_progress_tracks_completed_scenarios_while_running(root, tmp_path):
    slow = copy.deepcopy(load_config().data)
    slow["model"]["mock"] = {"latency_s": 0.03}               # ~0.12 s per flow (4 agents)
    cfg = tmp_path / "slow.yaml"
    cfg.write_text(yaml.safe_dump(slow))
    mgr = manager(root)
    job = mgr.create_job(RUN, ["mock-a", "mock-b"], provider="mock", base_config=str(cfg))
    seen = []

    def mid():
        p = mgr.get(job["job_id"])["progress"]
        seen.append(p["completed"])
        return 0 < p["completed"] < p["total"]
    until(mid, timeout=60)
    mgr.wait(job["job_id"])
    assert seen == sorted(seen)                                # monotonic
    assert mgr.get(job["job_id"])["progress"]["completed"] == FLOWS * 2


# --- single-job lock + FIFO queue ----------------------------------------------------------
def test_second_job_is_queued_not_run_concurrently(root):
    gate = Gate()
    mgr = manager(root, runner=gate)
    a = mgr.create_job(RUN, ["mock-a"], provider="mock")
    b = mgr.create_job(RUN, ["mock-b"], provider="mock")
    until(lambda: mgr.get(a["job_id"])["status"] == "running")
    qb = mgr.get(b["job_id"])
    assert qb["status"] == "queued" and qb["queue_position"] == 1
    assert mgr.running_job() == a["job_id"]
    gate.release()
    assert mgr.wait(a["job_id"])["status"] == "done"
    assert mgr.wait(b["job_id"])["status"] == "done"
    assert gate.max_active == 1 and gate.order == [a["job_id"], b["job_id"]]   # one at a time, FIFO
    assert [j["job_id"] for j in mgr.list_jobs()] == [b["job_id"], a["job_id"]]


# --- persistence across a restart + resume ---------------------------------------------------
def test_restart_marks_running_interrupted_and_resume_continues_to_done(root, monkeypatch):
    # 1) a job's worker dies after 3 persisted flows (existing test-only fault injection)
    monkeypatch.setenv("AGENTMETER_WORKER_CRASH_AFTER_SCENARIOS", "3")
    mgr = manager(root)
    job = mgr.create_job(RUN, ["mock-a", "mock-b"], provider="mock")
    mgr.wait(job["job_id"])
    db = root / "results" / RUN / "session.db"
    con = sqlite3.connect(db)
    run_ids_before = con.execute("SELECT run_id, status FROM runs").fetchall()
    con.close()
    assert len(run_ids_before) == 1 and run_ids_before[0][1] == "running"   # left incomplete
    # 2) simulate the SERVER dying mid-job: the store still says "running"
    mgr._update(job["job_id"], status="running", finished_at=None)
    monkeypatch.delenv("AGENTMETER_WORKER_CRASH_AFTER_SCENARIOS")

    fresh = manager(root)                                     # restart: re-reads the store
    j = fresh.get(job["job_id"])
    assert j["status"] == "interrupted" and "resume" in j["message"]
    assert j["progress"]["completed"] == 3                    # read back from session.db
    fresh.resume(job["job_id"])
    done = fresh.wait(job["job_id"])
    assert done["status"] == "done" and done["attempts"] == 2
    con = sqlite3.connect(db)
    try:
        runs = con.execute("SELECT run_id, status FROM runs").fetchall()
        n = con.execute("SELECT COUNT(*) FROM scenario_results").fetchone()[0]
    finally:
        con.close()
    assert runs == [(run_ids_before[0][0], "complete")]       # same run RESUMED, not restarted
    assert n == FLOWS * 2                                     # no duplicates
    assert fresh.result(job["job_id"])["per_model"][0]["efficiency"]["n_flows"] == FLOWS


def test_restart_requeues_queued_jobs_in_order(root):
    idle = manager(root, autostart=False)                     # accepted but never started
    a = idle.create_job(RUN, ["mock-a"], provider="mock")
    assert idle.get(a["job_id"])["status"] == "queued"
    gate = Gate()
    fresh = manager(root, runner=gate)                        # restart picks it up
    gate.release()
    assert fresh.wait(a["job_id"])["status"] == "done"


def test_failed_job_is_resumable_and_done_is_not(root, monkeypatch):
    def boom(job):
        raise RuntimeError("worker for model 'mock:mock-a' exited with code 3")
    mgr = manager(root, runner=boom)
    j = mgr.create_job(RUN, ["mock-a"], provider="mock")
    f = mgr.wait(j["job_id"])
    assert f["status"] == "failed" and "exited with code 3" in f["error"]
    mgr.runner = run_benchmark_job
    mgr.resume(j["job_id"])
    assert mgr.wait(j["job_id"])["status"] == "done"
    with pytest.raises(JobError) as e:
        mgr.resume(j["job_id"])
    assert e.value.code == "not_resumable"


# --- rejections ---------------------------------------------------------------------------
@pytest.mark.parametrize("run, models, kw, code", [
    (RUN, ["a", "b", "c"], {"provider": "mock"}, "too_many_models"),
    (RUN, ["a", "a"], {"provider": "mock"}, "bad_models"),
    ("csv_runs/nope", ["a"], {"provider": "mock"}, "run_missing"),
    ("../../etc", ["a"], {"provider": "mock"}, "bad_request"),
    (RUN, ["a"], {"provider": "openai"}, "bad_request"),
    (RUN, ["a"], {"provider": "mock", "base_config": "../secret.yaml"}, "bad_request"),
])
def test_bad_requests_are_rejected_clearly(root, run, models, kw, code):
    mgr = manager(root, autostart=False)
    with pytest.raises(JobError) as e:
        mgr.create_job(run, models, **kw)
    assert e.value.code == code
    assert not list((root / "jobs").glob("*.json")) if (root / "jobs").exists() else True


def test_no_gpu_fails_fast_for_real_models_but_mock_runs(root):
    mgr = manager(root, gpu_available=lambda: False, autostart=False)
    with pytest.raises(JobError) as e:
        mgr.create_job(RUN, ["Qwen/Qwen2.5-7B-Instruct"], provider="hf")
    assert e.value.code == "no_gpu" and e.value.status == 503 and "not fall back to CPU" in str(e.value)
    with pytest.raises(JobError) as e2:   # hf via the base config, no override
        mgr.create_job(RUN, ["Qwen/Qwen2.5-7B-Instruct"], base_config="run_full_l4.yaml")
    assert e2.value.code == "no_gpu"
    assert mgr.create_job(RUN, ["mock-a"], provider="mock")["status"] == "queued"


def test_unknown_job_and_results_before_done(root):
    gate = Gate()
    mgr = manager(root, runner=gate)
    with pytest.raises(JobError) as e:
        mgr.get("job_20990101_000000_abcdef")
    assert e.value.code == "not_found"
    with pytest.raises(JobError):
        mgr.get("../../etc/passwd")                          # ids are validated, no traversal
    j = mgr.create_job(RUN, ["mock-a"], provider="mock")
    with pytest.raises(JobError) as e:
        mgr.result(j["job_id"])
    assert e.value.code == "not_ready" and e.value.status == 409
    gate.release()
    mgr.wait(j["job_id"])


# --- HTTP API ---------------------------------------------------------------------------------
def _client(root, mgr):
    pytest.importorskip("flask")
    from agentmeter import pull_eval
    spec = importlib.util.spec_from_file_location("pull_eval_server",
                                                  PROJECT_ROOT / "scripts" / "pull_eval_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    base = root / "cfg.yaml"
    base.write_text(yaml.safe_dump({"run": {"models": ["mock/m"]}, "model": {"provider": "mock"},
                                    "dataset": {}, "pipeline": {}, "classes": [], "scoring": {},
                                    "storage": {}}))
    pmgr = pull_eval.JobManager(base_config=str(base), canonical_json=str(root / "c.json"),
                                out_dir=str(root / "pulls"))
    return srv.create_app(pmgr, local_results_dir=str(root / "results"), job_manager=mgr).test_client()


def test_http_endpoints_end_to_end(root):
    mgr = manager(root)
    c = _client(root, mgr)
    r = c.post("/api/jobs", json={"run": RUN, "models": ["mock-a", "mock-b"], "provider": "mock"})
    assert r.status_code == 202
    job = r.get_json()
    jid = job["job_id"]
    assert job["status_url"] == f"/api/jobs/{jid}" and job["result_url"] is None
    early = c.get(f"/api/jobs/{jid}/result")
    assert early.status_code in (200, 409)
    until(lambda: c.get(f"/api/jobs/{jid}").get_json()["status"] == "done")
    st = c.get(f"/api/jobs/{jid}").get_json()
    assert st["result_ready"] and st["result_url"] == f"/api/jobs/{jid}/result"
    assert st["progress"]["completed"] == FLOWS * 2 and "error_trace" not in st
    res = c.get(f"/api/jobs/{jid}/result")
    assert res.status_code == 200 and res.get_json()["relative_comparison"]
    lst = c.get("/api/jobs").get_json()
    assert lst["jobs"][0]["job_id"] == jid and lst["running"] is None
    again = c.post(f"/api/jobs/{jid}/resume")
    assert again.status_code == 409 and again.get_json()["code"] == "not_resumable"


@pytest.mark.parametrize("body, status, code", [
    ({"run": RUN, "models": ["a", "b", "c"], "provider": "mock"}, 400, "too_many_models"),
    ({"run": RUN, "models": "mock-a", "provider": "mock"}, 400, "bad_models"),
    ({"run": "csv_runs/missing", "models": ["a"], "provider": "mock"}, 404, "run_missing"),
    ({"run": "/etc/passwd", "models": ["a"], "provider": "mock"}, 400, "bad_request"),
    ({"models": ["a"], "provider": "mock"}, 400, "run_missing"),
    # Phase E: the server decides the provider; a mock server refuses "hf" outright
    # (the no_gpu guard still covers direct JobManager callers, tested above).
    ({"run": RUN, "models": ["Qwen/Qwen2.5-7B-Instruct"], "provider": "hf"}, 400, "provider_mismatch"),
])
def test_http_errors_have_clear_payloads(root, body, status, code):
    mgr = manager(root, gpu_available=lambda: False, autostart=False)
    c = _client(root, mgr)
    r = c.post("/api/jobs", json=body)
    assert r.status_code == status and r.get_json()["code"] == code and r.get_json()["error"]
    nf = c.get("/api/jobs/job_20990101_000000_abcdef")
    assert nf.status_code == 404 and nf.get_json()["code"] == "not_found"
