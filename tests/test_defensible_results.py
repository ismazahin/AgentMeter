"""Accuracy statistics, verdict-agreement examples (session/analyses.py) and the realistic SOC
projection (web/insights.js): additive context, never a score, rank or verdict."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from scipy.stats import binomtest

from agentmeter.config import PROJECT_ROOT
from agentmeter.session import analyses as A

SAMPLE = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"


def test_wilson_interval_known_values():
    lo, hi = A.wilson_interval(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-4) and hi == pytest.approx(0.9433, abs=1e-4)
    assert A.wilson_interval(0, 0) == (None, None)
    assert A.wilson_interval(10, 10)[1] == pytest.approx(1.0) and A.wilson_interval(0, 10)[0] == pytest.approx(0.0)


@pytest.mark.parametrize("b,c", [(0, 6), (3, 3), (1, 9), (12, 4)])
def test_mcnemar_matches_the_exact_binomial_test(b, c):
    assert A.mcnemar_exact(b, c) == pytest.approx(binomtest(min(b, c), b + c, 0.5).pvalue)


def test_holm():
    assert A.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])


def _sr(model, right):
    return [{"model": model, "scenario_id": f"f{i}", "correct": int(r)} for i, r in enumerate(right)]


def test_the_pairwise_test_uses_only_flows_where_exactly_one_was_right():
    sr = _sr("A", [1] * 40) + _sr("B", [1] * 30 + [0] * 10)
    out = A.accuracy_stats_block(sr, {"org/a": "A", "org/b": "B"}, True)
    pair = out["pairs"][0]
    assert (pair["only_a_right"], pair["only_b_right"], pair["n_paired"]) == (10, 0, 40)
    assert pair["differ"] and pair["verdict"].startswith("a is more accurate")
    close = A.accuracy_stats_block(_sr("A", [1] * 21 + [0] * 19) + _sr("B", [1] * 20 + [0] * 20), {"a": "A", "b": "B"}, True)
    assert not close["pairs"][0]["differ"] and "no evidence" in close["pairs"][0]["verdict"]
    assert A.accuracy_stats_block([], {}, False)["labelled"] is False


@pytest.fixture(scope="module")
def payload(tmp_path_factory):
    from agentmeter.ingest.run import process_csv
    from agentmeter.session.benchmark import run_session
    root = tmp_path_factory.mktemp("acc")
    process_csv(SAMPLE, out_root=root, max_flows=30)
    return run_session(root / "cicids2017_sample", ["mock-a", "mock-b"], provider="mock")


def test_a_session_carries_the_accuracy_statistics(payload):
    st = payload["accuracy_stats"]
    assert st["labelled"] and len(st["per_model"]) == 2 and len(st["pairs"]) == 1
    for m in st["per_model"]:
        lo, hi = m["ci95"]
        assert lo <= m["accuracy"] <= hi and m["n"] == 30
    acc = {pm["model"]: pm["accuracy"]["accuracy"] for pm in payload["per_model"]}
    assert {m["model"]: m["accuracy"] for m in st["per_model"]} == pytest.approx(acc)   # the same accuracy as scored
    assert payload["per_model"][0]["accuracy"]["confusion"]                              # the matrix the grid shows


def test_verdict_disagreements_carry_examples(payload):
    for m in payload["output_reliability"]["per_model"]:
        c = m["verdict_consistency"]
        if c:
            assert c["disagree"] == c["checked"] - c["agree"] and len(c["examples"]) == min(5, c["disagree"])
            for e in c["examples"]:
                assert e["verdict_class"] != e["label"] and len(e["verdict_text"]) <= 282


# --- the realistic SOC projection (insights.js) ----------------------------------------------
NODE = shutil.which("node")


def call(fn, *args):
    js = ("const I = require(%s); const a = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
          "process.stdout.write(JSON.stringify(I[%s](...a)));") % (json.dumps(str(PROJECT_ROOT / "web" / "insights.js")), json.dumps(fn))
    return json.loads(subprocess.run([NODE, "-e", js], input=json.dumps(list(args)), capture_output=True, text=True, check=True).stdout)


TRIAGE_ROW = {  # 40 attacks (Escalate/Investigate truth) and 10 benign (Dismiss truth)
    "model": "m", "n_attacks": 40, "n_benign": 10,
    "confusion": {"Escalate": {"Escalate": 28, "Investigate": 2, "Dismiss": 0},
                  "Investigate": {"Escalate": 1, "Investigate": 6, "Dismiss": 3},
                  "Dismiss": {"Escalate": 2, "Investigate": 1, "Dismiss": 7}},
}


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_the_soc_projection_reweights_attacks_and_benign_flows():
    p = call("socProjection", TRIAGE_ROW, 0.99, 10000)
    # attacks: 100 of 10,000; benign: 9,900
    assert p["attacks"] == pytest.approx(100) and p["benign"] == pytest.approx(9900)
    assert p["escalate_attacks"] == pytest.approx(100 * 29 / 40) and p["escalate_benign"] == pytest.approx(9900 * 2 / 10)
    assert p["missed_attacks"] == pytest.approx(100 * 3 / 40)
    assert p["escalation_precision"] == pytest.approx(72.5 / (72.5 + 1980))
    assert p["analyst_queue"] == pytest.approx(p["escalations"] + p["investigations"])
    bal = call("socProjection", TRIAGE_ROW, 0.2, 10000)                                   # the test set's own mix
    assert bal["escalation_precision"] > p["escalation_precision"]
    assert call("socProjection", {"model": "m"}, 0.99, 10000) is None                     # unlabelled: nothing to project
