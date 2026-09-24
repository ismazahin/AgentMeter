"""Phase 14 — advanced analytical expansion tests (CPU, no GPU).

Controlled DB with known values -> exact CoV, throughput, prefill/decode split,
Pareto membership, misclassification cost split; the additive-only guarantee; and
that the locked DB is opened read-only / never written.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pandas as pd
import pytest
import yaml

from agentmeter import analyze, analyze_advanced

CLASSES = ["Brute Force", "Volumetric DDoS", "Port Scanning", "DoS Hulk", "Benign"]
TARGETS = {"accuracy_pct": 80.0, "latency_s": 5.0, "vram_mb": 16000.0, "tokens_total": 1200.0}
TIERS = {"healthy_min": 80.0, "degraded_min": 60.0}

# Model A: latency [2,4,6] (mean 4, sample std 2 -> CoV 0.5); reason agent
#   ttft 1.0, wall 3.0 (decode 2.0), out 10, in 5 -> tokens 15; correct on scn 0,1.
# Model B: latency [5,5,5] (CoV 0); ttft 0.5, wall 2.5 (decode 2.0), out 20, in 5
#   -> tokens 25; correct on scn 0 only.
SPEC = {
    "A": {"lat": [2.0, 4.0, 6.0], "ttft": 1.0, "wall": 3.0, "out": 10, "correct": {0, 1}},
    "B": {"lat": [5.0, 5.0, 5.0], "ttft": 0.5, "wall": 2.5, "out": 20, "correct": {0}},
}


def _make_db(path):
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
    for m, s in SPEC.items():
        for si, lat in enumerate(s["lat"]):
            cls = CLASSES[si % len(CLASSES)]
            correct = 1 if si in s["correct"] else 0
            pred = cls if correct else "Unparseable"
            conn.execute("INSERT INTO scenario_results VALUES(?,?,?,?,?,?,?,?,?,?)",
                         ("r1", m, f"s{si}", pred, cls, correct, lat, 100.0, "complete", "t"))
            conn.execute("INSERT INTO agent_metrics VALUES(?,?,?,?,?,?,?,?,?)",
                         ("r1", m, f"s{si}", "reason", s["wall"], s["ttft"], 10.0, 5, s["out"]))
    # incomplete-run row that must be excluded
    conn.execute("INSERT INTO scenario_results VALUES('r2','A','sx','Benign','Benign',1,99,99,'complete','t')")
    conn.commit(); conn.close()


def _make_cfg(path, db_path):
    cfg = {
        "run": {"mode": "precompute", "device": "cpu", "require_gpu": False, "seed": 42},
        "model": {"provider": "mock", "name": "mock/m"},
        "dataset": {"path": "data/x.csv", "label_column": "label"},
        "pipeline": {"agents": ["perceive", "reason", "decide", "act"]},
        "classes": CLASSES,
        "scoring": {"weights": {"accuracy": 0.40, "latency": 0.25, "vram": 0.20, "tokens": 0.15},
                    "targets": TARGETS, "tiers": TIERS,
                    "sensitivity_weight_sets": {"equal": {"accuracy": 0.25, "latency": 0.25, "vram": 0.25, "tokens": 0.25}}},
        "storage": {"sqlite_path": str(db_path)},
    }
    path.write_text(yaml.safe_dump(cfg))


def _adv(tmp_path):
    db = tmp_path / "res.db"; _make_db(str(db))
    cfg = tmp_path / "cfg.yaml"; _make_cfg(cfg, db)
    return analyze_advanced.run_advanced(config_path=str(cfg), db_path=str(db)), str(db), str(cfg)


def _by_model(rows):
    return {r["model"]: r for r in rows}


# --- CoV ---------------------------------------------------------------

def test_latency_cov_exact(tmp_path):
    adv, _, _ = _adv(tmp_path)
    cov = _by_model(adv["latency_cov"])
    assert cov["A"]["n"] == 3                      # incomplete run excluded
    assert abs(cov["A"]["mean_latency_s"] - 4.0) < 1e-9
    assert abs(cov["A"]["std_latency_s"] - 2.0) < 1e-9
    assert abs(cov["A"]["cov"] - 0.5) < 1e-9
    assert cov["B"]["std_latency_s"] == 0.0 and cov["B"]["cov"] == 0.0


# --- prefill / decode --------------------------------------------------

def test_prefill_decode_exact(tmp_path):
    adv, _, _ = _adv(tmp_path)
    pm = _by_model(adv["prefill_decode"]["per_model"])
    assert abs(pm["A"]["mean_prefill_s"] - 1.0) < 1e-9
    assert abs(pm["A"]["mean_decode_s"] - 2.0) < 1e-9   # wall 3 - ttft 1
    assert abs(pm["B"]["mean_prefill_s"] - 0.5) < 1e-9
    assert abs(pm["B"]["mean_decode_s"] - 2.0) < 1e-9   # wall 2.5 - ttft 0.5
    # per-agent rows present for the reason agent
    pa = {(r["model"], r["agent_name"]): r for r in adv["prefill_decode"]["per_agent"]}
    assert abs(pa[("A", "reason")]["mean_prefill_s"] - 1.0) < 1e-9


# --- throughput --------------------------------------------------------

def test_throughput_exact(tmp_path):
    adv, _, _ = _adv(tmp_path)
    tp = _by_model(adv["throughput"])
    # A: total out 30 / total decode 6 = 5.0 ; B: 60 / 6 = 10.0
    assert abs(tp["A"]["tokens_per_s"] - 5.0) < 1e-9
    assert abs(tp["B"]["tokens_per_s"] - 10.0) < 1e-9
    assert abs(tp["A"]["mean_output_tokens"] - 10.0) < 1e-9


# --- Pareto ------------------------------------------------------------

def test_pareto_membership_on_run(tmp_path):
    adv, _, _ = _adv(tmp_path)
    pts = _by_model(adv["pareto"]["points"])
    # A: acc 2/3, lat 4, tokens 15 ; B: acc 1/3, lat 5, tokens 25 -> A dominates B
    assert pts["A"]["pareto_latency"] is True and pts["A"]["pareto_tokens"] is True
    assert pts["B"]["pareto_latency"] is False and pts["B"]["pareto_tokens"] is False


def test_pareto_membership_unit():
    pts = [
        {"model": "x", "accuracy": 0.9, "c": 10.0},   # best acc, mid cost -> front
        {"model": "y", "accuracy": 0.5, "c": 2.0},    # low acc, best cost -> front
        {"model": "z", "accuracy": 0.7, "c": 12.0},   # dominated by x (higher acc, lower cost)
    ]
    assert analyze_advanced.pareto_membership(pts, "c") == [True, True, False]


# --- misclassification RESOURCE cost -----------------------------------

def test_misclass_cost_split(tmp_path):
    adv, _, _ = _adv(tmp_path)
    rows = {(r["model"], r["group"]): r for r in adv["misclass_cost"]["rows"]}
    # A correct scns 0,1 (lat 2,4 -> mean 3); incorrect scn 2 (lat 6)
    assert rows[("A", "correct")]["n"] == 2
    assert abs(rows[("A", "correct")]["mean_latency_s"] - 3.0) < 1e-9
    assert rows[("A", "incorrect")]["n"] == 1
    assert abs(rows[("A", "incorrect")]["mean_latency_s"] - 6.0) < 1e-9
    assert abs(rows[("A", "correct")]["mean_tokens"] - 15.0) < 1e-9   # cost only
    # B: 1 correct, 2 incorrect
    assert rows[("B", "correct")]["n"] == 1 and rows[("B", "incorrect")]["n"] == 2


# --- additive-only + read-only -----------------------------------------

def test_advanced_is_purely_additive(tmp_path, monkeypatch):
    db = tmp_path / "res.db"; _make_db(str(db))
    cfg = tmp_path / "cfg.yaml"; _make_cfg(cfg, db)

    import json
    analyze.run_analysis(config_path=str(cfg), db_path=str(db), out_dir=str(tmp_path / "full"))
    full = json.loads((tmp_path / "full" / "analysis.json").read_text())

    monkeypatch.setattr(analyze_advanced, "aggregate_advanced", lambda *a, **k: None)
    analyze.run_analysis(config_path=str(cfg), db_path=str(db), out_dir=str(tmp_path / "base"))
    base = json.loads((tmp_path / "base" / "analysis.json").read_text())

    assert "advanced" in full and "advanced" not in base
    stripped = {k: v for k, v in full.items() if k != "advanced"}
    assert stripped == base
    for key in ("phase8", "per_agent", "statistics", "sensitivity", "phase7", "per_class", "notes"):
        assert full[key] == base[key]


def test_locked_db_opened_read_only(tmp_path):
    db = tmp_path / "res.db"; _make_db(str(db))
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    # the read-only connection cannot write
    conn = analyze._connect_ro(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO scenario_results VALUES('r1','A','zz','x','x',1,1,1,'complete','t')")
            conn.commit()
    finally:
        conn.close()

    cfg = tmp_path / "cfg.yaml"; _make_cfg(cfg, db)
    analyze_advanced.run_advanced(config_path=str(cfg), db_path=str(db))
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert before == after       # DB bytes unchanged by the analysis
