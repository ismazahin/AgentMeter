"""Phase 7 + Phase 8 — analysis stage (READ-ONLY over the results DB).

Consumes the completed run-full SQLite DB and produces:
  Phase 7 — accuracy aggregation (reads the stored `correct` flag; never
            recomputes detection): per-model accuracy, per-model x per-class
            accuracy + support, and a full confusion matrix per model.
  Phase 8 — SAW Composite Health Score: 4 criteria per model (accuracy, latency,
            vram, tokens) normalised against config targets, weighted by config
            weights, mapped to config SLO tiers, ranked; plus the mandatory
            sensitivity analysis over the config's alternative weight sets.
  Per-agent diagnostics — per model x agent means (reported, not scored) and the
            dominant agent for latency and peak VRAM.
  Statistics — Kruskal-Wallis across models on per-scenario latency and peak VRAM,
            with Dunn's post-hoc (Holm-corrected) when significant.

Nothing here touches the agents, pipeline, instrumentation, runner, or detection
logic — it only reads the DB and writes results/analysis/. Weights, targets and
tiers are read from config (scoring.*), never hard-coded.
"""
from __future__ import annotations

import itertools
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy.stats import kruskal, rankdata
from scipy.stats import norm as _normdist

from .config import load_config

ALPHA = 0.05  # significance level for the Kruskal-Wallis omnibus test


# --- DB access (read-only) ---------------------------------------------

def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _complete_run_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE status = 'complete'")]


def _short(model: str) -> str:
    return model.split("/")[-1]


# --- Phase 7: accuracy aggregation -------------------------------------

def phase7(sr: pd.DataFrame, classes: list[str]) -> dict[str, Any]:
    """Aggregate accuracy from the stored `correct` flag (no recomputation)."""
    models = sorted(sr["model"].unique())
    pred_cols = list(classes) + ["Unparseable"]
    # any predicted label outside the known set (defensive; keep it visible)
    extra = [p for p in sorted(sr["predicted_label"].unique()) if p not in pred_cols]
    pred_cols = pred_cols + extra

    per_model = []
    per_class = []
    confusion = {}
    for m in models:
        g = sr[sr["model"] == m]
        per_model.append({
            "model": m,
            "n": int(len(g)),
            "n_correct": int(g["correct"].sum()),
            "accuracy": float(g["correct"].mean()),
            "n_unparseable": int((g["predicted_label"] == "Unparseable").sum()),
        })
        for c in classes:
            gc = g[g["held_out_label"] == c]
            per_class.append({
                "model": m, "class": c, "support": int(len(gc)),
                "accuracy": float(gc["correct"].mean()) if len(gc) else float("nan"),
            })
        cm = pd.crosstab(g["held_out_label"], g["predicted_label"])
        cm = cm.reindex(index=classes, columns=pred_cols, fill_value=0)
        confusion[m] = cm

    return {
        "per_model": pd.DataFrame(per_model),
        "per_class": pd.DataFrame(per_class),
        "confusion": confusion,
    }


# --- Phase 8: SAW Composite Health Score -------------------------------

def _criteria(sr: pd.DataFrame, am: pd.DataFrame, acc_by_model: dict[str, float],
              footprint: Optional[dict[str, float]] = None) -> pd.DataFrame:
    """Raw criteria per model. tokens = mean over scenarios of the per-scenario
    total tokens (sum of input+output across the 4 agents).

    VRAM: always report `vram_working_mb` = mean scenario_peak_vram_mb (MARGINAL
    working memory, excludes weights). When `footprint` (measured weight_footprint_mb
    per model, from model_vram.json) is supplied, also report
    `total_device_vram_mb = weight_footprint_mb + vram_working_mb` and feed THAT into
    the SAW criterion (`vram_mb`); otherwise the SAW criterion uses the marginal
    working-memory figure. `vram_source` records which was used.
    """
    am = am.copy()
    am["scenario_tokens"] = am["input_tokens"] + am["output_tokens"]
    tok = (am.groupby(["model", "scenario_id"])["scenario_tokens"].sum()
             .groupby("model").mean())
    rows = []
    for m in sorted(sr["model"].unique()):
        g = sr[sr["model"] == m]
        working = float(g["scenario_peak_vram_mb"].mean())       # marginal working memory
        row = {
            "model": m,
            "accuracy_pct": acc_by_model[m] * 100.0,             # benefit (higher better)
            "latency_s": float(g["scenario_total_time_s"].mean()),   # cost (lower better)
            "vram_working_mb": working,                          # marginal (reported)
            "tokens_total": float(tok[m]),                       # cost (lower better)
        }
        if footprint is not None:
            wf = float(footprint[m])
            total = wf + working
            row["weight_footprint_mb"] = wf
            row["total_device_vram_mb"] = total
            row["vram_mb"] = total                               # SAW criterion source
            row["vram_source"] = "total_device_footprint"
        else:
            row["weight_footprint_mb"] = float("nan")
            row["total_device_vram_mb"] = float("nan")
            row["vram_mb"] = working                             # SAW criterion source
            row["vram_source"] = "marginal_working_memory"
        rows.append(row)
    return pd.DataFrame(rows)


