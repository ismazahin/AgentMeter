"""Phase 13 — per-attack-class RESOURCE breakdown (READ-ONLY).

Aggregates the EXISTING per-scenario measurements from the locked study by the
scenario's true attack class (scenario_results.held_out_label), reporting how
RESOURCE COST — latency, VRAM (marginal working memory), token usage — differs
across the 5 locked classes, per model.

This is measurement reporting, NOT detection quality: accuracy per class is carried
as a plain secondary field only, never ranked. Nothing here re-runs the pipeline,
recomputes the SAW ranking/statistics, or invents classes — it only groups rows
that already exist. It is purely additive to analysis.json (a new `per_class`
section); every existing key is left byte-for-byte unchanged.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pandas as pd

# VRAM here is the same MARGINAL working-memory figure used elsewhere in the
# analysis (scenario_peak_vram_mb) — peak per scenario, EXCLUDES model weights.
PER_CLASS_NOTE = (
    "RESOURCE cost per attack class (not detection quality). latency = "
    "scenario_total_time_s; vram = mean scenario_peak_vram_mb (MARGINAL working "
    "memory, excludes weights); tokens = per-scenario input+output summed over the "
    "4 agents. accuracy is a plain secondary field (mean of the stored `correct` "
    "flag), never a ranking. Grouped by the scenario's true class "
    "(scenario_results.held_out_label). No classes are invented; empty (model, "
    "class) cells report n=0 with null stats."
)


def _scenario_tokens(am: pd.DataFrame) -> pd.DataFrame:
    """Per-(model, scenario) total tokens = sum over agents of input+output."""
    a = am.copy()
    a["scenario_tokens"] = a["input_tokens"].fillna(0) + a["output_tokens"].fillna(0)
    return (a.groupby(["model", "scenario_id"])["scenario_tokens"].sum()
             .reset_index())


def per_class_table(sr: pd.DataFrame, am: pd.DataFrame, classes: list[str]) -> list[dict[str, Any]]:
    """One row per (model, class): resource cost aggregates + scenario count.

    `classes` is the LOCKED class list (from config) — every model is reported
    against exactly these classes, in this order; none are added or merged."""
    tok = _scenario_tokens(am)
    srt = sr.merge(tok, on=["model", "scenario_id"], how="left")
    rows: list[dict[str, Any]] = []
    for m in sorted(sr["model"].unique()):
        for c in classes:
            g = srt[(srt["model"] == m) & (srt["held_out_label"] == c)]
            n = int(len(g))
            row: dict[str, Any] = {"model": m, "class": c, "n": n}
            if n:
                row.update({
                    "mean_latency_s": float(g["scenario_total_time_s"].mean()),
                    "median_latency_s": float(g["scenario_total_time_s"].median()),
                    "mean_vram_mb": float(g["scenario_peak_vram_mb"].mean()),
                    "median_vram_mb": float(g["scenario_peak_vram_mb"].median()),
                    "mean_tokens": float(g["scenario_tokens"].mean()),
                    "median_tokens": float(g["scenario_tokens"].median()),
                    "accuracy": float(g["correct"].mean()),   # secondary context only
                })
            else:
                for k in ("mean_latency_s", "median_latency_s", "mean_vram_mb",
                          "median_vram_mb", "mean_tokens", "median_tokens", "accuracy"):
                    row[k] = None
            rows.append(row)
    return rows


def per_class_per_agent_table(sr: pd.DataFrame, am: pd.DataFrame) -> list[dict[str, Any]]:
    """One row per (model, class, agent): mean per-agent latency + marginal VRAM,
    so the per-agent dominance pattern can be inspected per class. Attaches the
    class to each agent row via its scenario_result (no recomputation)."""
    if am.empty:
        return []
    key = sr[["model", "scenario_id", "held_out_label"]].drop_duplicates()
    amc = am.merge(key, on=["model", "scenario_id"], how="inner")
    if amc.empty:
        return []
    agg = (amc.groupby(["model", "held_out_label", "agent_name"])
              .agg(n=("wall_time_s", "size"),
                   mean_wall_s=("wall_time_s", "mean"),
                   mean_vram_delta_mb=("vram_delta_mb", "mean"))
              .reset_index()
              .rename(columns={"held_out_label": "class"}))
    return [{"model": r["model"], "class": r["class"], "agent_name": r["agent_name"],
             "n": int(r["n"]), "mean_wall_s": float(r["mean_wall_s"]),
             "mean_vram_delta_mb": (float(r["mean_vram_delta_mb"])
                                    if pd.notna(r["mean_vram_delta_mb"]) else None)}
            for _, r in agg.iterrows()]


def aggregate_per_class(sr: pd.DataFrame, am: pd.DataFrame,
                        classes: list[str]) -> dict[str, Any]:
    """The full per_class section: note + per-(model,class) table + per-agent rows."""
    return {
        "note": PER_CLASS_NOTE,
        "classes": list(classes),
        "table": per_class_table(sr, am, classes),
        "per_agent": per_class_per_agent_table(sr, am),
    }


# --- standalone read-only entry point (DB -> per_class dict) ------------

def run_per_class(config_path: Optional[str] = None,
                  db_path: Optional[str] = None) -> dict[str, Any]:
    """Read the locked results DB READ-ONLY and return the per_class section.
    Reuses analyze's read-only connection; never writes anything."""
    from . import analyze  # local import to avoid a cycle at module load
    from .config import load_config

    cfg = load_config(config_path)
    classes = list(cfg.get("classes", []) or [])
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
    return aggregate_per_class(sr, am, classes)
