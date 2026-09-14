"""Phase 6 / pilot — per-model WORKER subprocess (VRAM isolation by process).

The reliable way to free a bitsandbytes 4-bit / accelerate device_map model's
GPU memory is to let the OS reclaim it: run exactly ONE model in a short-lived
process and exit. Every model therefore starts in a FRESH CUDA context, so its
readings are clean by construction — no cross-model contamination by load order.

This module is that worker. It loads one model, measures its own fresh-context
VRAM baseline, runs its scenarios through the EXISTING instrumented pipeline, and
either:
  - writes a per-model pilot JSON   (--mode pilot  --out <json>), or
  - persists into the run-full SQLite DB (--mode sqlite --run-id <id>),
then exits. On exit the OS reclaims all GPU memory.

Sequential-only, Mode A, config-driven, no-silent-CPU-fallback (require_gpu is
enforced here for HF runs). Fully mock/CPU testable: with the mock provider the
worker runs on CPU, the VRAM guard is skipped (no fabricated reading), and the
parent orchestrator spawns it exactly the same way.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from .config import load_config
from .pilot import _gpu_guard, check_vram_residual, free_cuda, release_provider


def _run_model_sqlite(cfg, model_name: str, run_id: str, n: Optional[int]) -> None:
    """Run one model's scenarios and persist them into the run-full SQLite DB.

    Skips (model, scenario) pairs already marked complete for this run, so a
    respawn after a crash finishes only what is left — each scenario is persisted
    atomically (all four agent rows + its verdict in one transaction).
    """
    from .dataset import DatasetLoader
    from .instrument import GpuProbe, MetricsCollector, make_instrumented_hook
    from .pipeline import Pipeline
    from .providers import get_provider
    from .runner import model_label
    from .storage import Storage

    _gpu_guard(cfg)  # no silent CPU fallback for HF runs
    cfg.data.setdefault("model", {})["name"] = model_name
    label = model_label(cfg)

    db_path = cfg.resolve_path("storage.sqlite_path", "results/agentmeter.db")
    store = Storage(db_path)
    try:
        done = {sid for (m, sid) in store.completed_pairs(run_id) if m == label}
        scenarios = DatasetLoader(cfg).load()
        if n is not None:
            scenarios = scenarios[:n]

        # Fresh-context VRAM baseline for THIS process (the whole point of the
        # subprocess): the guard below compares against it and should always pass.
        free_cuda()
        gpu0 = GpuProbe()
        baseline_vram_mb = gpu0.pynvml_used_mb() if gpu0.available else None
        tolerance_mb = float(cfg.get("run.free_vram_tolerance_mb", 500.0) or 500.0)

        provider = get_provider(cfg)
        before_gpu = GpuProbe()
        dev_before = before_gpu.pynvml_used_mb() if before_gpu.available else None
        if dev_before is not None:
            print(f"    device VRAM before load: {dev_before:,.1f} MB (fresh context)")
        provider.load()

        gpu = GpuProbe()
        collector = MetricsCollector()
        hook = make_instrumented_hook(collector, label, gpu)
        pipeline = Pipeline(cfg, provider, node_hook=hook)

        # Test-only fault injection: crash after N persisted scenarios to exercise
        # the parent's crash/resume path. Unset in normal use -> no effect.
        crash_after = os.environ.get("AGENTMETER_WORKER_CRASH_AFTER_SCENARIOS")
        persisted = 0
        try:
            for s in scenarios:
                if s.scenario_id in done:
                    continue
                offset = len(collector.rows)
                state = pipeline.run(s.scenario_id, s.feature_prompt)
                new_rows = collector.rows[offset:]

                verdict = state.get("verdict", {}) or {}
                pred = verdict.get("predicted_class", "Unparseable")
                ok = pred == s.held_out_label
                total_time = sum(r.wall_time_s for r in new_rows)
                vram_vals = [r.vram_peak_mb for r in new_rows if r.vram_peak_mb is not None]
                peak_vram = max(vram_vals) if vram_vals else None  # peak, not sum

                store.persist_scenario(
                    run_id=run_id,
                    model=label,
                    scenario_id=s.scenario_id,
                    predicted_label=pred,
                    held_out_label=s.held_out_label,
                    correct=ok,
                    scenario_total_time_s=total_time,
                    scenario_peak_vram_mb=peak_vram,
                    agent_rows=new_rows,
                )
                persisted += 1
                print(f"    {s.scenario_id:<10} -> {pred:<16} "
                      f"(gt={s.held_out_label}, {total_time:.4f}s) [persisted]")

                if crash_after is not None and persisted >= int(crash_after):
                    raise SystemExit(3)  # simulated mid-model crash (test-only)
        finally:
            release_provider(provider)

        # Fresh-context guard: should always pass now. If it ever trips, something
        # is genuinely wrong — surface it loudly.
        check_vram_residual(baseline_vram_mb, tolerance_mb, label)
    finally:
        store.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentmeter.worker",
        description="Run exactly ONE model in a fresh process (VRAM isolation).",
    )
    parser.add_argument("--config", type=str, default=None, help="path to config.yaml")
    parser.add_argument("--model", type=str, required=True, help="model id to run")
    parser.add_argument("--mode", choices=["pilot", "sqlite"], required=True)
    parser.add_argument("--n", type=int, default=None, help="scenarios (default: dataset.limit)")
    parser.add_argument("--out", type=str, default=None, help="pilot mode: per-model JSON path")
    parser.add_argument("--run-id", type=str, default=None, help="sqlite mode: run id to persist under")
    parser.add_argument("--proj-scenarios", type=int, default=1000)
    parser.add_argument("--proj-models", type=int, default=1)
    args = parser.parse_args(argv)

    if args.mode == "pilot":
        from .pilot import run_pilot

        res = run_pilot(
            config_path=args.config,
            n=args.n,
            model_name=args.model,
            json_path=args.out,
            proj_scenarios=args.proj_scenarios,
            proj_models=args.proj_models,
        )
        print(res.report)
        return 0

    # sqlite mode
    if not args.run_id:
        parser.error("--run-id is required in sqlite mode")
    cfg = load_config(args.config)
    _run_model_sqlite(cfg, args.model, args.run_id, args.n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
