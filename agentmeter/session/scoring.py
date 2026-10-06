"""Score a benchmark session: REUSES analyze.py over the session's own DB.

  accuracy + confusion  analyze.phase7(sr, classes)       classes = the run's 5 or 6
  raw criteria          analyze._criteria(...)
  SAW composite + tier  analyze.phase8(...)  (-> _normalise, _composite, _tier)
  sensitivity           analyze.sensitivity(...)
  per-agent             analyze.per_agent(am)
  statistics            analyze.statistics(sr)            (only with 2 models)
  provenance            analyze._provenance(db, cfg)      -> non_validated

A relative head-to-head view (relative.py) is added beside the absolute SAW,
over the same metrics, so two models under a fixed target are still separated.

Two SAW views are reported:
  * full SAW (config weights)      — only when the run is labelled (accuracy exists)
  * efficiency-only SAW            — always; accuracy weight 0 and the other
    weights renormalised to sum to 1 (same targets, tiers and maths). This is the
    basis for "which model is more resource-efficient", and the headline SAW of
    an unlabelled (PCAP) run.

Nothing is written to, or read from, the locked study DB.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from ..analysis import analyze
from ..config import load_config
from .relative import relative_comparison

SCHEMA = "agentmeter.session.v1"
TIE_REL = 0.01        # metric values within 1% of each other count as a tie
TIE_SCORE = 0.005     # composite (0-1) within 0.5 points (of 100) counts as a tie
AGENT_ORDER = ["perceive", "reason", "decide", "act"]


def efficiency_only_scoring(scoring: dict[str, Any]) -> dict[str, Any]:
    """Same scoring config with accuracy weighted 0 and the rest renormalised."""
    def renorm(w: dict[str, Any]) -> dict[str, float]:
        rest = {k: float(v) for k, v in w.items() if k != "accuracy"}
        s = sum(rest.values())
        return {"accuracy": 0.0, **{k: v / s for k, v in rest.items()}}

    out = dict(scoring)
    out["weights"] = renorm(scoring["weights"])
    out["sensitivity_weight_sets"] = {name: renorm(w) for name, w in
                                      (scoring.get("sensitivity_weight_sets") or {}).items()}
    return out


def _read(db: Path, run_id: str) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    conn = analyze._connect_ro(db)
    try:
        sr = pd.read_sql_query("SELECT * FROM scenario_results WHERE status='complete' AND run_id=?",
                               conn, params=[run_id])
        am = pd.read_sql_query("SELECT * FROM agent_metrics WHERE run_id=?", conn, params=[run_id])
        run = dict(conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())
    finally:
        conn.close()
    keep = set(zip(sr["model"], sr["scenario_id"]))
    am = am[[(m, s) in keep for m, s in zip(am["model"], am["scenario_id"])]].copy()
    return sr, am, run


def _finite(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _per_agent(am: pd.DataFrame) -> pd.DataFrame:
    """analyze.per_agent's table; its 'dominant' needs real VRAM, so guard it."""
    try:
        return analyze.per_agent(am)
    except (ValueError, KeyError, TypeError):
        # All-NaN VRAM (CPU/mock): reuse the same aggregation, skip VRAM dominance.
        tbl = (am.groupby(["model", "agent_name"])
                 .agg(mean_wall_s=("wall_time_s", "mean"), mean_ttft_s=("ttft_s", "mean"),
                      mean_vram_delta_mb=("vram_delta_mb", "mean"),
                      mean_input_tokens=("input_tokens", "mean"),
                      mean_output_tokens=("output_tokens", "mean")).reset_index())
        dom = []
        for m in sorted(am["model"].unique()):
            g = tbl[tbl["model"] == m]
            lat = g.loc[g["mean_wall_s"].idxmax()]
            dom.append({"model": m, "latency_dominant_agent": lat["agent_name"],
                        "latency_dominant_mean_wall_s": float(lat["mean_wall_s"]),
                        "vram_dominant_agent": None, "vram_dominant_mean_vram_delta_mb": None})
        return {"table": tbl, "dominant": pd.DataFrame(dom)}


def _statistics(sr: pd.DataFrame) -> dict[str, Any]:
    if sr["model"].nunique() < 2:
        return {"skipped": "statistics need 2 models"}
    out: dict[str, Any] = {}
    try:
        stats = analyze.statistics(sr.dropna(subset=["scenario_total_time_s"]))
        for metric, e in stats.items():
            if metric == "scenario_peak_vram_mb" and sr[metric].isna().all():
                out[metric] = {"skipped": "no VRAM readings (CPU/mock run)"}
                continue
            out[metric] = {k: (v.to_dict("index") if k == "dunn_matrix" else v) for k, v in e.items()}
    except ValueError as e:                       # e.g. identical values across models
        out["error"] = str(e)
    return out


