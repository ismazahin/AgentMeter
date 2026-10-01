"""Phase 30 — Telegram notifications for new analyses (CPU-only).

Covers the config/readiness, the sender (no-op when unset, best-effort, token never
returned), the new-results watcher (seed + dedup + no-flood + notifies only when
enabled), and the endpoints.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from agentmeter.server import notify

REPO = Path(__file__).resolve().parents[1]
TG = {"TELEGRAM_BOT_TOKEN": "123:ABCSECRET", "TELEGRAM_CHAT_ID": "999"}


def _write_analysis(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"phase8": {}}')


# --- config / status -----------------------------------------------------

def test_config_enabled_and_missing():
    assert notify.telegram_config(TG)["enabled"] is True
    c = notify.telegram_config({"TELEGRAM_BOT_TOKEN": "x"})
    assert c["enabled"] is False and c["missing"] == ["TELEGRAM_CHAT_ID"]


def test_status_presence_only_no_token_value():
    st = notify.notify_status(TG)
    assert st["enabled"] is True
    assert "123:ABCSECRET" not in str(st)          # never leak the token value


# --- sender --------------------------------------------------------------

def test_send_noop_when_unconfigured():
    res = notify.send_telegram("hi", env={})
    assert res["ok"] is False and res["disabled"] is True


def test_send_uses_poster_with_token_and_payload():
    seen = {}
    def poster(url, payload):
        seen["url"] = url; seen["payload"] = payload
        return {"ok": True, "status": 200}
    res = notify.send_telegram("hello phone", env=TG, poster=poster)
    assert res["ok"] is True
    assert "/bot123:ABCSECRET/sendMessage" in seen["url"]
    assert seen["payload"]["chat_id"] == "999" and seen["payload"]["text"] == "hello phone"


def test_send_never_raises_on_poster_error():
    def boom(url, payload):
        raise RuntimeError("network down")
    res = notify.send_telegram("x", env=TG, poster=boom)
    assert res["ok"] is False and "network down" in res["error"]


# --- new-results watcher -------------------------------------------------

def test_watcher_seeds_and_detects_new(tmp_path):
    _write_analysis(tmp_path, "user_runs/old_analysis/analysis.json")
    posts = []
    w = notify.NewResultsWatcher(tmp_path, env_reader=lambda: TG,
                                 poster=lambda u, p: posts.append(p) or {"ok": True})
    w.seed()                                        # 'old' is now already seen
    assert w.poll_once() == []                      # nothing new yet
    _write_analysis(tmp_path, "user_runs/new_analysis/analysis.json")
    new = w.poll_once()
    assert new == ["user_runs/new_analysis/analysis.json"]
    assert len(posts) == 1                          # one push for the new one
    assert w.poll_once() == []                      # dedup: not notified again


def test_watcher_no_flood_when_enabled_later(tmp_path):
    _write_analysis(tmp_path, "a_analysis/analysis.json")
    env = {}                                        # Telegram OFF
    posts = []
    w = notify.NewResultsWatcher(tmp_path, env_reader=lambda: dict(env),
                                 poster=lambda u, p: posts.append(p) or {"ok": True})
    w.seed()
    _write_analysis(tmp_path, "b_analysis/analysis.json")
    assert w.poll_once() == ["b_analysis/analysis.json"]   # tracked as new...
    assert posts == []                              # ...but NOT sent (disabled)
    env.update(TG)                                  # enable now
    _write_analysis(tmp_path, "c_analysis/analysis.json")
    assert w.poll_once() == ["c_analysis/analysis.json"]   # only the brand-new one
    assert len(posts) == 1                          # no flood of the earlier ones


# --- endpoints -----------------------------------------------------------

def _client(tmp_path, monkeypatch, env=None, results_dir=None):
    pytest.importorskip("flask")
    from agentmeter import pull_eval
    from agentmeter.util import envtools
    path = REPO / "scripts" / "pull_eval_server.py"
    spec = importlib.util.spec_from_file_location("pull_eval_server", path)
    srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
    monkeypatch.setattr(envtools, "read_dotenv_live", lambda *a, **k: dict(env or {}))
    base = tmp_path / "cfg.yaml"
    base.write_text(yaml.safe_dump({"run": {"models": ["mock/m"]}, "model": {"provider": "mock"},
                                    "dataset": {}, "pipeline": {}, "classes": [], "scoring": {},
                                    "storage": {}}))
    mgr = pull_eval.JobManager(base_config=str(base), canonical_json=str(tmp_path / "c.json"),
                               out_dir=str(tmp_path / "pulls"))
    app = srv.create_app(mgr, local_results_dir=str(results_dir or (tmp_path / "results")))
    return app.test_client()


def test_notify_status_endpoint(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, env=TG)
    d = client.get("/api/notify-status").get_json()
    assert d["enabled"] is True
    assert "123:ABCSECRET" not in str(d)


def test_notify_test_endpoint(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, env=TG)
    monkeypatch.setattr(notify, "send_telegram", lambda *a, **k: {"ok": True, "status": 200})
    assert client.post("/api/notify-test").get_json()["ok"] is True


def test_notify_check_endpoint_detects_new(tmp_path, monkeypatch):
    results = tmp_path / "results"; results.mkdir()
    client = _client(tmp_path, monkeypatch, env=TG, results_dir=results)   # watcher seeds empty
    monkeypatch.setattr(notify, "send_telegram", lambda *a, **k: {"ok": True})
    _write_analysis(results, "run_analysis/analysis.json")
    d = client.post("/api/notify-check").get_json()
    assert d["new"] == ["run_analysis/analysis.json"]
