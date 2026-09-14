"""Phase 5 — PILOT RUN.

Runs the full instrumented pipeline over a handful of scenarios with ONE real
model loaded in-process on the GPU (Mode A), sequentially, and produces:
  - a per-(scenario, agent) table: wall_time, ttft, vram_peak, tokens
  - per-agent mean cost
  - per-scenario totals + mean, and a projection to N scenarios x M models
  - verdicts compared against held-out ground truth (pilot accuracy)

Hard guard: if run.require_gpu is true and CUDA is unavailable, STOP — VRAM is
a core requirement and there is no silent CPU fallback.

This function is shared by `python main.py pilot ...` and the HF Space app.
It intentionally does NOT persist to SQLite (that is Phase 6). Output is a
JSON payload + a printable text report.
"""
from __future__ import annotations

import json
import math
import platform
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Config, load_config
from .dataset import DatasetLoader
from .instrument import GpuProbe, MetricsCollector, make_instrumented_hook
from .pipeline import Pipeline
from .providers import get_provider


@dataclass
class PilotResult:
    payload: dict[str, Any]
    report: str


def _gpu_guard(config: Config) -> None:
    """Enforce the no-silent-CPU-fallback rule before doing any heavy work."""
    require_gpu = bool(config.get("run.require_gpu", False))
    if not require_gpu:
        return
    try:
        import torch

        cuda = torch.cuda.is_available()
    except Exception:
        cuda = False
    if not cuda:
        raise RuntimeError(
            "STOP: run.require_gpu is true but torch.cuda is not available. "
            "VRAM measurement is a core requirement — refusing to run on CPU. "
            "Run this on the GPU host (HF Space, 1x L4)."
        )


def free_cuda() -> None:
    """Aggressively release GPU memory so the next model loads from a clean slate.

    Call this between models in a multi-model run — the second model's VRAM
    readings are only trustworthy if the first is fully freed.
    """
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.ipc_collect()
    except Exception:
        pass


def release_provider(provider) -> None:
    """SINGLE shared unload path used between models by BOTH run_pilot_models and
    the Phase 6 run-full runner (do not duplicate this logic elsewhere).

    Drops the model's references via provider.unload() — which, for the HF
    provider, also clears the accelerate/bitsandbytes device_map hooks that would
    otherwise pin the weights on the GPU — then aggressively frees CUDA so device
    VRAM returns to near-baseline before the next model loads.
    """
    if provider is not None:
        try:
            provider.unload()
        except Exception:
            pass
    free_cuda()


def startup_isolation_guard(baseline_mb, before_load_mb, tolerance_mb, model_label):
    """The REAL cross-process isolation check: did this worker START clean?

    In subprocess-per-model isolation the meaningful signal is whether a model's
    worker began in a fresh CUDA context — i.e. its device VRAM BEFORE loading its
    own weights is within tolerance of the established fresh-context baseline (the
    first worker's before-load). If a later worker starts far above that baseline,
    a previous process failed to release VRAM and the comparison is contaminated.

    (The per-worker POST-UNLOAD residual is NOT used here: a bnb-4bit worker can
    show a large in-process residual and still be perfectly clean, because the
    process exits immediately after and the OS reclaims all VRAM.)

    Returns a dict; WARNS LOUDLY only when a worker starts contaminated. Never
    fabricates a reading: with pynvml/CUDA unavailable (mock/CPU) the check is
    skipped and exceeded is False.
    """
    if baseline_mb is None or before_load_mb is None:
        print(
            f"  [VRAM isolation] {model_label}: startup check skipped — no device reading "
            f"(pynvml/CUDA unavailable), not fabricating one."
        )
        return {
            "kind": "startup_isolation",
            "device_vram_before_load_mb": before_load_mb,
            "baseline_vram_mb": baseline_mb,
            "startup_excess_mb": None,
            "tolerance_mb": tolerance_mb,
            "checked": False,
            "exceeded": False,
        }
    excess = before_load_mb - baseline_mb
    exceeded = excess > tolerance_mb
    if exceeded:
        print(
            f"  [VRAM isolation] WARNING: {model_label} STARTED CONTAMINATED — "
            f"before_load {before_load_mb:,.1f} MB vs fresh-context baseline "
            f"{baseline_mb:,.1f} MB (excess {excess:,.1f} MB > tolerance {tolerance_mb:,.1f} MB). "
            f"A previous process did not release VRAM — this model's readings are not clean."
        )
    else:
        print(
            f"  [VRAM isolation] OK: {model_label} started clean "
            f"(before_load {before_load_mb:,.1f} MB ~ baseline {baseline_mb:,.1f} MB, "
            f"excess {excess:,.1f} MB <= tolerance {tolerance_mb:,.1f} MB)."
        )
    return {
        "kind": "startup_isolation",
        "device_vram_before_load_mb": before_load_mb,
        "baseline_vram_mb": baseline_mb,
        "startup_excess_mb": excess,
        "tolerance_mb": tolerance_mb,
        "checked": True,
        "exceeded": exceeded,
    }


