"""Phase 17 — comparison workspace + report export tests (CPU).

Runs dashboard/report.js under node against 2-3 sample analysis.json objects and
checks: the comparison assembles aligned rows across sessions; CSV export is exact
from the source analysis.json; nothing writes to the locked study DB.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REPORT_JS = REPO / "dashboard" / "report.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")


def _analysis(models_saw, run_ids):
    """A minimal analysis.json with a phase8.saw_table + per_agent + per_class."""
    return {
        "run_ids": run_ids,
        "notes": {"vram_finding": "VRAM normalises to 1.0 for all models."},
        "phase8": {"saw_table": models_saw},
        "per_agent": {"table": [
            {"model": "A", "agent_name": "reason", "mean_wall_s": 3.0,
             "mean_ttft_s": 1.0, "mean_vram_delta_mb": 120.0,
             "mean_input_tokens": 260, "mean_output_tokens": 120}],
            "dominant": [{"model": "A", "latency_dominant_agent": "reason",
                          "vram_dominant_agent": "decide"}]},
        "per_class": {"table": [
            {"model": "A", "class": "Benign", "n": 60, "mean_latency_s": 2.0,
             "median_latency_s": 1.9, "mean_vram_mb": 100.0, "mean_tokens": 48.0,
             "accuracy": 0.5}]},
        "statistics": {"scenario_total_time_s": {"test": "Kruskal-Wallis", "H": 42.0,
                                                 "p_value": 1e-8, "significant": True}},
    }


# two sessions; same models A,B, different composites; session 2 adds model C
S1 = _analysis([
    {"model": "A", "rank": 1, "accuracy_pct": 26.7, "latency_s": 12.8,
     "total_device_vram_mb": None, "vram_mb": 1300.0, "vram_working_mb": 1300.0,
     "tokens_total": 3086.0, "composite": 0.489, "tier": "Critical"},
    {"model": "B", "rank": 2, "accuracy_pct": 33.3, "latency_s": 20.9,
     "total_device_vram_mb": None, "vram_mb": 1250.0, "vram_working_mb": 1250.0,
     "tokens_total": 5026.0, "composite": 0.462, "tier": "Critical"},
], ["run_l4"])
S2 = _analysis([
    {"model": "A", "rank": 2, "accuracy_pct": 27.3, "latency_s": 10.9,
     "total_device_vram_mb": None, "vram_mb": 1300.0, "vram_working_mb": 1300.0,
     "tokens_total": 3050.0, "composite": 0.506, "tier": "Critical"},
    {"model": "B", "rank": 1, "accuracy_pct": 31.7, "latency_s": 23.1,
     "total_device_vram_mb": None, "vram_mb": 1250.0, "vram_working_mb": 1250.0,
     "tokens_total": 5100.0, "composite": 0.456, "tier": "Critical"},
    {"model": "C", "rank": 3, "accuracy_pct": 20.0, "latency_s": 18.0,
     "total_device_vram_mb": None, "vram_mb": 900.0, "vram_working_mb": 900.0,
     "tokens_total": 4000.0, "composite": 0.410, "tier": "Critical"},
], ["run_a100"])


_DRIVER = r"""
const R = require(process.argv[2]);
const inp = JSON.parse(process.argv[3]);
const out = {
  compare: R.compareSessions(inp.sessions),
  saw_csv: R.sawCsv(inp.sessions[0].data),
  cmp_csv: R.comparisonCsv(inp.sessions),
  per_agent_csv: R.perAgentCsv(inp.sessions[0].data),
  per_class_csv: R.perClassCsv(inp.sessions[0].data),
  report_html: R.reportHtml(inp.sessions[0].data, "Test"),
};
process.stdout.write(JSON.stringify(out));
"""


def _run(sessions):
    driver = REPO / "tests" / "_report_driver.js"
    driver.write_text(_DRIVER)
    try:
        payload = json.dumps({"sessions": sessions})
        res = subprocess.run([NODE, str(driver), str(REPORT_JS), payload],
                             capture_output=True, text=True, check=True)
        return json.loads(res.stdout)
    finally:
        driver.unlink(missing_ok=True)


def test_compare_assembles_aligned_rows():
    out = _run([{"label": "L4", "data": S1}, {"label": "A100", "data": S2}])
    cmp = out["compare"]
    # union of models, ordered (A,B from S1, then C from S2)
    assert cmp["models"] == ["A", "B", "C"]
    assert [s["label"] for s in cmp["sessions"]] == ["L4", "A100"]
    assert cmp["sessions"][0]["top"] == "A" and cmp["sessions"][1]["top"] == "B"

    rowA = next(r for r in cmp["rows"] if r["model"] == "A")
    assert rowA["cells"][0]["composite"] == 0.489
    assert rowA["cells"][1]["composite"] == 0.506
    # best composite for A is session index 1 (A100, higher); best latency also A100 (lower)
    assert rowA["best"]["composite"] == 1
    assert rowA["best"]["latency_s"] == 1

    # model C is missing from session 0 -> null cell, present in session 1
    rowC = next(r for r in cmp["rows"] if r["model"] == "C")
    assert rowC["cells"][0] is None and rowC["cells"][1]["composite"] == 0.410


def test_three_way_comparison():
    out = _run([{"label": "one", "data": S1}, {"label": "two", "data": S2},
                {"label": "three", "data": S1}])
    cmp = out["compare"]
    assert len(cmp["sessions"]) == 3
    rowA = next(r for r in cmp["rows"] if r["model"] == "A")
    assert len(rowA["cells"]) == 3
    assert rowA["cells"][0]["composite"] == rowA["cells"][2]["composite"] == 0.489


def test_saw_csv_exact_from_analysis():
    out = _run([{"label": "L4", "data": S1}])
    lines = out["saw_csv"].split("\r\n")
    assert lines[0] == ("model,rank,accuracy_pct,latency_s,vram_working_mb,"
                        "total_device_vram_mb,vram_mb,tokens_total,composite,tier")
    # row for A: exact values from S1 (empty for the null total_device_vram_mb)
    assert lines[1] == "A,1,26.7,12.8,1300,,1300,3086,0.489,Critical"
    assert lines[2].startswith("B,2,33.3,20.9,1250,,1250,5026,0.462,Critical")


def test_comparison_csv_wide():
    out = _run([{"label": "L4", "data": S1}, {"label": "A100", "data": S2}])
    lines = out["cmp_csv"].split("\r\n")
    assert lines[0].startswith("model,L4 · composite,L4 · accuracy_pct,L4 · latency_s,"
                               "L4 · tokens_total,L4 · vram,A100 · composite")
    # A row: L4 composite 0.489 ... A100 composite 0.506
    a = lines[1].split(",")
    assert a[0] == "A" and a[1] == "0.489" and a[6] == "0.506"


def test_per_agent_and_per_class_csv():
    out = _run([{"label": "L4", "data": S1}])
    assert out["per_agent_csv"].split("\r\n")[0] == \
        "model,agent_name,mean_wall_s,mean_ttft_s,mean_vram_delta_mb,mean_input_tokens,mean_output_tokens"
    assert "A,reason,3,1,120,260,120" in out["per_agent_csv"]
    assert out["per_class_csv"].split("\r\n")[0] == \
        "model,class,n,mean_latency_s,median_latency_s,mean_vram_mb,mean_tokens,accuracy"
    assert "A,Benign,60,2,1.9,100,48,0.5" in out["per_class_csv"]


def test_report_html_has_findings_and_ranking():
    out = _run([{"label": "L4", "data": S1}])
    html = out["report_html"]
    assert "<title>Test</title>" in html and "SAW ranking" in html
    assert "statistically significant" in html   # from the statistics block
    assert "run_l4" in html                       # run id echoed


# --- server still never opens the locked DB; report.js is served ---------

def _load_server():
    path = REPO / "scripts" / "pull_eval_server.py"
    spec = importlib.util.spec_from_file_location("pull_eval_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_report_js_served_and_no_locked_db(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    import yaml
    srv = _load_server()
    from agentmeter import pull_eval

    csv = tmp_path / "f.csv"; csv.write_text("A,Label\n1,Benign\n")
    cfg = {"run": {"mode": "pilot", "device": "cpu", "require_gpu": False, "seed": 1, "models": ["mock/m"]},
           "model": {"provider": "mock", "name": "mock/m", "mock": {"latency_s": 0.0}},
           "dataset": {"path": str(csv), "label_column": "Label", "id_column": None,
                       "drop_columns": [], "limit": 1, "max_feature_chars": 100, "label_map": {}, "drop_labels": []},
           "pipeline": {"agents": ["perceive", "reason", "decide", "act"],
                        "max_new_tokens": {"perceive": 8, "reason": 8, "decide": 4, "act": 8}},
           "classes": ["Benign"], "mitre": {"Benign": "N/A"},
           "scoring": {"weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15}},
           "storage": {"sqlite_path": str(tmp_path / "LOCKED.db")}}
    base = tmp_path / "cfg.yaml"; base.write_text(yaml.safe_dump(cfg))

    real = sqlite3.connect

    def guard(target, *a, **k):
        if "agentmeter_full_l4.db" in str(target):
            raise AssertionError("locked DB opened")
        return real(target, *a, **k)
    monkeypatch.setattr(sqlite3, "connect", guard)

    mgr = pull_eval.JobManager(base_config=str(base), canonical_json=str(tmp_path / "c.json"),
                               out_dir=str(tmp_path / "pulls"))
    client = srv.create_app(mgr).test_client()
    r = client.get("/report.js")
    assert r.status_code == 200 and b"REPORT" in r.data
