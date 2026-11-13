"""Structured logs and GET /api/logs/recent: WARNING+ kept in memory (newest first), failed runs of
the window, JSON log lines on request, admins only in control-plane mode."""
from __future__ import annotations

import json
import logging

from flask import Flask

from agentmeter.server import access, logbook, runtoken
from agentmeter.server.jobs import JobManager


def test_warnings_and_errors_are_kept_newest_first_with_the_job_id():
    logbook.clear()
    logbook.install(json_logs=False)
    log = logging.getLogger("agentmeter.test")
    log.info("fine, not kept")
    log.warning("model download slow for job_20261102_101010_" + "a" * 32)
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("worker crashed")
    rows = logbook.recent(24)
    assert [r["level"] for r in rows] == ["ERROR", "WARNING"]
    assert rows[1]["job_id"] == "job_20261102_101010_" + "a" * 32
    assert rows[0]["exc"] == "ValueError: boom" and "t" not in rows[0]


def test_json_log_lines():
    rec = logging.LogRecord("agentmeter.x", logging.WARNING, __file__, 1, "hello %s", ("you",), None)
    d = json.loads(logbook.JsonFormatter().format(rec))
    assert d["level"] == "WARNING" and d["msg"] == "hello you" and d["logger"] == "agentmeter.x" and d["ts"].endswith("Z")


def test_the_endpoint_lists_problems_and_failed_runs(tmp_path, monkeypatch):
    logbook.clear()
    logbook.install(json_logs=False)
    from agentmeter.config import PROJECT_ROOT
    from agentmeter.ingest.run import process_csv
    import time
    process_csv(PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv", out_root=tmp_path / "results" / "csv_runs", max_flows=4)
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results", gpu_available=lambda: False, autostart=False)
    j = mgr.create_job("csv_runs/cicids2017_sample", ["mock-a"], provider="mock")["job_id"]
    mgr._update(j, status="failed", error="worker for model 'mock-a' made no progress for 900 s twice", error_code="flow_timeout",
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    app = Flask("t")
    logbook.register_logs(app, lambda: mgr)
    logging.getLogger("agentmeter.test").error("NVML read failed")
    d = app.test_client().get("/api/logs/recent").get_json()
    assert d["counts"]["errors"] == 1 and d["problems"][0]["msg"] == "NVML read failed"
    assert [f["job_id"] for f in d["failed_jobs"]] == [j] and "no progress" in d["failed_jobs"][0]["error"]
    assert d["failed_jobs"][0]["error_code"] == "flow_timeout" and d["counts"]["failed_jobs"] == 1
    monkeypatch.setattr(runtoken, "current", lambda: {"kind": "read", "admin": False})
    assert app.test_client().get("/api/logs/recent").status_code == 403
    monkeypatch.setattr(runtoken, "current", lambda: {"kind": "read", "admin": True})
    assert app.test_client().get("/api/logs/recent").status_code == 200
    assert access.required("GET", "/api/logs/recent")[0] == "read"
