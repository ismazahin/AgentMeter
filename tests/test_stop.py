"""Stop a benchmark mid-run (queued or running). AgentMeter MEASURES LLM resource efficiency.

* a queued job is stopped before it starts (it never runs);
* a running benchmark: the current model's worker process is terminated (real subprocess), no
  further model starts, the job ends "stopped" (final, not resumable, never scored);
* a running prepare stops at its next progress step;
* only the job's owner or an admin may stop it (control-plane mode), with a single-use token
  bound to that job;
* a stopped job is never uploaded as a session; the Worker is told who stopped it.
"""
from __future__ import annotations

import os
import stat
import sys
import threading
import time

import pytest

from agentmeter.run import runner
from agentmeter.server import control_plane
from agentmeter.server.jobs import JobError, JobManager, JobStopped
from test_control_plane import BACKEND_SECRET, FakeWorker, auth, prepare, tok  # noqa: F401
from test_control_plane import app  # noqa: F401  (fixture)
from test_jobs import RUN, Gate, manager, root, until  # noqa: F401  (root is a fixture)


def test_stop_a_queued_job_before_it_starts(root):
    gate = Gate()
    mgr = manager(root, runner=gate)
    first = mgr.create_job(RUN, ["mock-a"], provider="mock")
    second = mgr.create_job(RUN, ["mock-b"], provider="mock")
    until(lambda: mgr.get(first["job_id"])["status"] == "running")
    out = mgr.stop(second["job_id"], by="alice")
    assert out["status"] == "stopped" and "before it started" in out["message"]
    assert out["stopped_by"] == "alice"
    gate.release_evt.set()
    assert mgr.wait(first["job_id"], 120)["status"] == "done"
    time.sleep(0.3)
    assert gate.order == [first["job_id"]]                     # the stopped one never ran
    with pytest.raises(JobError) as e:
        mgr.resume(second["job_id"])                           # final: restart = a new benchmark
    assert e.value.code == "not_resumable"
    with pytest.raises(JobError) as e:
        mgr.stop(first["job_id"])                              # done jobs cannot be stopped
    assert e.value.code == "not_stoppable" and e.value.status == 409


def _sleeper(tmp_path):
    """An executable that ignores its arguments and sleeps: stands in for a model worker."""
    p = tmp_path / "fake_python"
    p.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(120)\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shebang script")
def test_stop_a_running_benchmark_terminates_the_model_process(root, tmp_path, monkeypatch):
    monkeypatch.setattr(runner.sys, "executable", _sleeper(tmp_path))
    spawned = []

    def run(job):                          # the real per-model spawn path, twice (two models)
        for m in job["models"]:
            spawned.append(m)
            runner._spawn_model_worker("cfg.yaml", m, "run_x", None)
        return "never"

    mgr = manager(root, runner=run)
    job = mgr.create_job(RUN, ["mock-a", "mock-b"], provider="mock")
    until(lambda: runner._PROC is not None)
    proc = runner._PROC
    t0 = time.monotonic()
    out = mgr.stop(job["job_id"], by="bob")
    assert time.monotonic() - t0 < 20
    assert proc.poll() is not None                             # the worker process is gone
    done = mgr.wait(job["job_id"], 30)
    assert done["status"] == "stopped" and done["stopped_by"] == "bob"
    assert "not scored" in done["message"] and not done.get("result_path")
    assert spawned == ["mock-a"]                               # the second model never started
    assert out["job_id"] == job["job_id"]
    # the next job starts clean (the stop flag is cleared)
    monkeypatch.setattr(runner.sys, "executable", sys.executable)
    mgr.runner = lambda j: "ok-path"
    ok = mgr.create_job(RUN, ["mock-a"], provider="mock")
    assert mgr.wait(ok["job_id"], 30)["status"] == "done"


def test_stop_a_running_prepare_at_its_next_progress_step(root):
    started = threading.Event()

    def slow_prepare(job, report):
        started.set()
        for _ in range(400):
            report(phase="reading", message="reading")
            time.sleep(0.05)
        return "csv_runs/never"

    mgr = manager(root, prepare_runner=slow_prepare, stop_runner=lambda: pytest.fail("benchmark stop for a prepare"))
    up = root / "u.csv"
    up.write_text("a,b\n1,2\n")
    job = mgr.create_prepare_job({"kind": "upload", "path": str(up), "filename": "u.csv"}, name="u", max_flows=5,
                                 other_attack=False, limits={})
    assert started.wait(10)
    mgr.stop(job["job_id"])
    assert mgr.wait(job["job_id"], 30)["status"] == "stopped"


