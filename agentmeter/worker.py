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
from .pilot import _gpu_guard, free_cuda, release_provider, startup_isolation_guard


def _free_bytes() -> Optional[int]:
    """Best-effort free disk (bytes) for logging; None if it can't be read."""
    import shutil

    for p in (os.getcwd(), os.path.expanduser("~"), os.sep):
        try:
            return shutil.disk_usage(p).free
        except Exception:
            continue
    return None


def _hf_cache_cleanup(model_id: str) -> Optional[dict]:
    """Delete ONLY this worker's own model from the Hugging Face hub cache.

    Opt-in resource management (harness only): after a worker has finished with
    and persisted its model, downloaded fp16 weights (~7-16 GB) are dead weight
    that can fill the disk across a multi-model run. This removes just this one
    model so the next worker downloads fresh into freed space.

    Safety:
      - Uses huggingface_hub's scan_cache_dir + delete_revisions (never rm -rf).
      - Targets ONLY repo_type == 'model' AND repo_id == model_id — never
        datasets, never other models, never system files.
      - Absent repo (already gone) is a no-op with a note, not a crash.
      - Any failure is logged as a WARNING and swallowed: results are already
        persisted and a stale cache is merely disk.

    Resume note: if a resumed run re-runs a model whose cache was cleared, it
    simply re-downloads on its next run — expected, not a bug.
    """
    try:
        from huggingface_hub import scan_cache_dir
    except Exception as e:  # hub not installed (e.g. CPU/mock host)
        print(f"  [cache cleanup] skipped: huggingface_hub unavailable ({e}).")
        return None

    try:
        free_before = _free_bytes()
        cache = scan_cache_dir()
        revisions: list[str] = []
        for repo in cache.repos:
            if repo.repo_type == "model" and repo.repo_id == model_id:  # this model only
                revisions = [rev.commit_hash for rev in repo.revisions]
                break
        if not revisions:
            print(f"  [cache cleanup] no cached 'model' entry for {model_id!r} — nothing to delete.")
            return {"repo_id": model_id, "deleted": False, "freed_bytes": 0}

        strategy = cache.delete_revisions(*revisions)
        expected = int(getattr(strategy, "expected_freed_size", 0) or 0)
        strategy.execute()
        free_after = _free_bytes()

        def _gb(x):
            return "n/a" if x is None else f"{x / 1e9:.1f} GB"

        print(f"  [cache cleanup] deleted model {model_id!r}: "
              f"~{expected / 1e9:.2f} GB reclaimed | disk free {_gb(free_before)} -> {_gb(free_after)}.")
        return {
            "repo_id": model_id,
            "deleted": True,
            "expected_freed_bytes": expected,
            "free_before_bytes": free_before,
            "free_after_bytes": free_after,
        }
    except Exception as e:
        print(f"  [cache cleanup] WARNING: cleanup for {model_id!r} failed ({e}); "
              f"continuing — results are already persisted, a stale cache is harmless.")
        return None


def maybe_cleanup_model_cache(cfg, model_id: str) -> Optional[dict]:
    """Run the model-cache cleanup only when explicitly opted in via config.

    DEFAULT is OFF (run.cleanup_model_cache_after: false) — never delete unless
    the user turns it on. Called after persistence, just before the worker exits.
    """
    if not bool(cfg.get("run.cleanup_model_cache_after", False)):
        return None
    return _hf_cache_cleanup(model_id)


def _vram_record(model_id, hardware, quant, before, after) -> dict:
    """Assemble one model's VRAM measurement record (weight footprint = after - before)."""
    footprint = (after - before) if (after is not None and before is not None) else None
    return {
        "model": model_id,
        "hardware_label": hardware,
        "quant": quant,
        "device_vram_before_load_mb": before,
        "device_vram_after_load_mb": after,
        "weight_footprint_mb": footprint,
    }


