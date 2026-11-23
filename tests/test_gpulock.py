"""One at a time on the GPU machine (agentmeter/server/gpulock.py): a benchmark and a data
preparation never run together, a waiting preparation goes before the worker's next job, and a
running job is never interrupted. Preparing next to a benchmark would change its readings."""
from __future__ import annotations

import os
import threading
import time

import pytest

from agentmeter.server import gpulock
from agentmeter.server.jobs import JobManager

pytestmark = pytest.mark.skipif(gpulock.fcntl is None, reason="the lock needs fcntl (Linux, macOS)")


def test_only_one_holder_and_who_it_is(tmp_path):
    lock = tmp_path / "busy.lock"
    assert gpulock.holder(lock) is None
    with gpulock.hold({"kind": "benchmark", "job_id": "job_a", "models": 2}, path=lock) as got:
        assert got
        h = gpulock.holder(lock)
        assert h["kind"] == "benchmark" and h["models"] == 2 and h["started_at"]
        seen = []
        with gpulock.hold({"kind": "prepare"}, path=lock, wait_s=1.2, on_wait=seen.append) as second:
            assert second is False                                   # still held: nothing for the second
        assert seen and seen[0]["kind"] == "benchmark"               # it was told who holds the machine
    assert gpulock.holder(lock) is None                              # released, info removed
    with gpulock.hold({"kind": "prepare"}, path=lock, wait_s=1) as got:
        assert got


def test_a_stop_gives_up_waiting(tmp_path):
    lock, stop = tmp_path / "busy.lock", threading.Event()
    with gpulock.hold({"kind": "benchmark"}, path=lock):
        threading.Timer(0.5, stop.set).start()
        t0 = time.monotonic()
        with gpulock.hold({"kind": "prepare"}, path=lock, stop=stop) as got:
            assert got is False
        assert time.monotonic() - t0 < 5


def test_the_waiting_marker_is_fresh_or_ignored(tmp_path):
    lock = tmp_path / "busy.lock"
    assert not gpulock.prep_wanted(lock)
    gpulock.want(True, lock)
    assert gpulock.prep_wanted(lock)
    old = time.time() - gpulock.WANTED_FRESH_S - 5                    # left over from a crash
    os.utime(lock.with_name(lock.name + ".prep-wanted"), (old, old))
    assert not gpulock.prep_wanted(lock)
    gpulock.want(True, lock)
    gpulock.want(False, lock)
    assert not gpulock.prep_wanted(lock)
    gpulock.wait_for_prep(path=lock)                                  # returns at once: nothing waits


def test_the_worker_lets_a_waiting_preparation_go_first_and_holds_the_machine_while_it_runs(tmp_path, monkeypatch):
    lock = tmp_path / "busy.lock"
    monkeypatch.setenv("AGENTMETER_BUSY_LOCK", str(lock))
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results", gpu_available=lambda: False, autostart=False)
    ran = []

    def run_one(job_id):                                              # what the worker sees while it runs
        ran.append((job_id, time.monotonic(), gpulock.holder(lock)))
    monkeypatch.setattr(mgr, "_run_one", run_one)

    gpulock.want(True, lock)                                          # a preparation is waiting
    mgr._queue.put("job_1")
    mgr.start()
    time.sleep(1.5)
    assert ran == []                                                  # the benchmark did not start
    t_free = time.monotonic()
    gpulock.want(False, lock)
    deadline = time.time() + 10
    while time.time() < deadline and not ran:
        time.sleep(0.1)
    assert ran and ran[0][0] == "job_1" and ran[0][1] >= t_free
    assert ran[0][2]["kind"] == "benchmark" and ran[0][2]["job_id"] == "job_1"   # it held the machine
    deadline = time.time() + 5
    while time.time() < deadline and gpulock.holder(lock) is not None:
        time.sleep(0.1)
    assert gpulock.holder(lock) is None

    # a preparation running holds the machine: the next job waits for it
    release = threading.Event()

    def prepare():
        with gpulock.hold({"kind": "prepare"}, path=lock):
            release.wait(10)
    threading.Thread(target=prepare, daemon=True).start()
    time.sleep(0.3)
    mgr._queue.put("job_2")
    time.sleep(1.5)
    assert [r[0] for r in ran] == ["job_1"]
    release.set()
    deadline = time.time() + 10
    while time.time() < deadline and len(ran) < 2:
        time.sleep(0.1)
    assert [r[0] for r in ran] == ["job_1", "job_2"]