def _efficiency(sr: pd.DataFrame, am: pd.DataFrame, model: str, diag: pd.DataFrame) -> dict[str, Any]:
    g = sr[sr["model"] == model]
    a = am[am["model"] == model].copy()
    a["tok"] = a["input_tokens"] + a["output_tokens"]
    per_flow = a.groupby("scenario_id").agg(inp=("input_tokens", "sum"), out=("output_tokens", "sum"),
                                            tok=("tok", "sum"))
    lat = g["scenario_total_time_s"]
    vram = g["scenario_peak_vram_mb"].dropna()
    rows = diag[diag["model"] == model].set_index("agent_name")
    per_agent = {}
    for ag in [x for x in AGENT_ORDER if x in rows.index] + [x for x in rows.index if x not in AGENT_ORDER]:
        r = rows.loc[ag]
        per_agent[ag] = {"mean_wall_s": _finite(r["mean_wall_s"]), "mean_ttft_s": _finite(r["mean_ttft_s"]),
                         "mean_vram_delta_mb": _finite(r["mean_vram_delta_mb"]),
                         "mean_input_tokens": _finite(r["mean_input_tokens"]),
                         "mean_output_tokens": _finite(r["mean_output_tokens"])}
    mean_lat = _finite(lat.mean())
    return {
        "n_flows": int(len(g)),
        "end_to_end": {
            "mean_latency_s": mean_lat, "median_latency_s": _finite(lat.median()),
            "p95_latency_s": _finite(lat.quantile(0.95)),
            "mean_peak_vram_mb": _finite(vram.mean()) if len(vram) else None,
            "mean_tokens_per_flow": _finite(per_flow["tok"].mean()),
            "mean_input_tokens_per_flow": _finite(per_flow["inp"].mean()),
            "mean_output_tokens_per_flow": _finite(per_flow["out"].mean()),
            "throughput_flows_per_s": (1.0 / mean_lat) if mean_lat else None,
        },
        "per_agent": per_agent,
    }


def _winner(values: dict[str, Optional[float]], lower_better: bool) -> Optional[str]:
    vals = {m: v for m, v in values.items() if v is not None}
    if len(vals) < 2:
        return None
    (m1, v1), (m2, v2) = sorted(vals.items(), key=lambda kv: kv[1], reverse=not lower_better)
    base = max(abs(v1), abs(v2))
    if base == 0 or abs(v1 - v2) / base < TIE_REL:
        return "tie"
    return m1


def _compare(models: list[str], per_model: dict[str, dict], eff: pd.DataFrame,
             full: Optional[pd.DataFrame], labelled: bool) -> Optional[dict[str, Any]]:
    if len(models) < 2:
        return None
    metrics = []
    for key, label in (("mean_latency_s", "mean end-to-end latency (s/flow)"),
                       ("mean_tokens_per_flow", "mean tokens per flow"),
                       ("mean_peak_vram_mb", "mean peak working VRAM (MB)")):
        vals = {m: per_model[m]["efficiency"]["end_to_end"][key] for m in models}
        w = _winner(vals, lower_better=True)
        metrics.append({"metric": key, "label": label, "lower_is_better": True, "values": vals,
                        "winner": w, "available": w is not None})
    e = eff.set_index("model")["composite"]
    a, b = models
    eff_w = "tie" if abs(e[a] - e[b]) < TIE_SCORE else (a if e[a] > e[b] else b)
    saw = full if full is not None else eff
    ranking = [{"rank": int(r["rank"]), "model": r["model"], "composite_100": round(float(r["composite"]) * 100, 2),
                "tier": r["tier"]} for _, r in saw.iterrows()]
    out = {
        "models": models,
        "metrics": metrics,
        "more_resource_efficient": {
            "model": eff_w, "basis": "efficiency-only SAW composite (latency, VRAM, tokens; accuracy excluded)",
            "composite_100": {m: round(float(e[m]) * 100, 2) for m in models}},
        "saw_basis": "full SAW (accuracy + efficiency, config weights)" if full is not None
                     else "efficiency-only SAW (no labels)",
        "saw_ranking": ranking,
        "accuracy": None,
    }
    if labelled:
        acc = {m: per_model[m]["accuracy"]["accuracy"] for m in models}
        out["accuracy"] = {"values": acc, "winner": _winner(acc, lower_better=False)}
    parts = [f"More resource-efficient: {eff_w if eff_w != 'tie' else 'tie (within 0.5 points)'}"]
    if labelled:
        aw = out["accuracy"]["winner"]
        parts.append(f"more accurate on this sample: {aw if aw != 'tie' else 'tie'}")
    top_tie = len(ranking) > 1 and abs(ranking[0]["composite_100"] - ranking[1]["composite_100"]) < TIE_SCORE * 100
    out["saw_top"] = "tie" if top_tie else ranking[0]["model"]
    parts.append(f"SAW top: {out['saw_top']} ({out['saw_basis']})")
    out["statement"] = "; ".join(parts) + "."
    return out


