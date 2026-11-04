#!/usr/bin/env python3
"""Calibrate the SAW targets from a real GPU study run.

The SAW normalises each cost criterion as clamp(target / value, 0, 1). Until calibrated, the
targets in config.yaml are placeholders. This script reads one or more session results
(session_results*.json of REAL GPU sessions on the same GPU) and proposes targets by a declared
rule, then prints the YAML to paste into config.yaml (or writes it with --write).

Rules (pick one and declare it in the thesis):
  best    target = the best (lowest) measured value among the models. This is the standard SAW
          normalisation for a cost criterion (min / value): the most efficient model scores 1.0
          and every other model is scored relative to it. Default.
  median  target = the median measured value: models at or better than the median score 1.0.
          Kinder, but saturates the top half (it separates the models less).

Usage:
  python scripts/calibrate_targets.py results/<session>/session_results_<run>.json [...]
  python scripts/calibrate_targets.py <files> --rule median
  python scripts/calibrate_targets.py <files> --write config.yaml
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CRITERIA = {"latency_s": "latency_s", "vram_mb": "vram_mb", "tokens_total": "tokens_total"}


def collect(paths: list[Path], allow_mock: bool = False) -> tuple[dict[str, dict[str, float]], set[str]]:
    """Per model, the measured value of each criterion; and the GPUs the sessions ran on."""
    per: dict[str, dict[str, float]] = {}
    gpus: set[str] = set()
    for p in paths:
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        env = d.get("environment") or {}
        if env.get("provider") != "real" and not allow_mock:
            raise SystemExit(f"{p}: not a real GPU session (provider {env.get('provider')!r}); "
                             "targets must come from real measurements (--allow-mock only for testing)")
        gpus.add(str(env.get("gpu_name") or "unknown GPU"))
        for r in (d.get("phase8") or {}).get("raw_criteria") or []:
            row = per.setdefault(r["model"], {})
            for k in CRITERIA:
                v = r.get(k)
                if v is not None and v == v:              # not None, not NaN
                    row.setdefault(k, float(v))
    if not per:
        raise SystemExit("no measured models in the given results")
    return per, gpus


def propose(per: dict[str, dict[str, float]], rule: str) -> dict[str, float]:
    out = {}
    for k in CRITERIA:
        vals = [m[k] for m in per.values() if k in m]
        if not vals:
            continue
        v = min(vals) if rule == "best" else statistics.median(vals)
        out[k] = round(v, 4 if k == "latency_s" else 1)
    return out


def write_targets(config: Path, targets: dict[str, float], source: str) -> None:
    """Replace the numbers under scoring.targets (keeps comments and every other line)."""
    lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
    out, in_targets = [], False
    for ln in lines:
        s = ln.strip()
        if ln.startswith("  targets_source:"):
            ln = f"  targets_source: {json.dumps(source)}\n"
        elif ln.startswith("  targets:"):
            in_targets = True
        elif in_targets and ln.startswith("    ") and ":" in s:
            key = s.split(":", 1)[0]
            if key in targets:
                comment = ln[ln.index("#"):].rstrip("\n") if "#" in ln else ""
                ln = f"    {key}: {targets[key]}" + (f"     {comment}" if comment else "") + "\n"
        elif in_targets and not ln.startswith("    "):
            in_targets = False
        out.append(ln)
    config.write_text("".join(out), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", type=Path, help="session_results*.json of real GPU sessions")
    ap.add_argument("--rule", choices=("best", "median"), default="best")
    ap.add_argument("--write", type=Path, help="config file whose scoring.targets to update")
    ap.add_argument("--allow-mock", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    per, gpus = collect(a.results, a.allow_mock)
    if len(gpus) > 1:
        print(f"refusing: the sessions ran on different GPUs ({', '.join(sorted(gpus))}); "
              "calibrate on one GPU", file=sys.stderr)
        return 2
    targets = propose(per, a.rule)
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    source = (f"calibrated {when}, rule={a.rule}, {len(per)} models on {next(iter(gpus))}, "
              f"from {len(a.results)} session(s)")

    print("Measured (per model):")
    for m, row in sorted(per.items()):
        print(f"  {m}: " + ", ".join(f"{k}={v:g}" for k, v in row.items()))
    print(f"\nProposed targets (rule: {a.rule}):")
    print(f"  targets_source: {json.dumps(source)}")
    print("  targets:")
    for k, v in targets.items():
        print(f"    {k}: {v}")
    print("    (accuracy_pct is unchanged: the efficiency-only SAW does not use it)")
    if a.write:
        write_targets(a.write, targets, source)
        print(f"\nwrote {a.write}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
