"""Parity test: the in-browser SAW math (dashboard/saw.js) must match the Python
analysis (agentmeter/analyze._composite / _tier) exactly.

This guarantees the dashboard's live sensitivity slider re-ranks models the same
way the committed analysis.json was scored. Runs saw.js under node (CPU only, no
GPU/Firebase); skips cleanly if node is unavailable.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from agentmeter import analyze

REPO = Path(__file__).resolve().parents[1]
SAW_JS = REPO / "dashboard" / "saw.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")

# A synthetic normalised table (values already in [0,1], the slider's input shape)
NORMALISED = [
    {"model": "Qwen2.5-7B-Instruct", "accuracy": 0.33375, "latency": 0.60, "vram": 1.0, "tokens": 0.42},
    {"model": "Mistral-7B-Instruct-v0.3", "accuracy": 0.41625, "latency": 0.30, "vram": 1.0, "tokens": 0.25},
    {"model": "Meta-Llama-3-8B-Instruct", "accuracy": 0.22875, "latency": 0.55, "vram": 1.0, "tokens": 0.66},
    {"model": "gemma-2-9b-it", "accuracy": 0.325, "latency": 0.20, "vram": 1.0, "tokens": 0.34},
    {"model": "Phi-3-mini-4k-instruct", "accuracy": 0.25875, "latency": 0.80, "vram": 1.0, "tokens": 0.71},
]
TIERS = {"healthy_min": 80.0, "degraded_min": 60.0}

# Several weight sets, incl. NON-normalised ones (must be renormalised identically)
WEIGHT_SETS = {
    "default": {"accuracy": 0.40, "latency": 0.25, "vram": 0.20, "tokens": 0.15},
    "equal": {"accuracy": 0.25, "latency": 0.25, "vram": 0.25, "tokens": 0.25},
    "accuracy_heavy": {"accuracy": 0.55, "latency": 0.20, "vram": 0.15, "tokens": 0.10},
    "unnormalised": {"accuracy": 2.0, "latency": 1.0, "vram": 3.0, "tokens": 1.0},
}

_DRIVER = r"""
const path = require('path');
const SAW = require(process.argv[2]);
const payload = JSON.parse(process.argv[3]);
const out = {};
for (const [name, w] of Object.entries(payload.weight_sets)) {
  out[name] = SAW.rankModels(payload.normalised, w, payload.tiers).map(r => ({
    model: r.model, composite: r.composite, rank: r.rank, tier: r.tier,
  }));
}
process.stdout.write(JSON.stringify(out));
"""


def _node_ranks() -> dict:
    driver = REPO / "tests" / "_saw_driver.js"
    driver.write_text(_DRIVER)
    try:
        payload = json.dumps({"normalised": NORMALISED, "tiers": TIERS,
                              "weight_sets": WEIGHT_SETS})
        res = subprocess.run([NODE, str(driver), str(SAW_JS), payload],
                             capture_output=True, text=True, check=True)
        return json.loads(res.stdout)
    finally:
        driver.unlink(missing_ok=True)


def _python_ranks(weights: dict) -> list[dict]:
    """Mirror the dashboard: renormalise weights to sum 1, then use the SAME
    analyze._composite / _tier the real analysis.json was built with, rank desc."""
    s = sum(float(v) for v in weights.values())
    w = {k: float(v) / s for k, v in weights.items()}
    norm = pd.DataFrame(NORMALISED)
    comp = analyze._composite(norm, w)
    order = comp.sort_values(ascending=False).index
    rows = []
    for rank, i in enumerate(order, start=1):
        c = float(comp[i])
        rows.append({"model": norm.loc[i, "model"], "composite": c, "rank": rank,
                     "tier": analyze._tier(c * 100.0, TIERS)})
    return rows


def test_saw_js_matches_python_analyze():
    node = _node_ranks()
    assert set(node) == set(WEIGHT_SETS)
    for name in WEIGHT_SETS:
        py = _python_ranks(WEIGHT_SETS[name])
        js = node[name]
        assert [r["model"] for r in js] == [r["model"] for r in py], f"ranking mismatch for {name}"
        for jr, pr in zip(js, py):
            assert jr["model"] == pr["model"]
            assert jr["rank"] == pr["rank"], f"rank mismatch {name}/{jr['model']}"
            assert jr["tier"] == pr["tier"], f"tier mismatch {name}/{jr['model']}"
            assert abs(jr["composite"] - pr["composite"]) < 1e-9, \
                f"composite mismatch {name}/{jr['model']}: js={jr['composite']} py={pr['composite']}"


def test_sample_analysis_composites_are_self_consistent():
    """The bundled sample's saw_table composites must equal SAW.composite over its
    own phase8.normalised under the default weights (guards against drift)."""
    sample = json.loads((REPO / "dashboard" / "sample_analysis.json").read_text())
    p8 = sample["phase8"]
    weights = p8["weights"]
    norm = {r["model"]: r for r in p8["normalised"]}
    s = sum(float(v) for v in weights.values())
    w = {k: float(v) / s for k, v in weights.items()}
    for row in p8["saw_table"]:
        n = norm[row["model"]]
        expected = sum(float(n[k]) * w[k] for k in ("accuracy", "latency", "vram", "tokens"))
        assert abs(expected - float(row["composite"])) < 1e-9, row["model"]