def run_pilot(
    config_path: Optional[str] = None,
    n: Optional[int] = None,
    proj_scenarios: int = 1000,
    proj_models: int = 2,
    json_path: Optional[str] = None,
    model_name: Optional[str] = None,
    baseline_vram_mb: Optional[float] = None,
) -> PilotResult:
    cfg = load_config(config_path)
    if model_name:  # override for multi-model runs (same config, swap the model)
        cfg.data.setdefault("model", {})["name"] = model_name
    _gpu_guard(cfg)

    # Start from a clean GPU so before-load VRAM reflects only what's resident
    # from anything prior (should be ~0 additional for this model).
    free_cuda()
    gpu = GpuProbe()
    device_vram_before_load_mb = gpu.pynvml_used_mb()

    provider = get_provider(cfg)
    load_t0 = time.perf_counter()
    provider.load()
    model_load_s = time.perf_counter() - load_t0  # excluded from per-agent readings

    model_label = cfg.get("model.name") if cfg.get("model.provider") == "hf" else f"mock:{provider.name}"
    # Device-level VRAM after weights are resident (captures quantized weights
    # that the torch allocator under-reports).
    device_vram_after_load_mb = gpu.pynvml_used_mb()
    collector = MetricsCollector()
    hook = make_instrumented_hook(collector, model_label, gpu)
    pipeline = Pipeline(cfg, provider, node_hook=hook)

    scenarios = DatasetLoader(cfg).load()
    if n is not None:
        scenarios = scenarios[:n]

    # --- run sequentially -------------------------------------------------
    verdict_rows: list[dict[str, Any]] = []
    correct = 0
    for s in scenarios:
        state = pipeline.run(s.scenario_id, s.feature_prompt)
        verdict = state.get("verdict", {}) or {}
        pred = verdict.get("predicted_class", "Unparseable")
        ok = pred == s.held_out_label
        correct += int(ok)
        verdict_rows.append(
            {
                "scenario_id": s.scenario_id,
                "predicted_class": pred,
                "mitre_technique": verdict.get("mitre_technique"),
                "held_out_label": s.held_out_label,
                "correct": ok,
            }
        )

    n_scn = len(scenarios)
    per_agent = collector.per_agent_summary()
    mean_scn = collector.mean_scenario_wall_s()
    accuracy = (correct / n_scn) if n_scn else 0.0
    proj_total_s = mean_scn * proj_scenarios * proj_models

    payload: dict[str, Any] = {
        "model_label": model_label,
        "provider": cfg.get("model.provider"),
        "device": getattr(provider, "device", None),
        "gpu_available": gpu.available,
        "gpu_name": _gpu_name(),
        "model_load_s": model_load_s,
        "model_weight_vram_mb": getattr(provider, "model_vram_mb", None),
        "quantization": getattr(provider, "quantization_label", "none"),
        "device_vram_before_load_mb": device_vram_before_load_mb,
        "device_vram_after_load_mb": device_vram_after_load_mb,
        "n_scenarios": n_scn,
        "agents": pipeline.agent_names,
        "metrics_rows": [r.as_dict() for r in collector.rows],
        "per_agent_summary": per_agent,
        "per_scenario_totals": collector.per_scenario_totals(),
        "mean_scenario_wall_s": mean_scn,
        "verdicts": verdict_rows,
        "accuracy": accuracy,
        "correct": correct,
        "projection": {
            "scenarios": proj_scenarios,
            "models": proj_models,
            "total_seconds": proj_total_s,
        },
        "platform": platform.platform(),
    }

    # SINGLE shared unload path: clears device_map hooks + frees CUDA. In
    # subprocess-per-model isolation the real guarantee is that THIS process
    # exits right after and the OS reclaims all VRAM — so the in-process residual
    # below is expected to be non-zero (esp. for bnb-4bit) and is NOT a failure.
    release_provider(provider)

    gpu_after = GpuProbe()
    device_vram_after_unload_mb = gpu_after.pynvml_used_mb() if gpu_after.available else None
    tolerance_mb = float(cfg.get("run.free_vram_tolerance_mb", 500.0) or 500.0)

    # The in-process post-unload residual: RECORD it (diagnostic) but do NOT drive
    # the guard from it — it is harmless because the worker process exits next.
    in_process_residual_mb = (
        (device_vram_after_unload_mb - device_vram_before_load_mb)
        if (device_vram_after_unload_mb is not None and device_vram_before_load_mb is not None)
        else None
    )

    # The guard's real invariant: did this worker START clean? Compare its
    # before-load VRAM to the fresh-context baseline (the parent-supplied first
    # worker's before-load; for the first/standalone worker, itself -> clean).
    guard_baseline = baseline_vram_mb if baseline_vram_mb is not None else device_vram_before_load_mb
    guard = startup_isolation_guard(
        guard_baseline, device_vram_before_load_mb, tolerance_mb, model_label
    )

    payload["baseline_vram_mb"] = guard_baseline
    payload["device_vram_after_unload_mb"] = device_vram_after_unload_mb
    payload["in_process_residual_mb"] = in_process_residual_mb
    payload["in_process_residual_note"] = (
        "expected non-zero for bnb-4bit; harmless — the worker process exits and "
        "the OS reclaims VRAM. Isolation is judged by vram_guard (startup), not this."
    )
    payload["free_vram_tolerance_mb"] = tolerance_mb
    payload["vram_guard"] = guard

    report = _format_report(payload)
    if json_path:
        from pathlib import Path

        p = Path(json_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2, default=str))
        payload["_json_path"] = str(p)

    return PilotResult(payload=payload, report=report)


