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

    args = parser.parse_args(argv)

    if args.command == "check-env":
        from agentmeter.env_check import main as check_main

        return check_main()

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
