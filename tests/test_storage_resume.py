"""Phase 6 — storage + checkpoint/resume tests (mock provider, CPU-only).

No GPU, no HF token, no network. Each model runs in a WORKER SUBPROCESS (VRAM
isolation by process); the parent spawns them sequentially. Covers exactly:
  (a) atomicity  — a mid-scenario failure rolls back; no half-written scenario
      is left that resume would skip;
  (b) resume     — a killed worker leaves its model incomplete; resume re-runs
      only that model, skipping done scenarios, with no duplicate rows;
  (c) config guard    — a changed config_fingerprint is refused;
  (d) hardware guard  — a changed hardware_label is refused;
  (e) isolation  — run-full spawns exactly one worker subprocess per model.
"""
from __future__ import annotations

import sqlite3

import pytest
import yaml

from agentmeter import runner
from agentmeter.config import load_config
from agentmeter.dataset import DatasetLoader
from agentmeter.instrument import AgentMetrics
from agentmeter.runner import (
    ConfigMismatchError,
    WorkerError,
    config_fingerprint,
    resolve_models,
    run_full,
)
from agentmeter.storage import Storage

# --- fixtures / helpers -------------------------------------------------

_CSV = (
    "Destination Port,Flow Duration,Total Fwd Packets,SYN Flag Count,Label\n"
    "80,100,1,0,Benign\n"
    "22,5000,60,0,Brute Force\n"
    "443,200,2,1,Port Scanning\n"
)

MODELS = ["mock/m0", "mock/m1", "mock/m2"]
N_SCEN = 3  # rows in _CSV


def _write_config(tmp_path, db_path) -> str:
    """Write a minimal mock, CPU-only config; return its path (str)."""
    csv_path = tmp_path / "flows.csv"
    csv_path.write_text(_CSV)
    cfg = {
        "run": {"mode": "pilot", "device": "cpu", "require_gpu": False,
                "seed": 42, "models": MODELS},
        "model": {"provider": "mock", "name": MODELS[0],
                  "mock": {"latency_s": 0.0}},
        "dataset": {"path": str(csv_path), "label_column": "Label",
                    "id_column": None, "drop_columns": [], "limit": N_SCEN,
                    "max_feature_chars": 4000, "label_map": {}, "drop_labels": []},
        "pipeline": {"agents": ["perceive", "reason", "decide", "act"],
                     "max_new_tokens": {"perceive": 160, "reason": 200,
                                        "decide": 12, "act": 160}},
        "classes": ["Brute Force", "Volumetric DDoS", "Port Scanning",
                    "DoS Hulk", "Benign"],
        "mitre": {"Benign": "N/A"},
        "scoring": {"weights": {"accuracy": 0.40, "latency": 0.25,
                                "vram": 0.20, "tokens": 0.15}},
        "storage": {"sqlite_path": str(db_path)},
        "output": {"results_dir": str(tmp_path)},
    }
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path)


def _integrity(db_path):
    """Return (dup_scn, dup_agent, bad_agent_count) — all should be empty."""
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    dup_scn = c.execute(
        "SELECT model, scenario_id, COUNT(*) n FROM scenario_results "
        "GROUP BY model, scenario_id HAVING n > 1"
    ).fetchall()
    dup_agent = c.execute(
        "SELECT model, scenario_id, agent_name, COUNT(*) n FROM agent_metrics "
        "GROUP BY model, scenario_id, agent_name HAVING n > 1"
    ).fetchall()
    bad = c.execute(
        "SELECT model, scenario_id, COUNT(*) n FROM agent_metrics "
        "GROUP BY model, scenario_id HAVING n <> 4"
    ).fetchall()
    c.close()
    return dup_scn, dup_agent, bad


def _current_fingerprint(cfg_path):
    cfg = load_config(cfg_path)
    models = resolve_models(cfg)
    scenarios = DatasetLoader(cfg).load()
    return config_fingerprint(cfg, models, len(scenarios))


# --- (a) atomicity: mid-scenario failure rolls back ---------------------

def test_persist_scenario_is_atomic(tmp_path):
    db = tmp_path / "atomic.db"
    store = Storage(db)
    store.create_run("run_x", "fp", "none", "cpu")

    agent_rows = [
        AgentMetrics("row_0000", "mock/m0", name, 0.01, None, None, 5, 3)
        for name in ("perceive", "reason", "decide", "act")
    ]

    # Simulate a crash MID-transaction: fail on the scenario_results INSERT,
    # after the four agent rows have already been inserted in the same txn.
    # (sqlite3.Connection.execute is read-only, so wrap the connection.)
    real_conn = store.conn

    class _FlakyConn:
        def execute(self, sql, *args, **kwargs):
            if sql.strip().startswith("INSERT INTO scenario_results"):
                raise RuntimeError("simulated crash mid-scenario")
            return real_conn.execute(sql, *args, **kwargs)

        def __enter__(self):
            return real_conn.__enter__()

        def __exit__(self, *exc):
            return real_conn.__exit__(*exc)  # commits, or ROLLS BACK on exception

        def __getattr__(self, name):
            return getattr(real_conn, name)

    store.conn = _FlakyConn()
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.persist_scenario(
            "run_x", "mock/m0", "row_0000", "Benign", "Benign", True,
            0.04, None, agent_rows,
        )
    store.conn = real_conn

    # Rollback: NOTHING for this scenario — not even the agent rows.
    assert store.table_counts()["scenario_results"] == 0
    assert store.table_counts()["agent_metrics"] == 0
    # And resume would NOT see it as complete (so it re-runs it, no skip).
    assert ("mock/m0", "row_0000") not in store.completed_pairs("run_x")

    # A subsequent clean persist succeeds and IS visible to resume.
    store.persist_scenario(
        "run_x", "mock/m0", "row_0000", "Benign", "Benign", True,
        0.04, None, agent_rows,
    )
    assert store.table_counts()["agent_metrics"] == 4
    assert ("mock/m0", "row_0000") in store.completed_pairs("run_x")
    store.close()


