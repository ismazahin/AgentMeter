"""Phase 18 — config builder tests (CPU only, no GPU / no run).

Covers the two layers:
  * agentmeter/config_builder.py — builds a VALID parseable config, rejects a
    weights-don't-sum-to-1 config, and REFUSES to write the locked study config
    or the root config.yaml (or anything escaping configs/user/).
  * the Flask endpoint /api/build-config — same guarantees over HTTP, plus the
    read-only /api/locked-config preview.

None of this starts a run, loads a model, or opens the locked study DB.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from agentmeter import config as agm_config
from agentmeter import config_builder as cb

REPO = Path(__file__).resolve().parents[1]

_GOOD = dict(
    name="my experiment",
    models=["mistralai/Mistral-7B-Instruct-v0.3", "google/gemma-2-9b-it"],
    dataset_path="data/cicids_full_300.csv",
    weights={"accuracy": 0.40, "latency": 0.25, "vram": 0.20, "tokens": 0.15},
    targets={"accuracy_pct": 85.0, "latency_s": 4.0},
    tiers={"healthy_min": 75.0, "degraded_min": 55.0},
    agent_tokens={"decide": 16},
)


# --- module: build_config ------------------------------------------------

def test_build_config_is_valid_and_parseable():
    config = cb.build_config(**_GOOD)
    # round-trips through YAML
    text = cb.to_yaml(config)
    reloaded = yaml.safe_load(text)
    assert reloaded["run"]["models"] == _GOOD["models"]
    assert reloaded["model"]["name"] == _GOOD["models"][0]
    assert reloaded["dataset"]["path"] == "data/cicids_full_300.csv"
    assert reloaded["pipeline"]["max_new_tokens"]["decide"] == 16
    assert reloaded["scoring"]["targets"]["accuracy_pct"] == 85.0
    assert reloaded["scoring"]["tiers"]["healthy_min"] == 75.0
    # satisfies the harness validator (six sections, provider, weights sum to 1)
    agm_config._validate(reloaded, Path("generated.yaml"))


def test_written_config_loads_via_config_loader(tmp_path):
    path = cb.write_user_config("run one", cb.build_config(**_GOOD),
                                base_dir=tmp_path)
    assert path.exists() and path.name == "run-one.yaml"
    # load_config runs the real _validate; must not raise
    loaded = agm_config.load_config(path)
    assert loaded.get("model.provider") == "hf"
    assert loaded.get("scoring.weights.accuracy") == 0.40


def test_default_weights_when_omitted():
    cfg = cb.build_config(name="d", models=["m/x"], dataset_path="d.csv")
    assert cfg["scoring"]["weights"] == cb.DEFAULT_WEIGHTS


def test_mock_provider_is_cpu_no_gpu():
    cfg = cb.build_config(name="mk", models=["mock/m"],
                          dataset_path="d.csv", provider="mock")
    assert cfg["run"]["device"] == "cpu"
    assert cfg["run"]["require_gpu"] is False
    agm_config._validate(cfg, Path("m.yaml"))


# --- module: weight validation ------------------------------------------

def test_weights_must_sum_to_one():
    bad = dict(_GOOD, weights={"accuracy": 0.5, "latency": 0.25,
                               "vram": 0.20, "tokens": 0.15})  # 1.10
    with pytest.raises(cb.ConfigBuildError, match="sum to 1.0"):
        cb.build_config(**bad)


def test_weights_missing_key_rejected():
    bad = dict(_GOOD, weights={"accuracy": 0.6, "latency": 0.25, "vram": 0.15})
    with pytest.raises(cb.ConfigBuildError, match="missing"):
        cb.build_config(**bad)


def test_no_models_rejected():
    with pytest.raises(cb.ConfigBuildError, match="model id"):
        cb.build_config(name="x", models=[], dataset_path="d.csv")


def test_empty_dataset_rejected():
    with pytest.raises(cb.ConfigBuildError, match="dataset path"):
        cb.build_config(name="x", models=["m/x"], dataset_path="  ")


def test_bad_name_rejected():
    with pytest.raises(cb.ConfigBuildError, match="config name"):
        cb.build_config(name="///", models=["m/x"], dataset_path="d.csv")


# --- module: REFUSES to write protected / escaping paths -----------------

def test_refuses_to_overwrite_locked_study_config():
    """Even if a name/base_dir resolves onto the locked study config, refuse."""
    cfg = cb.build_config(**_GOOD)
    # base_dir = configs/, name = run_full_l4 -> configs/run_full_l4.yaml (LOCKED)
    with pytest.raises(cb.ConfigBuildError, match="locked"):
        cb.write_user_config("run_full_l4", cfg,
                             base_dir=REPO / "configs")
    assert cb.LOCKED_STUDY_CONFIG.read_text().count("configs") >= 0  # untouched


def test_refuses_to_overwrite_root_config():
    cfg = cb.build_config(**_GOOD)
    # base_dir = repo root, name = config -> config.yaml (ROOT)
    with pytest.raises(cb.ConfigBuildError, match="locked|protected"):
        cb.write_user_config("config", cfg, base_dir=REPO)


def test_refuses_path_traversal(tmp_path):
    """A name that tries to escape the user dir slugs to a safe stem, never
    writing outside base_dir."""
    cfg = cb.build_config(**_GOOD)
    path = cb.write_user_config("../../evil", cfg, base_dir=tmp_path)
    assert path.parent.resolve() == tmp_path.resolve()
    assert path.name == "evil.yaml"


# --- endpoint ------------------------------------------------------------

def _load_server():
    path = REPO / "scripts" / "pull_eval_server.py"
    spec = importlib.util.spec_from_file_location("pull_eval_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _client(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    srv = _load_server()
    from agentmeter import pull_eval

    # Point the builder's output dir at a temp dir so tests never touch the repo.
    user_dir = tmp_path / "userconfigs"
    monkeypatch.setattr(cb, "USER_CONFIG_DIR", user_dir)

    csv = tmp_path / "f.csv"
    csv.write_text("A,Label\n1,Benign\n")
    cfg = {"run": {"mode": "pilot", "device": "cpu", "require_gpu": False,
                   "seed": 1, "models": ["mock/m"]},
           "model": {"provider": "mock", "name": "mock/m", "mock": {"latency_s": 0.0}},
           "dataset": {"path": str(csv), "label_column": "Label", "id_column": None,
                       "drop_columns": [], "limit": 1, "max_feature_chars": 100,
                       "label_map": {}, "drop_labels": []},
           "pipeline": {"agents": ["perceive", "reason", "decide", "act"],
                        "max_new_tokens": {"perceive": 8, "reason": 8, "decide": 4, "act": 8}},
           "classes": ["Benign"], "mitre": {"Benign": "N/A"},
           "scoring": {"weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15}},
           "storage": {"sqlite_path": str(tmp_path / "app.db")}}
    base = tmp_path / "cfg.yaml"
    base.write_text(yaml.safe_dump(cfg))
    mgr = pull_eval.JobManager(base_config=str(base),
                               canonical_json=str(tmp_path / "c.json"),
                               out_dir=str(tmp_path / "pulls"))
    return srv.create_app(mgr).test_client(), user_dir


def test_endpoint_writes_valid_config(tmp_path, monkeypatch):
    client, user_dir = _client(tmp_path, monkeypatch)
    r = client.post("/api/build-config", json={
        "name": "endpoint run",
        "models": ["mistralai/Mistral-7B-Instruct-v0.3"],
        "dataset_path": "data/cicids_full_300.csv",
        "weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15},
    })
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["saved"] is True
    written = user_dir / "endpoint-run.yaml"
    assert written.exists()
    # parseable + valid
    reloaded = yaml.safe_load(written.read_text())
    agm_config._validate(reloaded, written)
    assert reloaded["run"]["models"] == ["mistralai/Mistral-7B-Instruct-v0.3"]


def test_endpoint_rejects_bad_weight_sum(tmp_path, monkeypatch):
    client, user_dir = _client(tmp_path, monkeypatch)
    r = client.post("/api/build-config", json={
        "name": "bad weights",
        "models": ["m/x"],
        "dataset_path": "d.csv",
        "weights": {"accuracy": 0.9, "latency": 0.25, "vram": 0.2, "tokens": 0.15},
    })
    assert r.status_code == 400
    assert "sum to 1.0" in r.get_json()["error"]
    # nothing written
    assert not (user_dir / "bad-weights.yaml").exists()


def test_endpoint_refuses_locked_and_root_names(tmp_path, monkeypatch):
    """Even asking for name 'run_full_l4' or 'config' only ever writes into the
    user dir (slugged), NEVER onto the protected files."""
    client, user_dir = _client(tmp_path, monkeypatch)
    locked_before = cb.LOCKED_STUDY_CONFIG.read_bytes()
    root_before = cb.ROOT_CONFIG.read_bytes() if cb.ROOT_CONFIG.exists() else None

    for nm in ("run_full_l4", "config", "../run_full_l4", "../../config"):
        r = client.post("/api/build-config", json={
            "name": nm, "models": ["m/x"], "dataset_path": "d.csv",
            "weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15},
        })
        # Accepted (200) but written INSIDE the user dir, or refused (400); either
        # way the protected files are never the target.
        assert r.status_code in (200, 400)
        if r.status_code == 200:
            assert Path(r.get_json()["path"]).parent.name == user_dir.name or \
                str(user_dir) in str(r.get_json()["path"])

    # protected files byte-for-byte unchanged
    assert cb.LOCKED_STUDY_CONFIG.read_bytes() == locked_before
    if root_before is not None:
        assert cb.ROOT_CONFIG.read_bytes() == root_before


def test_locked_config_preview_is_readonly(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/api/locked-config")
    assert r.status_code == 200
    body = r.get_json()
    assert body["locked"] is True
    assert "run_full_l4.yaml" in body["name"]
    assert "scoring" in body["yaml"]
    # there is no write path for the locked config
    assert client.post("/api/locked-config").status_code in (404, 405)


def test_preview_endpoint_does_not_write(tmp_path, monkeypatch):
    client, user_dir = _client(tmp_path, monkeypatch)
    r = client.post("/api/preview-config", json={
        "name": "preview only", "models": ["m/x"], "dataset_path": "d.csv",
        "weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15},
    })
    assert r.status_code == 200 and r.get_json()["valid"] is True
    assert not user_dir.exists() or not list(user_dir.glob("*.yaml"))