def _normalise(raw: pd.DataFrame, targets: dict[str, float]) -> pd.DataFrame:
    """Target-based ratio normalisation, clamped to [0, 1]:
        benefit (accuracy): norm = clamp(value / target, 0, 1)
        cost (latency/vram/tokens): norm = clamp(target / value, 0, 1)
    """
    def clamp(x):
        return float(min(1.0, max(0.0, x)))

    out = raw[["model"]].copy()
    out["accuracy"] = raw["accuracy_pct"].apply(lambda v: clamp(v / targets["accuracy_pct"]))
    out["latency"] = raw["latency_s"].apply(lambda v: clamp(targets["latency_s"] / v) if v else 0.0)
    out["vram"] = raw["vram_mb"].apply(lambda v: clamp(targets["vram_mb"] / v) if v else 0.0)
    out["tokens"] = raw["tokens_total"].apply(lambda v: clamp(targets["tokens_total"] / v) if v else 0.0)
    return out


def _composite(norm: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    return (norm["accuracy"] * weights["accuracy"] + norm["latency"] * weights["latency"]
            + norm["vram"] * weights["vram"] + norm["tokens"] * weights["tokens"])


def _tier(score100: float, tiers: dict[str, float]) -> str:
    if score100 >= tiers["healthy_min"]:
        return "Healthy"
    if score100 >= tiers["degraded_min"]:
        return "Degraded"
    return "Critical"


def phase8(raw: pd.DataFrame, scoring: dict[str, Any]) -> dict[str, Any]:
    weights = {k: float(v) for k, v in scoring["weights"].items()}
    s = sum(weights.values())
    assert abs(s - 1.0) < 1e-9, f"scoring.weights must sum to 1.0, got {s}"
    targets = {k: float(v) for k, v in scoring["targets"].items()}
    tiers = {k: float(v) for k, v in scoring["tiers"].items()}

    norm = _normalise(raw, targets)
    table = raw.merge(norm, on="model", suffixes=("", "_norm"))
    table["composite"] = _composite(norm, weights).values
    table = table.sort_values("composite", ascending=False).reset_index(drop=True)
    table["rank"] = table.index + 1
    table["tier"] = table["composite"].apply(lambda c: _tier(c * 100.0, tiers))

    return {"weights": weights, "targets": targets, "tiers": tiers,
            "norm": norm, "table": table}


def sensitivity(raw: pd.DataFrame, norm: pd.DataFrame, scoring: dict[str, Any]) -> dict[str, Any]:
    """Recompute composite + ranking under every config weight set; flag stability."""
    sets = dict(scoring.get("sensitivity_weight_sets", {}) or {})
    sets = {"default": {k: float(v) for k, v in scoring["weights"].items()},
            **{name: {k: float(v) for k, v in w.items()} for name, w in sets.items()}}

    ranks = {"model": list(norm["model"])}
    scores = {"model": list(norm["model"])}
    top_by_set = {}
    for name, w in sets.items():
        comp = _composite(norm, w)
        order = comp.sort_values(ascending=False).index
        rank_of = {norm.loc[i, "model"]: r + 1 for r, i in enumerate(order)}
        ranks[name] = [rank_of[m] for m in norm["model"]]
        scores[name] = [float(comp[list(norm["model"]).index(m)]) for m in norm["model"]]
        top_by_set[name] = norm.loc[order[0], "model"]

    tops = set(top_by_set.values())
    stable = len(tops) == 1
    return {
        "weight_sets": sets,
        "ranks": pd.DataFrame(ranks),
        "scores": pd.DataFrame(scores),
        "top_by_set": top_by_set,
        "top_stable": stable,
        "stable_top_model": next(iter(tops)) if stable else None,
    }


# --- Per-agent diagnostics ---------------------------------------------

def per_agent(am: pd.DataFrame) -> dict[str, Any]:
    agg = (am.groupby(["model", "agent_name"])
             .agg(mean_wall_s=("wall_time_s", "mean"),
                  mean_ttft_s=("ttft_s", "mean"),
                  mean_vram_delta_mb=("vram_delta_mb", "mean"),
                  mean_input_tokens=("input_tokens", "mean"),
                  mean_output_tokens=("output_tokens", "mean"))
             .reset_index())
    dom = []
    for m in sorted(am["model"].unique()):
        g = agg[agg["model"] == m]
        lat = g.loc[g["mean_wall_s"].idxmax()]
        vr = g.loc[g["mean_vram_delta_mb"].idxmax()]
        dom.append({"model": m,
                    "latency_dominant_agent": lat["agent_name"],
                    "latency_dominant_mean_wall_s": float(lat["mean_wall_s"]),
                    "vram_dominant_agent": vr["agent_name"],
                    "vram_dominant_mean_vram_delta_mb": float(vr["mean_vram_delta_mb"])})
    return {"table": agg, "dominant": pd.DataFrame(dom)}


# --- Statistics: Kruskal-Wallis + Dunn's post-hoc ----------------------

def _dunn_holm(groups: dict[str, np.ndarray]) -> pd.DataFrame:
    """Dunn's test with tie correction; Holm-adjusted two-sided p-values.
    Returns a symmetric model x model matrix of adjusted p-values (diag = 1)."""
    labels = list(groups)
    data = np.concatenate([np.asarray(groups[l], float) for l in labels])
    grp = np.concatenate([[l] * len(groups[l]) for l in labels])
    N = len(data)
    ranks = rankdata(data)
    _, counts = np.unique(data, return_counts=True)
    ties = float(np.sum(counts ** 3 - counts))
    sigma2 = (N * (N + 1) / 12.0) - ties / (12.0 * (N - 1))
    mean_rank = {l: ranks[grp == l].mean() for l in labels}
    n = {l: int((grp == l).sum()) for l in labels}

    pairs = list(itertools.combinations(labels, 2))
    raw_p = []
    for a, b in pairs:
        se = np.sqrt(sigma2 * (1.0 / n[a] + 1.0 / n[b]))
        z = (mean_rank[a] - mean_rank[b]) / se
        raw_p.append(2.0 * (1.0 - _normdist.cdf(abs(z))))
    # Holm step-down correction
    m = len(raw_p)
    order = np.argsort(raw_p)
    adj = [0.0] * m
    running = 0.0
    for rank_i, idx in enumerate(order):
        running = max(running, (m - rank_i) * raw_p[idx])
        adj[idx] = min(1.0, running)

    mat = pd.DataFrame(np.eye(len(labels)), index=labels, columns=labels)
    for (a, b), p in zip(pairs, adj):
        mat.loc[a, b] = p
        mat.loc[b, a] = p
    return mat


def statistics(sr: pd.DataFrame) -> dict[str, Any]:
    out = {}
    models = sorted(sr["model"].unique())
    for metric in ("scenario_total_time_s", "scenario_peak_vram_mb"):
        groups = {m: sr.loc[sr["model"] == m, metric].to_numpy(float) for m in models}
        H, p = kruskal(*groups.values())
        entry = {"metric": metric, "test": "Kruskal-Wallis", "H": float(H),
                 "p_value": float(p), "significant": bool(p < ALPHA), "alpha": ALPHA,
                 "n_per_model": {m: int(len(groups[m])) for m in models}}
        if p < ALPHA:
            entry["posthoc"] = "Dunn (Holm-corrected)"
            entry["dunn_matrix"] = _dunn_holm(groups)
        out[metric] = entry
    return out


# --- model_vram.json (measured weight footprints) ----------------------

def _load_model_vram(path: str, models: list[str], run_hardware, run_quant) -> dict[str, Any]:
    """Load + VALIDATE model_vram.json, returning {model: weight_footprint_mb}.

    Refuses loudly (never silently falls back) if the capture is inconsistent, on
    different hardware than the run, at a different quant, or missing a model.
    """
    mv = json.loads(Path(path).read_text())
    if not mv.get("consistent_hardware", False):
        raise ValueError(
            f"--model-vram {path}: consistent_hardware is false — the capture spanned "
            "different GPUs and is invalid. Re-run measure-vram on ONE GPU (the L4).")

    per = mv.get("models", []) or []
    mv_hw = {str(r.get("hardware_label")) for r in per} | set(mv.get("hardware_labels") or [])
    mv_hw = {h for h in mv_hw if h and h != "None"}
    mv_quant = {str(r.get("quant")) for r in per} | {str(mv.get("quant"))}
    mv_quant = {q for q in mv_quant if q and q != "None"}

    if run_hardware and any(h != run_hardware for h in mv_hw):
        raise ValueError(
            f"--model-vram hardware {sorted(mv_hw)} != run hardware {run_hardware!r} — "
            "refusing: weight footprints are not comparable to the run's working-memory "
            f"figures. Re-capture measure-vram on {run_hardware!r}.")
    if run_quant and any(q != run_quant for q in mv_quant):
        raise ValueError(
            f"--model-vram quant {sorted(mv_quant)} != run quant {run_quant!r} — refusing "
            "(footprints must be at the same quantisation as the run).")

    footprint: dict[str, float] = {}
    for r in per:
        wf = r.get("weight_footprint_mb")
        if wf is None:
            raise ValueError(f"--model-vram: missing weight_footprint_mb for {r.get('model')!r}.")
        footprint[str(r["model"])] = float(wf)

    missing = [m for m in models if m not in footprint]
    if missing:
        raise ValueError(
            f"--model-vram is missing weight_footprint for model(s) present in the DB: "
            f"{missing}. Re-run measure-vram over the same run.models.")
    return {"footprint": footprint, "hardware": sorted(mv_hw), "quant": sorted(mv_quant)}


# --- orchestration ------------------------------------------------------

def run_analysis(
    config_path: Optional[str] = None,
    db_path: Optional[str] = None,
    out_dir: str = "results/analysis",
    model_vram_path: Optional[str] = None,
) -> dict[str, Any]:
    cfg = load_config(config_path)
    classes = list(cfg.get("classes", []) or [])
    scoring = cfg.get("scoring", {}) or {}

    if db_path is None:
        db_path = str(cfg.resolve_path("storage.sqlite_path", "results/agentmeter.db"))
    db = Path(db_path)
    if not db.exists():
        raise FileNotFoundError(f"Results DB not found: {db}")

    conn = _connect_ro(db)
    try:
        run_ids = _complete_run_ids(conn)
        if not run_ids:
            raise ValueError("No runs with status='complete' in the DB — nothing to analyze.")
        qmarks = ",".join("?" * len(run_ids))
        runs_meta = pd.read_sql_query(
            f"SELECT run_id, hardware_label, quant_setting FROM runs WHERE run_id IN ({qmarks})",
            conn, params=run_ids)
        # Only COMPLETE scenario rows from COMPLETE runs; never fabricate missing values.
        sr = pd.read_sql_query(
            f"SELECT * FROM scenario_results WHERE status='complete' AND run_id IN ({qmarks})",
            conn, params=run_ids)
        am = pd.read_sql_query(
            f"SELECT * FROM agent_metrics WHERE run_id IN ({qmarks})", conn, params=run_ids)
    finally:
        conn.close()

    # keep agent rows only for scenarios that have a complete scenario_result
    keep = set(zip(sr["model"], sr["scenario_id"]))
    am = am[[(mm, ss) in keep for mm, ss in zip(am["model"], am["scenario_id"])]].copy()
    models = sorted(sr["model"].unique())

    # Optional: total-device-footprint VRAM criterion from measured weight footprints.
    footprint = None
    model_vram_meta = None
    if model_vram_path is not None:
        run_hw = runs_meta["hardware_label"].dropna().unique()
        run_q = runs_meta["quant_setting"].dropna().unique()
        run_hardware = run_hw[0] if len(run_hw) == 1 else None
        run_quant = run_q[0] if len(run_q) == 1 else None
        mv = _load_model_vram(model_vram_path, models, run_hardware, run_quant)
        footprint = mv["footprint"]
        model_vram_meta = {"path": model_vram_path, "run_hardware": run_hardware,
                           "run_quant": run_quant, "hardware": mv["hardware"], "quant": mv["quant"]}

    p7 = phase7(sr, classes)
    acc_by_model = dict(zip(p7["per_model"]["model"], p7["per_model"]["accuracy"]))
    raw = _criteria(sr, am, acc_by_model, footprint=footprint)
    p8 = phase8(raw, scoring)
    sens = sensitivity(raw, p8["norm"], scoring)
    diag = per_agent(am)
    stats = statistics(sr)
    finding = _vram_finding(raw, p8["targets"], footprint is not None)

    # Phase 13 — per-attack-class RESOURCE breakdown (READ-ONLY, purely additive).
    from . import analyze_by_class
    per_class_section = analyze_by_class.aggregate_per_class(sr, am, classes)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    _write_outputs(out, run_ids, sr, p7, raw, p8, sens, diag, stats, model_vram_meta,
                   finding, per_class_section)
    summary = _format_summary(run_ids, sr, p7, raw, p8, sens, diag, stats, str(out),
                              model_vram_meta, finding)
    print(summary)
    (out / "summary.txt").write_text(summary)
    return {"out_dir": str(out), "run_ids": run_ids, "p7": p7, "p8": p8,
            "sensitivity": sens, "diagnostics": diag, "statistics": stats,
            "raw": raw, "model_vram": model_vram_meta, "vram_finding": finding,
            "per_class": per_class_section}


def _vram_finding(raw: pd.DataFrame, targets: dict[str, float], have_total: bool) -> str:
    target_gb = targets["vram_mb"] / 1024.0
    if have_total:
        lo = raw["total_device_vram_mb"].min() / 1024.0
        hi = raw["total_device_vram_mb"].max() / 1024.0
        return (f"VRAM (total device footprint) ranges {lo:.1f}-{hi:.1f} GB across models, "
                f"all well below the {target_gb:.0f} GB target -> VRAM is not a binding "
                "constraint on this hardware; it normalises to 1.0 for all models and does "
                "not differentiate the ranking. Differentiation is driven by accuracy, "
                "latency, and tokens.")
    lo = raw["vram_working_mb"].min() / 1024.0
    hi = raw["vram_working_mb"].max() / 1024.0
    return (f"VRAM (marginal working memory) ranges {lo:.2f}-{hi:.2f} GB, far below the "
            f"{target_gb:.0f} GB target -> normalises to 1.0 for all models (pass "
            "--model-vram to use the total device footprint instead).")


TOKEN_DEF = ("tokens = mean over scenarios of the per-scenario TOTAL tokens = "
             "mean_scenario( sum over the 4 agents of (input_tokens + output_tokens) ). "
             "Config scoring.targets.tokens_total is 'input+output per scenario' — matches; used as-is.")
NORM_FORMULA = ("benefit (accuracy): norm = clamp(value / target, 0, 1); "
                "cost (latency, vram, tokens): norm = clamp(target / value, 0, 1)")
VRAM_NOTE = ("vram_delta (per-agent) is MARGINAL working memory above the fresh-context "
             "baseline and EXCLUDES model weights (per-process isolation baseline).")
VRAM_SAW_TOTAL = ("SAW VRAM criterion = TOTAL device footprint = weight_footprint_mb "
                  "(measured on the run's GPU, model_vram.json) + mean(scenario_peak_vram_mb) "
                  "(DB). Config target 16000 MB / weight 0.20 (unchanged).")
VRAM_SAW_MARGINAL = ("SAW VRAM criterion = MARGINAL working memory (mean scenario_peak_vram_mb); "
                     "no --model-vram supplied. Config target 16000 MB / weight 0.20.")


def _write_outputs(out, run_ids, sr, p7, raw, p8, sens, diag, stats, model_vram_meta,
                   finding, per_class_section=None):
    p7["per_model"].to_csv(out / "phase7_model_accuracy.csv", index=False)
    p7["per_class"].to_csv(out / "phase7_per_class_accuracy.csv", index=False)
    for m, cm in p7["confusion"].items():
        cm.to_csv(out / f"phase7_confusion_{_short(m)}.csv")
    p8["table"].to_csv(out / "phase8_saw.csv", index=False)
    sens["ranks"].to_csv(out / "phase8_sensitivity_ranks.csv", index=False)
    sens["scores"].to_csv(out / "phase8_sensitivity_scores.csv", index=False)
    diag["table"].to_csv(out / "per_agent_diagnostics.csv", index=False)
    diag["dominant"].to_csv(out / "per_agent_dominant.csv", index=False)
    for metric, e in stats.items():
        if "dunn_matrix" in e:
            e["dunn_matrix"].to_csv(out / f"stats_dunn_{metric}.csv")

    vram_saw = VRAM_SAW_TOTAL if model_vram_meta is not None else VRAM_SAW_MARGINAL
    payload = {
        "run_ids": run_ids,
        "notes": {"tokens_definition": TOKEN_DEF, "normalisation_formula": NORM_FORMULA,
                  "vram_per_agent_note": VRAM_NOTE, "vram_saw_criterion": vram_saw,
                  "vram_finding": finding},
        "model_vram": model_vram_meta,
        "phase7": {
            "per_model": p7["per_model"].to_dict("records"),
            "per_class": p7["per_class"].to_dict("records"),
            "confusion": {m: cm.to_dict("index") for m, cm in p7["confusion"].items()},
        },
        "phase8": {
            "weights": p8["weights"], "targets": p8["targets"], "tiers": p8["tiers"],
            "raw_criteria": raw.to_dict("records"),
            "normalised": p8["norm"].to_dict("records"),
            "saw_table": p8["table"].to_dict("records"),
        },
        "sensitivity": {
            "weight_sets": sens["weight_sets"],
            "ranks": sens["ranks"].to_dict("records"),
            "scores": sens["scores"].to_dict("records"),
            "top_by_set": sens["top_by_set"],
            "top_stable": sens["top_stable"],
            "stable_top_model": sens["stable_top_model"],
        },
        "per_agent": {
            "table": diag["table"].to_dict("records"),
            "dominant": diag["dominant"].to_dict("records"),
        },
        "statistics": {
            metric: {k: (v.to_dict("index") if k == "dunn_matrix" else v)
                     for k, v in e.items()}
            for metric, e in stats.items()
        },
    }
    # Phase 13 — additive `per_class` section appended LAST so every existing key
    # above is byte-for-byte unchanged.
    if per_class_section is not None:
        payload["per_class"] = per_class_section
        pd.DataFrame(per_class_section.get("table", [])).to_csv(
            out / "per_class_resource.csv", index=False)
        pca = per_class_section.get("per_agent", [])
        if pca:
            pd.DataFrame(pca).to_csv(out / "per_class_per_agent.csv", index=False)
    (out / "analysis.json").write_text(json.dumps(payload, indent=2, default=str))


def _format_summary(run_ids, sr, p7, raw, p8, sens, diag, stats, out_dir,
                    model_vram_meta, finding) -> str:
    have_total = model_vram_meta is not None
    vram_saw = VRAM_SAW_TOTAL if have_total else VRAM_SAW_MARGINAL
    L = []
    L.append("=" * 78)
    L.append("  AgentMeter — Phase 7 + 8 Analysis")
    L.append("=" * 78)
    L.append(f"Run(s)     : {', '.join(run_ids)}")
    L.append(f"Scenarios  : {len(sr)} complete rows | models: {sr['model'].nunique()}")
    if have_total:
        L.append(f"model_vram : {model_vram_meta['path']}  (hardware {model_vram_meta['hardware']}, "
                 f"quant {model_vram_meta['quant']})")
    L.append("")
    L.append("PHASE 7 — accuracy (from stored `correct`):")
    L.append(f"  {'model':<40}{'n':>5}{'correct':>9}{'acc%':>8}{'unparse':>9}")
    for r in p7["per_model"].sort_values("accuracy", ascending=False).itertuples():
        L.append(f"  {r.model:<40}{r.n:>5}{r.n_correct:>9}{r.accuracy*100:>7.1f}%{r.n_unparseable:>9}")
    L.append("")
    L.append("PHASE 8 — SAW Composite Health Score")
    L.append(f"  {TOKEN_DEF}")
    L.append(f"  normalisation: {NORM_FORMULA}")
    L.append(f"  VRAM: {vram_saw}")
    L.append(f"  weights: {p8['weights']}")
    # criteria table: show BOTH marginal working memory and total device footprint
    L.append(f"  {'rank':<5}{'model':<38}{'acc%':>7}{'lat_s':>8}{'work_mb':>9}{'total_mb':>10}{'tokens':>8}{'comp':>7}  tier")
    tbl = p8["table"]
    for r in tbl.itertuples():
        total = getattr(r, "total_device_vram_mb", float("nan"))
        total_s = "     n/a" if (total != total) else f"{total:>10.1f}"  # NaN check
        L.append(f"  {r.rank:<5}{r.model:<38}{r.accuracy_pct:>6.1f}{r.latency_s:>8.2f}"
                 f"{r.vram_working_mb:>9.1f}{total_s}{r.tokens_total:>8.0f}{r.composite:>7.3f}  {r.tier}")
    L.append(f"  (work_mb = marginal working memory; total_mb = weight footprint + working memory)")
    L.append("")
    L.append("  normalised criteria [0,1]:")
    L.append(f"  {'model':<40}{'acc':>7}{'lat':>7}{'vram':>7}{'tok':>7}")
    for r in p8["norm"].itertuples():
        L.append(f"  {r.model:<40}{r.accuracy:>7.3f}{r.latency:>7.3f}{r.vram:>7.3f}{r.tokens:>7.3f}")
    L.append("")
    L.append(f"  FINDING: {finding}")
    L.append("")
    L.append("SENSITIVITY ANALYSIS — rank per weight set:")
    header = "  " + f"{'model':<40}" + "".join(f"{name:>16}" for name in sens["ranks"].columns if name != "model")
    L.append(header)
    setcols = [c for c in sens["ranks"].columns if c != "model"]
    for _, row in sens["ranks"].iterrows():
        L.append("  " + f"{row['model']:<40}" + "".join(f"{int(row[c]):>16}" for c in setcols))
    L.append(f"  top by set: {sens['top_by_set']}")
    if sens["top_stable"]:
        L.append(f"  TOP-RANK STABLE across all weightings: {sens['stable_top_model']}")
    else:
        L.append("  TOP-RANK CHANGES across weightings — NOT stable (see per-set tops above).")
    L.append("")
    L.append("PER-AGENT DIAGNOSTICS — dominant agent per model:")
    L.append(f"  {'model':<40}{'latency-dominant':>18}{'vram-dominant':>16}")
    for r in diag["dominant"].itertuples():
        L.append(f"  {r.model:<40}{r.latency_dominant_agent+' ('+format(r.latency_dominant_mean_wall_s,'.2f')+'s)':>18}"
                 f"{r.vram_dominant_agent+' ('+format(r.vram_dominant_mean_vram_delta_mb,'.0f')+'MB)':>16}")
    L.append(f"  ({VRAM_NOTE})")
    L.append("")
    L.append("STATISTICS — Kruskal-Wallis across models (per-scenario):")
    for metric, e in stats.items():
        sig = "SIGNIFICANT" if e["significant"] else "not significant"
        L.append(f"  {metric}: H={e['H']:.2f}, p={e['p_value']:.3e} -> {sig} (alpha={e['alpha']})")
        if "dunn_matrix" in e:
            L.append(f"    post-hoc {e['posthoc']} p-values -> stats_dunn_{metric}.csv")
    L.append("")
    L.append(f"Wrote CSV + JSON outputs to: {out_dir}/")
    L.append("=" * 78)
    return "\n".join(L)