# --- (b) resume skips completed pairs, no duplicates --------------------

def test_resume_reruns_only_the_incomplete_model(tmp_path, monkeypatch):
    """Kill a worker mid-model; resume re-runs only that model, no duplicates,
    no half-written scenarios. The worker runs in a subprocess, so the crash is
    injected via AGENTMETER_WORKER_CRASH_AFTER_SCENARIOS (inherited by the spawn)."""
    db = tmp_path / "resume.db"
    cfg_path = _write_config(tmp_path, db)

    # First model's worker crashes after persisting exactly 1 scenario. The parent
    # sees the non-zero exit and stops (WorkerError), leaving model 0 partial and
    # models 1..N never started.
    monkeypatch.setenv("AGENTMETER_WORKER_CRASH_AFTER_SCENARIOS", "1")
    with pytest.raises(WorkerError):
        run_full(config_path=cfg_path, fresh=True)

    store = Storage(db)
    incomplete = store.find_incomplete_run()
    assert incomplete is not None  # run still 'running'
    completed_before = store.completed_pairs(incomplete["run_id"])
    assert len(completed_before) == 1  # exactly one scenario persisted before crash
    store.close()
    _, _, bad = _integrity(db)
    assert bad == []  # the crashed scenario left NO half-written rows (atomic)

    # Resume (no --fresh, no crash env): the worker for the incomplete model skips
    # its 1 done scenario and finishes the rest; the other models then run.
    monkeypatch.delenv("AGENTMETER_WORKER_CRASH_AFTER_SCENARIOS")
    result = run_full(config_path=cfg_path)

    assert result.resumed is True
    assert result.scenarios_skipped == 1
    assert result.scenarios_run == len(MODELS) * N_SCEN - 1

    dup_scn, dup_agent, bad = _integrity(db)
    assert dup_scn == [] and dup_agent == [] and bad == []

    final = Storage(db)
    counts = final.table_counts()
    assert counts["scenario_results"] == len(MODELS) * N_SCEN
    assert counts["agent_metrics"] == len(MODELS) * N_SCEN * 4
    assert final.find_incomplete_run() is None  # run marked complete
    final.close()


# --- (c) changed config_fingerprint is refused --------------------------

def test_changed_fingerprint_is_refused(tmp_path):
    db = tmp_path / "cfg_guard.db"
    cfg_path = _write_config(tmp_path, db)

    # Seed an incomplete run whose fingerprint differs from the current config,
    # but on the SAME hardware (so only the fingerprint guard can trip).
    store = Storage(db)
    store.create_run("run_old", "DIFFERENT_FINGERPRINT", "none",
                     runner.hardware_label())
    store.close()

    with pytest.raises(ConfigMismatchError, match="DIFFERENT config"):
        run_full(config_path=cfg_path)

    # The incomplete run is untouched (still 'running').
    store = Storage(db)
    assert store.find_incomplete_run()["run_id"] == "run_old"
    store.close()


# --- (d) changed hardware_label is refused ------------------------------

def test_changed_hardware_is_refused(tmp_path):
    db = tmp_path / "hw_guard.db"
    cfg_path = _write_config(tmp_path, db)

    # Seed an incomplete run with the CORRECT fingerprint (so it gets past the
    # config guard) but a DIFFERENT hardware_label than this host.
    store = Storage(db)
    store.create_run("run_gpu", _current_fingerprint(cfg_path), "none",
                     "nvidia-l4-somewhere-else")
    store.close()

    assert runner.hardware_label() != "nvidia-l4-somewhere-else"  # sanity
    with pytest.raises(ConfigMismatchError, match="DIFFERENT\\s+hardware"):
        run_full(config_path=cfg_path)

    # Untouched; --fresh is the escape hatch and starts a clean run.
    store = Storage(db)
    assert store.find_incomplete_run()["run_id"] == "run_gpu"
    store.close()

    result = run_full(config_path=cfg_path, fresh=True)
    assert result.resumed is False
    assert result.scenarios_run == len(MODELS) * N_SCEN


# --- VRAM isolation: ONE worker subprocess per model, spawned sequentially -----

def test_run_full_spawns_one_worker_per_model(tmp_path, monkeypatch):
    """run-full must spawn exactly one worker subprocess per model (VRAM isolation
    by process). Each spawn is sequential (the real spawn BLOCKS on subprocess)."""
    db = tmp_path / "spawn.db"
    cfg_path = _write_config(tmp_path, db)

    real_spawn = runner._spawn_model_worker
    spawned: list[tuple[str, str]] = []

    def spy_spawn(config_path, model, run_id, n, *args, **kwargs):
        spawned.append((model, run_id))
        return real_spawn(config_path, model, run_id, n, *args, **kwargs)

    monkeypatch.setattr(runner, "_spawn_model_worker", spy_spawn)
    run_full(config_path=cfg_path, fresh=True)

    # One worker per model, and all under the same run.
    assert [m for m, _ in spawned] == MODELS
    assert len({rid for _, rid in spawned}) == 1

    # Same SQLite row shape as the in-process version produced.
    final = Storage(db)
    counts = final.table_counts()
    assert counts["runs"] == 1
    assert counts["scenario_results"] == len(MODELS) * N_SCEN
    assert counts["agent_metrics"] == len(MODELS) * N_SCEN * 4
    final.close()
