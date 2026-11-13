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
        assert m["low_util_share"] == 0 and m["low_util_energy_share"] == 0          # always busy
    # every other sample nearly idle (util 5%): half the time, and the 70 W samples' share of the energy
    energy["util_samples"] = [(t, 5.0 if i % 2 == 0 else 90.0) for i, t in enumerate(ts)]
    gt = analysed(golden, real=True, energy=energy)["gpu_timeline"]
    assert gt["low_util_pct"] == 10
    for m in gt["per_model"]:
        inw = [i for i, t in enumerate(ts) if win[m["model"]][0] <= t <= win[m["model"]][1]]
        low = [i for i in inw if i % 2 == 0]
        watts = lambda ix: sum(70.0 + (i % 2) * 10 for i in ix)  # noqa: E731
        assert m["low_util_share"] == pytest.approx(len(low) / len(inw))
        assert m["low_util_energy_share"] == pytest.approx(watts(low) / watts(inw))


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


def test_decide_failure_rate_counts_each_failing_flow_once():
    # flows 1-2: Decide replied nothing, so the label is also Unparseable; flow 3 is fine
    sr = [{"model": "m", "scenario_id": f"s{i}", "predicted_label": "Unparseable" if i < 3 else "Benign",
           "scenario_total_time_s": 1.0, "completed_at": None} for i in (1, 2, 3)]
    am = [{"model": "m", "scenario_id": f"s{i}", "agent_name": ag, "wall_time_s": 0.25, "input_tokens": 10,
           "output_tokens": 0 if (ag == "decide" and i < 3) else 5, "vram_delta_mb": None, "ttft_s": None}
          for i in (1, 2, 3) for ag in ("perceive", "reason", "decide", "act")]
    ag = analyses.agents_block(sr, am, {"M": "m"}, {})
    dec = next(a for a in ag["per_model"][0]["agents"] if a["agent"] == "decide")
    assert dec["failures"]["empty_output"] == 2 and dec["failures"]["unparseable_label"] == 2
    assert dec["failure_rate"] == pytest.approx(2 / 3)          # not (2 + 2) / 3


def test_drift_direction_and_wording_never_disagree():
    # rises through the run (rho > 0, p small), but the last quarter's MEDIAN is 12% below the first's:
    # no lasting slow-down — it must not say "12% above"
    last = [0.88 if i % 2 == 0 else 9.0 for i in range(25)]
    xs = [1.0] * 5 + [1.0] * 25 + [2.0 + 3.0 * i / 49 for i in range(50)] + last
    d = drift_test(xs)
    assert d["spearman_rho"] > 0 and d["p_value"] < 0.05 and d["ratio"] < 1
    assert d["verdict"] == "stable" and d["note"].startswith("No lasting drift") and "above" not in d["note"]


# --- annotations: flow starts and agent turns (run/extras.py "flow_times") ----------------------
def test_timeline_markers_place_each_agent_turn_from_the_flow_start():
    ft = {"m/a": {"flows": [{"scenario_id": "f1", "t": 105.0, "agents": [["perceive", 0.5], ["reason", 1.0], ["decide", 0.25]]},
                            {"scenario_id": "f0", "t": 100.0, "agents": [["perceive", 0.4]]}]},
          "m/b": None}
    mk = analyses.timeline_markers(ft, t0=100.0)
    assert [f["flow"] for f in mk["flows"]] == ["f0", "f1"]                       # in time order
    f1 = mk["flows"][1]
    assert f1["t_s"] == 5.0 and f1["end_s"] == 6.75
    assert f1["agents"] == [["perceive", 5.0, 5.5], ["reason", 5.5, 6.5], ["decide", 6.5, 6.75]]
    assert not mk["agent_turns_thinned"] and "outside every timed call" in mk["method"]


def test_timeline_markers_thin_agent_turns_beyond_the_cap(monkeypatch):
    monkeypatch.setattr(analyses, "MARKERS_MAX_AGENT_TURNS", 3)
    ft = {"m": {"flows": [{"scenario_id": f"f{i}", "t": float(i), "agents": [["a", 0.1], ["b", 0.1]]} for i in range(3)]}}
    mk = analyses.timeline_markers(ft, t0=0.0)
    assert mk["agent_turns_thinned"] and len(mk["flows"]) == 3 and all(f["agents"] == [] for f in mk["flows"])