def _short_name(model_name: str) -> str:
    return model_name.split("/")[-1]


def run_pilot_models(
    models: list[str],
    config_path: Optional[str] = None,
    n: Optional[int] = None,
    out_dir: str = "results",
    proj_scenarios: int = 1000,
) -> list[PilotResult]:
    """Benchmark several models SEQUENTIALLY, ONE SUBPROCESS PER MODEL.

    Each model runs in its own worker process (`agentmeter.worker --mode pilot`)
    that loads exactly one model, measures its own FRESH-context baseline, runs
    the same scenarios with the same config/quantization, writes
    results/pilot_<short>.json, then exits — so the OS reclaims all GPU memory and
    the next model starts clean by construction (fixes the bnb-4bit device_map
    leak that in-process unload could not). Never two workers at once. The parent
    aggregates the per-model JSONs into results/pilot_combined.json. Same pipeline
    + same scenarios for every model, so any difference is attributable to the
    model alone (Cara 1).
    """
    import subprocess
    import sys
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    proj_models = len(models)
    worker_config = str(load_config(config_path).path)
    tolerance_mb = float(
        load_config(config_path).get("run.free_vram_tolerance_mb", 500.0) or 500.0
    )

    # The fresh-context baseline is established by the FIRST worker's before-load
    # reading, then passed to every later worker so its startup-isolation guard is
    # judged against a known-clean reference (before_load ~ baseline -> clean).
    baseline_vram_mb: Optional[float] = None

    results: list[PilotResult] = []
    for i, name in enumerate(models):
        print(f"\n{'#'*70}\n# Model {i+1}/{len(models)}: {name}  (worker subprocess)\n{'#'*70}")
        out_json = out / f"pilot_{_short_name(name)}.json"
        cmd = [
            sys.executable, "-m", "agentmeter.worker",
            "--mode", "pilot",
            "--model", name,
            "--config", worker_config,
            "--out", str(out_json),
            "--proj-scenarios", str(proj_scenarios),
            "--proj-models", str(proj_models),
        ]
        if n is not None:
            cmd += ["--n", str(n)]
        if baseline_vram_mb is not None:
            cmd += ["--baseline-vram-mb", repr(float(baseline_vram_mb))]
        rc = subprocess.run(cmd).returncode  # BLOCK: strictly sequential
        if rc != 0:
            raise RuntimeError(
                f"pilot worker for model {name!r} exited with code {rc}. See its output above."
            )
        payload = json.loads(out_json.read_text())
        payload["_json_path"] = str(out_json)
        results.append(PilotResult(payload=payload, report=payload.get("_report", "")))
        # First worker establishes the fresh-context baseline for the rest.
        if baseline_vram_mb is None:
            baseline_vram_mb = payload.get("device_vram_before_load_mb")

    combined = {
        "kind": "agentmeter_pilot_combined",
        "free_vram_tolerance_mb": tolerance_mb,
        "baseline_vram_mb": baseline_vram_mb,
        "note": "One subprocess per model; each worker's vram_guard checks that it "
                "STARTED clean (before_load ~ baseline). in_process_residual_mb is a "
                "harmless diagnostic — the process exits and the OS reclaims VRAM.",
        "models": [r.payload for r in results],
        "model_labels": [r.payload.get("model_label") for r in results],
    }
    combined_path = out / "pilot_combined.json"
    combined_path.write_text(json.dumps(combined, indent=2, default=str))
    print(f"\nWrote combined results -> {combined_path}")
    print("Per-model files:")
    for r in results:
        print(f"  - {r.payload.get('_json_path')}")
    return results


