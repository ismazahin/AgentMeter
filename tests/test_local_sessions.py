"""Phase 16 — local result-file discovery tests (CPU, read-only).

The endpoint lists only results/ *.json files, rejects path traversal / absolute
paths / symlink escapes, returns valid JSON, and never opens the locked study DB.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from agentmeter import local_sessions

REPO_ROOT = Path(__file__).resolve().parents[1]


def _results(tmp_path):
    r = tmp_path / "results"
    (r / "analysis").mkdir(parents=True)
    (r / "pulls").mkdir()
    (r / "analysis" / "analysis.json").write_text(json.dumps({"phase8": {"normalised": []}}))
    (r / "pulls" / "analysis_x.json").write_text(json.dumps({"exploratory": True}))
    # non-JSON files that must NOT be listed (incl. a DB and a text file)
    (r / "agentmeter_full_l4.db").write_bytes(b"SQLITEfake")
    (r / "notes.txt").write_text("hello")
    (r / "bad.json").write_text("{ not valid json ")
    # a secret OUTSIDE results/ that traversal must never reach
    (tmp_path / "secret.json").write_text(json.dumps({"secret": 1}))
    return r


# --- module-level (pure) -----------------------------------------------

def test_list_only_results_json(tmp_path):
    r = _results(tmp_path)
    ls = local_sessions.LocalSessions(r)
    names = {f["name"] for f in ls.list()}
    assert names == {"analysis/analysis.json", "pulls/analysis_x.json", "bad.json"}
    assert "agentmeter_full_l4.db" not in names and "notes.txt" not in names
    for f in ls.list():
        assert set(f.keys()) == {"name", "size", "modified"}  # metadata only


def test_read_valid_json(tmp_path):
    r = _results(tmp_path)
    ls = local_sessions.LocalSessions(r)
    assert ls.read_json("analysis/analysis.json")["phase8"] == {"normalised": []}


def test_rejects_traversal_and_absolute(tmp_path):
    r = _results(tmp_path)
    ls = local_sessions.LocalSessions(r)
    for bad in ("../secret.json", "../secret.json", "analysis/../../secret.json",
                "/etc/passwd", "\\etc\\hosts"):
        with pytest.raises(ValueError):
            ls.resolve_safe(bad)
    # an absolute path to the outside secret is rejected too
    with pytest.raises(ValueError):
        ls.resolve_safe(str(tmp_path / "secret.json"))


def test_rejects_non_json_and_missing(tmp_path):
    r = _results(tmp_path)
    ls = local_sessions.LocalSessions(r)
    with pytest.raises(ValueError):
        ls.resolve_safe("agentmeter_full_l4.db")      # not .json
    with pytest.raises(FileNotFoundError):
        ls.resolve_safe("nope.json")


def test_symlink_escape_rejected(tmp_path):
    r = _results(tmp_path)
    ls = local_sessions.LocalSessions(r)
    link = r / "escape.json"
    try:
        link.symlink_to(tmp_path / "secret.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported here")
    # a symlink pointing outside results/ must not be readable
    with pytest.raises(ValueError):
        ls.resolve_safe("escape.json")
    # and it must not appear in the listing (its real path is outside root)
    assert "escape.json" not in {f["name"] for f in ls.list()}


def test_missing_results_dir_is_empty(tmp_path):
    ls = local_sessions.LocalSessions(tmp_path / "no-such-dir")
    assert ls.list() == []


# --- HTTP layer --------------------------------------------------------

def _load_server():
    path = REPO_ROOT / "scripts" / "pull_eval_server.py"
    spec = importlib.util.spec_from_file_location("pull_eval_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _client(tmp_path, results_dir):
    pytest.importorskip("flask")
    srv = _load_server()
    from agentmeter import pull_eval
    # a minimal mock config so JobManager builds without a GPU
    csv = tmp_path / "f.csv"; csv.write_text("A,Label\n1,Benign\n")
    cfg = {
        "run": {"mode": "pilot", "device": "cpu", "require_gpu": False, "seed": 1, "models": ["mock/m"]},
        "model": {"provider": "mock", "name": "mock/m", "mock": {"latency_s": 0.0}},
        "dataset": {"path": str(csv), "label_column": "Label", "id_column": None,
                    "drop_columns": [], "limit": 1, "max_feature_chars": 100, "label_map": {}, "drop_labels": []},
        "pipeline": {"agents": ["perceive", "reason", "decide", "act"],
                     "max_new_tokens": {"perceive": 8, "reason": 8, "decide": 4, "act": 8}},
        "classes": ["Benign"], "mitre": {"Benign": "N/A"},
        "scoring": {"weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15}},
        "storage": {"sqlite_path": str(tmp_path / "LOCKED.db")},
    }
    base = tmp_path / "cfg.yaml"; base.write_text(yaml.safe_dump(cfg))
    mgr = pull_eval.JobManager(base_config=str(base), canonical_json=str(tmp_path / "c.json"),
                               out_dir=str(tmp_path / "pulls"))
    return srv.create_app(mgr, local_results_dir=str(results_dir)).test_client()


def test_http_list_and_read(tmp_path):
    r = _results(tmp_path)
    client = _client(tmp_path, r)

    files = client.get("/api/local-sessions").get_json()["files"]
    names = {f["name"] for f in files}
    assert "analysis/analysis.json" in names
    assert not any(n.endswith(".db") or n.endswith(".txt") for n in names)

    ok = client.get("/api/local-sessions/analysis/analysis.json")
    assert ok.status_code == 200 and ok.get_json()["phase8"] == {"normalised": []}


def test_http_rejects_traversal_and_bad(tmp_path):
    r = _results(tmp_path)
    client = _client(tmp_path, r)
    assert client.get("/api/local-sessions/nope.json").status_code == 404
    assert client.get("/api/local-sessions/bad.json").status_code == 400        # invalid JSON
    assert client.get("/api/local-sessions/agentmeter_full_l4.db").status_code == 400  # not .json
    # traversal attempts (encoded / literal) never reach the outside secret
    for bad in ("..%2f..%2fsecret.json", "analysis/..%2f..%2fsecret.json"):
        assert client.get("/api/local-sessions/" + bad).status_code in (400, 404)


def test_local_sessions_never_opens_locked_db(tmp_path, monkeypatch):
    r = _results(tmp_path)
    # a fake locked DB inside results must never be opened by discovery
    real_connect = sqlite3.connect

    def guard(target, *a, **k):
        if "agentmeter_full_l4.db" in str(target):
            raise AssertionError(f"discovery opened the locked DB: {target}")
        return real_connect(target, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", guard)
    client = _client(tmp_path, r)
    client.get("/api/local-sessions")
    client.get("/api/local-sessions/analysis/analysis.json")
    # no assertion needed — the guard raises if the locked DB is ever opened
