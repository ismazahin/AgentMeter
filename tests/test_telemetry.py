"""Live telemetry (agentmeter/telemetry.py + GET /api/jobs/<id>/telemetry): a demo benchmark
reports each step in order, every flow with its per-agent timings, and paging by `since`."""
from __future__ import annotations

import importlib.util
import json

import pytest

from agentmeter import telemetry
from agentmeter.config import PROJECT_ROOT
from agentmeter.ingest.run import process_csv
from agentmeter.server.jobs import JobManager


@pytest.fixture
def app(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    for k in ("AGENTMETER_RUN_TOKEN_SECRET", "AGENTMETER_WORKER_URL", "AGENTMETER_CONTROL_PLANE_URL", "AGENTMETER_BACKEND_SECRET"):
        monkeypatch.delenv(k, raising=False)
    spec = importlib.util.spec_from_file_location("serve_tel", PROJECT_ROOT / "scripts" / "serve.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    process_csv(PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv", out_root=tmp_path / "results" / "csv_runs", max_flows=3)
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results", gpu_available=lambda: False)
    a = srv.create_app(local_results_dir=str(tmp_path / "results"), job_manager=mgr, mode="mock")
    return a.test_client(), mgr


def test_a_demo_benchmark_reports_every_step_in_order(app):
    c, mgr = app
    job = mgr.create_job("csv_runs/cicids2017_sample", ["mock-a", "mock-b"], provider="mock")
    assert mgr.wait(job["job_id"], timeout=180)["status"] == "done"
    r = c.get(f"/api/jobs/{job['job_id']}/telemetry").get_json()
    stages = [e["stage"] for e in r["events"]]
    order = ["queued", "start", "data", "hf", "worker", "load", "loaded", "measure", "flow", "worker_done", "scoring", "analysis", "results", "done"]
    pos = [stages.index(s) for s in order]
    assert pos == sorted(pos), stages
    assert stages.count("worker") == 2 and stages.count("flow") == 6            # 3 flows x 2 models
    flow = next(e for e in r["events"] if e["stage"] == "flow")
    assert [a["agent"] for a in flow["agents"]] == ["perceive", "reason", "decide", "act"] and flow["of"] == 3
    assert all(e["t"] for e in r["events"]) and "Demo mode" in r["events"][stages.index("hf")]["msg"]
    assert r["status"] == "done" and r["gpu"] is None and r["next"] == len(r["events"])
    more = c.get(f"/api/jobs/{job['job_id']}/telemetry?since={r['next'] - 2}").get_json()
    assert [e["stage"] for e in more["events"]] == stages[-2:] and more["next"] == r["next"]


def test_emit_is_a_no_op_outside_a_job_and_never_raises(tmp_path):
    telemetry.unbind()
    telemetry.emit("x", "nothing bound")                              # no file, no error
    p = tmp_path / "e.jsonl"
    telemetry.bind(p)
    telemetry.emit("x", "one", n=1, obj=object())                     # unserialisable detail: kept as text
    telemetry.unbind()
    p.write_text(p.read_text() + '{"stage": "partial"')               # a line still being written
    ev, nxt = telemetry.read(p)
    assert [e["msg"] for e in ev] == ["one"] and nxt == 1
    assert telemetry.read(tmp_path / "missing.jsonl", 5) == ([], 5)
    telemetry.emit("x", "unwritable", path=tmp_path / "no" / "dir" / "f.jsonl")   # swallowed
