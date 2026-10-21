"""Latency over the run (latency_trend) and the GPU timeline (gpu_timeline): the two
additive analyses that read per-flow order and the sampled GPU over time.

Both only ADD keys; every scored key stays as it was (test_scored_keys_golden.py).
"""
from __future__ import annotations

import copy
import json
import random
import shutil
import sys
import types
from pathlib import Path

import pytest

from agentmeter.config import load_config
from agentmeter.session import analyses
from agentmeter.session.analyses import drift_test, rolling_median
from agentmeter.session.benchmark import prepare_session
from agentmeter.session.scoring import score_session

FIXTURE = Path(__file__).parent / "fixtures" / "golden_session"
MODELS = ["Qwen/Qwen2.5-7B-Instruct", "microsoft/Phi-3-mini-4k-instruct"]
SCORED = ["per_model", "comparison", "relative_comparison", "phase8", "phase8_efficiency_only",
          "sensitivity", "statistics", "phase7", "per_agent", "per_class", "advanced", "session"]


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    run = tmp_path_factory.mktemp("trend") / "golden"
    shutil.copytree(FIXTURE, run)
    g = json.loads((run / "golden_session_results.json").read_text())
    plan = prepare_session(run, MODELS, provider="mock")
    payload = score_session(plan, g["session"]["run_id"])
    payload["environment"] = g["environment"]
    return {"run": run, "plan": plan, "run_id": g["session"]["run_id"], "payload": payload,
            "cfg": load_config(plan["config_path"]).data}


def analysed(golden, real=False, **kw):
    p = copy.deepcopy(golden["payload"])
    if real:
        p["environment"] = {**p["environment"], "provider": "real", "gpu_name": "NVIDIA L4"}
    analyses.add_analyses(p, db_path=golden["plan"]["db_path"], run_id=golden["run_id"],
                          run_dir=golden["run"], config_data=golden["cfg"], **kw)
    return p


# --- latency over the run --------------------------------------------------------------
def test_rolling_median_shrinks_at_the_ends():
    assert rolling_median([1, 5, 2, 8, 3], 3) == [3.0, 2.0, 5.0, 3.0, 5.5]


def test_drift_test_verdicts():
    random.seed(46)
    flat = [1 + random.random() * 0.1 for _ in range(60)]
    up = [1 + i * 0.02 + random.random() * 0.05 for i in range(60)]
    down = list(reversed(up))
    assert drift_test(flat)["verdict"] == "stable"
    d = drift_test(up)
    assert d["verdict"] == "slows_down" and d["skipped_warmup"] == 5 and d["n"] == 55 and d["ratio"] > 1.1
    assert d["note"].startswith("Slows down over the run")
    assert drift_test(down)["verdict"] == "speeds_up"
    # a significant but tiny trend (< 10% between quarters) is not called drift
    tiny = [1 + i * 0.0005 for i in range(60)]
    assert drift_test(tiny)["p_value"] < 0.05 and drift_test(tiny)["verdict"] == "stable"
    # constant latency and too few flows
    assert drift_test([2.0] * 30)["verdict"] == "stable"
    few = drift_test([1.0] * 12)
    assert few["tested"] is False and "Too few flows" in few["note"]


def test_latency_trend_block_reads_flows_in_run_order(golden):
    p = analysed(golden)
    lt = p["latency_trend"]
    sr, _ = analyses.read_rows(golden["plan"]["db_path"], golden["run_id"])
    assert lt["point_fields"] == ["flow", "latency_s", "rolling_median_s"]
    for m, pm in zip(lt["per_model"], p["per_model"]):
        xs = [r["scenario_total_time_s"] for r in sr if r["model"] == pm["db_label"]]
        assert m["n"] == len(xs) == 10 and [pt[1] for pt in m["points"]] == xs     # rowid = execution order
        assert [pt[0] for pt in m["points"]] == list(range(1, 11)) and m["window"] == 3
        assert m["drift"]["tested"] is False                                       # 10 flows - 5 warm-up < 10
    for k in SCORED:                                                               # additive only
        assert p[k] == analysed(golden)[k]


