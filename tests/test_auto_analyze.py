"""Phase 27 — run-full auto-chains the existing analyze step (CPU-only, mock).

Note on scope: `analyze` computes per-agent VRAM dominance and therefore needs the
VRAM column populated, which only happens on a GPU run — on a pure CPU/mock run it
raises (pre-existing behaviour we must NOT change). So these tests verify the
CHAINING itself (run-full invokes the existing analyze path, in the right place,
opt-out, model-vram passthrough, graceful on failure) with a spy, and verify the
"identical output / no logic change" property directly on a GPU-shaped DB.

Covered:
  (a) a successful run-full invokes the existing analyze path once, on its own DB,
      into a results/ location the dashboard discovers — and records it on the result;
  (b) auto_analyze=False (the CLI --no-analyze) does NOT call analyze;
  (c) a --model-vram file is passed through to the auto-analysis;
  (d) an induced analyze failure still leaves the completed run intact and does not
      raise (the raw data is safe);
  (e) the analyze path is deterministic, so the auto output equals a manual analyze
      on the same DB (no logic change).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from agentmeter.run.runner import run_full
from agentmeter.analysis import analyze

_CSV = (
    "Destination Port,Flow Duration,Total Fwd Packets,SYN Flag Count,Label\n"
    "80,100,1,0,Benign\n"
    "22,5000,60,0,Brute Force\n"
    "443,200,2,1,Port Scanning\n"
)
MODELS = ["mock/m0", "mock/m1"]
N_SCEN = 3
CLASSES = ["Brute Force", "Volumetric DDoS", "Port Scanning", "DoS Hulk", "Benign"]


def _write_config(tmp_path, db_path) -> str:
    csv_path = tmp_path / "flows.csv"
    csv_path.write_text(_CSV)
    cfg = {
        "run": {"mode": "pilot", "device": "cpu", "require_gpu": False,
                "seed": 42, "models": MODELS},
        "model": {"provider": "mock", "name": MODELS[0], "mock": {"latency_s": 0.0}},
        "dataset": {"path": str(csv_path), "label_column": "Label", "id_column": None,
                    "drop_columns": [], "limit": N_SCEN, "max_feature_chars": 4000,
                    "label_map": {}, "drop_labels": []},
        "pipeline": {"agents": ["perceive", "reason", "decide", "act"],
                     "max_new_tokens": {"perceive": 160, "reason": 200, "decide": 12, "act": 160}},
        "classes": CLASSES,
        "mitre": {"Benign": "N/A"},
        "scoring": {"weights": {"accuracy": 0.40, "latency": 0.25, "vram": 0.20, "tokens": 0.15},
                    "targets": {"accuracy_pct": 80.0, "latency_s": 5.0,
                                "vram_mb": 16000.0, "tokens_total": 1200.0},
                    "tiers": {"healthy_min": 80.0, "degraded_min": 60.0}},
        "storage": {"sqlite_path": str(db_path)},
        "output": {"results_dir": str(tmp_path)},
    }
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path)


def _out_dir(db_path) -> Path:
    p = Path(db_path)
    return p.parent / f"{p.stem}_analysis"


def _spy(record):
    """A stand-in for analyze.run_analysis that records the call and writes a stub
    analysis.json into out_dir (so we can also assert the file lands where expected)."""
    def fake(config_path=None, db_path=None, out_dir=None, model_vram_path=None):
        record.append({"config_path": config_path, "db_path": db_path,
                       "out_dir": out_dir, "model_vram_path": model_vram_path})
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "analysis.json").write_text('{"stub": true}')
        return {"stub": True}
    return fake


# (a) chaining on success -------------------------------------------------

def test_run_full_chains_analyze_into_results(tmp_path, monkeypatch):
    db = tmp_path / "run.db"
    cfg = _write_config(tmp_path, db)
    calls = []
    monkeypatch.setattr(analyze, "run_analysis", _spy(calls))

    res = run_full(config_path=cfg, fresh=True)               # auto_analyze default = on

    assert len(calls) == 1                                    # analyze was invoked once
    c = calls[0]
    assert c["db_path"] == str(db)                            # on the DB run-full wrote
    assert c["out_dir"] == str(_out_dir(db))                  # per-run dir
    assert c["config_path"] == cfg
    # discoverable: the output dir is under the configured results/ tree
    assert str(_out_dir(db)).startswith(str(tmp_path))
    assert (_out_dir(db) / "analysis.json").exists()
    assert res.analyzed is True and res.analysis_out_dir == str(_out_dir(db))


# (b) opt-out -------------------------------------------------------------

def test_no_analyze_skips(tmp_path, monkeypatch):
    db = tmp_path / "run.db"
    cfg = _write_config(tmp_path, db)
    calls = []
    monkeypatch.setattr(analyze, "run_analysis", _spy(calls))

    res = run_full(config_path=cfg, fresh=True, auto_analyze=False)

    assert calls == []                                        # analyze NOT called
    assert res.analyzed is False
    assert db.exists()                                        # raw run still present
    assert not (_out_dir(db) / "analysis.json").exists()


def test_config_can_disable_auto_analyze(tmp_path, monkeypatch):
    db = tmp_path / "run.db"
    # same config but run.auto_analyze: false
    csv_path = tmp_path / "flows.csv"; csv_path.write_text(_CSV)
    cfg = yaml.safe_load(Path(_write_config(tmp_path, db)).read_text())
    cfg["run"]["auto_analyze"] = False
    p = tmp_path / "cfg2.yaml"; p.write_text(yaml.safe_dump(cfg))
    calls = []
    monkeypatch.setattr(analyze, "run_analysis", _spy(calls))

    res = run_full(config_path=str(p), fresh=True)            # auto_analyze=None -> config decides
    assert calls == [] and res.analyzed is False


# (c) model-vram passthrough ---------------------------------------------

def test_model_vram_passed_through(tmp_path, monkeypatch):
    db = tmp_path / "run.db"
    cfg = _write_config(tmp_path, db)
    calls = []
    monkeypatch.setattr(analyze, "run_analysis", _spy(calls))

    run_full(config_path=cfg, fresh=True, model_vram_path="results/model_vram.json")
    assert calls[0]["model_vram_path"] == "results/model_vram.json"


# (d) analyze failure never fails the run --------------------------------

def test_analyze_failure_does_not_fail_the_run(tmp_path, monkeypatch):
    db = tmp_path / "run.db"
    cfg = _write_config(tmp_path, db)

    def boom(*a, **k):
        raise RuntimeError("induced analyze failure")
    monkeypatch.setattr(analyze, "run_analysis", boom)

    res = run_full(config_path=cfg, fresh=True)               # must NOT raise
    assert res.run_id and res.analyzed is False
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE status='complete'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM scenario_results").fetchone()[0] == len(MODELS) * N_SCEN
    finally:
        conn.close()
    assert not (_out_dir(db) / "analysis.json").exists()


# (e) identical output / no logic change --------------------------------

def _make_gpu_shaped_db(path):
    """A DB shaped like a GPU run (VRAM populated) so the real analyze path runs.
    Used only to prove the analyze path is deterministic -> auto == manual."""
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


def test_analyze_path_is_deterministic_auto_equals_manual(tmp_path):
    db = tmp_path / "gpu.db"
    _make_gpu_shaped_db(str(db))
    cfg = _write_config(tmp_path, db)  # storage.sqlite_path points at db

    # the auto path and the manual path call the SAME run_analysis with the same
    # args, so two invocations on the same DB must be byte-identical.
    analyze.run_analysis(config_path=cfg, db_path=str(db), out_dir=str(tmp_path / "auto"))
    analyze.run_analysis(config_path=cfg, db_path=str(db), out_dir=str(tmp_path / "manual"))
    auto = json.loads((tmp_path / "auto" / "analysis.json").read_text())
    manual = json.loads((tmp_path / "manual" / "analysis.json").read_text())
    assert auto == manual
