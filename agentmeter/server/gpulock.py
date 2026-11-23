"""One at a time on the GPU machine: a benchmark or a data preparation, never both.

Preparing a dataset is CPU work. Run next to a benchmark it would compete for the CPU, its cache,
memory bandwidth and the disk, and so change the benchmark's latency, cost and energy readings.
The GPU server's worker (jobs.py) and the preparation service (cloudprep/app.py) therefore share
one exclusive file lock on the machine:

  <lock>            flock()ed by whoever runs: the worker for a whole job, the preparation service
                    for a whole preparation (download, prepare, IDS scoring, upload)
  <lock>.info       JSON about the holder: {"kind": "benchmark"|"prepare", "job_id", "models",
                    "started_at"} (what the waiting page shows as "Running now")
  <lock>.prep-wanted  touched every few seconds while the preparation service has work waiting.
                    The worker does not START its next job while it is fresh, so a waiting
                    preparation goes before queued benchmarks. A running job is never interrupted.

The lock path is AGENTMETER_BUSY_LOCK, by default agentmeter-busy.lock in the system temp folder,
so two processes on one machine agree without configuration. Where fcntl is missing (Windows) the
lock is a no-op: the preparation service does not run next to a GPU server there.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import fcntl
except ImportError:                       # Windows
    fcntl = None  # type: ignore[assignment]

WANTED_FRESH_S = 30.0                    # a marker older than this is left over from a crash


def lock_path() -> Path:
    return Path(os.environ.get("AGENTMETER_BUSY_LOCK") or Path(tempfile.gettempdir()) / "agentmeter-busy.lock")


def _info_path(p: Path) -> Path:
    return p.with_name(p.name + ".info")


def _wanted_path(p: Path) -> Path:
    return p.with_name(p.name + ".prep-wanted")


@contextlib.contextmanager
def hold(info: dict[str, Any], *, wait_s: Optional[float] = None, on_wait=None,
         stop: Optional[threading.Event] = None, path: Optional[Path] = None) -> Iterator[bool]:
    """Hold the machine's lock for the duration of the block. Blocks until it is free (or wait_s
    passes: then yields False and holds nothing). on_wait(holder_info) is called every few seconds
    while it waits; stop, when set, gives up like a timeout."""
    p = path or lock_path()
    if fcntl is None:
        yield True
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a+")
    got, t0, last_note = False, time.monotonic(), 0.0
    try:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                pass
            now = time.monotonic()
            if (wait_s is not None and now - t0 >= wait_s) or (stop is not None and stop.is_set()):
                break
            if on_wait and now - last_note >= 5.0:
                last_note = now
                on_wait(holder(p))
            time.sleep(0.5)
        if got:
            _write_json(_info_path(p), {**info, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pid": os.getpid()})
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                _info_path(p).unlink()
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def holder(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Who holds the lock right now (from <lock>.info), or None when it is free."""
    p = path or lock_path()
    if fcntl is not None and p.exists():
        with open(p, "a+") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return None                                   # nobody holds it
            except BlockingIOError:
                pass
    try:
        return json.loads(_info_path(p).read_text())
    except (OSError, ValueError):
        return {"kind": "unknown"} if fcntl is not None and p.exists() else None


def want(on: bool, path: Optional[Path] = None) -> None:
    """The preparation service has work waiting (on) or not (off)."""
    w = _wanted_path(path or lock_path())
    if on:
        w.parent.mkdir(parents=True, exist_ok=True)
        w.touch()
    else:
        with contextlib.suppress(OSError):
            w.unlink()


def prep_wanted(path: Optional[Path] = None) -> bool:
    """A preparation is waiting or running (a fresh marker)."""
    try:
        return time.time() - _wanted_path(path or lock_path()).stat().st_mtime < WANTED_FRESH_S
    except OSError:
        return False


def wait_for_prep(stop: Optional[threading.Event] = None, path: Optional[Path] = None, poll_s: float = 1.0) -> None:
    """The worker, before it starts a job: let waiting preparations go first."""
    while prep_wanted(path) and not (stop is not None and stop.is_set()):
        time.sleep(poll_s)


def _write_json(p: Path, obj: Any) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, p)
