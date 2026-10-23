"""Optional load-scaling step: how one model copes with several flows at once.

Opt-in per session (worker flag --load-test). It runs AFTER the model's measured flows
are persisted, in the same worker (model still loaded), and never touches the measured
tables: it uses a plain pipeline (no instrumentation hook, so no CUDA peak-memory
counter is reset under another flow's feet) and writes its own JSON file, which the
session analysis reads into `load_scaling`. Nothing here changes a score, rank or
verdict.

For each concurrency level c (default 1, 2, 4) it runs the first max(8, 4c) flows with
c of them in flight at once (a thread pool), timing every flow end to end and the level
as a whole: throughput = flows / wall time. A level that raises (e.g. out of GPU memory)
is recorded with its error and the higher levels are skipped.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

LEVELS = (1, 2, 4)
MIN_FLOWS = 8
DIR = "load_scaling"


def flows_for(level: int, available: int) -> int:
    return min(available, max(MIN_FLOWS, 4 * level))


def out_path(db_path: str | Path, run_id: str, label: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", label)
    return Path(db_path).parent / DIR / f"{run_id}__{slug}.json"


def run_load_test(cfg, provider, scenarios: Sequence, label: str, out: str | Path,
                  levels: Sequence[int] = LEVELS) -> dict[str, Any]:
    from ..pipeline import Pipeline

    pipeline = Pipeline(cfg, provider)                     # plain: no instrumentation hook

    def one(s) -> float:
        t = time.perf_counter()
        pipeline.run(s.scenario_id, s.feature_prompt)
        return time.perf_counter() - t

    rows: list[dict[str, Any]] = []
    for c in levels:
        batch = list(scenarios[:flows_for(c, len(scenarios))])
        if not batch:
            break
        t0 = time.perf_counter()
        try:
            with ThreadPoolExecutor(max_workers=c, thread_name_prefix=f"agentmeter-load{c}") as pool:
                lat = list(pool.map(one, batch))
        except Exception as e:  # noqa: BLE001 — recorded; the measured flows are already safe
            rows.append({"concurrency": c, "n": len(batch), "error": f"{type(e).__name__}: {e}"[:300]})
            break
        wall = time.perf_counter() - t0
        rows.append({"concurrency": c, "n": len(batch), "wall_s": wall, "throughput_flows_per_s": len(batch) / wall,
                     "mean_latency_s": float(np.mean(lat)), "p50_latency_s": float(np.percentile(lat, 50)),
                     "p95_latency_s": float(np.percentile(lat, 95)), "error": None})
        print(f"    load c={c}: {len(batch)} flows in {wall:.3f}s ({len(batch) / wall:.3f} flows/s)")
    base = next((r for r in rows if r["concurrency"] == 1 and not r.get("error")), None)
    for r in rows:
        if base and not r.get("error"):
            r["speedup_vs_1"] = r["throughput_flows_per_s"] / base["throughput_flows_per_s"]
            r["efficiency"] = r["speedup_vs_1"] / r["concurrency"]
    result = {"model": label, "levels": rows, "provider": cfg.get("model.provider"),
              "method": ("After the measured flows, in the same worker: the first max("
                         f"{MIN_FLOWS}, 4c) flows with c in flight at once (thread pool), "
                         "uninstrumented; throughput = flows / wall time of the level.")}
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def read_results(db_path: str | Path, run_id: str, labels: dict[str, str]) -> dict[str, Optional[dict]]:
    """{model: result or None} for the session's models (labels: model -> DB label)."""
    found: dict[str, Optional[dict]] = {}
    for model, label in labels.items():
        p = out_path(db_path, run_id, label)
        try:
            found[model] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except (OSError, ValueError):
            found[model] = None
    return found