def test_a_stop_that_arrives_after_the_run_finished_keeps_it_done(root):
    gate = Gate(real=lambda job: "results.json")
    calls = []
    mgr = manager(root, runner=gate, stop_runner=lambda: calls.append(1))
    job = mgr.create_job(RUN, ["mock-a"], provider="mock")
    until(lambda: mgr.get(job["job_id"])["status"] == "running")
    mgr._stopping.add("other")                                  # unrelated stop does not leak
    gate.release_evt.set()
    assert mgr.wait(job["job_id"], 30)["status"] == "done"


# --- HTTP + control plane --------------------------------------------------------------------
def test_only_the_owner_or_an_admin_can_stop(app):
    c, mgr = app["c"], app["mgr"]

    def held_prepare(job, report):                 # stays running until stopped
        for _ in range(600):
            report(phase="reading", message="reading")
            time.sleep(0.05)
        return "csv_runs/never"

    mgr.prepare_runner = held_prepare
    jid = prepare(c, tok("prepare", sub="owner1", uname="olivia")).get_json()["job_id"]
    until(lambda: mgr.get(jid)["status"] == "running")
    assert c.post(f"/api/jobs/{jid}/stop").status_code == 401                          # no token
    assert c.post(f"/api/jobs/{jid}/stop", headers=auth(tok("resume", sub="owner1", job_id=jid))).status_code == 403
    other_job = "job_20260101_000000_" + "0" * 32
    r = c.post(f"/api/jobs/{jid}/stop", headers=auth(tok("stop", sub="owner1", job_id=other_job)))
    assert r.get_json()["code"] == "run_token_mismatch"                                # token for another job
    r = c.post(f"/api/jobs/{jid}/stop", headers=auth(tok("stop", sub="someone-else", job_id=jid)))
    assert r.status_code == 403 and r.get_json()["code"] == "forbidden"
    assert mgr.get(jid)["status"] == "running"
    t = tok("stop", sub="owner1", uname="olivia", job_id=jid)
    ok = c.post(f"/api/jobs/{jid}/stop", headers=auth(t))
    assert ok.status_code == 202, ok.get_json()
    done = mgr.wait(jid, 30)
    assert done["status"] == "stopped" and done["stopped_by"] == "olivia"
    assert c.post(f"/api/jobs/{jid}/stop", headers=auth(t)).get_json()["code"] == "run_token_used"   # single use
    # an admin may stop someone else's job
    jid2 = prepare(c, tok("prepare", sub="owner2", uname="oscar")).get_json()["job_id"]
    until(lambda: mgr.get(jid2)["status"] == "running")
    adm = c.post(f"/api/jobs/{jid2}/stop", headers=auth(tok("stop", sub="admin1", uname="isma", job_id=jid2, admin=True)))
    assert adm.status_code == 202, adm.get_json()
    assert mgr.wait(jid2, 30)["stopped_by"] == "isma"


def test_a_stopped_benchmark_is_never_uploaded_and_the_worker_hears_who(app, monkeypatch):
    fw = FakeWorker()
    mgr = app["mgr"]
    cp = control_plane.ControlPlane(control_plane.Client(fw.url, BACKEND_SECRET), get_manager=lambda: mgr, mode="mock",
                                    environment=lambda: {})
    job = {"job_id": "job_20261012_000000_" + "a" * 32, "kind": "benchmark", "status": "stopped", "stopped_by": "alice",
           "models": ["m"], "result_path": None}
    mgr._save(dict(job))
    assert cp.upload(job["job_id"]) is False
    assert cp.upload_pending() == 0
    cp._deliver("status", job)
    import json as _json
    sent = [(p, _json.loads(b)) for m, p, b in fw.calls if p.endswith("/status")]
    assert sent == [(f"/api/backend/jobs/{job['job_id']}/status", {"status": "stopped", "stopped_by": "alice"})]
    assert not [p for _, p, _ in fw.calls if "/sessions/" in p]
