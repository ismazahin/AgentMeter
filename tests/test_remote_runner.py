"""Phase 29 — remote runner (Vast/SSH GPU box) + live .env (CPU-only).

The web app GENERATES a download-only script that SSHes into a box the user already
rented and runs the EXISTING benchmark there, then copies results back. It reads the
SSH target from .env live. Tests cover: .env resolution + readiness, the generated
script (ssh/scp of the exact command), that NO secret is embedded, refusal when the
config or the SSH details are missing, and the endpoints.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from agentmeter.server import config_builder as cb
from agentmeter.server import remote_runner as rr

REPO = Path(__file__).resolve().parents[1]

FULL_ENV = {
    "VAST_SSH_HOST": "ssh5.vast.ai",
    "VAST_SSH_PORT": "12345",
    "VAST_SSH_USER": "root",
    "VAST_SSH_KEY": "C:\\Users\\me\\.ssh\\id_ed25519",
    "VAST_REMOTE_DIR": "/root/AgentMeter",
    "VAST_API_KEY": "SECRET-API-KEY-SHOULD-NOT-LEAK",
}


def _make_user_cfg(base_dir: Path, slug: str) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / f"{slug}.yaml").write_text(
        yaml.safe_dump({"storage": {"sqlite_path": f"results/user_runs/{slug}.db"}}))


# --- .env resolution -----------------------------------------------------

def test_ssh_config_ready_and_defaults():
    c = rr.ssh_config({"VAST_SSH_HOST": "h", "VAST_SSH_PORT": "22"})
    assert c["ready"] is True and c["missing"] == []
    assert c["user"] == "root" and c["remote_dir"] == "~/AgentMeter"  # defaults


def test_ssh_config_missing_required():
    c = rr.ssh_config({"VAST_SSH_USER": "root"})
    assert c["ready"] is False
    assert set(c["missing"]) == {"VAST_SSH_HOST", "VAST_SSH_PORT"}


def test_ssh_status_reports_presence_only():
    st = rr.ssh_status({"VAST_SSH_HOST": "h", "VAST_SSH_PORT": "22"})
    assert st["ready"] is True
    assert st["present"]["VAST_SSH_HOST"] is True and st["present"]["VAST_SSH_KEY"] is False
    # presence only — the value "h" must never be in the status payload
    assert "h" not in str(st["present"].values())


# --- generated script ----------------------------------------------------

def test_remote_bat_has_ssh_run_and_scp(tmp_path):
    _make_user_cfg(tmp_path, "my-run")
    fname, content = rr.remote_runner_script("my-run", "win", env=FULL_ENV, base_dir=tmp_path)
    assert fname == "run_remote_my-run.bat"
    assert "ssh " in content and "-p 12345" in content
    assert "root@ssh5.vast.ai" in content
    assert "cd /root/AgentMeter && python main.py --config configs/user/my-run.yaml run-full" in content
    assert "scp " in content and "-P 12345" in content                   # scp uses -P
    assert "results/user_runs/my-run_analysis" in content                # copies the analysis back


def test_remote_sh_generated(tmp_path):
    _make_user_cfg(tmp_path, "my-run")
    fname, content = rr.remote_runner_script("my-run", "unix", env=FULL_ENV, base_dir=tmp_path)
    assert fname == "run_remote_my-run.sh"
    assert content.startswith("#!/usr/bin/env bash")
    assert "python main.py --config configs/user/my-run.yaml run-full" in content


def test_remote_runner_never_embeds_secrets(tmp_path):
    _make_user_cfg(tmp_path, "my-run")
    _, content = rr.remote_runner_script("my-run", "win", env=FULL_ENV, base_dir=tmp_path)
    # the key PATH is referenced, but never the API key value
    assert 'id_ed25519' in content                      # key path is used (-i)
    assert "SECRET-API-KEY-SHOULD-NOT-LEAK" not in content
    assert "VAST_API_KEY" not in content


def test_remote_runner_refuses_without_ssh(tmp_path):
    _make_user_cfg(tmp_path, "my-run")
    with pytest.raises(cb.ConfigBuildError, match="SSH details missing"):
        rr.remote_runner_script("my-run", "win", env={}, base_dir=tmp_path)


def test_remote_runner_refuses_missing_config(tmp_path):
    with pytest.raises(cb.ConfigBuildError, match="not found"):
        rr.remote_runner_script("never", "win", env=FULL_ENV, base_dir=tmp_path)


# --- endpoints -----------------------------------------------------------

def _client(tmp_path, monkeypatch, env=None):
    pytest.importorskip("flask")
    from agentmeter import pull_eval
    from agentmeter.util import envtools
    path = REPO / "scripts" / "pull_eval_server.py"
    spec = importlib.util.spec_from_file_location("pull_eval_server", path)
    srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)

    user_dir = tmp_path / "userconfigs"
    monkeypatch.setattr(cb, "USER_CONFIG_DIR", user_dir)
    monkeypatch.setattr(envtools, "read_dotenv_live", lambda *a, **k: dict(env or {}))

    base = tmp_path / "cfg.yaml"
    base.write_text(yaml.safe_dump({
        "run": {"mode": "pilot", "device": "cpu", "require_gpu": False, "seed": 1, "models": ["mock/m"]},
        "model": {"provider": "mock", "name": "mock/m", "mock": {"latency_s": 0.0}},
        "dataset": {"path": str(tmp_path / "f.csv"), "label_column": "Label", "id_column": None,
                    "drop_columns": [], "limit": 1, "max_feature_chars": 100, "label_map": {}, "drop_labels": []},
        "pipeline": {"agents": ["perceive", "reason", "decide", "act"],
                     "max_new_tokens": {"perceive": 8, "reason": 8, "decide": 4, "act": 8}},
        "classes": ["Benign"], "mitre": {"Benign": "N/A"},
        "scoring": {"weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15}},
        "storage": {"sqlite_path": str(tmp_path / "a.db")}}))
    (tmp_path / "f.csv").write_text("A,Label\n1,Benign\n")
    mgr = pull_eval.JobManager(base_config=str(base), canonical_json=str(tmp_path / "c.json"),
                               out_dir=str(tmp_path / "pulls"))
    return srv.create_app(mgr).test_client()


def _save(client, name="remote run"):
    r = client.post("/api/build-config", json={
        "name": name, "models": ["m/x"], "dataset_path": "d.csv",
        "weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15}})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["name"].replace(".yaml", "")


def test_vast_status_endpoint(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, env=FULL_ENV)
    d = client.get("/api/vast-status").get_json()
    assert d["ready"] is True and d["missing"] == []
    # no secret value in the readiness payload
    assert "SECRET-API-KEY-SHOULD-NOT-LEAK" not in str(d)


def test_remote_endpoint_returns_attachment(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, env=FULL_ENV)
    stem = _save(client)
    r = client.get(f"/api/config-runner?name={stem}&os=win&target=remote")
    assert r.status_code == 200
    assert r.headers["Content-Disposition"] == f'attachment; filename="run_remote_{stem}.bat"'
    body = r.get_data(as_text=True)
    assert "root@ssh5.vast.ai" in body and "run-full" in body
    assert "SECRET-API-KEY-SHOULD-NOT-LEAK" not in body


def test_remote_endpoint_refuses_without_env(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, env={})     # no SSH details
    stem = _save(client)
    r = client.get(f"/api/config-runner?name={stem}&os=win&target=remote")
    assert r.status_code == 400
    assert "SSH details missing" in r.get_json()["error"]
