"""GET /api/gpu (the GPU & Queue page): what runs on the one GPU, what waits in order, what
finished last, and a live GPU reading — none in demo mode (never invented)."""
from __future__ import annotations

from flask import Flask

from agentmeter.config import PROJECT_ROOT
from agentmeter.ingest.run import process_csv
from agentmeter.server import jobs_api
from agentmeter.server.jobs import JobManager


def _app(tmp_path, provider="mock", env=None):
    process_csv(PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv", out_root=tmp_path / "results" / "csv_runs", max_flows=4)
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results", gpu_available=lambda: False, autostart=False)
    app = Flask("t")
    jobs_api.register_jobs(app, lambda: mgr, provider=provider, environment=lambda: env or {"provider": provider})
    return app, mgr


def test_queue_order_running_and_recent(tmp_path):
    app, mgr = _app(tmp_path)
    ids = [mgr.create_job("csv_runs/cicids2017_sample", [m], provider="mock", meta={"user": {"sub": "u1", "uname": "alice"}}, pipeline=None)["job_id"]
           for m in ("mock-a", "mock-b", "mock-c", "mock-d")]
    mgr._update(ids[0], status="done", finished_at="2026-11-02T10:00:00Z")
    mgr._update(ids[1], status="running", started_at="2026-11-02T10:01:00Z")
    d = app.test_client().get("/api/gpu").get_json()
    assert [j["job_id"] for j in d["running"]] == [ids[1]]
    assert [j["job_id"] for j in d["queued"]] == [ids[2], ids[3]]
    assert [j["queue_position"] for j in d["queued"]] == [1, 2]          # behind the running one
    assert d["queued"][0]["owner"] == "alice" and d["queued"][0]["models"] == ["mock-c"]
    assert [j["job_id"] for j in d["recent"]] == [ids[0]]
    assert set(d["running"][0]["progress"]) == {"completed", "total", "percent", "current_model", "phase"}


def test_demo_mode_has_no_gpu_reading(tmp_path, monkeypatch):
    from agentmeter import telemetry
    monkeypatch.setattr(telemetry, "gpu_sample", lambda: {"name": "should not be read"})
    app, _ = _app(tmp_path)
    d = app.test_client().get("/api/gpu").get_json()
    assert d["gpu"] is None and d["gpu_reason"] == "demo mode: no GPU"
    assert d["running"] == [] and d["queued"] == [] and d["recent"] == []


def test_real_mode_reads_the_gpu_now(tmp_path, monkeypatch):
    from agentmeter import telemetry
    sample = {"name": "NVIDIA L4", "util_pct": 37, "mem_used_mb": 5000, "mem_total_mb": 23034, "power_w": 41.5, "temp_c": 52, "t": 1.0}
    monkeypatch.setattr(telemetry, "gpu_sample", lambda: sample)
    app, _ = _app(tmp_path, provider="hf", env={"provider": "real", "gpu_name": "NVIDIA L4", "gpu_vram_total_mb": 23034,
                                                "driver_version": "550.1", "cuda_runtime_version": "12.4", "engine": "transformers"})
    d = app.test_client().get("/api/gpu").get_json()
    assert d["gpu"] == sample and d["gpu_reason"] is None
    assert d["device"] == {"name": "NVIDIA L4", "vram_total_mb": 23034, "driver_version": "550.1", "cuda": "12.4"}
    assert d["engine"] == "transformers"


def test_control_plane_mode_needs_a_read_token():
    from agentmeter.server import access
    assert access.required("GET", "/api/gpu")[0] == "read"