def test_the_timeline_carries_markers_and_a_synthetic_source_is_labelled(golden):
    label_of = {pm["model"]: pm["db_label"] for pm in golden["payload"]["per_model"]}
    sr, _ = analyses.read_rows(golden["plan"]["db_path"], golden["run_id"])
    ft = {m: {"flows": [{"scenario_id": "x", "t": 1010.0, "agents": [["perceive", 1.0]]}]} for m in label_of}
    energy = {"method": "synthetic (test only)", "interval_s": 0.1, "samples": [(1000.0, 60.0), (1020.0, 90.0)]}
    gt = analyses.gpu_timeline_block(sr, label_of, real=False, energy=energy, flow_times=ft)
    assert gt["sampled"] and gt["synthetic"] and len(gt["markers"]["flows"]) == len(label_of)
    assert gt["markers"]["flows"][0]["agents"] == [["perceive", 10.0, 11.0]]
    assert analyses.gpu_timeline_block(sr, label_of, real=False, energy={"method": "nvml", "samples": [(1.0, 1.0)]})["sampled"] is False


# ---------------------------------------------------------------------------
# GPU health: was the GPU normal while each model ran?
# ---------------------------------------------------------------------------
def _health(monkeypatch, samples, foreign=None, max_sm=2000.0):
    monkeypatch.setattr(analyses, "model_windows", lambda sr, label_of: {"m/a": (0.0, 100.0), "m/b": (200.0, 300.0)})
    energy = {"health_samples": samples, "max_sm_mhz": max_sm, "foreign_processes": foreign or {}, "method": "nvml"}
    return analyses.gpu_health_block([], {"m/a": "m/a", "m/b": "m/b"}, real=True, energy=energy)


def test_gpu_health_normal_when_clocks_and_temperature_are_fine(monkeypatch):
    s = [(t, 1900.0, 60.0, 0x4 if t % 20 == 0 else 0) for t in range(0, 101, 5)] + [(t, 1950.0, 58.0, 0) for t in range(200, 301, 5)]
    h = _health(monkeypatch, s)
    assert h["available"] and h["status"] == "normal"
    a = next(m for m in h["per_model"] if m["model"] == "m/a")
    assert a["status"] == "normal" and a["reasons"] == [] and a["power_cap_share"] > 0     # power cap is only reported
    assert a["clock_share"] == pytest.approx(0.95)


def test_gpu_health_flags_thermal_throttling_low_clocks_heat_and_a_shared_gpu(monkeypatch):
    s = [(t, 1100.0, 88.0, 0x20) for t in range(0, 101, 5)] + [(t, 1950.0, 58.0, 0) for t in range(200, 301, 5)]
    h = _health(monkeypatch, s, foreign={"m/b": [{"pid": 4242, "used_mb": 3000}]})
    assert h["status"] == "check"
    a, b = (next(m for m in h["per_model"] if m["model"] == x) for x in ("m/a", "m/b"))
    assert a["status"] == "check" and a["throttle_kinds"] == ["thermal slowdown (software)"]
    assert any("slowed the GPU down" in r for r in a["reasons"]) and any("55%" in r for r in a["reasons"]) and any("88 °C" in r for r in a["reasons"])
    assert b["status"] == "check" and b["reasons"] == ["1 other process held GPU memory (3,000 MB) — the GPU was shared"]


def test_gpu_health_counts_samples_around_a_short_run(monkeypatch):
    # A model whose flows all finish within one second: completed_at (1 s resolution) gives a window
    # of a few ms at the middle of that second, between two 0.1 s samples. Its readings still count.
    monkeypatch.setattr(analyses, "model_windows", lambda sr, label_of: {"m/a": (1000.5, 1000.501)})
    energy = {"health_samples": [(1000.0 + i / 10 + 0.05, 1800.0, 55.0, 0) for i in range(10)],
              "max_sm_mhz": 1800.0, "method": "synthetic (test only)", "interval_s": 0.1}
    h = analyses.gpu_health_block([], {"m/a": "m/a"}, real=False, energy=energy)
    assert h["available"] and h["synthetic"] and h["status"] == "normal"
    assert h["per_model"][0]["n_samples"] >= 1


def test_gpu_health_not_available_without_samples_or_on_demo():
    assert analyses.gpu_health_block([], {}, real=False)["available"] is False
    assert "not sampled" in analyses.gpu_health_block([], {}, real=True, energy={})["reason"]


def test_session_summary_carries_versions_and_gpu_health():
    from agentmeter.server.sessions import env_brief, health_brief
    res = {"environment": {"driver_version": "550.54", "torch": "2.4.0", "gpu_name": "L4", "engine": "transformers"},
           "gpu_health": {"available": True, "status": "check", "per_model": [{"model": "org/m", "reasons": ["the GPU reached 90 °C"]}]}}
    assert env_brief(res) == {"driver_version": "550.54", "torch": "2.4.0", "engine": "transformers"}
    assert health_brief(res) == {"status": "check", "reasons": ["m: the GPU reached 90 °C"], "synthetic": False}
    assert health_brief({}) is None
