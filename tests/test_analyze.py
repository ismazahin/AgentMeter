"""Phase 7 + 8 analysis tests (CPU, no GPU). Pure-function checks on synthetic
data plus a tiny end-to-end run over an in-memory-style temp DB."""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest
import yaml

from agentmeter import analyze

CLASSES = ["Brute Force", "Volumetric DDoS", "Port Scanning", "DoS Hulk", "Benign"]
TARGETS = {"accuracy_pct": 80.0, "latency_s": 5.0, "vram_mb": 16000.0, "tokens_total": 1200.0}
TIERS = {"healthy_min": 80.0, "degraded_min": 60.0}


# --- Phase 7: accuracy aggregation -------------------------------------

def test_accuracy_aggregation_counts_and_unparseable():
    sr = pd.DataFrame([
        {"model": "A", "held_out_label": "Benign", "predicted_label": "Benign", "correct": 1},
        {"model": "A", "held_out_label": "Benign", "predicted_label": "Unparseable", "correct": 0},
        {"model": "A", "held_out_label": "DoS Hulk", "predicted_label": "DoS Hulk", "correct": 1},
    ])
    res = analyze.phase7(sr, CLASSES)
    row = res["per_model"].iloc[0]
    assert row["n"] == 3
    assert row["n_correct"] == 2
    assert abs(row["accuracy"] - 2 / 3) < 1e-12
    assert row["n_unparseable"] == 1
    # confusion row sums equal per-class support; Unparseable column present
    cm = res["confusion"]["A"]
    assert "Unparseable" in cm.columns
    assert int(cm.loc["Benign"].sum()) == 2 and int(cm.loc["DoS Hulk"].sum()) == 1


# --- Phase 8: SAW normalisation, clamping, weights, tiers ---------------

def test_normalisation_clamps_and_directions():
    raw = pd.DataFrame([
        # accuracy above target -> clamp 1; latency below target -> clamp 1
        {"model": "hi", "accuracy_pct": 90.0, "latency_s": 2.5, "vram_mb": 100.0, "tokens_total": 600.0},
        # accuracy at half target; latency at 2x target -> 0.5
        {"model": "lo", "accuracy_pct": 40.0, "latency_s": 10.0, "vram_mb": 32000.0, "tokens_total": 2400.0},
    ])
    n = analyze._normalise(raw, TARGETS).set_index("model")
    # benefit (accuracy) = clamp(value/target)
    assert n.loc["hi", "accuracy"] == 1.0            # 90/80 -> clamp 1
    assert abs(n.loc["lo", "accuracy"] - 0.5) < 1e-12  # 40/80
    # cost (latency/vram/tokens) = clamp(target/value)
    assert n.loc["hi", "latency"] == 1.0             # 5/2.5 -> clamp 1
    assert abs(n.loc["lo", "latency"] - 0.5) < 1e-12  # 5/10
    assert abs(n.loc["lo", "vram"] - 0.5) < 1e-12     # 16000/32000
    assert abs(n.loc["lo", "tokens"] - 0.5) < 1e-12   # 1200/2400
    # everything stays within [0, 1]
    vals = n[["accuracy", "latency", "vram", "tokens"]].to_numpy()
    assert (vals >= 0).all() and (vals <= 1).all()


def test_weights_sum_check_rejects_bad_weights():
    raw = pd.DataFrame([{"model": "A", "accuracy_pct": 50, "latency_s": 5, "vram_mb": 100, "tokens_total": 600}])
    bad = {"weights": {"accuracy": 0.5, "latency": 0.25, "vram": 0.2, "tokens": 0.15},  # sums to 1.10
           "targets": TARGETS, "tiers": TIERS}
    with pytest.raises(AssertionError, match="sum to 1.0"):
        analyze.phase8(raw, bad)


def test_tier_mapping_at_boundaries():
    assert analyze._tier(80.0, TIERS) == "Healthy"
    assert analyze._tier(79.999, TIERS) == "Degraded"
    assert analyze._tier(60.0, TIERS) == "Degraded"
    assert analyze._tier(59.999, TIERS) == "Critical"


# --- end-to-end over a small temp DB -----------------------------------

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
    # also an INCOMPLETE run whose rows must be ignored
    conn.execute("INSERT INTO runs VALUES('r2','fp','4bit-nf4','L4','t0',NULL,'running','')")
    agents = ["perceive", "reason", "decide", "act"]
    # model A: all correct; model B: all wrong (one Unparseable). Distinct latency/vram per model.
    for mi, (model, correct, pred_fn, lat, vram) in enumerate([
            ("A", 1, lambda c: c, 1.0, 100.0),
            ("B", 0, lambda c: "Unparseable", 4.0, 400.0)]):
        for si in range(10):
            cls = CLASSES[si % len(CLASSES)]
            conn.execute("INSERT INTO scenario_results VALUES(?,?,?,?,?,?,?,?,?,?)",
                         ("r1", model, f"row_{si:04d}", pred_fn(cls), cls, correct,
                          lat + si * 0.01, vram + si, "complete", "t"))
            for a in agents:
                conn.execute("INSERT INTO agent_metrics VALUES(?,?,?,?,?,?,?,?,?)",
                             ("r1", model, f"row_{si:04d}", a, 0.1, 0.01, 10.0, 100, 20))
    # a row under the incomplete run — must never enter the aggregation
    conn.execute("INSERT INTO scenario_results VALUES('r2','A','row_9999','Benign','Benign',1,1,1,'complete','t')")
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
                    "sensitivity_weight_sets": {
                        "equal": {"accuracy": 0.25, "latency": 0.25, "vram": 0.25, "tokens": 0.25},
                        "accuracy_heavy": {"accuracy": 0.55, "latency": 0.20, "vram": 0.15, "tokens": 0.10}}},
        "storage": {"sqlite_path": str(db_path)},
    }
    path.write_text(yaml.safe_dump(cfg))


def test_run_analysis_end_to_end(tmp_path):
    db = tmp_path / "res.db"
    _make_db(str(db))
    cfg = tmp_path / "cfg.yaml"
    _make_cfg(cfg, db)
    out = tmp_path / "analysis"

    res = analyze.run_analysis(config_path=str(cfg), db_path=str(db), out_dir=str(out))

    pm = res["p7"]["per_model"].set_index("model")
    assert pm.loc["A", "accuracy"] == 1.0        # model A all correct
    assert pm.loc["B", "accuracy"] == 0.0        # model B all wrong
    assert pm.loc["B", "n_unparseable"] == 10
    assert pm.loc["A", "n"] == 10 and pm.loc["B", "n"] == 10  # incomplete-run row excluded

    saw = res["p8"]["table"]
    assert set(saw["model"]) == {"A", "B"}
    assert ((saw["composite"] >= 0) & (saw["composite"] <= 1)).all()
    assert saw.iloc[0]["model"] == "A"           # A ranks above B (higher accuracy, lower cost)

    assert isinstance(res["sensitivity"]["top_stable"], bool)
    # Kruskal-Wallis ran on both metrics
    assert set(res["statistics"].keys()) == {"scenario_total_time_s", "scenario_peak_vram_mb"}
    assert (out / "analysis.json").exists()
    assert (out / "phase8_saw.csv").exists()
