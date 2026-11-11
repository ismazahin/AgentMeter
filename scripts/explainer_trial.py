"""Try the session explainer's models on a real GPU, once, before relying on them.

Run on a rented GPU box (the same kind your benchmarks use), with no benchmark running:

    python scripts/explainer_trial.py --run-dir <a finished session's run folder>
    python scripts/explainer_trial.py --run-dir <...> --model swiss-ai/Apertus-8B-Instruct-2509

For each model (default: every model in configs/explainer.yaml) it copies the session's
session_results.json and flows_<run>.json to a scratch folder, writes the explanation there with
the real model (never touching the session itself), and prints how long it took, how many
sections passed the checks, why any were dropped, and the text, so you can judge speed and
quality. The model must already be downloaded (HF_HUB_OFFLINE is respected).
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentmeter.session import explain as E  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, help="a finished session's run folder (has session_results.json)")
    ap.add_argument("--model", action="append", help="model id to try (repeatable); default: every configured model")
    args = ap.parse_args(argv)

    src = Path(args.run_dir)
    res_p = src / "session_results.json"
    if not res_p.is_file():
        print(f"no session_results.json in {src}", file=sys.stderr)
        return 2
    res = json.loads(res_p.read_text(encoding="utf-8"))
    cfg = E.load_config()
    specs = [m for m in cfg["models"] if not args.model or m["id"] in args.model]
    for mid in args.model or []:
        if not any(m["id"] == mid for m in specs):
            specs.append({"id": mid, "engine": "hf", "min_free_vram_gb": 0})
    print(f"Free GPU memory: {E._free_vram_gb() or 'unknown'} GB\n")
    for spec in specs:
        work = Path(tempfile.mkdtemp(prefix="am-explain-trial-"))
        shutil.copy(res_p, work)
        for f in src.glob("flows_*.json"):
            shutil.copy(f, work)
        one = copy.deepcopy(cfg)
        one["enabled"] = True
        one["models"] = [dict(spec, min_free_vram_gb=0)]
        t0 = time.time()
        out = E.explain_session(res, work, real=True, cfg=one)
        wall = time.time() - t0
        print("=" * 78)
        print(f"{spec['id']} ({spec.get('engine', 'hf')}): {out.get('status')} in {wall:.0f} s wall"
              f" (model load {out.get('load_s')} s), tokens {out.get('tokens')}")
        if out.get("reason"):
            print("reason:", out["reason"])
        if out.get("trace"):
            print("full error:\n" + out["trace"])
        for k, why in (out.get("dropped") or {}).items():
            print(f"  dropped {k}: {why}")
        for k, v in (out.get("sections") or {}).items():
            print(f"\n[{k}]\n{v}")
        print(f"\n(written to {work / E.OUT_NAME})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
