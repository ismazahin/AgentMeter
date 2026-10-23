"""Session insights (dashboard/insights.js) — the read-only derivations behind the
session tabs, run under node against a small synthetic session_results.json.

Each view only restates stored numbers; these tests pin the arithmetic and the
edge cases (ties, missing readings, unlabelled runs, single-model runs).
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSIGHTS_JS = REPO / "dashboard" / "insights.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")

A, B = "org/model-a", "org/model-b"


def call(fn: str, *args):
    """INSIGHTS.<fn>(*args) under node; returns the JSON result."""
    js = ("const I = require(%s); const a = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
          "process.stdout.write(JSON.stringify(I[%s](...a)));") % (json.dumps(str(INSIGHTS_JS)), json.dumps(fn))
    out = subprocess.run([NODE, "-e", js], input=json.dumps(list(args)), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _agent(name, share, lat):
    return {"agent": name, "latency_share": share, "mean_latency_s": lat, "mean_input_tokens": 100, "mean_output_tokens": 10}


SESSION = {
    "session": {"accuracy_available": True, "n_flows": 40},
    "phase8": {
        "normalised": [
            {"model": "mock:" + A, "accuracy": 0.5, "latency": 1.0, "vram": 0.0, "tokens": 0.8},
            {"model": "mock:" + B, "accuracy": 0.25, "latency": 0.9, "vram": 0.0, "tokens": 0.6},
        ],
        "weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15},
        "targets": {"accuracy_pct": 80.0, "latency_s": 15.0, "tokens_total": 2500.0, "vram_mb": 8000.0},
    },
    "per_model": [
        {"model": A, "accuracy": {"accuracy": 0.40}, "efficiency": {"end_to_end": {"median_latency_s": 2.0, "p95_latency_s": 5.0}}},
        {"model": B, "accuracy": {"accuracy": 0.20}, "efficiency": {"end_to_end": {"median_latency_s": 3.0, "p95_latency_s": 4.5}}},
    ],
    "relative_comparison": {"metrics": [
        {"metric": "mean_latency_s", "label": "mean latency", "available": True, "winner": A, "pct_better": 12.5, "p_value": 0.01},
        {"metric": "mean_tokens_per_flow", "label": "tokens per flow", "available": True, "winner": B, "pct_better": 30.0, "p_value": 0.2},
        {"metric": "mean_peak_vram_mb", "label": "VRAM", "available": False, "winner": None, "pct_better": None},
        {"metric": "accuracy", "label": "accuracy", "available": True, "winner": A, "pct_better": 99.0},
    ]},
    "agents": {"per_model": [
        {"model": A, "agents": [_agent("perceive", 0.2, 0.4), _agent("reason", 0.5, 1.0)], "overhead": {"handoff_share_of_total": 0.10}},
        {"model": B, "agents": [_agent("perceive", 0.6, 1.8), _agent("reason", 0.3, 0.9)], "overhead": {"handoff_share_of_total": 0.25}},
    ]},
    "cost_energy": {"per_model": [
        {"model": A, "cost_per_1k_flows_usd": 0.02, "cost_status": "measured"},
        {"model": B, "cost_per_1k_flows_usd": 0.05, "cost_status": "measured"},
    ]},
}


def test_criteria_profile_flags_missing_criteria_and_strips_mock_prefix():
    pr = call("criteriaProfile", SESSION)
    assert pr["criteria"] == ["accuracy", "latency", "vram", "tokens"]
    assert [r["model"] for r in pr["rows"]] == [A, B]
    assert pr["rows"][1]["scores"]["tokens"] == 0.6
    assert pr["missing"] == {"accuracy": False, "latency": False, "vram": True, "tokens": False}
    unl = copy.deepcopy(SESSION)
    unl["session"]["accuracy_available"] = False
    assert call("criteriaProfile", unl)["missing"]["accuracy"] is True


def test_key_findings_restate_stored_numbers():
    f = {x["key"]: x["text"] for x in call("keyFindings", SESSION)}
    # the biggest efficiency win (accuracy is excluded: context only)
    assert f["gap"] == "Biggest measured gap: model-b is 30% better on tokens per flow (p = 0.20)."
    # the widest p95 / median ratio: A 5/2 = 2.5x
    assert f["tail"].startswith("Slowest 5% of flows: model-a takes 2.5× its median time")
    assert "25% of all tokens for model-b" in f["overhead"]
    assert f["cost"].startswith("Cheapest to run: model-a at $0.0200 per 1,000 flows ($0.0500 for model-b)")
    assert f["accuracy"] == "Accuracy (context): model-a is highest at 40.0%."


def test_key_findings_merge_a_shared_slowest_step_and_report_ties():
    s = copy.deepcopy(SESSION)
    s["agents"]["per_model"][1]["agents"] = [_agent("perceive", 0.1, 0.2), _agent("reason", 0.7, 2.0)]
    s["per_model"][1]["accuracy"]["accuracy"] = 0.40
    s["relative_comparison"]["metrics"] = [m for m in s["relative_comparison"]["metrics"] if not m["available"]]
    s["cost_energy"] = {"per_model": [{"model": A, "cost_per_1k_flows_usd": None, "cost_reason": "mock run"}]}
    out = call("keyFindings", s)
    agents = [x["text"] for x in out if x["key"] == "agent"]
    assert agents == ["Where the time goes: the reason step is the slowest for every model (50% of each flow for model-a, 70% of each flow for model-b)."]
    f = {x["key"]: x["text"] for x in out}
    assert f["gap"] == "No efficiency metric separates the models beyond the tie range."
    assert f["accuracy"] == "Accuracy (context): the models tie at 40.0%."
    assert f["cost"] == "Cost and energy were not measured in this run: mock run."


def test_key_findings_skip_accuracy_when_unlabelled_and_handle_one_model():
    s = copy.deepcopy(SESSION)
    s["session"]["accuracy_available"] = False
    s["relative_comparison"] = None
    s["per_model"] = s["per_model"][:1]
    keys = [x["key"] for x in call("keyFindings", s)]
    assert "accuracy" not in keys and "gap" not in keys and "tail" in keys


def _two(a, b, lab=True):
    return {"session": {"accuracy_available": lab},
            "phase8": {"normalised": [dict(a, model="mock:" + A), dict(b, model="mock:" + B)],
                       "weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15}}}


def test_weight_stability_finds_the_crossing():
    # accuracy weight t, the rest in stored ratio (0.25 : 0.2 : 0.15):
    #   A(t) = 0.2t + (1-t)*0.5417, B(t) = 0.6t + (1-t)*0.3333  ->  cross at t = 0.2083/0.6083 = 0.342
    s = _two({"accuracy": 0.2, "latency": 1.0, "vram": 0.0, "tokens": 0.5},
             {"accuracy": 0.6, "latency": 0.5, "vram": 0.0, "tokens": 0.5})
    st = call("weightStability", s)
    acc = next(r for r in st["rows"] if r["criterion"] == "accuracy")
    assert acc["leader"] == B and acc["changes"] is True
    assert [sg["leader"] for sg in acc["segments"]] == [A, B]
    assert abs(acc["stable"][0] - 0.342) < 0.002 and acc["stable"][1] == 1
    assert call("stabilitySentence", acc) == "model-b stays first while this weight is between 0.34 and 1.00 (stored 0.40)."
    vram = next(r for r in st["rows"] if r["criterion"] == "vram")
    assert vram["no_data"] is True
    assert call("stabilitySentence", vram) == "No readings for this criterion in the run: its weight changes nothing."


def test_weight_stability_ties_point_ties_and_unlabelled():
    same = {"accuracy": 0.3, "latency": 1.0, "vram": 0.0, "tokens": 0.8}
    st = call("weightStability", _two(same, dict(same)))
    assert all(r["leader"] is None for r in st["rows"])
    assert call("stabilitySentence", st["rows"][0]) == "At the stored weight (0.40) the top place is a tie."
    # both models score 1.0 on VRAM: weight exactly 1 ties them; that single point is folded away
    st = call("weightStability", _two({"accuracy": 0.5, "latency": 1.0, "vram": 1.0, "tokens": 0.8},
                                      {"accuracy": 0.2, "latency": 0.9, "vram": 1.0, "tokens": 0.6}))
    vram = next(r for r in st["rows"] if r["criterion"] == "vram")
    assert vram["changes"] is False and vram["point_tie"] == 1
    assert call("stabilitySentence", vram) == "model-a stays first for any weight from 0 to 1 (at exactly 1.00 every model ties)."
    # unlabelled: accuracy is not a criterion; the stored weights renormalise over the other three
    st = call("weightStability", _two(same, {"accuracy": 0, "latency": 0.5, "vram": 0, "tokens": 0.9}, lab=False))
    assert [r["criterion"] for r in st["rows"]] == ["latency", "vram", "tokens"]
    assert abs(st["weights"]["latency"] - 0.25 / 0.6) < 1e-9 and st["weights"]["accuracy"] == 0


def test_class_metrics_from_the_confusion_matrix():
    # rows = truth, columns = predicted (+ Unparseable)
    cm = {"Benign": {"Benign": 6, "DoS": 2, "Scan": 0, "Unparseable": 0},
          "DoS": {"Benign": 1, "DoS": 3, "Scan": 0, "Unparseable": 1},
          "Scan": {"Benign": 4, "DoS": 0, "Scan": 0, "Unparseable": 0}}
    s = {"session": {"accuracy_available": True}, "per_model": [{"model": A, "accuracy": {"confusion": cm}}]}
    m = call("classMetrics", s)["models"][0]
    by = {c["cls"]: c for c in m["classes"]}
    # Benign: tp 6, fp 1+4, support 8 -> P 6/11, R 6/8
    assert by["Benign"]["tp"] == 6 and by["Benign"]["fp"] == 5 and by["Benign"]["support"] == 8
    assert abs(by["Benign"]["precision"] - 6 / 11) < 1e-12 and by["Benign"]["recall"] == 0.75
    # DoS: the unparseable answer is a miss (fn), not a false positive of any class
    assert by["DoS"]["fp"] == 2 and by["DoS"]["fn"] == 2 and by["DoS"]["recall"] == 0.6
    # Scan never predicted: precision undefined, F1 0, counted as 0 in the macro average
    assert by["Scan"]["precision"] is None and by["Scan"]["f1"] == 0
    assert abs(m["macro"]["precision"] - (6 / 11 + 3 / 5 + 0) / 3) < 1e-12
    assert m["unparseable"] == 1 and m["n"] == 17
    assert m["mixups"][0] == {"truth": "Scan", "predicted": "Benign", "count": 4, "share": 1}
    assert [(x["truth"], x["predicted"]) for x in m["mixups"]] == [("Scan", "Benign"), ("Benign", "DoS"), ("DoS", "Benign")]
    assert call("classMetrics", {"session": {"accuracy_available": False}})["available"] is False


def test_latency_histogram_shares_bins_and_scale():
    s = {"latency_distribution": {"bin_edges_s": [1.0, 2.0, 3.0, 5.0], "per_model": [
        {"model": "mock:" + A, "n": 6, "histogram": [1, 4, 1], "p50": 2.0, "p95": 3.0, "p99": 5.0},
        {"model": B, "n": 3, "histogram": [3, 0, 0], "p50": 1.5, "p95": None, "p99": 9.0}]}}
    h = call("latencyHistogram", s)
    assert h["ymax"] == 4 and [m["model"] for m in h["models"]] == [A, B]
    assert h["models"][0]["bins"][2] == {"from": 3.0, "to": 5.0, "count": 1}
    marks = {m["key"]: m["pos"] for m in h["models"][0]["marks"]}
    assert marks == {"p50": 0.25, "p95": 0.5, "p99": 1}
    b = {m["key"]: m["pos"] for m in h["models"][1]["marks"]}
    assert b["p95"] is None and b["p99"] == 1          # clamped to the axis
    assert call("latencyHistogram", {}) is None


def test_effect_sizes_positions_on_a_symmetric_axis():
    s = {"effect_sizes": {"model_a": A, "model_b": B, "metrics": [
        {"metric": "latency_s", "available": True, "cliffs_delta": -0.5, "magnitude": "large", "diff_mean_a_minus_b": -1.0,
         "ci95_low": -2.0, "ci95_high": -0.5, "ci_excludes_zero": True, "kruskal_wallis_p": 0.001},
        {"metric": "peak_vram_mb", "available": False, "reason": "no VRAM readings"}]}}
    e = call("effectSizes", s)
    r = e["rows"][0]
    assert r["half_range"] == 2.0 * 1.15 and r["excludes_zero"] is True
    assert abs(r["pos"]["low"] - (-2.0 + 2.3) / 4.6) < 1e-12 and abs(r["pos"]["diff"] - 1.3 / 4.6) < 1e-12
    assert e["rows"][1] == {"metric": "peak_vram_mb", "available": False, "reason": "no VRAM readings"}
    assert call("effectSizes", {"effect_sizes": {"skipped": "needs two models"}}) is None


def test_handoff_graph_edges_carry_the_senders_output_tokens():
    ag = {"agent_order": ["perceive", "reason", "decide", "act"],
          "handoffs": {"perceive": [], "reason": ["perceive"], "decide": ["reason", "perceive"], "act": ["reason"]},
          "per_model": [{"model": "mock:" + A, "overhead": {"handoff_tokens_per_flow": 16 + 14 + 16 + 14},
                         "agents": [{"agent": "perceive", "mean_output_tokens": 16, "mean_latency_s": 1.0, "latency_share": 0.5},
                                    {"agent": "reason", "mean_output_tokens": 14},
                                    {"agent": "decide", "mean_output_tokens": 2}, {"agent": "act", "mean_output_tokens": 18}]}]}
    g = call("handoffGraph", {"agents": ag})
    m = g["per_model"][0]
    assert m["model"] == A
    assert [(e["from"], e["to"], e["tokens"]) for e in m["edges"]] == [
        ("perceive", "reason", 16), ("reason", "decide", 14), ("perceive", "decide", 16), ("reason", "act", 14)]
    # the edges add up to the stored overhead estimate (same basis)
    assert m["resent_tokens"] == ag["per_model"][0]["overhead"]["handoff_tokens_per_flow"]
    assert m["nodes"][0] == {"agent": "perceive", "latency_s": 1.0, "share": 0.5, "in_tokens": None, "out_tokens": 16}
    assert call("handoffGraph", {}) is None


def test_class_agent_time_stacks_steps_per_class():
    rows = [{"model": "mock:" + A, "class": c, "agent_name": a, "mean_wall_s": t, "n": 5}
            for c, ts in (("Benign", (1.0, 2.0, 1.0, 0.0)), ("DoS", (0.5, 0.5, 3.0, 1.0)))
            for a, t in zip(("perceive", "reason", "decide", "act"), ts)]
    c = call("classAgentTime", {"per_class": {"per_agent": rows}})
    assert c["classes"] == ["Benign", "DoS"] and c["max_total"] == 5.0
    dos = c["per_model"][0]["rows"][1]
    assert dos["total"] == 5.0 and dos["slowest_agent"] == "decide" and dos["n"] == 5
    assert [p["share"] for p in dos["parts"]] == [0.1, 0.1, 0.6, 0.2]
    assert call("classAgentTime", {"per_class": {"per_agent": []}}) is None


def _costed():
    return {"session": {"accuracy_available": True}, "environment": {"provider": "real"},
            "cost_energy": {"price": {"usd_per_hour": 0.8}, "per_model": [
                {"model": A, "cost_per_1k_flows_usd": 12.8 * 1000 / 3600 * 0.8, "wh_per_flow": 0.21, "wh_per_1k_flows": 210.0},
                {"model": B, "cost_per_1k_flows_usd": None, "wh_per_flow": None, "wh_per_1k_flows": None}]},
            "per_model": [{"model": A, "accuracy": {"accuracy": 0.25}, "efficiency": {"end_to_end": {"mean_latency_s": 12.8}}},
                          {"model": B, "accuracy": {"accuracy": 0.0}, "efficiency": {"end_to_end": {"mean_latency_s": 20.9}}}]}


def test_workload_projection_matches_the_stored_cost_basis():
    pj = call("workloadProjection", _costed(), {"flows_per_day": 20000})
    a, b = pj["models"]
    assert pj["price_source"] == "stored" and pj["usd_per_hour"] == 0.8
    assert abs(a["gpu_hours_per_day"] - 20000 * 12.8 / 3600) < 1e-9
    # same basis as the stored cost: 20 x the stored cost per 1,000 flows
    assert abs(a["cost_per_day"] - 20 * _costed()["cost_energy"]["per_model"][0]["cost_per_1k_flows_usd"]) < 1e-9
    assert abs(a["cost_per_month"] - 30 * a["cost_per_day"]) < 1e-9
    assert abs(a["kwh_per_day"] - 4.2) < 1e-12 and b["kwh_per_day"] is None
    assert abs(a["capacity_per_gpu_day"] - 6750) < 1e-9 and a["gpus_needed"] == 3 and abs(a["utilisation"] - 20000 / 6750) < 1e-12
    # your own price wins over the stored one; no price at all -> no cost
    assert call("workloadProjection", _costed(), {"flows_per_day": 1000, "usd_per_hour": "2"})["models"][1]["cost_per_day"] == 1000 * 20.9 / 3600 * 2
    s = _costed(); s["cost_energy"]["price"] = {}
    assert call("workloadProjection", s, {"flows_per_day": 1000})["models"][0]["cost_per_day"] is None
    # exactly one GPU's day is one GPU, not two
    assert call("workloadProjection", _costed(), {"flows_per_day": 6750})["models"][0]["gpus_needed"] == 1


def test_cost_per_correct_answer():
    a, b = call("costPerCorrect", _costed())
    assert abs(a["gpu_s_per_correct"] - 12.8 / 0.25) < 1e-12 and a["flows_per_correct"] == 4
    assert abs(a["usd_per_1k_correct"] - 4 * 12.8 * 1000 / 3600 * 0.8) < 1e-9 and abs(a["wh_per_correct"] - 0.84) < 1e-12
    assert b["gpu_s_per_correct"] is None and b["usd_per_1k_correct"] is None          # no correct answers
    s = _costed(); s["session"]["accuracy_available"] = False
    assert call("costPerCorrect", s) is None


def test_deployment_fit_cells():
    s = {"per_model": [{"model": A, "efficiency": {"end_to_end": {"mean_latency_s": 1.4, "p95_latency_s": 2.0}}},
                       {"model": B, "efficiency": {"end_to_end": {"mean_latency_s": 8.0, "p95_latency_s": 12.5}}}],
         "memory": {"per_model": [{"model": A, "total_peak_mb": 8192}, {"model": B, "total_peak_mb": None, "reason": "no VRAM readings"}]},
         "cost_energy": {"per_model": [{"model": A, "cost_per_1k_flows_usd": 0.31}, {"model": B, "cost_per_1k_flows_usd": None, "cost_reason": "mock run"}]}}
    fit = call("deploymentFit", s)
    ids = [p["id"] for p in fit["profiles"]]
    a = dict(zip(ids, fit["models"][0]["cells"]))
    b = dict(zip(ids, fit["models"][1]["cells"]))
    assert a["gpu8"]["result"] == "pass"                       # at the limit counts as fitting
    assert a["rt2"]["result"] == "pass" and b["rt10"]["result"] == "fail" and b["rt60"]["result"] == "pass"
    assert abs(a["vol10k"]["value"] - 86400 / 1.4) < 1e-9 and a["vol100k"]["result"] == "fail"
    assert b["gpu8"] == {"id": "gpu8", "result": "no_data", "value": None, "reason": "no VRAM readings"}
    assert b["usd1"]["reason"] == "mock run"
    # B: batch (8 s) and 10,000 a day (86,400 / 8 = 10,800) only
    assert fit["models"][0]["passes"] == 9 and fit["models"][1]["passes"] == 2