def _caveats(meta: dict[str, Any], provider: str, sr: pd.DataFrame, six: bool) -> list[str]:
    c = ["NON-VALIDATED user run: exploratory; not the locked 5-model CIC-IDS2017 study and "
         "never merged into its ranking or statistics."]
    if not meta["capabilities"]["accuracy"]:
        c.append("Efficiency only: " + "; ".join(meta.get("accuracy_unavailable_reasons") or
                                                 ["no ground-truth labels"]) + ".")
    if meta.get("selection_mode") == "label_aware_balanced":
        c.append("Balanced sample: rows were selected with equal shares per class, so accuracy is a "
                 "per-class (macro-like) view, not real-world prevalence.")
    if six:
        c.append("6-class run: 'Other Attack' groups attack labels outside the study taxonomy; it is "
                 "not part of the locked 5-class baseline.")
    if meta.get("feature_match", {}).get("status") == "approximate":
        c.append("PCAP features are approximate (whole-frame vs payload lengths) — not numerically "
                 "comparable with CIC-IDS2017 rows.")
    if sr["scenario_peak_vram_mb"].isna().all():
        c.append("No VRAM readings (CPU/mock run): the VRAM criterion contributes 0 for every model, "
                 "so it does not change the ranking.")
    if provider == "mock":
        c.append("MOCK provider: deterministic heuristic, not a language model — demonstrates the "
                 "flow only; numbers are not model measurements.")
    n = int(sr.groupby("model").size().min()) if len(sr) else 0
    if n < 30:
        c.append(f"Small sample ({n} flows per model): differences may not be meaningful.")
    return c


