"""Phase 32B — decision-support layer tests (CPU).

Runs dashboard/report.js (+ saw.js for live ranking) under node against a sample
analysis.json and checks the four read-only decision-support pieces:
  1. recommendation() — reflects the live ranking + the non-negotiable Critical-tier
     caveat, and the top model changes when the weights change.
  2. constraintFilter() — PASS/FAIL against measured values, "none pass", and
     graceful degradation when a requested dimension isn't in the data.
  3. optimisationHint() — resource-only text with NO accuracy/quality-improvement claims.
  4. decisionBrief() — one-page HTML with values pulled verbatim from analysis.json.

Nothing here recomputes a locked number; ranking is reused from saw.js.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REPORT_JS = REPO / "dashboard" / "report.js"
SAW_JS = REPO / "dashboard" / "saw.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")

TIERS = {"healthy_min": 80.0, "degraded_min": 60.0}
WEIGHTS_DEFAULT = {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15}
WEIGHTS_ACC_HEAVY = {"accuracy": 0.85, "latency": 0.05, "vram": 0.05, "tokens": 0.05}
TARGETS = {"accuracy_pct": 80.0, "latency_s": 5.0, "vram_mb": 16000.0, "tokens_total": 1200.0}


def _saw_row(model, acc_pct, lat_s, vram_mb, tokens, norm, composite, rank):
    return {"model": model, "accuracy_pct": acc_pct, "latency_s": lat_s,
            "total_device_vram_mb": vram_mb, "vram_mb": vram_mb, "vram_working_mb": 1300.0,
            "tokens_total": tokens, "accuracy": norm["accuracy"], "latency": norm["latency"],
            "vram": norm["vram"], "tokens": norm["tokens"], "composite": composite,
            "rank": rank, "tier": "Critical"}


def _norm(model, a, l, v, t):
    return {"model": model, "accuracy": a, "latency": l, "vram": v, "tokens": t}


# Mirrors the real locked sample shape: default weights -> Qwen #1; accuracy-heavy -> Mistral.
NORMALISED = [
    _norm("Qwen2.5-7B-Instruct", 0.33375, 0.38875, 1.0, 0.38875),
    _norm("Mistral-7B-Instruct-v0.3", 0.41625, 0.23875, 1.0, 0.23875),
    _norm("Meta-Llama-3-8B-Instruct", 0.22875, 0.31625, 1.0, 0.31625),
    _norm("gemma-2-9b-it", 0.325, 0.215, 1.0, 0.215),
    _norm("Phi-3-mini-4k-instruct", 0.25875, 0.26875, 1.0, 0.26875),
]
SAW_TABLE = [
    _saw_row("Qwen2.5-7B-Instruct", 26.7, 12.8617, 5200.0, 3086.82,
             NORMALISED[0], 0.489, 1),
    _saw_row("Mistral-7B-Instruct-v0.3", 33.3, 20.9424, 5000.0, 5026.18,
             NORMALISED[1], 0.462, 2),
    _saw_row("Meta-Llama-3-8B-Instruct", 18.3, 15.8103, 6600.0, 3794.47,
             NORMALISED[2], 0.372, 3),
    _saw_row("gemma-2-9b-it", 32.5, 10.5, 6000.0, 2800.0,
             NORMALISED[3], 0.360, 4),
    _saw_row("Phi-3-mini-4k-instruct", 20.7, 13.1, 3100.0, 3300.0,
             NORMALISED[4], 0.300, 5),
]
DATA = {
    "run_ids": ["run_l4"],
    "notes": {"vram_finding": "VRAM normalises to 1.0 for all models."},
    "phase8": {"weights": WEIGHTS_DEFAULT, "targets": TARGETS, "tiers": TIERS,
               "normalised": NORMALISED, "saw_table": SAW_TABLE},
    "per_agent": {"table": [], "dominant": [
        {"model": "Qwen2.5-7B-Instruct", "latency_dominant_agent": "reason",
         "vram_dominant_agent": "decide"},
        {"model": "Mistral-7B-Instruct-v0.3", "latency_dominant_agent": "reason",
         "vram_dominant_agent": "decide"}]},
}


_DRIVER = r"""
const SAW = require(process.argv[2]);
const R = require(process.argv[3]);
const inp = JSON.parse(process.argv[4]);
const data = inp.data, tiers = inp.tiers;
function rank(w) { return SAW.rankModels(data.phase8.normalised, w, tiers); }
const rankedDefault = rank(inp.wDefault);
const rankedAccHeavy = rank(inp.wAccHeavy);
const out = {
  rec_default: R.recommendation(data, rankedDefault, inp.wDefault),
  rec_accheavy: R.recommendation(data, rankedAccHeavy, inp.wAccHeavy),
  filt_generous: R.constraintFilter(data, {latency_s: 999, vram_mb: 99999, tokens_total: 99999, accuracy_pct: 0}),
  filt_minacc30: R.constraintFilter(data, {accuracy_pct: 30}),
  filt_lat5: R.constraintFilter(data, {latency_s: 5}),
  filt_empty: R.constraintFilter(data, {}),
  filt_missing: R.constraintFilter(inp.dataNoTokens, {tokens_total: 1200, accuracy_pct: 30}),
  hint_qwen: R.optimisationHint(data, "Qwen2.5-7B-Instruct"),
  hint_default: R.optimisationHint(data, null),
  brief: R.decisionBrief(data, rankedDefault, inp.wDefault, "Test brief"),
};
process.stdout.write(JSON.stringify(out));
"""


def _run():
    driver = REPO / "tests" / "_decision_driver.js"
    driver.write_text(_DRIVER)
    # a variant with tokens_total stripped from every saw row (missing-field case)
    data_no_tokens = json.loads(json.dumps(DATA))
    for r in data_no_tokens["phase8"]["saw_table"]:
        r.pop("tokens_total", None)
    payload = json.dumps({"data": DATA, "dataNoTokens": data_no_tokens, "tiers": TIERS,
                          "wDefault": WEIGHTS_DEFAULT, "wAccHeavy": WEIGHTS_ACC_HEAVY})
    try:
        res = subprocess.run([NODE, str(driver), str(SAW_JS), str(REPORT_JS), payload],
                             capture_output=True, text=True, check=True)
        return json.loads(res.stdout)
    finally:
        driver.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def out():
    return _run()


# --- 1. recommendation ---------------------------------------------------

def test_recommendation_tracks_live_ranking(out):
    # default weights -> Qwen tops; accuracy-heavy -> Mistral tops (ranking is reused from saw.js)
    assert out["rec_default"]["top"] == "Qwen2.5-7B-Instruct"
    assert "Qwen2.5-7B-Instruct" in out["rec_default"]["headline"]
    assert out["rec_accheavy"]["top"] == "Mistral-7B-Instruct-v0.3"
    assert "Mistral-7B-Instruct-v0.3" in out["rec_accheavy"]["headline"]


def test_recommendation_has_critical_caveat(out):
    cav = out["rec_default"]["caveat"]
    assert out["rec_default"]["allCritical"] is True
    assert "Critical tier" in cav
    assert "none is recommended for zero-shot deployment as-is" in cav
    assert "80%" in cav                      # accuracy target echoed from the data
    # the caveat is weight-independent: same honesty note under accuracy-heavy weights
    assert "none is recommended for zero-shot deployment as-is" in out["rec_accheavy"]["caveat"]


# --- 2. constraint filter ------------------------------------------------

def test_filter_all_pass_when_generous(out):
    f = out["filt_generous"]
    assert f["activeCount"] == 4 and f["anyPass"] is True
    assert all(x["pass"] for x in f["results"])


def test_filter_min_accuracy(out):
    f = out["filt_minacc30"]
    passing = {x["model"] for x in f["results"] if x["pass"]}
    # only Mistral (33.3) and gemma (32.5) clear a 30% accuracy floor
    assert passing == {"Mistral-7B-Instruct-v0.3", "gemma-2-9b-it"}
    fail = next(x for x in f["results"] if x["model"] == "Qwen2.5-7B-Instruct")
    assert fail["pass"] is False and fail["failed"] == ["accuracy"]


def test_filter_none_pass(out):
    f = out["filt_lat5"]                      # every model's latency > 5 s
    assert f["anyPass"] is False
    assert all((not x["pass"]) and x["failed"] == ["latency"] for x in f["results"])


def test_filter_empty_is_vacuous(out):
    f = out["filt_empty"]
    assert f["activeCount"] == 0
    # with no active constraints nothing is judged failed
    assert all(x["pass"] and x["failed"] == [] for x in f["results"])


def test_filter_degrades_on_missing_field(out):
    f = out["filt_missing"]
    # tokens_total requested but absent -> ignored (never invented); accuracy still applies
    assert "tokens_total" in f["ignored"]
    assert f["activeCount"] == 1
    passing = {x["model"] for x in f["results"] if x["pass"]}
    assert passing == {"Mistral-7B-Instruct-v0.3", "gemma-2-9b-it"}


# --- 3. optimisation guidance (resource only) ----------------------------

_FORBIDDEN = ["accuracy", "fine-tune", "finetune", "fine tune", "prompt", "retrieval",
              "few-shot", "few shot", "chain-of-thought", "improve", "detection",
              "better answer", "quality"]


def test_optimisation_is_resource_only(out):
    for key in ("hint_qwen", "hint_default"):
        text = out[key]["text"].lower()
        assert text, f"{key} empty"
        for bad in _FORBIDDEN:
            assert bad not in text, f"{key} leaked a non-resource claim: {bad!r} in {text!r}"
    # it still names the dominant-cost agents from the per-agent data
    assert out["hint_qwen"]["latency_agent"] == "reason"
    assert out["hint_qwen"]["vram_agent"] == "decide"
    assert "reason" in out["hint_qwen"]["text"] and "decide" in out["hint_qwen"]["text"]


# --- 4. decision brief ---------------------------------------------------

def test_decision_brief_values_from_analysis(out):
    html = out["brief"]
    assert "<title>Test brief</title>" in html
    assert "run_l4" in html                          # run id echoed verbatim
    assert "Qwen2.5-7B-Instruct" in html             # top model in recommendation + ranking
    assert "26.7%" in html and "12.86 s" in html     # Qwen accuracy + latency, not recomputed
    assert "Critical" in html                        # tier reality
    assert "none is recommended for zero-shot deployment as-is" in html
    # resource-only brief: no accuracy-improvement / detection-quality language in the findings
    low = html.lower()
    for bad in ["fine-tune", "prompt engineering", "improve accuracy", "detection quality"]:
        assert bad not in low
