"""Benchmark a prepared user run (from scripts/ingest.py) with up to 2 models.

    # CPU smoke test (mock provider, no GPU):
    python scripts/benchmark.py results/csv_runs/<name> --models mock-a mock-b --provider mock

    # Colab A100, real models (hf + 4-bit NF4 + require_gpu from the study config):
    python scripts/benchmark.py results/csv_runs/<name> \\
        --models mistralai/Mistral-7B-Instruct-v0.3 Qwen/Qwen2.5-7B-Instruct \\
        --base-config configs/run_full_l4.yaml

Models run SEQUENTIALLY, one subprocess each (the existing runner). Results go to
<run_dir>/session.db and <run_dir>/session_results.json — never the locked study
DB. Re-running the same command after a crash resumes; --fresh starts over.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agentmeter.ingest.unified import InputContractError  # noqa: E402
from agentmeter.session.benchmark import RESULTS_JSON, SessionError, run_session  # noqa: E402


def _fmt(v, spec="{:.4f}"):
    return "—" if v is None else spec.format(v)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir", help="results/csv_runs/<name> or results/pcap_runs/<name>")
    ap.add_argument("--models", nargs="+", required=True, help="1 or 2 model ids")
    ap.add_argument("--base-config", default=None,
                    help="config to derive the session from (default config.yaml; "
                         "configs/run_full_l4.yaml for hf + 4-bit NF4 on a GPU)")
    ap.add_argument("--provider", choices=["mock", "hf"], default=None,
                    help="override model.provider from the base config")
    ap.add_argument("--fresh", action="store_true", help="abandon an incomplete session and restart")
    args = ap.parse_args(argv)

    try:
        p = run_session(args.run_dir, args.models, base_config=args.base_config,
                        provider=args.provider, fresh=args.fresh)
    except (SessionError, InputContractError, FileNotFoundError) as e:
        print(f"Rejected: {e}", file=sys.stderr)
        return 2

    s = p["session"]
    print("")
    print(f"Session   {s['run_id']} · {s['provider']} · {s['hardware']} · quant {s['quant']}")
    print(f"Input     {s['input_role']} -> {s['evaluation_mode']} · {s['class_scheme']} "
          f"({', '.join(s['class_set'])}) · {s['n_flows']} flows")
    for m in p["per_model"]:
        e = m["efficiency"]["end_to_end"]
        acc = m["accuracy"]
        print(f"  {m['model']}")
        print(f"    latency {_fmt(e['mean_latency_s'])} s/flow · tokens {_fmt(e['mean_tokens_per_flow'], '{:.0f}')}"
              f" · peak VRAM {_fmt(e['mean_peak_vram_mb'], '{:.0f}')} MB"
              f" · SAW {m['saw']['composite_100']} ({m['saw']['tier']})"
              + (f" · accuracy {acc['accuracy']:.1%}" if acc else " · accuracy n/a"))
        print("    per agent (mean wall s): " + ", ".join(
            f"{a} {_fmt(v['mean_wall_s'])}" for a, v in m["efficiency"]["per_agent"].items()))
    if p["comparison"]:
        print(f"Compare   {p['comparison']['statement']}")
    for c in s["caveats"]:
        print(f"  ! {c}")
    print(f"Wrote     {Path(s['run_dir']) / RESULTS_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
