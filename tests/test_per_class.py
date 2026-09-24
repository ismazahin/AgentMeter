"""Phase 13 — per-attack-class RESOURCE breakdown tests (CPU, no GPU).

Builds a small results DB with KNOWN per-class resource values and asserts the
aggregation produces the right means/medians/counts per (model, class); that all
5 locked classes are handled and none invented; and that adding the `per_class`
section leaves the existing analysis output (SAW ranking, per-agent, statistics)
byte-for-byte unchanged.
"""
from __future__ import annotations

import json
import sqlite3

import pandas as pd
import pytest
import yaml

from agentmeter import analyze, analyze_by_class

CLASSES = ["Brute Force", "Volumetric DDoS", "Port Scanning", "DoS Hulk", "Benign"]
TARGETS = {"accuracy_pct": 80.0, "latency_s": 5.0, "vram_mb": 16000.0, "tokens_total": 1200.0}
TIERS = {"healthy_min": 80.0, "degraded_min": 60.0}

BASE_LAT = {"A": 1.0, "B": 5.0}
BASE_VRAM = {"A": 100.0, "B": 500.0}
AGENTS = ["perceive", "reason", "decide", "act"]
K = 3  # scenarios per (model, class)


def _make_db(path):
    """3 scenarios per (model, class). For model m, class index ci, scenario k:
        latency = BASE_LAT[m] + ci + k      -> mean/median = BASE_LAT[m] + ci + 1
        vram    = BASE_VRAM[m] + ci*10 + k  -> mean/median = BASE_VRAM[m] + ci*10 + 1
        tokens  = 40*(ci+1) + 8             (constant per class; 4 agents)
    Model A always correct, B always wrong."""
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE runs(run_id TEXT, config_fingerprint TEXT, quant_setting TEXT,
        hardware_label TEXT, started_at TEXT, finished_at TEXT, status TEXT, notes TEXT);
      CREATE TABLE scenario_results(run_id TEXT, model TEXT, scenario_id TEXT,
        predicted_label TEXT, held_out_label TEXT, correct INTEGER,
        scenario_total_time_s REAL, scenario_peak_vram_mb REAL, status TEXT, completed_at TEXT);
      CREATE TABLE agent_metrics(run_id TEXT, model TEXT, scenario_id TEXT, agent_name TEXT,
        wall_time_s REAL, ttft_s REAL, vram_delta_mb REAL, input_tokens INTEGER, output_tokens INTEGER);
    """)
    conn.execute("INSERT INTO runs VALUES('r1','fp','4bit-nf4','L4','t0','t1','complete','')")
    conn.execute("INSERT INTO runs VALUES('r2','fp','4bit-nf4','L4','t0',NULL,'running','')")
    for m in ("A", "B"):
        for ci, cls in enumerate(CLASSES):
            for k in range(K):
                sid = f"row_{m}_{ci}_{k}"
                lat = BASE_LAT[m] + ci + k
                vram = BASE_VRAM[m] + ci * 10 + k
                pred = cls if m == "A" else "Unparseable"
                conn.execute("INSERT INTO scenario_results VALUES(?,?,?,?,?,?,?,?,?,?)",
                             ("r1", m, sid, pred, cls, 1 if m == "A" else 0,
                              lat, vram, "complete", "t"))
                for a in AGENTS:
                    conn.execute("INSERT INTO agent_metrics VALUES(?,?,?,?,?,?,?,?,?)",
                                 ("r1", m, sid, a, 0.1, 0.01, 10.0, 10 * (ci + 1), 2))
    # an incomplete-run row that must NEVER enter the aggregation
    conn.execute("INSERT INTO scenario_results VALUES('r2','A','row_x','Benign','Benign',1,99,99,'complete','t')")
    conn.commit(); conn.close()


def _make_cfg(path, db_path):
    cfg = {
        "run": {"mode": "precompute", "device": "cpu", "require_gpu": False, "seed": 42},
        "model": {"provider": "mock", "name": "mock/m"},
        "dataset": {"path": "data/x.csv", "label_column": "label"},
        "pipeline": {"agents": AGENTS},
        "classes": CLASSES,
        "scoring": {"weights": {"accuracy": 0.40, "latency": 0.25, "vram": 0.20, "tokens": 0.15},
                    "targets": TARGETS, "tiers": TIERS,
                    "sensitivity_weight_sets": {
                        "equal": {"accuracy": 0.25, "latency": 0.25, "vram": 0.25, "tokens": 0.25}}},
        "storage": {"sqlite_path": str(db_path)},
    }
    path.write_text(yaml.safe_dump(cfg))


def _by_key(rows):
    return {(r["model"], r["class"]): r for r in rows}


# --- aggregation correctness -------------------------------------------

def test_per_class_aggregation_values(tmp_path):
    db = tmp_path / "res.db"; _make_db(str(db))
    cfg = tmp_path / "cfg.yaml"; _make_cfg(cfg, db)

    sec = analyze_by_class.run_per_class(config_path=str(cfg), db_path=str(db))
    rows = sec["table"]

    # exactly 2 models x 5 classes; the 5 LOCKED classes, none invented
    assert len(rows) == 2 * len(CLASSES)
    assert {r["class"] for r in rows} == set(CLASSES)
    assert sec["classes"] == CLASSES

    bk = _by_key(rows)
    for m in ("A", "B"):
        for ci, cls in enumerate(CLASSES):
            r = bk[(m, cls)]
            assert r["n"] == K                                    # incomplete run excluded
            assert abs(r["mean_latency_s"] - (BASE_LAT[m] + ci + 1)) < 1e-9
            assert abs(r["median_latency_s"] - (BASE_LAT[m] + ci + 1)) < 1e-9
            assert abs(r["mean_vram_mb"] - (BASE_VRAM[m] + ci * 10 + 1)) < 1e-9
            assert abs(r["mean_tokens"] - (40 * (ci + 1) + 8)) < 1e-9
            assert r["accuracy"] == (1.0 if m == "A" else 0.0)    # secondary field only


def test_per_class_per_agent_present_for_all_classes(tmp_path):
    db = tmp_path / "res.db"; _make_db(str(db))
    cfg = tmp_path / "cfg.yaml"; _make_cfg(cfg, db)
    sec = analyze_by_class.run_per_class(config_path=str(cfg), db_path=str(db))
    pa = sec["per_agent"]
    # 2 models x 5 classes x 4 agents
    assert len(pa) == 2 * len(CLASSES) * len(AGENTS)
    assert {r["class"] for r in pa} == set(CLASSES)
    assert {r["agent_name"] for r in pa} == set(AGENTS)
    sample = next(r for r in pa if r["model"] == "A" and r["class"] == "Benign"
                  and r["agent_name"] == "reason")
    assert sample["n"] == K
    assert abs(sample["mean_wall_s"] - 0.1) < 1e-9
    assert abs(sample["mean_vram_delta_mb"] - 10.0) < 1e-9


def test_empty_class_reports_zero_not_invented(tmp_path):
    """A model missing a class reports n=0 with null stats — never dropped or invented."""
    db = tmp_path / "res.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
      CREATE TABLE scenario_results(run_id TEXT, model TEXT, scenario_id TEXT,
        predicted_label TEXT, held_out_label TEXT, correct INTEGER,
        scenario_total_time_s REAL, scenario_peak_vram_mb REAL, status TEXT, completed_at TEXT);
      CREATE TABLE agent_metrics(run_id TEXT, model TEXT, scenario_id TEXT, agent_name TEXT,
        wall_time_s REAL, ttft_s REAL, vram_delta_mb REAL, input_tokens INTEGER, output_tokens INTEGER);
    """)
    # only "Benign" rows for model A
    conn.execute("INSERT INTO scenario_results VALUES('r1','A','s0','Benign','Benign',1,2.0,50.0,'complete','t')")
    conn.execute("INSERT INTO agent_metrics VALUES('r1','A','s0','reason',0.2,0.02,5.0,10,2)")
    conn.commit(); conn.close()
    sr = pd.read_sql_query("SELECT * FROM scenario_results", sqlite3.connect(str(db)))
    am = pd.read_sql_query("SELECT * FROM agent_metrics", sqlite3.connect(str(db)))
    rows = analyze_by_class.per_class_table(sr, am, CLASSES)
    assert {r["class"] for r in rows} == set(CLASSES)     # all 5 present
    bk = _by_key(rows)
    assert bk[("A", "Benign")]["n"] == 1
    for c in ("Brute Force", "Volumetric DDoS", "Port Scanning", "DoS Hulk"):
        assert bk[("A", c)]["n"] == 0
        assert bk[("A", c)]["mean_latency_s"] is None       # null, not fabricated


# --- additivity: existing analysis output is unchanged -----------------

def test_per_class_is_purely_additive(tmp_path, monkeypatch):
    db = tmp_path / "res.db"; _make_db(str(db))
    cfg = tmp_path / "cfg.yaml"; _make_cfg(cfg, db)

    # WITH per_class
    analyze.run_analysis(config_path=str(cfg), db_path=str(db), out_dir=str(tmp_path / "full"))
    full = json.loads((tmp_path / "full" / "analysis.json").read_text())

    # WITHOUT per_class (skip the section) — the pre-Phase-13 output
    monkeypatch.setattr(analyze_by_class, "aggregate_per_class", lambda *a, **k: None)
    analyze.run_analysis(config_path=str(cfg), db_path=str(db), out_dir=str(tmp_path / "base"))
    base = json.loads((tmp_path / "base" / "analysis.json").read_text())

    assert "per_class" in full and "per_class" not in base
    # the ONLY difference is the added key; everything else is byte-for-byte identical
    stripped = {k: v for k, v in full.items() if k != "per_class"}
    assert stripped == base
    # spot-check the protected blocks explicitly
    for key in ("phase8", "per_agent", "statistics", "sensitivity", "phase7", "notes"):
        assert full[key] == base[key]
