"""Live telemetry of a running benchmark: what the system is doing right now, step by step
(queue, data, Hugging Face, download, 4-bit load, each flow through the agents, idle power,
scoring), for the "Live telemetry" panel of the run page.

One JSON-lines file per job (<jobs dir>/<job id>.events.jsonl). The job's thread binds it
(bind / unbind); a model's worker subprocess gets its path in AGENTMETER_TELEMETRY_FILE and
appends to the same file. emit() never raises and is a no-op when nothing is bound, so code
that runs outside a job (CLI, tests) is unaffected. Events are written OUTSIDE the timed
sections (between flows, around a model load), so they never change a measurement.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

ENV = "AGENTMETER_TELEMETRY_FILE"
MAX_READ = 500
_local = threading.local()


def bind(path: str | Path) -> None:
    _local.path = str(path)


def unbind() -> None:
    _local.path = None


def current_path() -> Optional[str]:
    return getattr(_local, "path", None) or os.environ.get(ENV) or None


def emit(stage: str, msg: str, *, path: Optional[str | Path] = None, **detail: Any) -> None:
    p = str(path) if path else current_path()
    if not p:
        return
    try:
        line = json.dumps({"t": round(time.time(), 3), "stage": stage, "msg": msg, **detail}, default=str)
        with open(p, "a", encoding="utf-8") as f:       # one short append per event (atomic enough)
            f.write(line + "\n")
    except (OSError, TypeError, ValueError):
        pass


def read(path: str | Path, since: int = 0, limit: int = MAX_READ) -> tuple[list[dict[str, Any]], int]:
    """Events after the first `since` lines; returns (events, next since)."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return [], since
    out = []
    for ln in lines[since:since + limit]:
        if not ln.endswith("\n"):          # a line still being written: next poll
            break
        try:
            out.append(json.loads(ln))
        except ValueError:
            out.append({"stage": "log", "msg": ln.strip()[:300]})
    return out, since + len(out)


_NVML: dict[str, Any] = {}


def gpu_sample() -> Optional[dict[str, Any]]:
    """The GPU right now (NVML): utilisation, memory, board power, temperature. None without
    a GPU. Read on request (the page polls about every 2 s); never part of a measurement."""
    try:
        import pynvml
        if "h" not in _NVML:
            pynvml.nvmlInit()
            _NVML["h"] = pynvml.nvmlDeviceGetHandleByIndex(0)
            n = pynvml.nvmlDeviceGetName(_NVML["h"])
            _NVML["name"] = n.decode() if isinstance(n, bytes) else n
        h = _NVML["h"]
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
        try:
            power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
        except pynvml.NVMLError:
            power = None
        try:
            temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        except pynvml.NVMLError:
            temp = None
        return {"name": _NVML["name"], "util_pct": util.gpu, "mem_used_mb": round(mem.used / 2 ** 20),
                "mem_total_mb": round(mem.total / 2 ** 20), "power_w": power, "temp_c": temp, "t": round(time.time(), 3)}
    except Exception:  # noqa: BLE001 — no pynvml, no driver, no GPU: no reading (never invented)
        return None
