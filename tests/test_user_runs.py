"""Phase 19 — reconfigurable-run result handling (CPU only, no GPU).

Guarantees that a user/custom config run is kept STRICTLY separate from the locked
validation study:
  * a config from the config builder writes to results/user_runs/<name>.db — never
    the locked study DB;
  * analyze() flags an analysis from any non-locked DB as non_validated / a "user
    run", and flags the locked study DB as the validated baseline;
  * running analyze on a user DB never alters a separate (locked) DB or its analysis
    output — byte-for-byte unchanged.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from agentmeter import analyze, config_builder as cb

CLASSES = ["Brute Force", "Volumetric DDoS", "Port Scanning", "DoS Hulk", "Benign"]
TARGETS = {"accuracy_pct": 80.0, "latency_s": 5.0, "vram_mb": 16000.0, "tokens_total": 1200.0}
TIERS = {"healthy_min": 80.0, "degraded_min": 60.0}


# --- A user config never targets the locked study DB -------------------

def test_user_config_writes_to_user_runs_dir():
    cfg = cb.build_config(name="my custom run", models=["m/x"],
                          dataset_path="data/x.csv")
    path = cfg["storage"]["sqlite_path"]
    assert path == "results/user_runs/my-custom-run.db"
    # explicitly NOT the locked study DB
    assert "agentmeter_full_l4.db" not in path


# --- provenance classification (pure) ----------------------------------

class _CfgStub:
    def __init__(self, p):
        self.path = p


def test_provenance_flags_user_run_non_validated(tmp_path):
    userdb = tmp_path / "user_runs" / "run.db"
    prov = analyze._provenance(userdb, _CfgStub("configs/user/run.yaml"))
    assert prov["non_validated"] is True
    assert prov["validated_study"] is False
    assert prov["run_kind"] == "user_run"
    assert "NOT the validated baseline" in prov["note"]


def test_provenance_flags_locked_db_validated():
    prov = analyze._provenance(analyze.LOCKED_STUDY_DB,
                               _CfgStub("configs/run_full_l4.yaml"))
    assert prov["validated_study"] is True
    assert prov["non_validated"] is False
    assert prov["run_kind"] == "validated_study"


# --- end-to-end: a user-run analysis is flagged non_validated ----------

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
    for model, correct, lat, vram in [("A", 1, 1.0, 100.0), ("B", 0, 4.0, 400.0)]:
        for si in range(10):
            cls = CLASSES[si % len(CLASSES)]
            pred = cls if correct else "Unparseable"
            conn.execute("INSERT INTO scenario_results VALUES(?,?,?,?,?,?,?,?,?,?)",
                         ("r1", model, f"row_{si:04d}", pred, cls, correct,
                          lat + si * 0.01, vram + si, "complete", "t"))
            for a in ["perceive", "reason", "decide", "act"]:
                conn.execute("INSERT INTO agent_metrics VALUES(?,?,?,?,?,?,?,?,?)",
                             ("r1", model, f"row_{si:04d}", a, 0.1, 0.01, 10.0, 100, 20))
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
                        "equal": {"accuracy": 0.25, "latency": 0.25, "vram": 0.25, "tokens": 0.25}}},
        "storage": {"sqlite_path": str(db_path)},
    }
    path.write_text(yaml.safe_dump(cfg))


def test_user_run_analysis_is_flagged_non_validated(tmp_path):
    db = tmp_path / "user_runs" / "custom.db"
    db.parent.mkdir(parents=True)
    _make_db(str(db))
    cfg = tmp_path / "custom.yaml"; _make_cfg(cfg, db)
    out = tmp_path / "analysis_custom"

    res = analyze.run_analysis(config_path=str(cfg), db_path=str(db), out_dir=str(out))

    assert res["provenance"]["non_validated"] is True
    assert res["provenance"]["run_kind"] == "user_run"

    payload = json.loads((out / "analysis.json").read_text())
    assert payload["provenance"]["non_validated"] is True
    assert payload["provenance"]["validated_study"] is False
    # summary text carries the visible warning
    assert "USER/CUSTOM RUN" in (out / "summary.txt").read_text()


def test_user_run_never_touches_the_locked_baseline(tmp_path, monkeypatch):
    """Treat one DB as the locked baseline; running a user-config analysis on a
    SEPARATE DB must leave the locked DB and its analysis byte-for-byte unchanged."""
    # stand-in locked baseline
    locked = tmp_path / "agentmeter_full_l4.db"
    _make_db(str(locked))
    monkeypatch.setattr(analyze, "LOCKED_STUDY_DB", locked.resolve())

    locked_cfg = tmp_path / "locked.yaml"; _make_cfg(locked_cfg, locked)
    locked_out = tmp_path / "locked_analysis"
    base = analyze.run_analysis(config_path=str(locked_cfg), db_path=str(locked),
                                out_dir=str(locked_out))
    assert base["provenance"]["validated_study"] is True   # this IS the baseline now

    locked_db_bytes = locked.read_bytes()
    locked_json_bytes = (locked_out / "analysis.json").read_bytes()

    # now a user-config run on a DIFFERENT db
    userdb = tmp_path / "user_runs" / "u.db"; userdb.parent.mkdir(parents=True)
    _make_db(str(userdb))
    user_cfg = tmp_path / "user.yaml"; _make_cfg(user_cfg, userdb)
    user = analyze.run_analysis(config_path=str(user_cfg), db_path=str(userdb),
                                out_dir=str(tmp_path / "user_analysis"))
    assert user["provenance"]["non_validated"] is True

    # locked DB + locked analysis unchanged, byte-for-byte
    assert locked.read_bytes() == locked_db_bytes
    assert (locked_out / "analysis.json").read_bytes() == locked_json_bytes
