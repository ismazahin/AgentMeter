"""measure-vram — capture each model's WEIGHT FOOTPRINT (harness/analysis only).

The full-run DB stores only MARGINAL working memory (scenario_peak_vram_mb /
vram_delta_mb); it never recorded the resident model weights. To complete the
Phase 8 SAW "total device footprint" VRAM criterion we need, per model, the
device VRAM before vs after loading its weights at the SAME quant as the run.

This module loads each model ONCE in its OWN subprocess (reusing the
subprocess-per-model + require_gpu + cache-cleanup path so each model gets a fresh
CUDA context and nothing accumulates on disk), reads device VRAM before/after,
computes weight_footprint_mb = after - before, and writes results/model_vram.json.

NO scenarios, NO pipeline, NO generation — load-and-read only. It refuses to run
without CUDA (no fabricated readings), and stamps the hardware label so a mismatch
(e.g. a T4 instead of the L4 the run used) is visible and rejectable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from .config import load_config
from .pilot import _gpu_guard
from .runner import quant_setting, resolve_models


def _require_cuda(cfg) -> None:
    """Fail loudly on CPU — measure-vram is inherently GPU-only, never fabricated."""
    _gpu_guard(cfg)  # honours run.require_gpu (no silent CPU fallback)
    try:
        import torch

        ok = bool(torch.cuda.is_available())
    except Exception:
        ok = False
    if not ok:
        raise RuntimeError(
            "measure-vram requires a CUDA GPU — refusing to run on CPU (no fabricated "
            "readings). Run it on the SAME hardware as the full run (the L4)."
        )


def _spawn_measure_worker(config_path: str, model: str, out_json: str) -> int:
    """Spawn ONE per-model measure worker and BLOCK until it exits (sequential)."""
    cmd = [
        sys.executable, "-m", "agentmeter.worker",
        "--mode", "measure",
        "--model", model,
        "--out", out_json,
    ]
    if config_path:
        cmd += ["--config", config_path]
    return subprocess.run(cmd).returncode


def run_measure_vram(
    config_path: Optional[str] = None,
    out_path: str = "results/model_vram.json",
) -> dict[str, Any]:
    cfg = load_config(config_path)
    _require_cuda(cfg)

    models = resolve_models(cfg)
    quant = quant_setting(cfg)
    tol = float(cfg.get("run.free_vram_tolerance_mb", 500.0) or 500.0)
    worker_config = str(cfg.path)

    print("=" * 78)
    print("  AgentMeter — measure-vram (per-model weight footprint; load-and-read only)")
    print("=" * 78)
    print(f"Quant     : {quant}   models: {len(models)}   (each in its own subprocess)")
    print("")

    records: list[dict[str, Any]] = []
    for i, model in enumerate(models):
        print(f"[{i+1}/{len(models)}] {model}: loading (no scenarios) ...")
        fd, side = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            rc = _spawn_measure_worker(worker_config, model, side)
            if rc != 0:
                raise RuntimeError(
                    f"measure worker for model {model!r} exited with code {rc}. "
                    "See its output above (e.g. gated licence / OOM / non-L4 GPU)."
                )
            records.append(json.loads(Path(side).read_text()))
        finally:
            try:
                os.remove(side)
            except OSError:
                pass

    # Cross-model startup cleanliness (the VRAM-guard analog): every worker should
    # begin at the same fresh-context baseline; a big excess means a prior process
    # failed to release VRAM and this footprint is not trustworthy.
    baseline = records[0]["device_vram_before_load_mb"]
    hardware = set()
    for r in records:
        r["startup_excess_mb"] = r["device_vram_before_load_mb"] - baseline
        r["started_clean"] = abs(r["startup_excess_mb"]) <= tol
        hardware.add(r["hardware_label"])

    payload = {
        "quant": quant,
        "hardware_labels": sorted(hardware),
        "consistent_hardware": len(hardware) == 1,
        "free_vram_tolerance_mb": tol,
        "fresh_context_baseline_mb": baseline,
        "definition": ("weight_footprint_mb = device_vram_after_load_mb - "
                       "device_vram_before_load_mb; Phase 8 total device footprint = "
                       "weight_footprint_mb + mean(scenario_peak_vram_mb from the DB)."),
        "models": records,
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str))

    print("")
    print(f"{'model':<40}{'before_mb':>11}{'after_mb':>11}{'weight_mb':>11}  hardware")
    for r in records:
        print(f"{r['model']:<40}{r['device_vram_before_load_mb']:>11,.1f}"
              f"{r['device_vram_after_load_mb']:>11,.1f}{r['weight_footprint_mb']:>11,.1f}"
              f"  {r['hardware_label']}")
    print("")
    if not payload["consistent_hardware"]:
        print("!" * 78)
        print(f"WARNING: mixed hardware across models {sorted(hardware)} — footprints are "
              f"NOT comparable. Re-run all models on ONE GPU (the L4).")
        print("!" * 78)
    else:
        print(f"Hardware: {sorted(hardware)[0]}  (must match the full run's L4)")
    dirty = [r["model"] for r in records if not r["started_clean"]]
    if dirty:
        print(f"WARNING: model(s) did NOT start from the fresh-context baseline "
              f"(possible leftover VRAM): {dirty}")
    print(f"\nWrote {out}")
    print("=" * 78)
    return payload
