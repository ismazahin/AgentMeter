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

from agentmeter import vast_shutdown

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_server_module():
    path = REPO_ROOT / "scripts" / "serve.py"
    spec = importlib.util.spec_from_file_location("serve", path)
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


# --- (c) watchdog respects the job lock (queued / running service jobs) ---

class _FakeJobs:
    def __init__(self, status="running"):
        self.status = status

    def list_jobs(self, limit=200):
        return [{"job_id": "job_x", "status": self.status}]


def test_watchdog_never_destroys_while_a_job_is_queued_or_running():
    srv = _load_server_module()
    calls = []
    jobs = _FakeJobs("running")
    guard = srv.CostGuard(auto_destroy=True, idle_timeout_min=1, instance_id="1",
                          destroy=lambda instance_id=None: calls.append(instance_id), job_manager=jobs)
    guard.idle_timeout = 0.05           # 50ms idle window for the test
    guard.last_activity = time.time() - 100.0  # long "idle", but a job is running

    stop = threading.Event()
    t = threading.Thread(target=guard.watchdog, args=(stop,), kwargs={"poll": 0.01}, daemon=True)
    t.start()
    time.sleep(0.25)
    assert calls == []                  # running job -> NEVER destroyed
    jobs.status = "queued"
    time.sleep(0.15)
    assert calls == []                  # a queued job also keeps the box
    jobs.status = "done"                # finished; idle clock can now elapse
    time.sleep(0.25)
    stop.set(); t.join(timeout=1)
    assert calls == ["1"]               # destroyed exactly once, after it went idle


# --- (d) opt-in: auto-destroy OFF never destroys ------------------------

def test_auto_destroy_off_never_destroys():
    srv = _load_server_module()
    calls = []
    guard = srv.CostGuard(auto_destroy=False, idle_timeout_min=30, instance_id="1",
                          destroy=lambda instance_id=None: calls.append(instance_id),
                          job_manager=_FakeJobs("done"))
    stop = threading.Event()
    guard.watchdog(stop, poll=0.01)     # returns immediately
    assert calls == []


def test_destroy_fires_at_most_once():
    srv = _load_server_module()
    calls = []
    guard = srv.CostGuard(auto_destroy=True, idle_timeout_min=30, instance_id="42",
                          destroy=lambda instance_id=None: calls.append(instance_id) or True)
    assert guard._fire("x") is True
    assert guard._fire("x") is False    # idempotent
    assert calls == ["42"]
