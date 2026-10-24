"""Scheduled re-runs (server/schedules.py): limits, firing (never stacked), owner checks, and
the control-plane token rules (a "schedule" token for the session)."""
from __future__ import annotations

import importlib.util
import secrets
import time

import pytest
import yaml
from agentmeter.config import PROJECT_ROOT
from agentmeter.ingest.run import process_csv
from agentmeter.server import runtoken
from agentmeter.server.jobs import JobError, JobManager
from agentmeter.server.schedules import RETRY_S, Schedules

SECRET = "test-run-token-secret-0123456789"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def tok(kind, sub="u1", uname="alice", **extra):
    now = int(time.time())
    return {"Authorization": "Bearer " + runtoken.sign({"typ": "run", "aud": "agentmeter-backend", "jti": secrets.token_hex(16),
                                                        "sub": sub, "uname": uname, "kind": kind, "iat": now, "exp": now + 300,
                                                        **extra}, SECRET)}


@pytest.fixture
def mgr(tmp_path):
    process_csv(PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv", out_root=tmp_path / "results" / "csv_runs", max_flows=4)
    m = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results", gpu_available=lambda: False, autostart=False)
    return m


def done_job(mgr, user=None):
    j = mgr.create_job("csv_runs/cicids2017_sample", ["mock-a", "mock-b"], provider="mock",
                       meta={"user": user} if user else None, pipeline=None)
    mgr._update(j["job_id"], status="done")
    while not mgr._queue.empty():
        mgr._queue.get_nowait()
    return j["job_id"]


def test_create_validates_and_fires_on_time_without_stacking(mgr):
    clock = Clock()
    s = Schedules(lambda: mgr, clock=clock)
    src = done_job(mgr)
    for every, runs, msg in ((5, 3, "every_hours"), (169, 3, "every_hours"), (6, 0, "runs"), (6, 31, "runs"), ("6", 3, "every_hours"), (6, True, "runs")):
        with pytest.raises(JobError, match=msg):
            s.create(src, every, runs)
    with pytest.raises(JobError) as e:
        s.create("job_20250101_000000_abcdef", 6, 2)
    assert e.value.status == 404
    sch = s.create(src, 6, 2)
    assert sch["schedule_id"].startswith("sch_") and sch["active"] and sch["models"] == ["mock-a", "mock-b"]
    assert s.tick() == []                                   # not due yet
    clock.t += 6 * 3600
    first = s.tick()
    assert len(first) == 1
    job = mgr.get(first[0])
    assert job["status"] == "queued" and job["schedule_id"] == sch["schedule_id"] and job["models"] == ["mock-a", "mock-b"]
    assert job["run_name"] == "csv_runs/cicids2017_sample"
    # the next firing waits while the previous scheduled run is still queued
    clock.t += 6 * 3600
    assert s.tick() == []
    row = s.list()[0]
    assert "still queued" in row["waiting"] and row["runs_done"] == 1
    mgr._update(first[0], status="done")
    clock.t += RETRY_S
    second = s.tick()
    assert len(second) == 1
    row = s.list()[0]
    assert row["runs_done"] == 2 and not row["active"] and row["ended"] == "completed" and row["next_at"] is None
    clock.t += 30 * 3600
    assert s.tick() == []                                   # a finished schedule never fires again


def test_queue_full_postpones_and_a_missing_set_ends_the_schedule(mgr, tmp_path):
    clock, full = Clock(), {"v": True}
    s = Schedules(lambda: mgr, clock=clock, queue_full=lambda: full["v"])
    src = done_job(mgr)
    s.create(src, 6, 5)
    clock.t += 6 * 3600
    assert s.tick() == [] and "queue is full" in s.list()[0]["waiting"]
    full["v"] = False
    import shutil
    shutil.rmtree(tmp_path / "results" / "csv_runs" / "cicids2017_sample")
    clock.t += RETRY_S
    assert s.tick() == []
    row = s.list()[0]
    assert not row["active"] and row["ended"] == "failed" and "not found" in row["last_error"]


def test_owner_rules_and_per_user_limit(mgr):
    s = Schedules(lambda: mgr, clock=Clock())
    alice, bob = {"sub": "u1", "uname": "alice"}, {"sub": "u2", "uname": "bob"}
    src = done_job(mgr, alice)
    with pytest.raises(JobError, match="owner or an admin"):
        s.create(src, 6, 2, bob)
    ids = [s.create(src, 6, 2, alice)["schedule_id"] for _ in range(3)]
    with pytest.raises(JobError, match="at most 3 active"):
        s.create(src, 6, 2, alice)
    assert s.create(src, 6, 2, bob, admin=True)["owner"] == "bob"
    with pytest.raises(JobError, match="owner or an admin"):
        s.cancel(ids[0], bob)
    with pytest.raises(JobError, match="another session"):
        s.cancel(ids[0], alice, token_job="job_other")
    c = s.cancel(ids[0], alice, token_job=src)
    assert not c["active"] and c["ended"] == "cancelled" and c["ended_by"] == "alice"
    assert s.create(src, 6, 2, alice)["active"]            # a slot is free again
    running = mgr.create_job("csv_runs/cicids2017_sample", ["mock-a"], provider="mock")
    with pytest.raises(JobError, match="only a finished session"):
        s.create(running["job_id"], 6, 2)


@pytest.fixture
def app(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    for k in ("AGENTMETER_ALLOWED_ORIGINS", "AGENTMETER_WORKER_URL", "AGENTMETER_BACKEND_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTMETER_RUN_TOKEN_SECRET", SECRET)
    spec = importlib.util.spec_from_file_location("serve_sch", PROJECT_ROOT / "scripts" / "serve.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    process_csv(PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv", out_root=tmp_path / "results" / "csv_runs", max_flows=4)
    m = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results", gpu_available=lambda: False, autostart=False)
    a = srv.create_app(local_results_dir=str(tmp_path / "results"), job_manager=m, mode="mock")
    return a.test_client(), m, a


def test_http_routes_need_a_schedule_token_for_the_session(app):
    c, mgr, a = app
    src = done_job(mgr, {"sub": "u1", "uname": "alice"})
    body = {"job_id": src, "every_hours": 24, "runs": 7}
    assert c.post("/api/schedules", json=body).status_code == 401                       # no token
    assert c.post("/api/schedules", json=body, headers=tok("read")).get_json()["code"] == "run_token_wrong_kind"
    r = c.post("/api/schedules", json=body, headers=tok("schedule", job_id="job_other"))
    assert r.status_code == 403 and r.get_json()["code"] == "run_token_mismatch"
    r = c.post("/api/schedules", json=body, headers=tok("schedule", sub="u2", uname="bob", job_id=src))
    assert r.status_code == 403 and r.get_json()["code"] == "forbidden"
    r = c.post("/api/schedules", json=body, headers=tok("schedule", job_id=src))
    assert r.status_code == 201
    sch = r.get_json()
    assert sch["owner"] == "alice" and sch["every_hours"] == 24 and sch["runs_total"] == 7 and "base_config" not in sch
    assert c.get("/api/schedules").status_code == 401
    lst = c.get("/api/schedules", headers=tok("read")).get_json()
    assert [x["schedule_id"] for x in lst["schedules"]] == [sch["schedule_id"]] and lst["limits"]["max_runs"] == 30
    r = c.post(f"/api/schedules/{sch['schedule_id']}/cancel", headers=tok("schedule", sub="u2", uname="bob", job_id=src))
    assert r.status_code == 403
    r = c.post(f"/api/schedules/{sch['schedule_id']}/cancel", headers=tok("schedule", sub="u9", uname="root", job_id=src, admin=True))
    assert r.status_code == 200 and r.get_json()["ended"] == "cancelled"
    assert a.config["SCHEDULES"].list()[0]["ended_by"] == "root"


def test_a_scheduled_run_is_marked_in_the_session_list(app):
    c, mgr, a = app
    src = done_job(mgr)
    sched = a.config["SCHEDULES"]
    sched.clock = Clock()
    sched.create(src, 6, 1)
    sched.clock.t += 6 * 3600
    jid = sched.tick()[0]
    from agentmeter.server.sessions import session_summary
    assert session_summary(mgr._load(jid))["schedule_id"].startswith("sch_")
    assert session_summary(mgr._load(src))["schedule_id"] is None
