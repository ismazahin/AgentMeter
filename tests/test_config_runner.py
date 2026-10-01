"""Phase 28 — downloadable .bat/.sh runner for a USER config (CPU-only).

The web app only GENERATES a script file for download; it runs nothing. These tests
cover the generator and the endpoint:
  * a valid .bat (Windows) and .sh (Unix) wrapping exactly the existing command
    `python main.py --config configs/user/<slug>.yaml run-full`;
  * refusal of the locked study config, the root config, traversal, a bad os, and a
    non-existent config;
  * the endpoint returns the file as an attachment download.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from agentmeter.server import config_builder as cb

REPO = Path(__file__).resolve().parents[1]


def _make_user_cfg(base_dir: Path, slug: str) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / f"{slug}.yaml").write_text("run: {}\n")


# --- generator (unit) ----------------------------------------------------

def test_runner_bat_wraps_exact_command(tmp_path):
    _make_user_cfg(tmp_path, "my-run")
    fname, content = cb.runner_script("my run", "win", base_dir=tmp_path)
    assert fname == "run_my-run.bat"
    assert "python main.py --config configs/user/my-run.yaml run-full" in content
    assert content.startswith("@echo off")
    assert "pause" in content                          # window stays open
    assert 'if not exist "main.py" goto nomain' in content   # clear "wrong folder" guard
    assert "\r\n" in content                           # CRLF for Windows
    assert 'cd /d "%~dp0"' in content                  # runs from the repo root
    # references ONLY the user config, never the locked/root config
    assert "run_full_l4" not in content and "config.yaml" not in content


def test_runner_sh_wraps_exact_command(tmp_path):
    _make_user_cfg(tmp_path, "my-run")
    fname, content = cb.runner_script("my-run", "unix", base_dir=tmp_path)
    assert fname == "run_my-run.sh"
    assert content.startswith("#!/usr/bin/env bash")
    assert "python main.py --config configs/user/my-run.yaml run-full" in content
    assert "read -r -p" in content                     # prompt keeps the terminal open
    assert '[ ! -f "main.py" ]' in content             # clear "wrong folder" guard
    assert "\r\n" not in content                       # LF only


def test_runner_tolerates_path_or_extension_in_name(tmp_path):
    # the dashboard may pass a full saved path (Unix "/" or Windows "\") or a .yaml
    # name; all must resolve to the same user config.
    _make_user_cfg(tmp_path, "my-experiment")
    for nm in ("my-experiment", "my-experiment.yaml",
               "configs/user/my-experiment.yaml", "configs\\user\\my-experiment"):
        fname, content = cb.runner_script(nm, "win", base_dir=tmp_path)
        assert fname == "run_my-experiment.bat"
        assert "configs/user/my-experiment.yaml run-full" in content


def test_runner_refuses_nonexistent_config(tmp_path):
    with pytest.raises(cb.ConfigBuildError, match="not found"):
        cb.runner_script("does-not-exist", "win", base_dir=tmp_path)


def test_runner_refuses_bad_os(tmp_path):
    _make_user_cfg(tmp_path, "my-run")
    with pytest.raises(cb.ConfigBuildError, match="win.*unix|os must"):
        cb.runner_script("my-run", "macos", base_dir=tmp_path)


def test_runner_traversal_is_sanitized(tmp_path):
    # slugify strips path separators, so "../../evil" -> "evil"; with no such file
    # in the user dir it is simply not found (never escapes the directory).
    with pytest.raises(cb.ConfigBuildError):
        cb.runner_script("../../evil", "win", base_dir=tmp_path)


def test_runner_refuses_locked_and_root_config():
    # base_dir at the real configs/ so name 'run_full_l4' would resolve onto the
    # LOCKED study config -> must be refused as protected, not turned into a runner.
    with pytest.raises(cb.ConfigBuildError, match="protected"):
        cb.runner_script("run_full_l4", "win", base_dir=cb.PROJECT_ROOT / "configs")
    # 'config' at the repo root resolves onto the root config -> protected too.
    with pytest.raises(cb.ConfigBuildError, match="protected"):
        cb.runner_script("config", "win", base_dir=cb.PROJECT_ROOT)


# --- endpoint ------------------------------------------------------------

def _client(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    path = REPO / "scripts" / "pull_eval_server.py"
    spec = importlib.util.spec_from_file_location("pull_eval_server", path)
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    from agentmeter import pull_eval

    user_dir = tmp_path / "userconfigs"
    monkeypatch.setattr(cb, "USER_CONFIG_DIR", user_dir)

    csv = tmp_path / "f.csv"; csv.write_text("A,Label\n1,Benign\n")
    base = tmp_path / "cfg.yaml"
    base.write_text(yaml.safe_dump({
        "run": {"mode": "pilot", "device": "cpu", "require_gpu": False, "seed": 1, "models": ["mock/m"]},
        "model": {"provider": "mock", "name": "mock/m", "mock": {"latency_s": 0.0}},
        "dataset": {"path": str(csv), "label_column": "Label", "id_column": None, "drop_columns": [],
                    "limit": 1, "max_feature_chars": 100, "label_map": {}, "drop_labels": []},
        "pipeline": {"agents": ["perceive", "reason", "decide", "act"],
                     "max_new_tokens": {"perceive": 8, "reason": 8, "decide": 4, "act": 8}},
        "classes": ["Benign"], "mitre": {"Benign": "N/A"},
        "scoring": {"weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15}},
        "storage": {"sqlite_path": str(tmp_path / "app.db")}}))
    mgr = pull_eval.JobManager(base_config=str(base), canonical_json=str(tmp_path / "c.json"),
                               out_dir=str(tmp_path / "pulls"))
    return srv.create_app(mgr).test_client(), user_dir


def _save_config(client, name="endpoint run"):
    r = client.post("/api/build-config", json={
        "name": name, "models": ["m/x"], "dataset_path": "d.csv",
        "weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15}})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["name"].replace(".yaml", "")


def test_endpoint_returns_bat_attachment(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    stem = _save_config(client)                        # creates configs/user/<stem>.yaml
    r = client.get(f"/api/config-runner?name={stem}&os=win")
    assert r.status_code == 200
    assert r.headers["Content-Disposition"] == f'attachment; filename="run_{stem}.bat"'
    body = r.get_data(as_text=True)
    assert f"python main.py --config configs/user/{stem}.yaml run-full" in body
    assert "pause" in body


def test_endpoint_returns_sh_attachment(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    stem = _save_config(client)
    r = client.get(f"/api/config-runner?name={stem}&os=unix")
    assert r.status_code == 200
    assert r.headers["Content-Disposition"] == f'attachment; filename="run_{stem}.sh"'
    assert "#!/usr/bin/env bash" in r.get_data(as_text=True)


def test_endpoint_refuses_nonexistent(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/api/config-runner?name=never-built&os=win")
    assert r.status_code == 400
    assert "not found" in r.get_json()["error"]


def test_endpoint_refuses_locked_name(tmp_path, monkeypatch):
    # 'run_full_l4' does not exist in the temp user dir -> refused (never the locked file)
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/api/config-runner?name=run_full_l4&os=win")
    assert r.status_code == 400


def test_endpoint_refuses_bad_os(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    stem = _save_config(client)
    r = client.get(f"/api/config-runner?name={stem}&os=macos")
    assert r.status_code == 400
