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


def run_pilot(
    config_path: Optional[str] = None,
    n: Optional[int] = None,
    proj_scenarios: int = 1000,
    proj_models: int = 2,
    json_path: Optional[str] = None,
    model_name: Optional[str] = None,
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

    provider.unload()
    free_cuda()  # fully release VRAM so the next model (if any) starts clean

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
    """Benchmark several models SEQUENTIALLY in one process (never in parallel).

    For each model: fully free the GPU, load the model, run the same scenarios
    with the same config/quantization, save results/pilot_<short>.json, then
    unload and free VRAM before the next model. Also writes a combined file
    results/pilot_combined.json. Same pipeline + same scenarios for every model,
    so any difference is attributable to the model alone (Cara 1).
    """
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    proj_models = len(models)
    results: list[PilotResult] = []

    for i, name in enumerate(models):
        print(f"\n{'#'*70}\n# Model {i+1}/{len(models)}: {name}\n{'#'*70}")
        free_cuda()  # guarantee the previous model is gone before this one loads
        res = run_pilot(
            config_path=config_path,
            n=n,
            proj_scenarios=proj_scenarios,
            proj_models=proj_models,
            json_path=str(out / f"pilot_{_short_name(name)}.json"),
            model_name=name,
        )
        print(res.report)
        results.append(res)
        free_cuda()  # and free it again before the loop continues

    combined = {
        "kind": "agentmeter_pilot_combined",
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
