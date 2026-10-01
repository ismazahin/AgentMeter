"""Phase 31 — optional HTTP Basic Auth gate for internet exposure (CPU-only).

OFF by default (local use unchanged). When AGENTMETER_AUTH_PASS is set, every route
needs the right Basic credentials, except CORS preflight (OPTIONS) and /health.
"""
from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]


def _client(tmp_path):
    pytest.importorskip("flask")
    from agentmeter import pull_eval
    path = REPO / "scripts" / "pull_eval_server.py"
    spec = importlib.util.spec_from_file_location("pull_eval_server", path)
    srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
    base = tmp_path / "cfg.yaml"
    base.write_text(yaml.safe_dump({"run": {"models": ["mock/m"]}, "model": {"provider": "mock"},
                                    "dataset": {}, "pipeline": {}, "classes": [], "scoring": {},
                                    "storage": {}}))
    mgr = pull_eval.JobManager(base_config=str(base), canonical_json=str(tmp_path / "c.json"),
                               out_dir=str(tmp_path / "pulls"))
    return srv.create_app(mgr, local_results_dir=str(tmp_path / "results")).test_client()


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_auth_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTMETER_AUTH_PASS", raising=False)
    client = _client(tmp_path)
    assert client.get("/api/vast-status").status_code == 200   # open, no creds needed


def test_auth_on_requires_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTMETER_AUTH_USER", "me")
    monkeypatch.setenv("AGENTMETER_AUTH_PASS", "s3cret")
    client = _client(tmp_path)

    # no creds -> 401 with a Basic challenge (browser will prompt)
    r = client.get("/api/vast-status")
    assert r.status_code == 401 and "Basic" in r.headers.get("WWW-Authenticate", "")

    # wrong creds -> 401
    assert client.get("/api/vast-status", headers=_basic("me", "nope")).status_code == 401
    assert client.get("/api/vast-status", headers=_basic("x", "s3cret")).status_code == 401

    # correct creds -> through
    assert client.get("/api/vast-status", headers=_basic("me", "s3cret")).status_code == 200

    # the dashboard itself is gated too
    assert client.get("/").status_code == 401
    assert client.get("/", headers=_basic("me", "s3cret")).status_code in (200, 304)


def test_health_and_preflight_stay_open(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTMETER_AUTH_PASS", "s3cret")
    client = _client(tmp_path)
    assert client.get("/health").status_code == 200              # uptime checks allowed
    # CORS preflight (OPTIONS) must not be blocked by auth
    assert client.open("/api/build-config", method="OPTIONS").status_code in (200, 204)


def test_protects_cost_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTMETER_AUTH_PASS", "s3cret")
    client = _client(tmp_path)
    # /pull-eval spends GPU money — must be behind auth
    assert client.post("/pull-eval", json={}).status_code == 401
