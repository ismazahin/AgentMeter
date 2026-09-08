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

    args = parser.parse_args(argv)

    if args.command == "check-env":
        from agentmeter.env_check import main as check_main

        return check_main()

    if args.command == "load-data":
        return _load_data(args.show)

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


if __name__ == "__main__":
    sys.exit(main())