def test_latency_trend_thins_long_series(monkeypatch):
    monkeypatch.setattr(analyses, "TREND_MAX_POINTS", 50)
    sr = [{"model": "m", "scenario_total_time_s": float(i)} for i in range(1000)]
    m = analyses.latency_trend_block(sr, {"M": "m"})["per_model"][0]
    assert m["n"] == 1000 and m["thinned"] and len(m["points"]) == 50
    assert m["points"][0][0] == 1 and m["points"][-1][0] == 1000
    assert m["drift"]["verdict"] == "slows_down"                                   # stats use every flow


# --- GPU timeline ------------------------------------------------------------------------
def test_gpu_timeline_not_sampled_on_mock(golden):
    gt = analysed(golden)["gpu_timeline"]
    assert gt["sampled"] is False and "mock" in gt["reason"] and gt["points"] == []


def test_gpu_timeline_marks_model_and_idle_windows(golden):
    label_of = {pm["model"]: pm["db_label"] for pm in golden["payload"]["per_model"]}
    sr, _ = analyses.read_rows(golden["plan"]["db_path"], golden["run_id"])
    win = analyses.model_windows(sr, label_of)
    t0 = min(w[0] for w in win.values()) - 5
    t1 = max(w[1] for w in win.values()) + 5
    ts = [t0 + i * 0.5 for i in range(int((t1 - t0) / 0.5) + 1)]
    energy = {"method": "nvml", "interval_s": 0.5, "samples": [(t, 70.0 + (i % 2) * 10) for i, t in enumerate(ts)],
              "util_samples": [(t, 90.0) for t in ts], "mem_samples": [(t, 6000.0) for t in ts],
              "idle": {next(iter(label_of.values())): {"t0": t0, "t1": t0 + 2}}}
    gt = analysed(golden, real=True, energy=energy)["gpu_timeline"]
    assert gt["sampled"] and gt["has_util"] and gt["has_mem"] and gt["n_samples"] == len(ts)
    assert gt["points"][0][0] == 0 and gt["points"][0][1] == 70.0 and gt["points"][0][2] == 90.0
    assert {w["model"] for w in gt["windows"]} == set(label_of)
    for w in gt["windows"]:
        assert w["from_s"] == pytest.approx(win[w["model"]][0] - t0) and w["to_s"] == pytest.approx(win[w["model"]][1] - t0)
    assert gt["idle"][0]["from_s"] == 0 and gt["idle"][0]["to_s"] == 2
    for m in gt["per_model"]:
        assert m["mean_util_pct"] == 90.0 and m["peak_power_w"] in (70.0, 80.0) and m["peak_mem_mb"] == 6000.0


def test_gpu_timeline_thins_to_bucket_means(golden, monkeypatch):
    monkeypatch.setattr(analyses, "TIMELINE_MAX_POINTS", 10)
    samples = [(1000.0 + i, float(i)) for i in range(100)]
    gt = analyses.gpu_timeline_block([], {}, real=True, energy={"samples": samples})
    assert gt["thinned"] and len(gt["points"]) == 10
    assert gt["points"][0] == [4.5, 4.5, None, None]                               # mean of samples 0..9
    assert gt["has_util"] is False


def test_power_sampler_records_utilisation(monkeypatch):
    from agentmeter.session.power import PowerSampler
    fake = types.SimpleNamespace(nvmlInit=lambda: None, nvmlDeviceGetHandleByIndex=lambda i: "h",
                                 nvmlDeviceGetPowerUsage=lambda h: 50000,
                                 nvmlDeviceGetMemoryInfo=lambda h: types.SimpleNamespace(used=1024 * 1024 * 1024),
                                 nvmlDeviceGetUtilizationRates=lambda h: types.SimpleNamespace(gpu=87))
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    s = PowerSampler(interval_s=0.05)
    assert s.start()
    import time
    time.sleep(0.25)
    r = s.stop()
    assert r["util_samples"] and {u for _, u in r["util_samples"]} == {87.0}
    assert [t for t, _ in r["util_samples"]] == [t for t, _ in r["samples"]][:len(r["util_samples"])]


