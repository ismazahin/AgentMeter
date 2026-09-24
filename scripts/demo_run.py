"""Phase 15 — live single-scenario demo (CLI).

Loads ONE of the fixed 5 models (4-bit NF4, via the EXISTING HFProvider), pulls
ONE scenario from the EXISTING 300-scenario dataset, and runs it through the
EXISTING instrumented 4-agent pipeline (Perceive -> Reason -> Decide -> Act),
streaming each agent's output live so a viewer sees the chain-of-thought step by
step. At the end it prints per-agent + end-to-end latency, VRAM (marginal working
memory) and token counts, plus predicted vs true class.

This is a DEMO, not a benchmark: it writes NOTHING to any DB, runs exactly ONE
scenario per invocation, reuses (never reimplements) the pipeline / provider /
instrumentation, and enforces require_gpu (no silent CPU) for the live HF run.

    python scripts/demo_run.py --model Qwen/Qwen2.5-7B-Instruct --class "Port Scanning"
    python scripts/demo_run.py --model Qwen/Qwen2.5-7B-Instruct --scenario row_0042
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agentmeter.config import load_config  # noqa: E402

DEFAULT_CONFIG = str(REPO_ROOT / "configs" / "run_full_l4.yaml")


def _pick_scenario(scenarios, scenario_id: Optional[str], klass: Optional[str]):
    """Select EXACTLY ONE scenario by id or by true class (first match)."""
    if scenario_id:
        matches = [s for s in scenarios if s.scenario_id == scenario_id]
        if not matches:
            raise ValueError(f"scenario id {scenario_id!r} not found in the dataset "
                             f"({len(scenarios)} scenarios).")
        return matches[0]
    if klass:
        matches = [s for s in scenarios if s.held_out_label == klass]
        if not matches:
            raise ValueError(f"no scenario with true class {klass!r} in the dataset.")
        return matches[0]
    raise ValueError("provide exactly one of --scenario <id> or --class <class>.")


def _make_stream_hook(inner_hook, collector, stream):
    """Wrap the instrumented hook to stream each agent's output + live metrics as
    it completes (agents run sequentially, so this is step-by-step CoT)."""
    def hook(name, agent_fn, state, provider, config):
        stream("")
        stream("── " + name.upper() + " ──")
        update = inner_hook(name, agent_fn, state, provider, config)  # runs + records
        text = (update.get(name) or "").strip()
        stream(text if text else "(no text output)")
        m = collector.rows[-1] if collector.rows else None
        if m is not None:
            vram = f"{m.vram_peak_mb:.1f} MB" if m.vram_peak_mb is not None else "n/a (CPU)"
            ttft = f"{m.ttft_s:.3f} s" if m.ttft_s is not None else "n/a"
            stream(f"   [{name}: {m.wall_time_s:.3f} s wall · ttft {ttft} · vram {vram} · "
                   f"tokens in {m.input_tokens} / out {m.output_tokens}]")
        return update
    return hook


def run_demo(
    model: str,
    scenario_id: Optional[str] = None,
    klass: Optional[str] = None,
    config_path: str = DEFAULT_CONFIG,
    get_provider: Optional[Callable[[Any], Any]] = None,
    stream: Callable[[str], None] = print,
    save: Optional[str] = None,
) -> dict[str, Any]:
    """Run ONE scenario through the real instrumented pipeline. Returns a summary
    dict. Writes nothing to any DB. `get_provider` is injectable for CPU tests."""
    from agentmeter.dataset import DatasetLoader
    from agentmeter.instrument import GpuProbe, MetricsCollector, make_instrumented_hook
    from agentmeter.pilot import _gpu_guard, release_provider
    from agentmeter.pipeline import Pipeline
    from agentmeter.providers import get_provider as default_get_provider
    from agentmeter.runner import model_label, resolve_models

    cfg = load_config(config_path)

    # Reject any model outside the fixed set (from the study config's run.models).
    fixed = resolve_models(cfg)
    if model not in fixed:
        raise ValueError(
            f"model {model!r} is not one of the fixed {len(fixed)} models: {fixed}. "
            "The demo only runs the locked model set.")

    classes = list(cfg.get("classes", []) or [])
    if klass and klass not in classes:
        raise ValueError(f"class {klass!r} is not one of the {len(classes)} locked "
                         f"classes: {classes}.")

    cfg.data.setdefault("model", {})["name"] = model
    label = model_label(cfg)

    scenarios = DatasetLoader(cfg).load()
    scn = _pick_scenario(scenarios, scenario_id, klass)   # EXACTLY ONE

    # Live HF demo must run on GPU — no silent CPU fallback (mock config skips this).
    _gpu_guard(cfg)

    stream("=" * 70)
    stream(f"  AgentMeter — live single-scenario demo")
    stream(f"  Model    : {model}")
    stream(f"  Scenario : {scn.scenario_id}   (true class hidden from the model)")
    stream("=" * 70)
    stream("Feature prompt (what the model sees):")
    stream(scn.feature_prompt)

    gp = get_provider or default_get_provider
    provider = gp(cfg)
    provider.load()

    gpu = GpuProbe()
    collector = MetricsCollector()
    inner = make_instrumented_hook(collector, label, gpu)
    hook = _make_stream_hook(inner, collector, stream)
    pipeline = Pipeline(cfg, provider, node_hook=hook)

    try:
        state = pipeline.run(scn.scenario_id, scn.feature_prompt)
    finally:
        release_provider(provider)

    verdict = state.get("verdict", {}) or {}
    pred = verdict.get("predicted_class", "Unparseable")
    rows = collector.rows
    total_latency = sum(r.wall_time_s for r in rows)
    vram_vals = [r.vram_peak_mb for r in rows if r.vram_peak_mb is not None]
    peak_vram = max(vram_vals) if vram_vals else None
    tok_in = sum(r.input_tokens for r in rows)
    tok_out = sum(r.output_tokens for r in rows)

    result = {
        "model": model,
        "scenario_id": scn.scenario_id,
        "true_class": scn.held_out_label,
        "predicted_class": pred,
        "correct": bool(pred == scn.held_out_label),
        "total_latency_s": total_latency,
        "peak_vram_mb": peak_vram,
        "input_tokens": tok_in,
        "output_tokens": tok_out,
        "per_agent": [
            {"agent": r.agent_name, "wall_s": r.wall_time_s, "ttft_s": r.ttft_s,
             "vram_peak_mb": r.vram_peak_mb, "input_tokens": r.input_tokens,
             "output_tokens": r.output_tokens}
            for r in rows
        ],
    }

    stream("")
    stream("-" * 70)
    stream("  RESULT")
    stream(f"  Predicted : {pred}")
    stream(f"  True      : {scn.held_out_label}   ->  {'CORRECT' if result['correct'] else 'INCORRECT'}")
    stream(f"  Latency   : {total_latency:.3f} s end-to-end")
    stream(f"  VRAM      : {('%.1f MB (marginal working memory)' % peak_vram) if peak_vram is not None else 'n/a (CPU)'}")
    stream(f"  Tokens    : {tok_in} in / {tok_out} out")
    stream("  Per-agent :")
    for r in rows:
        vr = f"{r.vram_peak_mb:.1f} MB" if r.vram_peak_mb is not None else "n/a"
        stream(f"    {r.agent_name:<9} {r.wall_time_s:>7.3f} s   vram {vr:>10}   "
               f"tok {r.input_tokens:>4}/{r.output_tokens:<4}")
    stream("-" * 70)
    stream("(demo only — nothing was written to any results DB)")

    if save:
        Path(save).parent.mkdir(parents=True, exist_ok=True)
        Path(save).write_text(json.dumps(result, indent=2, default=str))
        stream(f"Saved a throwaway copy to {save}")

    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AgentMeter live single-scenario demo (one model, one scenario, no DB writes).")
    ap.add_argument("--model", required=True, help="one of the fixed 5 study models")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--scenario", default=None, help="a scenario id, e.g. row_0042")
    g.add_argument("--class", dest="klass", default=None, help="a class name; runs the first scenario of that class")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="study config (default run_full_l4.yaml)")
    ap.add_argument("--save", default=None, help="optional throwaway JSON path for the result")
    args = ap.parse_args(argv)

    try:
        run_demo(model=args.model, scenario_id=args.scenario, klass=args.klass,
                 config_path=args.config, save=args.save)
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)   # e.g. require_gpu with no CUDA
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
