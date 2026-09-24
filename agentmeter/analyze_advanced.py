"""Phase 14 — read-only analytical expansion for Chapter 4.

Extra measurement views computed ENTIRELY from the existing locked run data
(scenario_results + agent_metrics). No re-runs, no inference, no GPU. It reads the
results DB READ-ONLY and is purely additive to analysis.json (a new `advanced`
section); every existing key is left byte-for-byte unchanged.

Views (all resource / measurement framing, never a detection ranking):
  1. pareto          — accuracy vs mean latency, and accuracy vs mean tokens, with
                       Pareto-front membership (the MCDA trade-off story).
  2. latency_cov     — per-model coefficient of variation (std/mean) of end-to-end
                       latency across scenarios (lower = more predictable).
  3. prefill_decode  — per-model (and per-agent) mean TTFT (prefill) vs
                       wall - TTFT (decode) — where temporal cost concentrates.
  4. throughput      — per-model output tokens / generation (decode) seconds.
  5. misclass_cost   — per-model mean latency + tokens split by correct vs
                       incorrect predictions. RESOURCE COST ONLY — it never asks
                       why a model misclassifies or which detects better;
                       accuracy stays descriptive context.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

ADVANCED_NOTE = (
    "Read-only analytical expansion computed from the existing locked run data "
    "(no re-runs). Resource/measurement framing only. TTFT is the per-agent "
    "time-to-first-token (prefill proxy); decode = wall_time_s - ttft_s. VRAM "
    "elsewhere is marginal working memory. misclass_cost compares RESOURCE COST "
    "when right vs wrong — it does not analyse or rank why models misclassify."
)


def _scenario_tokens(am: pd.DataFrame) -> pd.DataFrame:
    a = am.copy()
    a["scenario_tokens"] = a["input_tokens"].fillna(0) + a["output_tokens"].fillna(0)
    return a.groupby(["model", "scenario_id"])["scenario_tokens"].sum().reset_index()


def _acc_lat_tok(sr: pd.DataFrame, am: pd.DataFrame) -> pd.DataFrame:
    """Per-model accuracy, mean end-to-end latency, mean total tokens, count."""
    tok = _scenario_tokens(am)
    srt = sr.merge(tok, on=["model", "scenario_id"], how="left")
    rows = []
    for m in sorted(sr["model"].unique()):
        g = srt[srt["model"] == m]
        rows.append({
            "model": m,
            "n": int(len(g)),
            "accuracy": float(g["correct"].mean()) if len(g) else None,
            "accuracy_pct": float(g["correct"].mean() * 100.0) if len(g) else None,
            "mean_latency_s": float(g["scenario_total_time_s"].mean()) if len(g) else None,
            "mean_tokens": float(g["scenario_tokens"].mean()) if len(g) else None,
        })
    return pd.DataFrame(rows)


# --- 1. Pareto frontier ------------------------------------------------

def pareto_membership(points: list[dict[str, Any]], cost_key: str) -> list[bool]:
    """A point is Pareto-optimal (maximise accuracy, minimise `cost_key`) if no
    other point dominates it: another with accuracy >= and cost <=, strictly
    better in at least one. Points with missing values are never on the front."""
    out = []
    for a in points:
        av, ac = a.get("accuracy"), a.get(cost_key)
        if av is None or ac is None:
            out.append(False)
            continue
        dominated = False
        for b in points:
            if b is a:
                continue
            bv, bc = b.get("accuracy"), b.get(cost_key)
            if bv is None or bc is None:
                continue
            if bv >= av and bc <= ac and (bv > av or bc < ac):
                dominated = True
                break
        out.append(not dominated)
    return out


def pareto(sr: pd.DataFrame, am: pd.DataFrame) -> dict[str, Any]:
    base = _acc_lat_tok(sr, am)
    pts = base.to_dict("records")
    lat_front = pareto_membership(pts, "mean_latency_s")
    tok_front = pareto_membership(pts, "mean_tokens")
    rows = []
    for r, lf, tf in zip(pts, lat_front, tok_front):
        rows.append({**r, "pareto_latency": bool(lf), "pareto_tokens": bool(tf)})
    return {
        "note": "Accuracy (higher better) vs mean latency / mean tokens (lower "
                "better). pareto_latency / pareto_tokens flag the non-dominated "
                "models on each front.",
        "points": rows,
    }


# --- 2. Latency stability (CoV) ----------------------------------------

def latency_cov(sr: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-model coefficient of variation of end-to-end latency = std/mean
    (sample std, ddof=1). Lower CoV = more predictable latency."""
    rows = []
    for m in sorted(sr["model"].unique()):
        g = sr[sr["model"] == m]["scenario_total_time_s"].astype(float)
        n = int(len(g))
        mean = float(g.mean()) if n else None
        std = float(g.std(ddof=1)) if n > 1 else (0.0 if n == 1 else None)
        cov = (std / mean) if (mean not in (None, 0) and std is not None) else None
        rows.append({"model": m, "n": n, "mean_latency_s": mean,
                     "std_latency_s": std, "cov": cov})
    return rows


# --- 3. Prefill vs decode ----------------------------------------------