# --- load scaling (opt-in) -------------------------------------------------------------------
def test_load_test_runs_each_level_and_writes_its_own_file(golden, tmp_path):
    from agentmeter.data.dataset import DatasetLoader
    from agentmeter.providers import get_provider
    from agentmeter.run.load_test import flows_for, run_load_test
    cfg = load_config(golden["plan"]["config_path"])
    cfg.data.setdefault("model", {})["name"] = "mock-a"
    scen = DatasetLoader(cfg).load()
    out = tmp_path / "ls.json"
    r = run_load_test(cfg, get_provider(cfg), scen, "mock:mock-a", out)
    assert json.loads(out.read_text()) == r
    assert [lv["concurrency"] for lv in r["levels"]] == [1, 2, 4]
    assert [lv["n"] for lv in r["levels"]] == [flows_for(c, len(scen)) for c in (1, 2, 4)] == [8, 8, 10]
    for lv in r["levels"]:
        assert lv["error"] is None and lv["throughput_flows_per_s"] == pytest.approx(lv["n"] / lv["wall_s"])
        assert lv["p50_latency_s"] <= lv["p95_latency_s"] and lv["efficiency"] == pytest.approx(lv["speedup_vs_1"] / lv["concurrency"])
    assert r["levels"][0]["speedup_vs_1"] == 1


def test_load_test_records_a_failing_level_and_stops(golden, tmp_path):
    from agentmeter.data.dataset import DatasetLoader
    from agentmeter.providers import get_provider
    from agentmeter.run.load_test import run_load_test
    cfg = load_config(golden["plan"]["config_path"])
    prov = get_provider(cfg)
    real_gen, calls = prov.generate, {"n": 0}

    def gen(*a, **k):                        # fails once the second level is running
        calls["n"] += 1
        if calls["n"] > 8 * 4:
            raise RuntimeError("CUDA out of memory")
        return real_gen(*a, **k)
    prov.generate = gen
    r = run_load_test(cfg, prov, DatasetLoader(cfg).load(), "mock:x", tmp_path / "x.json")
    assert [lv["concurrency"] for lv in r["levels"]] == [1, 2]
    assert r["levels"][1]["error"].startswith("RuntimeError: CUDA out of memory") and "throughput_flows_per_s" not in r["levels"][1]


def test_load_scaling_is_opt_in_per_job(tmp_path):
    from agentmeter.ingest.run import process_csv
    from agentmeter.config import PROJECT_ROOT
    from agentmeter.server.jobs import JobManager
    process_csv(PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv", out_root=tmp_path / "results" / "csv_runs", max_flows=6)
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results")
    off = mgr.create_job("csv_runs/cicids2017_sample", ["mock-a"], provider="mock")
    on = mgr.create_job("csv_runs/cicids2017_sample", ["mock-a", "mock-b"], provider="mock", load_test=True)
    assert mgr.wait(off["job_id"])["status"] == "done" and mgr.wait(on["job_id"])["status"] == "done"
    a, b = mgr.result(off["job_id"]), mgr.result(on["job_id"])
    assert a["load_scaling"]["measured"] is False and "not requested" in a["load_scaling"]["reason"]
    ls = b["load_scaling"]
    assert ls["measured"] and [m["model"] for m in ls["per_model"]] == ["mock-a", "mock-b"]
    assert all([lv["concurrency"] for lv in m["levels"]] == [1, 2, 4] for m in ls["per_model"])
    # the measured flows are the same with or without the extra step (6 flows, none from the load step)
    assert b["per_model"][0]["efficiency"]["n_flows"] == a["per_model"][0]["efficiency"]["n_flows"] == 6
    assert b["session_identity"]["settings_fingerprint"] == a["session_identity"]["settings_fingerprint"]