def _gpu_name() -> Optional[str]:
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.get_device_name(torch.cuda.current_device())
    except Exception:
        pass
    return None


def _format_report(p: dict[str, Any]) -> str:
    def fmt(v, spec="{:.4f}"):
        return "  n/a" if v is None else spec.format(v)

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("  AgentMeter — Phase 5 PILOT RUN")
    lines.append("=" * 78)
    lines.append(f"Model            : {p['model_label']}  (provider={p['provider']}, device={p['device']})")
    lines.append(f"Quantization     : {p.get('quantization', 'none')}")
    lines.append(f"GPU              : {p['gpu_name'] or 'none'}  (cuda={p['gpu_available']})")
    if p.get("model_weight_vram_mb") is not None:
        lines.append(f"Model weights    : {p['model_weight_vram_mb']:,.1f} MB (torch allocator; load took {p['model_load_s']:.1f}s, excluded)")
    if p.get("device_vram_after_load_mb") is not None:
        before = p.get("device_vram_before_load_mb")
        before_s = f"{before:,.1f}" if before is not None else "n/a"
        lines.append(f"Device VRAM      : {before_s} MB before load -> "
                     f"{p['device_vram_after_load_mb']:,.1f} MB after (NVML; low 'before' = clean isolation)")
    lines.append(f"Agents           : {' -> '.join(p['agents'])}")
    lines.append(f"Scenarios        : {p['n_scenarios']}")
    lines.append("")

    # per (scenario, agent)
    lines.append(f"{'scenario':<10} {'agent':<9} {'wall_s':>9} {'ttft_s':>8} {'vram_mb':>9} {'in_tok':>7} {'out_tok':>8}")
    lines.append("-" * 78)
    for r in p["metrics_rows"]:
        lines.append(
            f"{r['scenario_id']:<10} {r['agent_name']:<9} {r['wall_time_s']:>9.4f} "
            f"{fmt(r['ttft_s']):>8} {fmt(r['vram_peak_mb'], '{:.1f}'):>9} "
            f"{r['input_tokens']:>7} {r['output_tokens']:>8}"
        )

    # per-agent means
    lines.append("")
    lines.append("Per-agent mean cost (across scenarios):")
    lines.append(f"{'agent':<9} {'mean_wall_s':>12} {'mean_ttft_s':>12} {'mean_vram_mb':>13} {'mean_in':>9} {'mean_out':>9}")
    lines.append("-" * 78)
    for name in p["agents"]:
        a = p["per_agent_summary"].get(name)
        if not a:
            continue
        vram = "  n/a" if math.isnan(a["mean_vram_mb"]) else f"{a['mean_vram_mb']:.1f}"
        ttft = "  n/a" if math.isnan(a["mean_ttft_s"]) else f"{a['mean_ttft_s']:.4f}"
        lines.append(
            f"{name:<9} {a['mean_wall_s']:>12.4f} {ttft:>12} {vram:>13} "
            f"{a['mean_input_tokens']:>9.1f} {a['mean_output_tokens']:>9.1f}"
        )

    # accuracy + projection
    lines.append("")
    lines.append(f"Verdicts vs ground truth : {p['correct']}/{p['n_scenarios']} correct  (pilot accuracy {p['accuracy']*100:.1f}%)")
    lines.append(f"Mean per-scenario wall   : {p['mean_scenario_wall_s']:.4f} s")
    proj = p["projection"]
    total = proj["total_seconds"]
    lines.append(f"Projection               : {proj['scenarios']} scenarios x {proj['models']} models")
    lines.append(f"   ~ {total:.0f} s = {total/60:.1f} min = {total/3600:.2f} h  (from THIS run's timings)")
    lines.append("=" * 78)
    return "\n".join(lines)