def prefill_decode(am: pd.DataFrame) -> dict[str, Any]:
    """Per-model and per-(model,agent) mean prefill (TTFT) vs decode (wall-TTFT).

    TTFT may be NULL on non-GPU rows; treated as 0 there (all time is decode)."""
    a = am.copy()
    a["ttft_s"] = a["ttft_s"].fillna(0.0)
    a["decode_s"] = (a["wall_time_s"].fillna(0.0) - a["ttft_s"]).clip(lower=0.0)

    # per (model, scenario): sum across the agents, then mean across scenarios
    per_scn = (a.groupby(["model", "scenario_id"])
                 .agg(prefill_s=("ttft_s", "sum"), decode_s=("decode_s", "sum"))
                 .reset_index())
    per_model = []
    for m in sorted(a["model"].unique()):
        g = per_scn[per_scn["model"] == m]
        per_model.append({"model": m, "n": int(len(g)),
                          "mean_prefill_s": float(g["prefill_s"].mean()) if len(g) else None,
                          "mean_decode_s": float(g["decode_s"].mean()) if len(g) else None})

    agg = (a.groupby(["model", "agent_name"])
             .agg(mean_prefill_s=("ttft_s", "mean"),
                  mean_decode_s=("decode_s", "mean"),
                  n=("wall_time_s", "size"))
             .reset_index())
    per_agent = [{"model": r["model"], "agent_name": r["agent_name"], "n": int(r["n"]),
                  "mean_prefill_s": float(r["mean_prefill_s"]),
                  "mean_decode_s": float(r["mean_decode_s"])}
                 for _, r in agg.iterrows()]
    return {"per_model": per_model, "per_agent": per_agent}


# --- 4. Output throughput ----------------------------------------------

def throughput(am: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-model output tokens per second = total output tokens / total decode
    (generation) seconds, plus per-scenario means for context."""
    a = am.copy()
    a["ttft_s"] = a["ttft_s"].fillna(0.0)
    a["decode_s"] = (a["wall_time_s"].fillna(0.0) - a["ttft_s"]).clip(lower=0.0)
    a["output_tokens"] = a["output_tokens"].fillna(0)

    per_scn = (a.groupby(["model", "scenario_id"])
                 .agg(out=("output_tokens", "sum"), decode=("decode_s", "sum"))
                 .reset_index())
    rows = []
    for m in sorted(a["model"].unique()):
        gm = a[a["model"] == m]
        total_out = float(gm["output_tokens"].sum())
        total_decode = float(gm["decode_s"].sum())
        gs = per_scn[per_scn["model"] == m]
        rows.append({
            "model": m,
            "n": int(len(gs)),
            "total_output_tokens": total_out,
            "total_decode_s": total_decode,
            "tokens_per_s": (total_out / total_decode) if total_decode > 0 else None,
            "mean_output_tokens": float(gs["out"].mean()) if len(gs) else None,
            "mean_decode_s": float(gs["decode"].mean()) if len(gs) else None,
        })
    return rows


# --- 5. Misclassification RESOURCE cost (cost only) --------------------

def misclass_cost(sr: pd.DataFrame, am: pd.DataFrame) -> dict[str, Any]:
    """Per-model mean latency + mean tokens split by correct (TP/TN) vs incorrect
    (FP/FN) predictions. RESOURCE COST ONLY — descriptive, not a ranking."""
    tok = _scenario_tokens(am)
    srt = sr.merge(tok, on=["model", "scenario_id"], how="left")
    rows = []
    for m in sorted(sr["model"].unique()):
        g = srt[srt["model"] == m]
        for label, mask in (("correct", g["correct"] == 1), ("incorrect", g["correct"] == 0)):
            gg = g[mask]
            rows.append({
                "model": m, "group": label, "n": int(len(gg)),
                "mean_latency_s": float(gg["scenario_total_time_s"].mean()) if len(gg) else None,
                "mean_tokens": float(gg["scenario_tokens"].mean()) if len(gg) else None,
            })
    return {
        "note": "RESOURCE cost when a model is right (correct = TP/TN) vs wrong "
                "(incorrect = FP/FN). Cost comparison only — not an analysis of why "
                "models misclassify or which detects better.",
        "rows": rows,
    }


# --- assembly ----------------------------------------------------------

def aggregate_advanced(sr: pd.DataFrame, am: pd.DataFrame) -> dict[str, Any]:
    return {
        "note": ADVANCED_NOTE,
        "pareto": pareto(sr, am),
        "latency_cov": latency_cov(sr),
        "prefill_decode": prefill_decode(am),
        "throughput": throughput(am),
        "misclass_cost": misclass_cost(sr, am),
    }


def run_advanced(config_path: Optional[str] = None,
                 db_path: Optional[str] = None) -> dict[str, Any]:
    """Read the locked results DB READ-ONLY and return the `advanced` section."""
    from . import analyze
    from .config import load_config

    cfg = load_config(config_path)
    if db_path is None:
        db_path = str(cfg.resolve_path("storage.sqlite_path", "results/agentmeter.db"))
    db = Path(db_path)
    if not db.exists():
        raise FileNotFoundError(f"Results DB not found: {db}")

    conn = analyze._connect_ro(db)
    try:
        run_ids = analyze._complete_run_ids(conn)
        if not run_ids:
            raise ValueError("No runs with status='complete' in the DB.")
        qmarks = ",".join("?" * len(run_ids))
        sr = pd.read_sql_query(
            f"SELECT * FROM scenario_results WHERE status='complete' AND run_id IN ({qmarks})",
            conn, params=run_ids)
        am = pd.read_sql_query(
            f"SELECT * FROM agent_metrics WHERE run_id IN ({qmarks})", conn, params=run_ids)
    finally:
        conn.close()
    keep = set(zip(sr["model"], sr["scenario_id"]))
    am = am[[(mm, ss) in keep for mm, ss in zip(am["model"], am["scenario_id"])]].copy()
    return aggregate_advanced(sr, am)
