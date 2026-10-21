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
