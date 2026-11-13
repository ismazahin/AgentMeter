"""Analyse traffic and the Cost planner (web/insights.js), run under node: the IDS figures at a
chosen threshold, and the LLM cost projected from earlier measured sessions."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")


def call(fn, *args):
    js = ("const I = require(%s); const a = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
          "process.stdout.write(JSON.stringify(I[%s](...a)));") % (json.dumps(str(REPO / "web" / "insights.js")), json.dumps(fn))
    return json.loads(subprocess.run([NODE, "-e", js], input=json.dumps(list(args)), capture_output=True, text=True,
                                     check=True).stdout)


ROWS = [  # 3 attacks, 3 benign; two flows in the benchmark sample
    {"flow_id": "a", "score": 0.9, "label": "DoS Hulk", "selected": True},
    {"flow_id": "b", "score": 0.4, "label": "Port Scanning", "selected": False},
    {"flow_id": "c", "score": 0.05, "label": "Port Scanning", "selected": True},
    {"flow_id": "d", "score": 0.6, "label": "Benign", "selected": False},
    {"flow_id": "e", "score": 0.1, "label": "Benign", "selected": False},
    {"flow_id": "f", "score": 0.0, "label": "Benign", "selected": True},
]


def test_the_figures_at_a_threshold():
    t = call("trafficAnalysis", ROWS, 0.3)
    assert (t["flagged"], t["cleared"], t["alerts_in_benchmark_sample"]) == (3, 3, 1)
    assert t["recall"] == pytest.approx(2 / 3) and t["precision"] == pytest.approx(2 / 3)
    assert t["false_positive_rate"] == pytest.approx(1 / 3)
    assert (t["missed_attacks"], t["false_alarms"]) == (1, 1)
    assert t["per_class"][0] == {"cls": "Port Scanning", "n": 2, "recall": 0.5}       # the weakest class first


def test_a_lower_threshold_catches_more_and_raises_more_false_alarms():
    lo, hi = call("trafficAnalysis", ROWS, 0.01), call("trafficAnalysis", ROWS, 0.95)
    assert lo["recall"] == 1 and lo["false_alarms"] == 2
    assert hi["flagged"] == 0 and hi["recall"] == 0 and hi["precision"] is None


def test_unlabelled_traffic_gives_counts_only():
    rows = [dict(r, label=None) for r in ROWS]
    t = call("trafficAnalysis", rows, 0.3)
    assert t["labelled"] is False and t["flagged"] == 3
    assert t["recall"] is None and t["false_positive_rate"] is None and t["per_class"] == []


def _s(sid, at, gpu="NVIDIA L4", **metrics):
    return {"session_id": sid, "status": "Done", "demo": False, "gpu": gpu, "pipeline": None, "finished_at": at,
            "metrics": {m: v for m, v in metrics.items()}}


SUMMARIES = [
    _s("old", "20261101_100000", qwen={"mean_latency_s": 9.0, "cost_per_1k_flows_usd": 2.0, "wh_per_flow": 0.5, "n_flows": 50}),
    _s("new", "20261105_100000", qwen={"mean_latency_s": 6.0, "cost_per_1k_flows_usd": 1.5, "wh_per_flow": 0.4, "n_flows": 80},
       phi={"mean_latency_s": 3.0, "cost_per_1k_flows_usd": None, "wh_per_flow": None, "n_flows": 80}),
    _s("other-gpu", "20261106_100000", gpu="RTX 3090", qwen={"mean_latency_s": 5.0, "n_flows": 10}),
    dict(_s("demo", "20261107_100000", qwen={"mean_latency_s": 0.01}), demo=True),
    dict(_s("custom", "20261108_100000", qwen={"mean_latency_s": 1.0}), pipeline=["decide"]),
]


def test_the_projection_uses_the_latest_real_session_per_model_and_gpu():
    p = call("projectCost", SUMMARIES, {"gpu": "NVIDIA L4", "flows": 2000})
    assert [x["model"] for x in p] == ["phi", "qwen"]                                   # fastest first
    q = p[1]
    assert q["source_session"] == "new" and q["latency_s_per_flow"] == 6.0               # not the demo/custom/older one
    assert q["gpu_hours"] == pytest.approx(2000 * 6 / 3600) and q["cost_usd"] == pytest.approx(3.0)
    assert q["energy_wh"] == pytest.approx(800) and q["fits_in_a_day"] is True
    assert p[0]["cost_usd"] is None                                                       # no price measured: no cost
    assert q["usd_per_gpu_hour"] == pytest.approx(1.5 / (1000 * 6 / 3600))                # the price that benchmark paid
    assert q["gpus_all_day"] == 1 and p[0]["usd_per_gpu_hour"] is None
    big = call("projectCost", SUMMARIES, {"gpu": "NVIDIA L4", "flows": 20000})
    assert [x["gpus_all_day"] for x in big] == [1, 2]                                     # 16.7 h fits one GPU, 33.3 h needs two


def test_the_projection_filters_models_and_spans_gpus_when_none_is_chosen():
    p = call("projectCost", SUMMARIES, {"models": ["qwen"], "flows": 100000})
    assert sorted((x["gpu"], x["source_session"]) for x in p) == [("NVIDIA L4", "new"), ("RTX 3090", "other-gpu")]
    assert all(x["fits_in_a_day"] is False for x in p)                                   # 100k flows x 5-6 s > a day