def score_session(plan: dict[str, Any], run_id: str) -> dict[str, Any]:
    db = Path(plan["db_path"])
    cfg = load_config(plan["config_path"])
    meta = plan["metadata"]
    classes = list(plan["classes"])
    labelled = bool(meta["capabilities"]["accuracy"])
    sr, am, run = _read(db, run_id)
    models = [m["model"] for m in plan["models"]]
    label_of = {}
    for m in models:                                  # DB labels: "mock:<name>" for mock
        hits = [x for x in sr["model"].unique() if x == m or x == f"mock:{m}"]
        label_of[m] = hits[0] if hits else m
    db_models = [label_of[m] for m in models]

    scoring = cfg.get("scoring")
    p7 = analyze.phase7(sr, classes) if labelled else None
    acc_by_model = (dict(zip(p7["per_model"]["model"], p7["per_model"]["accuracy"])) if p7
                    else {m: 0.0 for m in db_models})
    raw = analyze._criteria(sr, am, acc_by_model)
    eff_scoring = efficiency_only_scoring(scoring)
    p8_eff = analyze.phase8(raw, eff_scoring)
    p8_full = analyze.phase8(raw, scoring) if labelled else None
    head, head_scoring = (p8_full, scoring) if labelled else (p8_eff, eff_scoring)
    sens = analyze.sensitivity(raw, head["norm"], head_scoring)
    diag = _per_agent(am)
    provenance = analyze._provenance(db, cfg)

    per_model: dict[str, dict[str, Any]] = {}
    for m, dbm in zip(models, db_models):
        entry: dict[str, Any] = {"model": m, "db_label": dbm,
                                 "canonical": next(x["canonical"] for x in plan["models"] if x["model"] == m)}
        entry["efficiency"] = _efficiency(sr, am, dbm, diag["table"])
        e = p8_eff["table"].set_index("model").loc[dbm]
        entry["saw_efficiency_only"] = {"composite_100": round(float(e["composite"]) * 100, 2),
                                        "tier": e["tier"], "rank": int(e["rank"])}
        if labelled:
            f = p8_full["table"].set_index("model").loc[dbm]
            pm = p7["per_model"].set_index("model").loc[dbm]
            entry["saw"] = {"composite_100": round(float(f["composite"]) * 100, 2), "tier": f["tier"],
                            "rank": int(f["rank"])}
            entry["accuracy"] = {
                "accuracy": float(pm["accuracy"]), "n": int(pm["n"]), "n_correct": int(pm["n_correct"]),
                "n_unparseable": int(pm["n_unparseable"]),
                "per_class": [r for r in p7["per_class"].to_dict("records") if r["model"] == dbm],
                "confusion": p7["confusion"][dbm].to_dict("index"),
            }
        else:
            entry["saw"] = entry["saw_efficiency_only"]
            entry["accuracy"] = None
        per_model[m] = entry

    # comparison in model order, on DB labels
    eff_tbl = p8_eff["table"].replace({"model": {v: k for k, v in label_of.items()}})
    full_tbl = p8_full["table"].replace({"model": {v: k for k, v in label_of.items()}}) if labelled else None
    comparison = _compare(models, per_model, eff_tbl, full_tbl, labelled)
    caveats = _caveats(meta, plan["provider"], sr, plan["six_class"])
    # Phase 39 — additive head-to-head view over the same metrics (absolute SAW unchanged).
    stats = _statistics(sr)
    p_values = {key: stats[metric]["p_value"] for key, metric in
                (("mean_latency_s", "scenario_total_time_s"), ("mean_peak_vram_mb", "scenario_peak_vram_mb"))
                if isinstance(stats.get(metric), dict) and "p_value" in stats[metric]}
    relative = relative_comparison(models, per_model, labelled, caveats, run.get("hardware_label"),
                                   p_values=p_values)

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "session": {
            "run_id": run_id, "run_dir": plan["run_dir"], "db_path": str(db),
            "models": plan["models"], "provider": plan["provider"],
            "hardware": run.get("hardware_label"), "quant": run.get("quant_setting"),
            "execution": "sequential, one subprocess per model (existing runner)",
            "n_flows": plan["n_flows"],
            "source_type": meta["source_type"], "input_role": meta["input_role"],
            "evaluation_mode": meta["evaluation_mode"], "labelled": labelled,
            "accuracy_available": labelled,
            "class_set": classes, "class_scheme": "6-class" if plan["six_class"] else "5-class",
            "selection_mode": meta.get("selection_mode"), "feature_match": meta.get("feature_match"),
            "non_validated": True,
            "caveats": caveats,
        },
        "per_model": [per_model[m] for m in models],
        "comparison": comparison,                 # absolute (SAW vs fixed targets)
        "relative_comparison": relative,          # head-to-head A vs B (Phase 39)
        # --- dashboard-compatible sections (same keys/shape as analysis.json) ---------
        "notes": {"tokens_definition": analyze.TOKEN_DEF, "normalisation_formula": analyze.NORM_FORMULA,
                  "vram_per_agent_note": analyze.VRAM_NOTE, "vram_saw_criterion": analyze.VRAM_SAW_MARGINAL,
                  "saw_mode": ("full SAW (config weights)" if labelled else
                               "efficiency-only SAW: accuracy weight 0, other weights renormalised")},
        "phase8": {"weights": head["weights"], "targets": head["targets"], "tiers": head["tiers"],
                   "raw_criteria": raw.to_dict("records"), "normalised": head["norm"].to_dict("records"),
                   "saw_table": head["table"].to_dict("records")},
        "phase8_efficiency_only": {"weights": p8_eff["weights"],
                                   "saw_table": p8_eff["table"].to_dict("records")},
        "sensitivity": {"weight_sets": sens["weight_sets"], "ranks": sens["ranks"].to_dict("records"),
                        "scores": sens["scores"].to_dict("records"), "top_by_set": sens["top_by_set"],
                        "top_stable": sens["top_stable"], "stable_top_model": sens["stable_top_model"]},
        "per_agent": {"table": diag["table"].to_dict("records"),
                      "dominant": diag["dominant"].to_dict("records")},
        "statistics": stats,
        "provenance": provenance,
    }
    if labelled:
        payload["phase7"] = {"per_model": p7["per_model"].to_dict("records"),
                             "per_class": p7["per_class"].to_dict("records"),
                             "confusion": {m: cm.to_dict("index") for m, cm in p7["confusion"].items()}}
    return analyze._json_clean(payload)


def write_results(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str, allow_nan=False), encoding="utf-8")
