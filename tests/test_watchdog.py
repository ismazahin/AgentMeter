"""The runner's watchdog: a worker that makes no progress for longer than the per-flow limit is
stopped (WATCHDOG_RC); one that keeps making progress, or exits by itself, is left alone."""
from __future__ import annotations

import itertools
import subprocess
import sys
import time

from agentmeter.run import runner


def _fake_worker(monkeypatch, code: str):
    real = subprocess.Popen
    monkeypatch.setattr(runner.subprocess, "Popen", lambda cmd, env=None: real([sys.executable, "-c", code]))


def test_a_silent_worker_is_stopped(monkeypatch):
    _fake_worker(monkeypatch, "import time; time.sleep(60)")
    monkeypatch.setenv("AGENTMETER_LOAD_TIMEOUT_S", "1")
    t0 = time.monotonic()
    rc = runner._spawn_model_worker("cfg.yaml", "m", "run1", None, progress=lambda: (0, 0))
    assert rc == runner.WATCHDOG_RC and time.monotonic() - t0 < 20


def test_a_worker_that_keeps_progressing_is_left_alone(monkeypatch):
    _fake_worker(monkeypatch, "import time; time.sleep(5)")
    monkeypatch.setenv("AGENTMETER_LOAD_TIMEOUT_S", "1")
    monkeypatch.setenv("AGENTMETER_FLOW_TIMEOUT_S", "1")
    tick = itertools.count()
    assert runner._spawn_model_worker("cfg.yaml", "m", "run1", None, progress=lambda: (next(tick), 0)) == 0


def test_after_the_first_flow_the_per_flow_limit_applies(monkeypatch):
    _fake_worker(monkeypatch, "import time; time.sleep(60)")
    monkeypatch.setenv("AGENTMETER_LOAD_TIMEOUT_S", "600")      # loading may take long…
    monkeypatch.setenv("AGENTMETER_FLOW_TIMEOUT_S", "1")        # …but once flows run, each must finish in time
    calls = itertools.count()
    t0 = time.monotonic()
    rc = runner._spawn_model_worker("cfg.yaml", "m", "run1", None, progress=lambda: (1 if next(calls) >= 1 else 0, 0))
    assert rc == runner.WATCHDOG_RC and time.monotonic() - t0 < 20


def test_without_progress_tracking_the_worker_is_simply_awaited(monkeypatch):
    _fake_worker(monkeypatch, "raise SystemExit(3)")
    assert runner._spawn_model_worker("cfg.yaml", "m", "run1", None) == 3


def test_limits_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENTMETER_FLOW_TIMEOUT_S", "120")
    monkeypatch.setenv("AGENTMETER_LOAD_TIMEOUT_S", "bad")
    assert runner.flow_timeout_s() == 120 and runner.load_timeout_s() == 3600