def _measure_model_vram(cfg, model_name: str, out_json: str) -> None:
    """Measure ONE model's weight footprint: read device VRAM, load the model at the
    configured quant, read device VRAM again. NO scenarios, NO pipeline, NO
    generation — load-and-read only. Uses the exact same provider load path as the
    real run (HFProvider; 4bit-nf4 -> device_map={"":0}). Enforces require_gpu and
    refuses to fabricate a reading if pynvml/CUDA is unavailable.
    """
    import json as _json
    from pathlib import Path as _Path

    from .instrument import GpuProbe
    from .providers import get_provider
    from .runner import quant_setting

    _gpu_guard(cfg)  # require_gpu; no silent CPU fallback
    import torch  # available once _gpu_guard passed
    if not torch.cuda.is_available():
        raise RuntimeError("measure-vram requires a CUDA GPU (torch.cuda unavailable).")

    cfg.data.setdefault("model", {})["name"] = model_name
    quant = quant_setting(cfg)

    # Fresh CUDA context; read the device-level baseline BEFORE any weights.
    free_cuda()
    gpu = GpuProbe()
    before = gpu.pynvml_used_mb() if gpu.available else None
    if before is None:
        raise RuntimeError("pynvml unavailable — cannot read device VRAM (refusing to fabricate).")
    hardware = torch.cuda.get_device_name(torch.cuda.current_device())

    provider = get_provider(cfg)   # HFProvider; same 4bit-nf4 device_map load as the run
    try:
        provider.load()            # load-and-read ONLY — no scenarios, no generation
        after = gpu.pynvml_used_mb()
    finally:
        release_provider(provider)  # free VRAM before this process exits

    rec = _vram_record(model_name, hardware, quant, before, after)
    _Path(out_json).write_text(_json.dumps(rec))
    fp = rec["weight_footprint_mb"]
    print(f"    {model_name}: before={before:,.1f} MB  after={after:,.1f} MB  "
          f"weight_footprint={fp:,.1f} MB  on {hardware}  ({quant})")

    # Throwaway capture: always free this model's HF cache so nothing accumulates
    # on disk across the 5 loads (independent of the cleanup flag).
    _hf_cache_cleanup(model_name)


def _run_model_sqlite(
    cfg,
    model_name: str,
    run_id: str,
    n: Optional[int],
    baseline_vram_mb: Optional[float] = None,
    vram_out: Optional[str] = None,
) -> None:
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

        tolerance_mb = float(cfg.get("run.free_vram_tolerance_mb", 500.0) or 500.0)

        # This worker's fresh-context device VRAM BEFORE loading its own weights.
        # It is the isolation signal: compared against the parent-supplied baseline
        # (the first worker's before-load), it says whether THIS worker started
        # clean. Report it to the parent via the sidecar so it can set the baseline.
        free_cuda()
        before_gpu = GpuProbe()
        dev_before = before_gpu.pynvml_used_mb() if before_gpu.available else None
        if dev_before is not None:
            print(f"    device VRAM before load: {dev_before:,.1f} MB (fresh context)")
        if vram_out:
            import json as _json
            from pathlib import Path as _Path

            _Path(vram_out).write_text(_json.dumps({"device_vram_before_load_mb": dev_before}))

        provider = get_provider(cfg)
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

        # Startup-isolation guard: did THIS worker start clean vs the fresh-context
        # baseline the parent established (first worker's before-load; for the first
        # worker, itself -> clean)? Should always pass now; loud if a worker ever
        # started contaminated (a previous process failed to release VRAM).
        guard_baseline = baseline_vram_mb if baseline_vram_mb is not None else dev_before
        startup_isolation_guard(guard_baseline, dev_before, tolerance_mb, label)
    finally:
        store.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentmeter.worker",
        description="Run exactly ONE model in a fresh process (VRAM isolation).",
    )
    parser.add_argument("--config", type=str, default=None, help="path to config.yaml")
    parser.add_argument("--model", type=str, required=True, help="model id to run")
    parser.add_argument("--mode", choices=["pilot", "sqlite", "measure"], required=True)
    parser.add_argument("--n", type=int, default=None, help="scenarios (default: dataset.limit)")
    parser.add_argument("--out", type=str, default=None, help="pilot mode: per-model JSON path")
    parser.add_argument("--run-id", type=str, default=None, help="sqlite mode: run id to persist under")
    parser.add_argument("--baseline-vram-mb", type=float, default=None,
                        help="fresh-context baseline (first worker's before-load) for the startup guard")
    parser.add_argument("--vram-out", type=str, default=None,
                        help="sqlite mode: sidecar path to write this worker's before-load reading")
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
            baseline_vram_mb=args.baseline_vram_mb,
        )
        print(res.report)
        # Results are written; opt-in cache cleanup runs last, before exit.
        maybe_cleanup_model_cache(load_config(args.config), args.model)
        return 0

    if args.mode == "measure":
        if not args.out:
            parser.error("--out is required in measure mode")
        cfg = load_config(args.config)
        _measure_model_vram(cfg, args.model, args.out)
        return 0

    # sqlite mode
    if not args.run_id:
        parser.error("--run-id is required in sqlite mode")
    cfg = load_config(args.config)
    _run_model_sqlite(
        cfg, args.model, args.run_id, args.n,
        baseline_vram_mb=args.baseline_vram_mb, vram_out=args.vram_out,
    )
    # Rows are persisted and the provider released; opt-in cache cleanup runs
    # last, before exit, so the next worker downloads into freed space.
    maybe_cleanup_model_cache(cfg, args.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
