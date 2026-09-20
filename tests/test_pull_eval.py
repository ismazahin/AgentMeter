"""Phase 11 — pull/eval verify suite (CPU, no GPU / no server / no network).

Covers the integrity guarantees of agentmeter/pull_eval.py with a MOCK provider
and an INJECTED HF-metadata fetcher:

  (a) size guard   — a > 8B model (and a non-causal-LM model) is rejected from HF
                     metadata BEFORE any download;
  (b) single lock  — a second /pull-eval while one is running is refused;
  (c) real harness — a pull runs through the REAL runner/worker/pipeline (mock
                     provider): the pull DB has exactly 4 agent rows per scenario;
  (d) never locked — a pull writes its OWN DB, never the locked study DB, and the
                     path guard refuses a config that would resolve to it;
  (e) exploratory  — merged analysis flags the pulled model exploratory:true /
                     validated:false and leaves the canonical statistics + saw
                     table byte-for-byte unchanged.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
import types

import pytest
import yaml

from agentmeter import pull_eval

# --- fixtures / helpers -------------------------------------------------

_CSV = (
    "Destination Port,Flow Duration,Total Fwd Packets,SYN Flag Count,Label\n"
    "80,100,1,0,Benign\n"
    "22,5000,60,0,Brute Force\n"
    "443,200,2,1,Port Scanning\n"
)
N_SCEN = 3


def _mock_base_config(tmp_path, locked_db) -> str:
    """A minimal mock, CPU-only base study config (require_gpu False)."""
    csv_path = tmp_path / "flows.csv"
    csv_path.write_text(_CSV)
    cfg = {
        "run": {"mode": "pilot", "device": "cpu", "require_gpu": False,
                "seed": 42, "models": ["mock/base"]},
        "model": {"provider": "mock", "name": "mock/base", "mock": {"latency_s": 0.0}},
        "dataset": {"path": str(csv_path), "label_column": "Label",
                    "id_column": None, "drop_columns": [], "limit": N_SCEN,
                    "max_feature_chars": 4000, "label_map": {}, "drop_labels": []},
        "pipeline": {"agents": ["perceive", "reason", "decide", "act"],
                     "max_new_tokens": {"perceive": 160, "reason": 200,
                                        "decide": 12, "act": 160}},
        "classes": ["Brute Force", "Volumetric DDoS", "Port Scanning",
                    "DoS Hulk", "Benign"],
        "mitre": {"Benign": "N/A"},
        "scoring": {
            "weights": {"accuracy": 0.40, "latency": 0.25, "vram": 0.20, "tokens": 0.15},
            "targets": {"accuracy_pct": 80.0, "latency_s": 5.0,
                        "vram_mb": 16000.0, "tokens_total": 1200.0},
            "tiers": {"healthy_min": 80.0, "degraded_min": 60.0},
        },
        "storage": {"sqlite_path": str(locked_db)},
        "output": {"results_dir": str(tmp_path)},
    }
    path = tmp_path / "base_cfg.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path)


def _info(params, pipeline_tag="text-generation", architectures=None):
    """A duck-typed stand-in for huggingface_hub ModelInfo."""
    st = types.SimpleNamespace(total=params, parameters={"F32": params})
    return types.SimpleNamespace(
        safetensors=st, pipeline_tag=pipeline_tag,
        config={"architectures": architectures or ["LlamaForCausalLM"]},
    )


CANONICAL = {
    "run_ids": ["run_locked"],
    "notes": {"vram_finding": "VRAM normalises to 1.0 for all models."},
    "phase8": {
        "weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15},
        "targets": {"accuracy_pct": 80.0, "latency_s": 5.0,
                    "vram_mb": 16000.0, "tokens_total": 1200.0},
        "tiers": {"healthy_min": 80.0, "degraded_min": 60.0},
        "normalised": [
            {"model": "A", "accuracy": 0.33, "latency": 0.39, "vram": 1.0, "tokens": 0.39},
            {"model": "B", "accuracy": 0.42, "latency": 0.24, "vram": 1.0, "tokens": 0.24},
        ],
        "saw_table": [
            {"model": "A", "accuracy_pct": 26.7, "latency_s": 12.8, "total_device_vram_mb": 5200.0,
             "tokens_total": 3086.0, "composite": 0.489, "rank": 1, "tier": "Critical"},
            {"model": "B", "accuracy_pct": 33.3, "latency_s": 20.9, "total_device_vram_mb": 5000.0,
             "tokens_total": 5026.0, "composite": 0.462, "rank": 2, "tier": "Critical"},
        ],
    },
    "statistics": {
        "scenario_total_time_s": {"metric": "scenario_total_time_s", "test": "Kruskal-Wallis",
                                  "H": 42.0, "p_value": 1.2e-8, "significant": True, "alpha": 0.05},
    },
}


def _canonical_json(tmp_path) -> str:
    p = tmp_path / "canonical.json"
    p.write_text(json.dumps(CANONICAL, indent=2))
    return str(p)


def _run_mock_pull(tmp_path, model_id="mock/pulled"):
    """Run one model through the REAL orchestrator with the mock provider.
    Returns (cfg_path, db_path)."""
    locked = tmp_path / "LOCKED_study.db"
    base = _mock_base_config(tmp_path, locked)
    out_dir = tmp_path / "pulls"
    cfg_path, db_path = pull_eval.build_pull_config(base, model_id, out_dir=out_dir)
    pull_eval.run_pull(cfg_path)   # real runner.run_full, mock provider, CPU
    return cfg_path, db_path, str(locked)


# --- (a) size / type guard BEFORE download ------------------------------

def test_size_guard_rejects_over_8b():
    fetch = lambda mid: _info(13_000_000_000)   # 13B
    with pytest.raises(ValueError, match="8B|above the"):
        pull_eval.validate_model("org/too-big", info_fetcher=fetch)


def test_size_guard_accepts_under_8b():
    fetch = lambda mid: _info(7_200_000_000)
    info = pull_eval.validate_model("org/ok-7b", info_fetcher=fetch)
    assert info["params"] == 7_200_000_000
    assert info["params_b"] == 7.2


def test_guard_rejects_non_causal_lm():
    fetch = lambda mid: _info(1_000_000_000, pipeline_tag="image-classification",
                              architectures=["ResNetForImageClassification"])
    with pytest.raises(ValueError, match="causal-LM"):
        pull_eval.validate_model("org/an-image-model", info_fetcher=fetch)


def test_guard_rejects_when_params_unknown():
    # No safetensors index -> cannot verify size -> refuse (never download blindly).
    bad = types.SimpleNamespace(safetensors=None, pipeline_tag="text-generation",
                                config={"architectures": ["LlamaForCausalLM"]})
    with pytest.raises(ValueError, match="parameter count"):
        pull_eval.validate_model("org/unknown-size", info_fetcher=lambda mid: bad)


# --- (b) single-job lock ------------------------------------------------

def test_single_job_lock_rejects_second_run(tmp_path):
    locked = tmp_path / "LOCKED.db"
    base = _mock_base_config(tmp_path, locked)
    gate = threading.Event()

    def blocking_run_full(config_path, n=None, fresh=False):
        gate.wait(5.0)   # hold the job "running" until the test releases it
        return None

    mgr = pull_eval.JobManager(
        base_config=base, canonical_json=_canonical_json(tmp_path),
        info_fetcher=lambda mid: _info(7_000_000_000),
        run_full=blocking_run_full, out_dir=str(tmp_path / "pulls"),
    )
    mgr.start("org/first")
    # Wait until the job is actually active (past validation/config build).
    for _ in range(200):
        if mgr.snapshot().is_active() and mgr.snapshot().state in ("pulling", "running"):
            break
        time.sleep(0.01)
    assert mgr.snapshot().is_active()

    with pytest.raises(RuntimeError, match="already running|one at a time"):
        mgr.start("org/second")

    gate.set()
    for _ in range(200):
        if mgr.snapshot().state in ("done", "error"):
            break
        time.sleep(0.01)
    # first job resolves (done or error), and the lock is now free
    assert not mgr.snapshot().is_active()


# --- (c) real harness: 4 agents per scenario ----------------------------

def test_pull_runs_real_pipeline_four_agents(tmp_path):
    _, db_path, _ = _run_mock_pull(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # exactly 4 agent rows per scenario (real Perceive->Reason->Decide->Act)
        bad = conn.execute(
            "SELECT scenario_id, COUNT(*) n FROM agent_metrics "
            "GROUP BY model, scenario_id HAVING n <> 4"
        ).fetchall()
        assert bad == []
        agents = {r["agent_name"] for r in conn.execute("SELECT DISTINCT agent_name FROM agent_metrics")}
        assert agents == {"perceive", "reason", "decide", "act"}
        n_scn = conn.execute("SELECT COUNT(*) n FROM scenario_results WHERE status='complete'").fetchone()["n"]
        assert n_scn == N_SCEN
    finally:
        conn.close()


# --- (d) never writes the locked DB -------------------------------------

def test_pull_never_writes_locked_db(tmp_path):
    cfg_path, db_path, locked = _run_mock_pull(tmp_path)
    assert db_path != locked
    # the locked study DB was never created/written by the pull
    assert not __import__("os").path.exists(locked)
    # and the pull DB is under results/pulls-style out dir, not the locked path
    assert "pull" in db_path


def test_build_pull_config_refuses_locked_path(tmp_path):
    # Craft a base config whose storage path IS exactly what build_pull_config
    # would derive, so the guard must trip.
    out_dir = tmp_path / "pulls"
    out_dir.mkdir(parents=True)
    slug = pull_eval.model_slug("org/collide")
    collide = out_dir / f"agentmeter_pull_{slug}.db"
    base = _mock_base_config(tmp_path, collide)
    with pytest.raises(ValueError, match="locked study DB|never write"):
        pull_eval.build_pull_config(base, "org/collide", out_dir=out_dir)


# --- (e) merged analysis: exploratory row, canonical untouched ----------

def test_merged_analysis_marks_exploratory_and_preserves_canonical(tmp_path):
    cfg_path, db_path, _ = _run_mock_pull(tmp_path, model_id="mock/pulled")
    canonical_path = _canonical_json(tmp_path)

    merged = pull_eval.merged_analysis(canonical_path, db_path, cfg_path, "mock/pulled")

    # exploratory block present + flagged
    exp = merged["exploratory"]
    assert exp["exploratory"] is True and exp["validated"] is False
    assert exp["model"] == "mock/pulled"
    assert exp["saw_row"]["exploratory"] is True
    assert set(exp["normalised"].keys()) >= {"model", "accuracy", "latency", "vram", "tokens"}
    assert exp["n_scenarios"] == N_SCEN

    # canonical statistics + saw table are byte-for-byte unchanged
    assert merged["statistics"] == CANONICAL["statistics"]
    assert merged["phase8"]["saw_table"] == CANONICAL["phase8"]["saw_table"]
    assert merged["phase8"]["normalised"] == CANONICAL["phase8"]["normalised"]
    # the pulled model was NOT injected into the validated normalised list
    assert all(r["model"] != "mock/pulled" for r in merged["phase8"]["normalised"])

    # source canonical file on disk is untouched by the read
    assert json.loads(open(canonical_path).read()) == CANONICAL


# --- server: dashboard + API served SAME-ORIGIN (Vast.ai) ---------------

def _load_server_module():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "pull_eval_server.py"
    spec = importlib.util.spec_from_file_location("pull_eval_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_server_serves_dashboard_and_api_same_origin(tmp_path):
    pytest.importorskip("flask")
    srv = _load_server_module()

    locked = tmp_path / "LOCKED.db"
    base = _mock_base_config(tmp_path, locked)
    gate = threading.Event()

    def blocking_run_full(config_path, n=None, fresh=False):
        gate.wait(5.0)   # keep any started job parked; never touches merged_analysis
        return None

    # size-aware fetcher: models whose id says "big" report > 8B
    fetch = lambda mid: _info(13_000_000_000 if "big" in mid else 7_000_000_000)
    mgr = pull_eval.JobManager(
        base_config=base, canonical_json=_canonical_json(tmp_path),
        info_fetcher=fetch, run_full=blocking_run_full,
        out_dir=str(tmp_path / "pulls"),
    )
    client = srv.create_app(mgr).test_client()

    # (1) dashboard served at the root — SAME origin as the API
    r = client.get("/")
    assert r.status_code == 200
    assert b"AgentMeter" in r.data and b"Add a model" in r.data

    # (2) static assets served too
    assert b"SAW" in client.get("/saw.js").data
    assert b"PULL_CONFIG" in client.get("/pull-config.js").data
    assert client.get("/sample_analysis.json").status_code == 200

    # (3) no arbitrary file access — only the allow-listed assets
    assert client.get("/secret.txt").status_code == 404
    assert client.get("/agentmeter/pull_eval.py").status_code == 404

    # (4) API works with RELATIVE (same-origin) paths
    assert client.get("/health").get_json() == {"ok": True}
    assert client.get("/status").get_json()["state"] == "idle"
    assert client.get("/analysis").status_code == 404   # nothing run yet

    # (5) CORS fallback present for the file:// case (Origin: null). flask-cors
    # reflects the request origin; the manual fallback returns "*" — both permit it.
    r = client.get("/status", headers={"Origin": "null"})
    assert r.headers.get("Access-Control-Allow-Origin") in ("*", "null")

    # (6) size guard enforced through the API (before any job starts)
    rej = client.post("/pull-eval", json={"model_id": "org/too-big"})
    assert rej.status_code == 400 and "8B" in (rej.get_json().get("error") or "")

    # (7) a valid pull is accepted same-origin, then the single-job lock holds
    ok = client.post("/pull-eval", json={"model_id": "org/ok-7b"})
    assert ok.status_code == 200 and ok.get_json()["accepted"] is True
    for _ in range(200):
        if mgr.snapshot().is_active():
            break
        time.sleep(0.01)
    busy = client.post("/pull-eval", json={"model_id": "org/second"})
    assert busy.status_code == 409

    gate.set()   # let the parked job finish/exit
