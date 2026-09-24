"""Phase 15 — live single-scenario demo tests (CPU, mock provider, no GPU).

Verifies the demo reuses the REAL pipeline (4 agents), runs exactly ONE scenario,
rejects a model outside the fixed set, and NEVER writes the locked study DB.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

CLASSES = ["Brute Force", "Volumetric DDoS", "Port Scanning", "DoS Hulk", "Benign"]
MODELS = ["mock/m0", "mock/m1"]
_CSV = (
    "Destination Port,Flow Duration,Total Fwd Packets,SYN Flag Count,Label\n"
    "80,100,1,0,Benign\n"
    "22,5000,60,0,Brute Force\n"
    "443,200,2,1,Port Scanning\n"
    "53,300,3,0,Volumetric DDoS\n"
    "8080,400,4,1,DoS Hulk\n"
)


def _load_demo():
    path = REPO_ROOT / "scripts" / "demo_run.py"
    spec = importlib.util.spec_from_file_location("demo_run", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mock_cfg(tmp_path):
    csv = tmp_path / "flows.csv"; csv.write_text(_CSV)
    cfg = {
        "run": {"mode": "pilot", "device": "cpu", "require_gpu": False,
                "seed": 42, "models": MODELS},
        "model": {"provider": "mock", "name": MODELS[0], "mock": {"latency_s": 0.0}},
        "dataset": {"path": str(csv), "label_column": "Label", "id_column": None,
                    "drop_columns": [], "limit": None, "max_feature_chars": 4000,
                    "label_map": {}, "drop_labels": []},
        "pipeline": {"agents": ["perceive", "reason", "decide", "act"],
                     "max_new_tokens": {"perceive": 160, "reason": 200, "decide": 12, "act": 160}},
        "classes": CLASSES,
        "mitre": {"Benign": "N/A"},
        "scoring": {"weights": {"accuracy": 0.40, "latency": 0.25, "vram": 0.20, "tokens": 0.15}},
        "storage": {"sqlite_path": str(tmp_path / "should_not_be_used.db")},
    }
    p = tmp_path / "cfg.yaml"; p.write_text(yaml.safe_dump(cfg))
    return str(p)


def _sink():
    out = []
    return out, (lambda line="": out.append(str(line)))


# --- reuses the real 4-agent pipeline ----------------------------------

def test_demo_reuses_real_pipeline_four_agents(tmp_path):
    demo = _load_demo()
    cfg = _mock_cfg(tmp_path)
    lines, stream = _sink()
    res = demo.run_demo(model="mock/m0", klass="Port Scanning", config_path=cfg, stream=stream)

    agents = [a["agent"] for a in res["per_agent"]]
    assert agents == ["perceive", "reason", "decide", "act"]   # the real pipeline ran
    assert res["true_class"] == "Port Scanning"
    assert "predicted_class" in res and isinstance(res["correct"], bool)
    # streamed each agent's step live
    assert any("PERCEIVE" in l for l in lines) and any("DECIDE" in l for l in lines)
    assert any("RESULT" in l for l in lines)


# --- exactly one scenario runs -----------------------------------------

def test_demo_runs_exactly_one_scenario(tmp_path):
    demo = _load_demo()
    cfg = _mock_cfg(tmp_path)
    _, stream = _sink()
    # pick by explicit scenario id (row_0002 is the Port Scanning row)
    res = demo.run_demo(model="mock/m0", scenario_id="row_0002", config_path=cfg, stream=stream)
    assert res["scenario_id"] == "row_0002"
    assert len(res["per_agent"]) == 4                     # one scenario => 4 agent rows
    # by class picks the first matching scenario, and only that one
    res2 = demo.run_demo(model="mock/m0", klass="Benign", config_path=cfg, stream=stream)
    assert res2["scenario_id"] == "row_0000" and res2["true_class"] == "Benign"
    assert len(res2["per_agent"]) == 4


# --- rejects a model outside the fixed set -----------------------------

def test_demo_rejects_unknown_model(tmp_path):
    demo = _load_demo()
    cfg = _mock_cfg(tmp_path)
    _, stream = _sink()
    with pytest.raises(ValueError, match="not one of the fixed"):
        demo.run_demo(model="acme/not-a-study-model", klass="Benign", config_path=cfg, stream=stream)


def test_demo_rejects_unknown_class_and_missing_selector(tmp_path):
    demo = _load_demo()
    cfg = _mock_cfg(tmp_path)
    _, stream = _sink()
    with pytest.raises(ValueError, match="locked classes"):
        demo.run_demo(model="mock/m0", klass="Nonexistent", config_path=cfg, stream=stream)
    with pytest.raises(ValueError, match="scenario id"):
        demo.run_demo(model="mock/m0", scenario_id="row_9999", config_path=cfg, stream=stream)


# --- never writes the locked study DB ----------------------------------

def test_demo_never_opens_locked_db(tmp_path, monkeypatch):
    demo = _load_demo()
    cfg = _mock_cfg(tmp_path)
    _, stream = _sink()

    real_connect = sqlite3.connect

    def guard(target, *a, **k):
        if "agentmeter_full_l4.db" in str(target):
            raise AssertionError(f"demo opened the locked study DB: {target}")
        return real_connect(target, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", guard)
    res = demo.run_demo(model="mock/m0", klass="DoS Hulk", config_path=cfg, stream=stream)
    assert res["scenario_id"] == "row_0004"   # completed without touching the locked DB


# --- --save writes only the throwaway file (still no DB) ----------------

def test_demo_save_is_throwaway_file(tmp_path):
    demo = _load_demo()
    cfg = _mock_cfg(tmp_path)
    _, stream = _sink()
    out = tmp_path / "demo_out.json"
    demo.run_demo(model="mock/m1", klass="Benign", config_path=cfg, stream=stream, save=str(out))
    import json
    saved = json.loads(out.read_text())
    assert saved["model"] == "mock/m1" and len(saved["per_agent"]) == 4
