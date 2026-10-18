"""Admin 4-bit verification of extra models (model catalog phase 4).

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product. No GPU and no
network: the test-load child process and the Hugging Face records are faked.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from agentmeter.server import control_plane
from agentmeter.server import model_catalog as mc
from agentmeter.server import model_store as ms
from agentmeter.server.jobs import JobManager
from test_control_plane import BACKEND_SECRET, FakeWorker, auth, tok  # noqa: F401
from test_jobs import root, until  # noqa: F401  (root is a fixture)
from test_model_catalog import repo

MODEL = "Qwen/Qwen2.5-3B-Instruct"


@pytest.fixture(autouse=True)
def clean_state():
    mc.set_state({"models": {}, "added": []})
    yield
    mc.set_state({"models": {}, "added": []})


def mgr_with(root, verify):  # noqa: F811
    return JobManager(jobs_dir=root / "jobs", results_root=root / "results", gpu_available=lambda: True,
                      model_preparer=lambda job, report: None, verify_runner=verify)


def test_a_verify_job_records_the_result(root):  # noqa: F811
    m = mgr_with(root, lambda model: {"status": "ok", "weights_mb": 2100.0, "seconds": 40.0})
    job = m.create_verify_job(MODEL, environment={"transformers": "4.46.0", "gpu_name": "NVIDIA L4"},
                              meta={"user": {"sub": "a1", "uname": "isma"}})
    done = m.wait(job["job_id"], 30)
    assert done["status"] == "done" and done["verification"]["status"] == "ok"
    want = {"weights_mb": 2100.0, "gpu": "NVIDIA L4", "transformers": "4.46.0", "by": "isma"}
    assert {k: done["verification"].get(k) for k in want} == want
    assert mc.get_state()["models"][MODEL]["verified"]["status"] == "ok"           # offered at once
    assert all(j["kind"] != "benchmark" for j in m.list_jobs() if j["job_id"] == job["job_id"])


def test_a_failed_load_is_recorded_as_failed(root):  # noqa: F811
    m = mgr_with(root, lambda model: {"status": "failed", "error": "unknown architecture"})
    done = m.wait(m.create_verify_job(MODEL)["job_id"], 30)
    assert done["status"] == "done" and done["verification"]["status"] == "failed"
    assert "unknown architecture" in done["message"]

    def boom(model):
        raise RuntimeError("download failed")
    m2 = mgr_with(root, boom)
    d2 = m2.wait(m2.create_verify_job(MODEL)["job_id"], 30)
    assert d2["status"] == "failed" and d2["verification"]["status"] == "failed"
    assert mc.get_state()["models"][MODEL]["verified"]["status"] == "failed"


def test_verify_needs_a_gpu(root):  # noqa: F811
    m = JobManager(jobs_dir=root / "jobs", results_root=root / "results", gpu_available=lambda: False, autostart=False)
    from agentmeter.server.jobs import JobError
    with pytest.raises(JobError) as e:
        m.create_verify_job(MODEL)
    assert e.value.code == "no_gpu"


def test_verify_model_reads_the_child_result_and_survives_failures(tmp_path):
    def spawn(script):
        def f(model, log):
            with open(log, "wb") as fh:
                return subprocess.Popen([sys.executable, "-c", script], stdout=fh, stderr=subprocess.STDOUT)
        return f
    ok = ms.verify_model(MODEL, spawn=spawn("print('loading'); print('AM_VERIFY ' + '{\"status\": \"ok\", \"weights_mb\": 2000}')"))
    assert ok == {"status": "ok", "weights_mb": 2000}
    bad = ms.verify_model(MODEL, spawn=spawn("raise SystemExit('CUDA error: out of memory')"))
    assert bad["status"] == "failed" and "out of memory" in bad["error"]
    slow = ms.verify_model(MODEL, spawn=spawn("import time; time.sleep(30)"), timeout_s=1)
    assert slow["status"] == "failed" and "timed out" in slow["error"]


def test_the_control_plane_hears_the_result(root):  # noqa: F811
    fw = FakeWorker()
    m = mgr_with(root, lambda model: {"status": "ok"})
    cp = control_plane.ControlPlane(control_plane.Client(fw.url, BACKEND_SECRET), get_manager=lambda: m, mode="real",
                                    environment=lambda: {})
    job = m.wait(m.create_verify_job(MODEL)["job_id"], 30)
    cp._deliver("status", job)
    sent = [json.loads(b) for meth, p, b in fw.calls if p == "/api/backend/models/verified"]
    assert sent and sent[0]["model"] == MODEL and sent[0]["result"]["status"] == "ok"
    assert not [p for _, p, _ in fw.calls if "/sessions/" in p]                     # never uploaded as a session


def test_the_heartbeat_applies_the_admin_state():
    cp = control_plane.ControlPlane.__new__(control_plane.ControlPlane)
    cp.apply_limits, cp.limits = None, None
    cp._got_limits({"model_state": {"models": {MODEL: {"enabled": False}}, "added": [{"id": "x/y", "note": "n"}]}})
    st = mc.get_state()
    assert st["models"][MODEL] == {"enabled": False} and st["added"] == [{"id": "x/y", "note": "n"}]


# ---------------------------------------------------------------------------
# HTTP: POST /api/models/verify (admin run token, real GPU only)
# ---------------------------------------------------------------------------
def make_app(tmp_path, monkeypatch, real, mgr):
    pytest.importorskip("flask")
    from flask import Flask

    from agentmeter.server import access
    monkeypatch.setenv("AGENTMETER_RUN_TOKEN_SECRET", "test-run-token-secret-0123456789")
    monkeypatch.setenv("AGENTMETER_HF_METADATA", "on")
    monkeypatch.setattr(mc, "default_fetcher", lambda mid, token=None, timeout=None: repo(mid, 3.0))
    monkeypatch.setattr("agentmeter.server.runtime.model_on_disk", lambda m: False)
    a = Flask("t")
    access.install(a, mode="real", queue_depth=lambda: 0)
    mc.register_models(a, gpu_vram_mb=lambda: 24576 if real else None, real=real, get_manager=lambda: mgr,
                       environment=lambda: {"gpu_name": "NVIDIA L4"})
    return a.test_client()


def test_verify_endpoint(tmp_path, monkeypatch, root):  # noqa: F811
    m = mgr_with(root, lambda model: {"status": "ok"})
    c = make_app(tmp_path, monkeypatch, True, m)
    body = {"model": MODEL}
    assert c.post("/api/models/verify", json=body).status_code == 401                              # no token
    r = c.post("/api/models/verify", json=body, headers=auth(tok("verify", model=MODEL)))
    assert r.status_code == 403 and r.get_json()["code"] == "forbidden"                            # not an admin
    r = c.post("/api/models/verify", json=body, headers=auth(tok("verify", model="x/other", admin=True)))
    assert r.get_json()["code"] == "run_token_mismatch"
    r = c.post("/api/models/verify", json={"model": "evil/x"}, headers=auth(tok("verify", model="evil/x", admin=True)))
    assert r.get_json()["code"] == "model_not_offered"
    r = c.post("/api/models/verify", json=body, headers=auth(tok("verify", model=MODEL, admin=True, uname="isma")))
    assert r.status_code == 202, r.get_json()
    assert m.wait(r.get_json()["job_id"], 30)["verification"]["status"] == "ok"
    cat = c.get("/api/models/catalog", headers=auth(tok("read"))).get_json()
    row = next(x for x in cat["extra"] if x["id"] == MODEL)
    assert row["available"] and row["verification"]["status"] == "ok" and cat["mode"] == "real"
    other = next(x for x in cat["extra"] if x["id"] != MODEL)
    assert not other["available"] and other["unavailable_reason"] == "not yet verified at 4-bit"


def test_verify_is_not_needed_in_demo_mode(tmp_path, monkeypatch, root):  # noqa: F811
    c = make_app(tmp_path, monkeypatch, False, mgr_with(root, lambda m: {"status": "ok"}))
    r = c.post("/api/models/verify", json={"model": MODEL}, headers=auth(tok("verify", model=MODEL, admin=True)))
    assert r.status_code == 409 and r.get_json()["code"] == "not_needed"
