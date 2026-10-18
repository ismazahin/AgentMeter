"""Download-on-first-use of extra models (agentmeter/server/model_store.py).

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product. No network
and no GPU: the download child process is a small Python script that writes bytes into a
fake Hugging Face cache folder.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from agentmeter.run import runner
from agentmeter.server import model_store as ms
from agentmeter.server.jobs import JobManager
from test_jobs import RUN, manager, root, until  # noqa: F401  (root is a fixture)

GB = 1024 ** 3
MODEL = "Qwen/Qwen2.5-3B-Instruct"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """HF_HOME in tmp; returns a helper that writes a model's snapshot (= on disk)."""
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)

    def put(model, nbytes=1000):
        d = ms.cache_dir(model)
        (d / "blobs").mkdir(parents=True, exist_ok=True)
        (d / "blobs" / "abc").write_bytes(b"x" * nbytes)
        snap = d / "snapshots" / "r1"
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "config.json").write_text("{}")
        (snap / "model.safetensors").symlink_to(d / "blobs" / "abc")
    return put


def fake_spawn(script):
    """A spawn() that runs `script` (Python) in a child process instead of snapshot_download."""
    def spawn(model, log_path):
        with open(log_path, "wb") as log:
            return subprocess.Popen([sys.executable, "-c", script], stdout=log, stderr=subprocess.STDOUT)
    return spawn


def test_download_reports_progress_and_finishes(hub):
    reports = []
    blobs = ms.cache_dir(MODEL) / "blobs"
    script = (f"import os,time,pathlib\np=pathlib.Path(r'{blobs}');p.mkdir(parents=True,exist_ok=True)\n"
              "for i in range(4):\n    (p/'part').write_bytes(b'x'*(i+1)*1000); time.sleep(0.2)\n")
    runner.clear_stop()
    ms.download_model(MODEL, lambda **kw: reports.append(kw), total=4000, spawn=fake_spawn(script),
                      on_disk=lambda m: True, poll_s=0.05)
    assert reports and reports[-1] == {"done_bytes": 4000, "total_bytes": 4000}
    assert any(0 < r["done_bytes"] < 4000 for r in reports)                 # progress while it ran


def test_a_failed_or_gated_download_raises_a_clear_error(hub):
    runner.clear_stop()
    with pytest.raises(ms.ModelDownloadError) as e:
        ms.download_model(MODEL, lambda **kw: None, spawn=fake_spawn("print('GATED: GatedRepoError: 403'); raise SystemExit(3)"),
                          poll_s=0.05)
    assert e.value.code == "gated_no_access" and "accept its licence" in str(e.value)
    with pytest.raises(ms.ModelDownloadError) as e:
        ms.download_model(MODEL, lambda **kw: None, spawn=fake_spawn("print('ConnectionError: boom'); raise SystemExit(2)"),
                          poll_s=0.05)
    assert e.value.code == "download_failed" and "boom" in str(e.value)
    with pytest.raises(ms.ModelDownloadError, match="incomplete"):
        ms.download_model(MODEL, lambda **kw: None, spawn=fake_spawn("pass"), on_disk=lambda m: False, poll_s=0.05)


def test_stop_terminates_the_download(hub):
    runner.clear_stop()
    import threading
    t = threading.Timer(0.5, runner.request_stop, kwargs={"grace_s": 5})
    t.start()
    t0 = time.monotonic()
    with pytest.raises(runner.Stopped):
        ms.download_model(MODEL, lambda **kw: None, spawn=fake_spawn("import time; time.sleep(60)"), poll_s=0.05)
    assert time.monotonic() - t0 < 10 and runner._PROC is None
    runner.clear_stop()


def test_ensure_job_models_only_downloads_what_is_missing(hub):
    hub("Qwen/Qwen2.5-7B-Instruct")                                     # a study model, on disk
    got, reports = [], []
    job = {"provider": "hf", "models": ["Qwen/Qwen2.5-7B-Instruct", MODEL]}
    out = ms.ensure_job_models(job, lambda **kw: reports.append(kw), sizes=lambda m: 6 * GB, free=lambda m: 100 * GB,
                               download=lambda m, report, total: (got.append(m), report(done_bytes=total, total_bytes=total)))
    assert out == got == [MODEL]
    assert [r["phase"] for r in reports][0] == "downloading" and reports[-1]["phase"] == "downloaded"
    assert reports[0]["index"] == 1 and reports[0]["count"] == 1 and reports[0]["total_bytes"] == 6 * GB
    assert ms.ensure_job_models({"provider": "mock", "models": [MODEL]}, lambda **kw: None) == []   # demo: nothing


def test_not_enough_disk_stops_before_downloading(hub):
    with pytest.raises(ms.ModelDownloadError) as e:
        ms.ensure_job_models({"provider": "hf", "models": [MODEL]}, lambda **kw: None, sizes=lambda m: 30 * GB,
                             free=lambda m: 20 * GB, download=lambda *a, **k: pytest.fail("must not download"))
    assert e.value.code == "low_disk" and "bigger disk" in str(e.value)


def test_the_job_shows_the_download_then_runs(root, hub):  # noqa: F811
    seen = []

    def preparer(job, report):
        for b in (0, 3 * GB, 6 * GB):
            report(model=MODEL, index=1, count=1, phase="downloading", done_bytes=b, total_bytes=6 * GB)
            time.sleep(0.6)
        report(model=MODEL, index=1, count=1, phase="downloaded", done_bytes=6 * GB, total_bytes=6 * GB)

    def run(job):
        seen.append(mgr.get(job["job_id"]).get("model_download"))
        return "results.json"
    mgr = JobManager(jobs_dir=root / "jobs", results_root=root / "results", runner=run, gpu_available=lambda: True,
                     model_preparer=preparer)
    job = mgr.create_job(RUN, [MODEL], provider="hf")
    until(lambda: (mgr.get(job["job_id"]).get("model_download") or {}).get("done_bytes", 0) > 0)
    mid = mgr.get(job["job_id"])
    assert mid["status"] == "running" and mid["model_download"]["phase"] == "downloading" and "downloading" in mid["message"]
    assert mgr.wait(job["job_id"], 30)["status"] == "done"
    assert seen[0]["phase"] == "downloaded"                              # the benchmark starts after the download


def test_a_stop_during_the_download_ends_the_job_stopped(root, hub):  # noqa: F811
    def preparer(job, report):
        for _ in range(200):
            report(model=MODEL, index=1, count=1, phase="downloading", done_bytes=1, total_bytes=10)
            time.sleep(0.05)
    mgr = JobManager(jobs_dir=root / "jobs", results_root=root / "results", runner=lambda j: pytest.fail("ran"),
                     gpu_available=lambda: True, model_preparer=preparer, stop_runner=lambda: None)
    job = mgr.create_job(RUN, [MODEL], provider="hf")
    until(lambda: mgr.get(job["job_id"]).get("model_download"))
    mgr.stop(job["job_id"], by="alice")
    assert mgr.wait(job["job_id"], 30)["status"] == "stopped"
