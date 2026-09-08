"""AgentMeter CLI entry point.

Subcommands are added phase by phase. Phase 0 provides:
    python main.py check-env      # confirm the environment (no GPU needed)
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentmeter", description="AgentMeter harness")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check-env", help="Phase 0: verify the environment (no GPU required)")

    p_load = sub.add_parser("load-data", help="Phase 1: load dataset, show isolation + a sample prompt")
    p_load.add_argument("--show", type=int, default=1, help="how many sample scenarios to print")

    p_run = sub.add_parser("run-pipeline", help="Phase 2: run the 4-agent pipeline (mock model)")
    p_run.add_argument("--n", type=int, default=1, help="how many scenarios to run")

    args = parser.parse_args(argv)

    if args.command == "check-env":
        from agentmeter.env_check import main as check_main

        return check_main()

    if args.command == "load-data":
        return _load_data(args.show)

    if args.command == "run-pipeline":
        return _run_pipeline(args.n)

    parser.print_help()
    return 0


def _load_data(show: int) -> int:
    from agentmeter.config import load_config
    from agentmeter.dataset import DatasetLoader

    cfg = load_config()
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


def _run_pipeline(n: int) -> int:
    from agentmeter.config import load_config
    from agentmeter.dataset import DatasetLoader
    from agentmeter.pipeline import Pipeline
    from agentmeter.providers import get_provider

    cfg = load_config()
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


if __name__ == "__main__":
    sys.exit(main())
