"""AgentMeter CLI entry point.

Subcommands are added phase by phase. Phase 0 provides:
    python main.py check-env      # confirm the environment (no GPU needed)
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentmeter", description="AgentMeter harness")
    parser.add_argument("--config", type=str, default=None, help="path to a config.yaml (default: ./config.yaml)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check-env", help="Phase 0: verify the environment (no GPU required)")

    p_load = sub.add_parser("load-data", help="Phase 1: load dataset, show isolation + a sample prompt")
    p_load.add_argument("--show", type=int, default=1, help="how many sample scenarios to print")

    p_run = sub.add_parser("run-pipeline", help="Phase 2: run the 4-agent pipeline (mock model)")
    p_run.add_argument("--n", type=int, default=1, help="how many scenarios to run")

    p_bench = sub.add_parser("bench", help="Phase 3: run instrumented pipeline, print per-agent metrics")
    p_bench.add_argument("--n", type=int, default=None, help="scenarios to run (default: dataset.limit)")
    p_bench.add_argument("--json", type=str, default=None, help="write raw metrics to this JSON path")
    p_bench.add_argument("--proj-scenarios", type=int, default=1000, help="projection: total scenarios")
    p_bench.add_argument("--proj-models", type=int, default=2, help="projection: number of models")

    p_pilot = sub.add_parser("pilot", help="Phase 5: pilot run (1 real model on GPU, 5-10 scenarios)")
    p_pilot.add_argument("--n", type=int, default=None, help="scenarios to run (default: dataset.limit)")
    p_pilot.add_argument("--json", type=str, default="results/pilot.json", help="write pilot payload here")
    p_pilot.add_argument("--proj-scenarios", type=int, default=1000, help="projection: total scenarios")
    p_pilot.add_argument("--proj-models", type=int, default=2, help="projection: number of models")

    args = parser.parse_args(argv)

    if args.command == "check-env":
        from agentmeter.env_check import check_environment, format_report
        from agentmeter.config import load_config

        report = check_environment(load_config(args.config))
        print(format_report(report))
        return 0 if report.ok else 1

    if args.command == "load-data":
        return _load_data(args.show, args.config)

    if args.command == "run-pipeline":
        return _run_pipeline(args.n, args.config)

    if args.command == "bench":
        return _bench(args.n, args.json, args.proj_scenarios, args.proj_models, args.config)

    if args.command == "pilot":
        from agentmeter.pilot import run_pilot

        try:
            result = run_pilot(
                config_path=args.config,
                n=args.n,
                proj_scenarios=args.proj_scenarios,
                proj_models=args.proj_models,
                json_path=args.json,
            )
        except RuntimeError as e:
            print(f"\n{e}\n", file=sys.stderr)
            return 1
        print(result.report)
        if result.payload.get("_json_path"):
            print(f"\nWrote pilot payload -> {result.payload['_json_path']}")
        return 0

    parser.print_help()
    return 0


def _load_data(show: int, config_path: str | None = None) -> int:
    from agentmeter.config import load_config
    from agentmeter.dataset import DatasetLoader

    cfg = load_config(config_path)
    loader = DatasetLoader(cfg)
    scenarios = loader.load()
    summary = loader.summarize(scenarios)

    print("=" * 62)
    print("  AgentMeter — Phase 1: Dataset Loader + Isolation")
    print("=" * 62)
    print(f"Dataset          : {summary['dataset_path']}")
    print(f"Scenarios loaded : {summary['n_scenarios']}")
    print(f"Label column     : {loader.label_column} (stripped from model view)")
    print(f"Label distribution:")
    for lbl, n in summary["label_distribution"].items():
        print(f"    {lbl:<18} {n}")
    if summary["labels_not_in_config_classes"]:
        print(f"WARNING: labels not in config.classes: {summary['labels_not_in_config_classes']}")
    print("")

    for s in scenarios[: max(0, show)]:
        leaked = loader.label_column.lower() in s.feature_prompt.lower() or (
            s.held_out_label.lower() in s.feature_prompt.lower()
        )
        print("-" * 62)
        print(f"scenario_id      : {s.scenario_id}")
        print(f"MODEL SEES (feature-only prompt):")
        for line in s.feature_prompt.splitlines():
            print(f"    {line}")
        print(f"HELD-OUT LABEL (memory only, hidden from model): {s.held_out_label}")
        print(f"isolation check  : {'LEAK DETECTED!' if leaked else 'OK (label not in prompt)'}")
    print("-" * 62)
    return 0


def _run_pipeline(n: int, config_path: str | None = None) -> int:
    from agentmeter.config import load_config
    from agentmeter.dataset import DatasetLoader
    from agentmeter.pipeline import Pipeline
    from agentmeter.providers import get_provider

    cfg = load_config(config_path)
    provider = get_provider(cfg)
    provider.load()
    pipeline = Pipeline(cfg, provider)
    scenarios = DatasetLoader(cfg).load()

    print("=" * 62)
    print("  AgentMeter — Phase 2: Linear 4-Agent Pipeline (mock model)")
    print("=" * 62)
    print(f"Provider : {provider.name}")
    print(f"Agents   : {' -> '.join(pipeline.agent_names)}")
    print("")

    correct = 0
    run_n = min(n, len(scenarios))
    for s in scenarios[:run_n]:
        state = pipeline.run(s.scenario_id, s.feature_prompt)
        verdict = state.get("verdict", {})
        pred = verdict.get("predicted_class", "?")
        ok = pred == s.held_out_label
        correct += int(ok)
        print("-" * 62)
        print(f"scenario_id   : {s.scenario_id}")
        print(f"  Perceive    : {state.get('perceive', '')}")
        print(f"  Reason      : {state.get('reason', '')}")
        print(f"  Decide      : {state.get('decide', '')}")
        print(f"  Act verdict : class={pred} | mitre={verdict.get('mitre_technique')}")
        print(f"  ground truth: {s.held_out_label}  -> {'CORRECT' if ok else 'wrong'}")
    print("-" * 62)
    print(f"Pipeline produced verdicts for {run_n} scenario(s). "
          f"Mock agreement with ground truth: {correct}/{run_n} "
          f"(mock heuristic only — not a real model).")
    provider.unload()
    return 0


def _bench(n, json_path, proj_scenarios, proj_models, config_path: str | None = None) -> int:
    import json as _json
    import math
    from pathlib import Path

    from agentmeter.config import load_config
    from agentmeter.dataset import DatasetLoader
    from agentmeter.instrument import GpuProbe, MetricsCollector, make_instrumented_hook
    from agentmeter.pipeline import Pipeline
    from agentmeter.providers import get_provider

    cfg = load_config(config_path)
    provider = get_provider(cfg)
    provider.load()

    model_label = (
        cfg.get("model.name") if cfg.get("model.provider") == "hf" else f"mock:{provider.name}"
    )
    collector = MetricsCollector()
    gpu = GpuProbe()
    hook = make_instrumented_hook(collector, model_label, gpu)
    pipeline = Pipeline(cfg, provider, node_hook=hook)

    scenarios = DatasetLoader(cfg).load()
    if n is not None:
        scenarios = scenarios[:n]

    print("=" * 72)
    print("  AgentMeter — Phase 3: Per-Agent Instrumentation")
    print("=" * 72)
    print(f"Model label   : {model_label}")
    print(f"GPU / VRAM     : {'available (torch.cuda)' if gpu.available else 'NOT available -> vram_peak_mb = None (expected on CPU/mock)'}")
    weight_vram = getattr(provider, "model_vram_mb", None)
    if weight_vram is not None:
        print(f"Model weights  : {weight_vram:,.1f} MB resident VRAM (excluded from per-agent deltas)")
    if getattr(provider, "device", None) is not None:
        print(f"Device         : {provider.device}")
    print(f"Agents         : {' -> '.join(pipeline.agent_names)}")
    print(f"Scenarios      : {len(scenarios)}")
    print("")

    for s in scenarios:
        pipeline.run(s.scenario_id, s.feature_prompt)

    # --- per (scenario, agent) rows ---
    def fmt(v, spec="{:.4f}"):
        return "  n/a" if v is None else spec.format(v)

    print(f"{'scenario':<10} {'agent':<9} {'wall_s':>9} {'ttft_s':>8} {'vram_mb':>9} {'in_tok':>7} {'out_tok':>8}")
    print("-" * 72)
    for r in collector.rows:
        print(f"{r.scenario_id:<10} {r.agent_name:<9} {r.wall_time_s:>9.4f} "
              f"{fmt(r.ttft_s, '{:.4f}'):>8} {fmt(r.vram_peak_mb, '{:.1f}'):>9} "
              f"{r.input_tokens:>7} {r.output_tokens:>8}")

    # --- per-agent means ---
    print("")
    print("Per-agent mean cost (across scenarios):")
    print(f"{'agent':<9} {'mean_wall_s':>12} {'mean_ttft_s':>12} {'mean_vram_mb':>13} {'mean_in':>9} {'mean_out':>9}")
    print("-" * 72)
    summary = collector.per_agent_summary()
    for name in pipeline.agent_names:
        a = summary.get(name)
        if not a:
            continue
        vram = "  n/a" if math.isnan(a["mean_vram_mb"]) else f"{a['mean_vram_mb']:.1f}"
        ttft = "  n/a" if math.isnan(a["mean_ttft_s"]) else f"{a['mean_ttft_s']:.4f}"
        print(f"{name:<9} {a['mean_wall_s']:>12.4f} {ttft:>12} {vram:>13} "
              f"{a['mean_input_tokens']:>9.1f} {a['mean_output_tokens']:>9.1f}")

    # --- totals + projection ---
    mean_scn = collector.mean_scenario_wall_s()
    print("")
    print(f"Mean per-scenario wall time : {mean_scn:.4f} s")
    total_proj = mean_scn * proj_scenarios * proj_models
    print(f"Projection (illustrative)   : {proj_scenarios} scenarios x {proj_models} models")
    print(f"   ~ {total_proj:.1f} s  = {total_proj/60:.1f} min = {total_proj/3600:.2f} h")
    print("(Projection uses THIS run's timings. On CPU/mock these are tiny and")
    print(" not representative — real numbers come from the Phase 5 GPU pilot.)")

    if json_path:
        out = Path(json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model_label": model_label,
            "gpu_available": gpu.available,
            "rows": [r.as_dict() for r in collector.rows],
            "per_agent_summary": summary,
            "mean_scenario_wall_s": mean_scn,
        }
        out.write_text(_json.dumps(payload, indent=2, default=str))
        print(f"\nWrote raw metrics -> {out}")

    provider.unload()
    return 0


if __name__ == "__main__":
    sys.exit(main())
