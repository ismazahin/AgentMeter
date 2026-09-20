"""Phase 11 — Vast.ai self-destroy verify suite (CPU, no GPU / no Vast / no network).

Covers the cost-safety guarantees with the HTTP client and the destroy call
MONKEYPATCHED — no real network call, no real instance is ever destroyed:

  (a) missing creds -> destroy_instance() warns, makes NO HTTP call, no raise;
  (b) with creds     -> exactly ONE destroy request to the right endpoint + auth;
  (c) run lock       -> the watchdog never destroys while a job is "running";
  (d) opt-in         -> with auto-destroy OFF, a completed job never destroys;
  (+) ordering       -> destroy fires only AFTER the merged analysis is on disk.
"""
from __future__ import annotations

import importlib.util
import os
import threading
import time
from pathlib import Path

import pytest

from agentmeter import pull_eval, vast_shutdown

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_server_module():
    path = REPO_ROOT / "scripts" / "pull_eval_server.py"
    spec = importlib.util.spec_from_file_location("pull_eval_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _clear_vast_env(monkeypatch):
    for k in ("VAST_API_KEY", "VAST_INSTANCE_ID", "VAST_CONTAINER_ID",
              "CONTAINER_ID", "VAST_CONTAINERLABEL"):
        monkeypatch.delenv(k, raising=False)


# --- (a) missing credentials: no HTTP call, no raise --------------------

def test_destroy_noop_without_credentials(monkeypatch):
    _clear_vast_env(monkeypatch)
    calls = []

    def spy(*a, **k):
        calls.append((a, k))
        return type("R", (), {"status_code": 200})()

    # no api key, no instance id anywhere
    res = vast_shutdown.destroy_instance(instance_id=None, api_key=None,
                                         http_delete=spy, sleep=lambda s: None)
    assert res is False
    assert calls == []   # NO HTTP call attempted


def test_destroy_noop_with_key_but_no_instance(monkeypatch):
    _clear_vast_env(monkeypatch)
    calls = []
    res = vast_shutdown.destroy_instance(
        instance_id=None, api_key="secret", sleep=lambda s: None,
        http_delete=lambda *a, **k: calls.append(1))
    assert res is False and calls == []


# --- (b) with creds: exactly one destroy request to the right endpoint --

def test_destroy_issues_single_delete_to_correct_endpoint(monkeypatch):
    _clear_vast_env(monkeypatch)
    calls = []

    def spy(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return type("R", (), {"status_code": 200})()

    ok = vast_shutdown.destroy_instance(
        instance_id="1234567", api_key="sk-abc",
        http_delete=spy, sleep=lambda s: None)

    assert ok is True
    assert len(calls) == 1                                   # exactly one request
    assert calls[0]["url"].endswith("/instances/1234567/")   # right endpoint
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-abc"


def test_destroy_retries_then_succeeds(monkeypatch):
    _clear_vast_env(monkeypatch)
    attempts = {"n": 0}
    slept = []

    def flaky(url, headers=None, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient")
        return type("R", (), {"status_code": 200})()

    ok = vast_shutdown.destroy_instance(
        instance_id="9", api_key="k", http_delete=flaky,
        retries=3, sleep=lambda s: slept.append(s))
    assert ok is True and attempts["n"] == 3
    assert slept == [2.0, 4.0]   # exponential backoff between attempts


def test_instance_id_from_container_label(monkeypatch):
    _clear_vast_env(monkeypatch)
    monkeypatch.setenv("VAST_CONTAINERLABEL", "C.7654321")
    assert vast_shutdown.get_instance_id() == "7654321"   # digits extracted


# --- (c) watchdog respects the run lock ---------------------------------

class _FakeSnap:
    def __init__(self, active):
        self._active = active

    def is_active(self):
        return self._active


class _FakeManager:
    def __init__(self, active=True):
        self.active = active

    def snapshot(self):
        return _FakeSnap(self.active)


def test_watchdog_never_destroys_while_job_running():
    srv = _load_server_module()
    calls = []
    mgr = _FakeManager(active=True)
    guard = srv.CostGuard(manager=mgr, auto_destroy=True, idle_timeout_min=1,
                          instance_id="1",
                          destroy=lambda instance_id=None: calls.append(instance_id))
    guard.idle_timeout = 0.05           # 50ms idle window for the test
    guard.last_activity = time.time() - 100.0  # long "idle", but a job is running

    stop = threading.Event()
    t = threading.Thread(target=guard.watchdog, args=(stop,), kwargs={"poll": 0.01},
                         daemon=True)
    t.start()
    time.sleep(0.25)
    assert calls == []                  # running job -> NEVER destroyed

    mgr.active = False                  # job finished; idle clock can now elapse
    time.sleep(0.25)
    stop.set(); t.join(timeout=1)
    assert calls == ["1"]               # destroyed exactly once, after it went idle


# --- (d) opt-in: auto-destroy OFF never destroys ------------------------

def test_auto_destroy_off_completed_job_does_not_destroy():
    srv = _load_server_module()
    calls = []
    guard = srv.CostGuard(manager=_FakeManager(active=False), auto_destroy=False,
                          idle_timeout_min=30, instance_id="1",
                          destroy=lambda instance_id=None: calls.append(instance_id))
    # a completed job fires on_complete -> must NOT destroy when opt-in is off
    guard.on_complete(state=None)
    assert calls == []
    # and the watchdog is a no-op when auto-destroy is off
    stop = threading.Event()
    guard.watchdog(stop, poll=0.01)     # returns immediately
    assert calls == []


def test_auto_destroy_on_completed_job_destroys_once():
    srv = _load_server_module()
    calls = []
    guard = srv.CostGuard(manager=_FakeManager(active=False), auto_destroy=True,
                          idle_timeout_min=30, instance_id="42",
                          destroy=lambda instance_id=None: calls.append(instance_id))
    guard.on_complete(state=None)
    guard.on_complete(state=None)       # idempotent — fires at most once
    assert calls == ["42"]


# --- (+) ordering: destroy only AFTER results are flushed to disk -------

def test_destroy_fires_only_after_analysis_flushed(tmp_path, monkeypatch):
    """End-to-end through JobManager: destroy must see the merged analysis JSON
    already on disk (results persisted BEFORE the very last destroy step)."""
    srv = _load_server_module()

    # Stub the heavy bits: run_full does nothing; merged_analysis returns a dict.
    monkeypatch.setattr(pull_eval, "merged_analysis",
                        lambda *a, **k: {"exploratory": {"validated": False}})

    # A minimal valid base config so build_pull_config succeeds.
    import yaml
    csv = tmp_path / "f.csv"
    csv.write_text("A,Label\n1,Benign\n")
    base_cfg = {
        "run": {"mode": "pilot", "device": "cpu", "require_gpu": False,
                "seed": 1, "models": ["mock/base"]},
        "model": {"provider": "mock", "name": "mock/base", "mock": {"latency_s": 0.0}},
        "dataset": {"path": str(csv), "label_column": "Label", "id_column": None,
                    "drop_columns": [], "limit": 1, "max_feature_chars": 100,
                    "label_map": {}, "drop_labels": []},
        "pipeline": {"agents": ["perceive", "reason", "decide", "act"],
                     "max_new_tokens": {"perceive": 8, "reason": 8, "decide": 4, "act": 8}},
        "classes": ["Benign"], "mitre": {"Benign": "N/A"},
        "scoring": {"weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15},
                    "targets": {"accuracy_pct": 80.0, "latency_s": 5.0,
                                "vram_mb": 16000.0, "tokens_total": 1200.0},
                    "tiers": {"healthy_min": 80.0, "degraded_min": 60.0}},
        "storage": {"sqlite_path": str(tmp_path / "LOCKED.db")},
        "output": {"results_dir": str(tmp_path)},
    }
    base = tmp_path / "base.yaml"
    base.write_text(yaml.safe_dump(base_cfg))

    holder = {}
    seen = {}

    def spy(instance_id=None):
        snap = holder["mgr"].snapshot()
        seen["path"] = snap.analysis_path
        seen["exists_at_destroy"] = bool(snap.analysis_path and os.path.exists(snap.analysis_path))
        seen["calls"] = seen.get("calls", 0) + 1
        return True

    guard = srv.CostGuard(manager=None, auto_destroy=True, idle_timeout_min=0,
                          instance_id="7", destroy=spy)
    mgr = pull_eval.JobManager(
        base_config=str(base), canonical_json=str(tmp_path / "canon.json"),
        info_fetcher=lambda mid: type("I", (), {
            "safetensors": type("S", (), {"total": 7_000_000_000})(),
            "pipeline_tag": "text-generation",
            "config": {"architectures": ["LlamaForCausalLM"]}})(),
        run_full=lambda config_path, n=None, fresh=False: None,
        out_dir=str(tmp_path / "pulls"), on_complete=guard.on_complete,
    )
    guard.manager = mgr
    holder["mgr"] = mgr

    mgr.start("org/model-7b")
    for _ in range(300):
        if mgr.snapshot().state in ("done", "error"):
            break
        time.sleep(0.01)

    assert mgr.snapshot().state == "done"
    assert seen.get("calls") == 1                 # destroyed exactly once
    assert seen.get("exists_at_destroy") is True  # analysis JSON was on disk FIRST
    assert os.path.exists(seen["path"])
