"""Phase 46 — additive efficiency analyses for a benchmark session.

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product.
Everything here READS the session's own DB (the same rows scoring.py read) and the
already-scored payload, and ADDS keys to session_results.json. Nothing here feeds
back into any score, SAW value, rank or verdict (tests/test_scored_keys_golden.py).

  agents                A. per-agent breakdown: latency / tokens and their share of
                           the flow, per-agent peak working VRAM, agentic overhead
                           (hand-off tokens), output failures, per-flow timeline
  latency_distribution  D. p50/p95/p99, histogram, warm-up check (first N flows)
  latency_trend         latency per flow in run order, rolling median and a drift test
                           (Spearman rank correlation of order vs latency, warm-up excluded)
  effect_sizes          D. Cliff's delta + bootstrap 95% CI of the A-B difference
                           (latency, VRAM, tokens), beside the existing Kruskal-Wallis p
  memory                working VRAM (excludes weights) vs total peak (weights + working)
  cost_energy           B. cost per 1,000 flows (measured time x GPU $/h) and energy
                           (sampled GPU board power) — "not measured" on mock/CPU
  load_scaling          the opt-in load-scaling step (run/load_test.py): throughput and latency
                           with 1, 2, 4 flows in flight, read from its own files
  gpu_timeline          the sampled board power, GPU utilisation and device memory over the
                           session (thinned), with each model's measured window and idle window
  decision_helper       C. constraint rules (configs/constraint_rules.yaml), evaluated
                           with the rule file's defaults; the UI/PDF re-evaluate with
                           the user's limits
  session_identity      prepared-set hash, GPU, settings fingerprint (Compare /
                           Leaderboard validity checks)
"""
from __future__ import annotations

import bisect
import hashlib
import itertools
import json
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ..pipeline.agents import HANDOFFS

AGENT_ORDER = ["perceive", "reason", "decide", "act"]
MIN_STABLE_N = 20            # flows per model below which estimates are flagged unstable
WARMUP_N = 5                 # first N flows compared with the rest
WARMUP_RATIO = 1.2           # first-N median > 1.2 x the rest -> warm-up effect
N_BOOT = 2000
BOOT_SEED = 46
HIST_BINS = 20
TIMELINE_MAX_POINTS = 600    # GPU samples stored at most this many (bucket means beyond)
TREND_MAX_POINTS = 400       # per-flow series stored at most this long (evenly thinned beyond)
TREND_MIN_N = 10             # flows after warm-up needed for a drift test
TREND_ALPHA = 0.05
TREND_RATIO = 1.10           # last-quarter median vs first-quarter median: >10% apart to call it drift
SMALL_NOTE = ("Sample too small for a stable estimate ({n} flows per model; at least "
              f"{MIN_STABLE_N} recommended): read these numbers as indicative only.")


# ---------------------------------------------------------------------------
# small statistics (pure functions; unit-tested)
# ---------------------------------------------------------------------------
def percentiles(xs, qs=(50, 95, 99)) -> dict[str, Optional[float]]:
    """Linear-interpolated percentiles (numpy default = pandas quantile default)."""
    a = np.asarray([x for x in xs if x is not None and not _nan(x)], dtype=float)
    if not len(a):
        return {f"p{q}": None for q in qs}
    return {f"p{q}": float(np.percentile(a, q)) for q in qs}


def cliffs_delta(a, b) -> Optional[float]:
    """P(a > b) - P(a < b) over all pairs, in [-1, 1]. Negative = a tends to be smaller."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if not len(a) or not len(b):
        return None
    diff = a[:, None] - b[None, :]
    return float(((diff > 0).sum() - (diff < 0).sum()) / (len(a) * len(b)))


def cliffs_magnitude(d: Optional[float]) -> Optional[str]:
    """Romano et al. (2006) thresholds."""
    if d is None:
        return None
    x = abs(d)
    return "negligible" if x < 0.147 else "small" if x < 0.33 else "medium" if x < 0.474 else "large"


def bootstrap_mean_diff_ci(a, b, n_boot: int = N_BOOT, seed: int = BOOT_SEED,
                           level: float = 0.95) -> dict[str, Optional[float]]:
    """Percentile bootstrap CI of mean(a) - mean(b), resampling each group independently."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 2 or len(b) < 2:
        return {"diff": None, "ci_low": None, "ci_high": None}
    rng = np.random.default_rng(seed)
    ia = rng.integers(0, len(a), size=(n_boot, len(a)))
    ib = rng.integers(0, len(b), size=(n_boot, len(b)))
    d = a[ia].mean(axis=1) - b[ib].mean(axis=1)
    lo, hi = np.percentile(d, [100 * (1 - level) / 2, 100 * (1 + level) / 2])
    return {"diff": float(a.mean() - b.mean()), "ci_low": float(lo), "ci_high": float(hi)}


def integrate_energy_wh(samples: list[tuple[float, float]], t0: float, t1: float,
                        edge_tol_s: float = 1.0) -> Optional[float]:
    """Trapezoid integral of power samples (unix_s, watts) over [t0, t1], in Wh. Window edges
    are linearly interpolated between the samples around them. None unless the samples
    cover the window (to within `edge_tol_s`, the timestamp resolution): power outside the
    sampled span is never extrapolated."""
    pts = sorted((float(t), float(w)) for t, w in samples)
    if t1 <= t0 or len(pts) < 2 or pts[0][0] > t0 + edge_tol_s or pts[-1][0] < t1 - edge_tol_s:
        return None

    ts = [t for t, _ in pts]

    def at(t: float) -> float:                     # the window edges: interpolate (binary search)
        if t <= ts[0]:
            return pts[0][1]
        if t >= ts[-1]:
            return pts[-1][1]
        i = bisect.bisect_left(ts, t)
        (ta, wa), (tb, wb) = pts[i - 1], pts[i]
        return wb if t == tb else wa if tb == ta else wa + (wb - wa) * (t - ta) / (tb - ta)

    inner = [(t, w) for t, w in pts if t0 < t < t1]   # samples inside: their own reading
    xs = [t0] + [t for t, _ in inner] + [t1]
    ys = [at(t0)] + [w for _, w in inner] + [at(t1)]
    joules = sum((xs[i + 1] - xs[i]) * (ys[i] + ys[i + 1]) / 2 for i in range(len(xs) - 1))
    return joules / 3600.0


def cost_per_1k_flows(mean_flow_s: Optional[float], usd_per_hour: Optional[float]) -> Optional[float]:
    if mean_flow_s is None or usd_per_hour is None:
        return None
    return float(mean_flow_s) * 1000.0 / 3600.0 * float(usd_per_hour)


def _nan(x) -> bool:
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True


def _f(x) -> Optional[float]:
    return None if x is None or _nan(x) else float(x)


# ---------------------------------------------------------------------------
# reading the session DB (rowid order = execution order)
# ---------------------------------------------------------------------------
def read_rows(db: str | Path, run_id: str) -> tuple[list[dict], list[dict]]:
    con = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sr = [dict(r) for r in con.execute(
            "SELECT * FROM scenario_results WHERE run_id=? AND status='complete' ORDER BY rowid", (run_id,))]
        keep = {(r["model"], r["scenario_id"]) for r in sr}
        am = [dict(r) for r in con.execute(
            "SELECT * FROM agent_metrics WHERE run_id=? ORDER BY rowid", (run_id,))
            if (r["model"], r["scenario_id"]) in keep]
    finally:
        con.close()
    return sr, am


def _agents_in_order(am: list[dict]) -> list[str]:
    seen = list(dict.fromkeys(r["agent_name"] for r in am))
    if not set(seen) <= set(AGENT_ORDER):     # a user-defined pipeline: the order the agents ran in
        return seen
    return [a for a in AGENT_ORDER if a in seen]


# ---------------------------------------------------------------------------
# A. agents
# ---------------------------------------------------------------------------
def agents_block(sr: list[dict], am: list[dict], label_of: dict[str, str],
                 budgets: dict[str, int], decide_agent: str = "decide",
                 pipeline: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """pipeline: spec.describe() of the session's pipeline (None = the default four)."""
    order = _agents_in_order(am)
    handoffs = {a["name"]: list(a["reads"]) for a in pipeline} if pipeline else HANDOFFS
    out_models = []
    for model, dbm in label_of.items():
        rows = [r for r in am if r["model"] == dbm]
        flows = [r for r in sr if r["model"] == dbm]
        by_flow: dict[str, dict[str, dict]] = {}
        for r in rows:
            by_flow.setdefault(r["scenario_id"], {})[r["agent_name"]] = r
        n = len(by_flow)
        tot_wall = sum(r["wall_time_s"] or 0 for r in rows)
        tot_tok = sum((r["input_tokens"] or 0) + (r["output_tokens"] or 0) for r in rows)
        agents = []
        for ag in order:
            g = [r for r in rows if r["agent_name"] == ag]
            if not g:
                continue
            wall = [r["wall_time_s"] or 0 for r in g]
            tin = [r["input_tokens"] or 0 for r in g]
            tout = [r["output_tokens"] or 0 for r in g]
            vram = [r["vram_delta_mb"] for r in g if r["vram_delta_mb"] is not None]
            cap = budgets.get(ag)
            fail = {
                "empty_output": sum(1 for x in tout if x == 0),
                "hit_token_cap": (sum(1 for x in tout if x >= cap) if cap else None),
                "token_cap": cap,
                "unparseable_label": (sum(1 for f in flows if f["predicted_label"] == "Unparseable")
                                      if ag == decide_agent else None),
                "timeouts": None, "retries": 0,
            }
            # failing FLOWS, each counted once: an empty Decide reply is also an Unparseable label
            bad = {r["scenario_id"] for r in g if (r["output_tokens"] or 0) == 0}
            if ag == decide_agent:
                bad |= {f["scenario_id"] for f in flows if f["predicted_label"] == "Unparseable"}
            fails = len(bad)
            agents.append({
                "agent": ag, "n": len(g),
                "mean_latency_s": float(np.mean(wall)),
                "latency_share": (sum(wall) / tot_wall) if tot_wall else None,
                "mean_input_tokens": float(np.mean(tin)), "mean_output_tokens": float(np.mean(tout)),
                "token_share": ((sum(tin) + sum(tout)) / tot_tok) if tot_tok else None,
                "mean_peak_vram_mb": float(np.mean(vram)) if vram else None,
                "max_peak_vram_mb": float(np.max(vram)) if vram else None,
                "failures": fail,
                "failure_rate": fails / len(g),
                "cap_rate": (fail["hit_token_cap"] / len(g)) if fail["hit_token_cap"] is not None else None,
            })
        # agentic overhead: earlier agents' outputs re-sent in later prompts (estimated
        # from the recorded output-token counts; re-tokenisation can differ by a few tokens)
        handoff = task = 0.0
        for fl in by_flow.values():
            for ag, r in fl.items():
                h = sum((fl[src]["output_tokens"] or 0) for src in handoffs.get(ag, []) if src in fl)
                handoff += h
                task += max(0, (r["input_tokens"] or 0) - h)
        timeline = {}
        for sid, fl in by_flow.items():
            t, segs = 0.0, []
            for ag in order:
                if ag in fl:
                    w = float(fl[ag]["wall_time_s"] or 0)
                    segs.append([ag, round(t, 6), round(w, 6)])
                    t += w
            timeline[sid] = segs
        out_models.append({
            "model": model, "n_flows": n, "agents": agents,
            "overhead": {
                "handoff_tokens_per_flow": (handoff / n) if n else None,
                "task_input_tokens_per_flow": (task / n) if n else None,
                "output_tokens_per_flow": (sum(r["output_tokens"] or 0 for r in rows) / n) if n else None,
                "handoff_share_of_total": (handoff / tot_tok) if tot_tok else None,
                "handoff_share_of_input": (handoff / (handoff + task)) if (handoff + task) else None,
            },
            "timeline": timeline,
        })
    return {
        "agent_order": order,
        "handoffs": {a: handoffs.get(a, []) for a in order},
        "pipeline": ([{k: a.get(k) for k in ("name", "title", "kind", "builtin", "reads", "features", "instructions")}
                      for a in pipeline] if pipeline else None),
        "label_agent": decide_agent,
        "notes": {
            "latency": "Per-agent latency = the agent's own model call (perf_counter); shares are of "
                       "the summed agent time, which is the flow's end-to-end latency.",
            "vram": "Per-agent peak working VRAM = torch allocator peak above the pre-call baseline "
                    "(excludes the loaded weights); null on CPU/mock.",
            "overhead": "Agentic overhead = tokens of earlier agents' outputs re-sent in later prompts "
                        "(hand-offs: " + ", ".join(f"{a} <- {'+'.join(s)}" for a, s in handoffs.items() if s)
                        + "). Estimated from the recorded output-token counts; the rest of the input "
                          "(system prompt, flow features, instructions) is the task input.",
            "failures": "empty_output = 0 output tokens; hit_token_cap = output reached the agent's "
                        "max_new_tokens (likely truncated); unparseable_label = the label agent's reply matched no "
                        "allowed label. The pipeline has no timeouts or retries by design (one call per "
                        "agent), so timeouts are not applicable and retries are always 0.",
            "timeline": "Agents run back-to-back; a flow's timeline is the cumulative sum of its agents' "
                        "recorded call times (framework overhead between calls is not part of the measurement).",
        },
        "per_model": out_models,
    }


# ---------------------------------------------------------------------------
# D. latency distribution + effect sizes
# ---------------------------------------------------------------------------
def latency_block(sr: list[dict], label_of: dict[str, str]) -> dict[str, Any]:
    allv = [r["scenario_total_time_s"] for r in sr if r["scenario_total_time_s"] is not None]
    lo, hi = (min(allv), max(allv)) if allv else (0.0, 0.0)
    edges = list(np.linspace(lo, hi if hi > lo else lo + 1e-9, HIST_BINS + 1))
    per = []
    for model, dbm in label_of.items():
        xs = [r["scenario_total_time_s"] for r in sr if r["model"] == dbm and r["scenario_total_time_s"] is not None]
        n = len(xs)
        pc = percentiles(xs)
        counts = np.histogram(xs, bins=edges)[0].tolist() if xs else []
        if n >= 2 * WARMUP_N:
            first, rest = float(np.median(xs[:WARMUP_N])), float(np.median(xs[WARMUP_N:]))
            ratio = first / rest if rest else None
            warm = {"n_first": WARMUP_N, "median_first_s": first, "median_rest_s": rest, "ratio": ratio,
                    "warmup_effect": bool(ratio is not None and ratio > WARMUP_RATIO),
                    "note": (f"The first {WARMUP_N} flows were {ratio:.2f}x the median of the rest: a warm-up "
                             "effect; the mean includes it." if ratio and ratio > WARMUP_RATIO else
                             f"No warm-up effect: the first {WARMUP_N} flows are within "
                             f"{round((WARMUP_RATIO - 1) * 100)}% of the rest (ratio {ratio:.2f}).")
                    if ratio is not None else "rest median is 0"}
        else:
            warm = {"n_first": WARMUP_N, "warmup_effect": None,
                    "note": f"Too few flows for a warm-up check (needs at least {2 * WARMUP_N})."}
        per.append({"model": model, "n": n, **pc,
                    "min_s": float(min(xs)) if xs else None, "max_s": float(max(xs)) if xs else None,
                    "mean_s": float(np.mean(xs)) if xs else None, "histogram": counts, "warmup": warm,
                    "small_sample": n < MIN_STABLE_N})
    n_min = min((p["n"] for p in per), default=0)
    return {"metric": "end-to-end latency per flow (s)", "bin_edges_s": [float(e) for e in edges],
            "per_model": per, "warmup_rule": f"median of the first {WARMUP_N} flows vs the rest; "
                                             f"flagged above {WARMUP_RATIO}x",
            "small_sample_note": SMALL_NOTE.format(n=n_min) if n_min < MIN_STABLE_N else None}


def rolling_median(xs: list[float], window: int) -> list[float]:
    """Centred rolling median (the window shrinks at the ends)."""
    h = window // 2
    return [float(np.median(xs[max(0, i - h):i + h + 1])) for i in range(len(xs))]


def drift_test(xs: list[float], skip: int = WARMUP_N) -> dict[str, Any]:
    """Does latency trend over the run? Spearman rho of flow order vs latency on the flows after
    the first `skip` (the warm-up check covers those), plus last-quarter / first-quarter medians."""
    from scipy.stats import spearmanr
    ys = xs[skip:] if len(xs) - skip >= TREND_MIN_N else []
    if not ys:
        return {"tested": False, "n": len(xs) - skip if len(xs) > skip else 0,
                "note": f"Too few flows for a drift test (needs at least {TREND_MIN_N} after the first {skip})."}
    q = max(1, len(ys) // 4)
    first, last = float(np.median(ys[:q])), float(np.median(ys[-q:]))
    ratio = last / first if first else None
    if len(set(ys)) < 2:
        rho, p = 0.0, 1.0
    else:
        r = spearmanr(np.arange(len(ys)), ys)
        rho, p = float(r.statistic), float(r.pvalue)
    # a trend needs the rank test AND the quarter medians to agree on its direction and size
    up = p < TREND_ALPHA and rho > 0 and ratio is not None and ratio > TREND_RATIO
    down = p < TREND_ALPHA and rho < 0 and ratio is not None and ratio < 2 - TREND_RATIO
    verdict = "slows_down" if up else "speeds_up" if down else "stable"
    pct = abs(ratio - 1) * 100 if ratio is not None else None
    tail = f" (Spearman rho {rho:.2f}, p = {p:.2g})."
    if verdict == "slows_down":
        note = f"Slows down over the run: the last quarter's median is {pct:.0f}% above the first's" + tail
    elif verdict == "speeds_up":
        note = f"Speeds up over the run: the last quarter's median is {pct:.0f}% below the first's" + tail
    elif ratio is None:
        note = "Stable over the run."
    elif p < TREND_ALPHA:
        note = (f"No lasting drift: latency follows a rank trend over the run (Spearman rho {rho:.2f}, "
                f"p = {p:.2g}), but the last quarter's median is {ratio:.2f}x the first's — not a "
                f"{round((TREND_RATIO - 1) * 100)}% change in that direction.")
    else:
        note = (f"Stable over the run: no significant trend (Spearman rho {rho:.2f}, p = {p:.2g}; "
                f"last/first quarter medians {ratio:.2f}x).")
    return {"tested": True, "n": len(ys), "skipped_warmup": skip, "spearman_rho": rho, "p_value": p,
            "first_quarter_median_s": first, "last_quarter_median_s": last, "ratio": ratio,
            "verdict": verdict, "note": note}


FLOW_TEXT_CHARS = 1200        # each agent answer kept up to this many characters in flows_<run>.json


def flows_file(db: str | Path, run_id: str, label_of: dict[str, str], labelled: bool = True) -> dict[str, Any]:
    """Every flow of the run, per model: truth, answer, time, and each agent's time, tokens and
    answer text (when it was recorded) — the flow explorer's data. Read-only; written beside the
    results as flows_<run_id>.json, never inside session_results.json."""
    sr, am = read_rows(db, run_id)
    con = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)
    try:
        has = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_outputs'").fetchone()
        texts = {(m, s, a): t for m, s, a, t in con.execute(
            "SELECT model, scenario_id, agent_name, text FROM agent_outputs WHERE run_id=?", (run_id,))} if has else {}
    finally:
        con.close()
    order = _agents_in_order(am)
    model_of = {dbm: m for m, dbm in label_of.items()}
    agents: dict[tuple, list] = {}
    for r in am:
        agents.setdefault((r["model"], r["scenario_id"]), []).append(r)
    flows: dict[str, dict] = {}
    for r in sr:                                    # execution order of the first model
        if r["model"] not in model_of:
            continue
        # unlabelled input (PCAP): its placeholder label is no ground truth — no truth, no right/wrong
        f = flows.setdefault(r["scenario_id"], {"scenario_id": r["scenario_id"],
                                                "truth": r["held_out_label"] if labelled else None, "per_model": {}})
        rows = sorted(agents.get((r["model"], r["scenario_id"]), []), key=lambda a: order.index(a["agent_name"]) if a["agent_name"] in order else 99)
        f["per_model"][model_of[r["model"]]] = {
            "predicted": r["predicted_label"],
            "correct": bool(r["correct"]) if labelled and r["correct"] is not None else None,
            "latency_s": _f(r["scenario_total_time_s"]),
            "agents": [{"agent": a["agent_name"], "wall_s": _f(a["wall_time_s"]), "input_tokens": a["input_tokens"],
                        "output_tokens": a["output_tokens"],
                        "text": (lambda t: None if t is None else (t if len(t) <= FLOW_TEXT_CHARS else t[:FLOW_TEXT_CHARS] + " …[cut]"))(
                            texts.get((r["model"], r["scenario_id"], a["agent_name"])))} for a in rows]}
    return {"run_id": run_id, "agent_order": order, "models": list(label_of), "labelled": labelled, "has_text": bool(texts),
            "text_chars": FLOW_TEXT_CHARS, "flows": list(flows.values())}


RANK_BOOT = 500


def rank_stability_block(sr: list[dict], am: list[dict], label_of: dict[str, str],
                         scoring: Optional[dict[str, Any]], n_boot: int = RANK_BOOT, seed: int = BOOT_SEED) -> dict[str, Any]:
    """How sure is the ranking? Resample the session's flows with replacement (the same flows for
    every model: a paired bootstrap), recompute each model's efficiency-only SAW on every resample
    (same targets and weights as the stored score) and count how often each model comes first.
    Also, per efficiency metric, how often each model has the lowest mean."""
    models = list(label_of)
    if len(models) < 2 or not scoring:
        return {"skipped": "rank stability needs two or more models"}
    from .scoring import efficiency_only_scoring
    eff = efficiency_only_scoring(scoring)
    w, t = eff["weights"], eff.get("targets") or {}
    tok: dict[tuple, float] = {}
    for r in am:
        k = (r["model"], r["scenario_id"])
        tok[k] = tok.get(k, 0.0) + (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
    rows = {(r["model"], r["scenario_id"]): r for r in sr}
    sids = sorted({sid for (m, sid) in rows if all((label_of[x], sid) in rows for x in models)})
    if len(sids) < 5:
        return {"skipped": "too few flows measured by every model"}
    def col(m, key):
        return np.array([np.nan if rows[(label_of[m], s_)][key] is None else float(rows[(label_of[m], s_)][key]) for s_ in sids])
    lat = {m: col(m, "scenario_total_time_s") for m in models}
    vram = {m: col(m, "scenario_peak_vram_mb") for m in models}
    tk = {m: np.array([tok.get((label_of[m], s_), 0.0) for s_ in sids]) for m in models}
    has_vram = all(not np.isnan(vram[m]).all() for m in models)

    def norm(v, target):
        return 0.0 if not v or not target or np.isnan(v) else float(min(1.0, max(0.0, target / v)))

    rng = np.random.default_rng(seed)
    n = len(sids)
    first = {m: 0.0 for m in models}
    ranks = {m: [] for m in models}
    best = {k: {m: 0.0 for m in models} for k in ("latency", "vram", "tokens")}
    top_ties = 0
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        score, means = {}, {}
        for m in models:
            ml, mt = float(np.mean(lat[m][idx])), float(np.mean(tk[m][idx]))
            mv = float(np.nanmean(vram[m][idx])) if has_vram else float("nan")
            means[m] = {"latency": ml, "vram": mv, "tokens": mt}
            score[m] = (w.get("latency", 0) * norm(ml, t.get("latency_s")) + w.get("tokens", 0) * norm(mt, t.get("tokens_total"))
                        + (w.get("vram", 0) * norm(mv, t.get("vram_mb")) if has_vram else 0.0))
        top = max(score.values())
        leaders = [m for m in models if abs(score[m] - top) < 1e-12]
        if len(leaders) > 1:
            top_ties += 1
        for m in leaders:
            first[m] += 1.0 / len(leaders)
        for m in models:
            ranks[m].append(1 + sum(1 for o in models if score[o] > score[m] + 1e-12))
        for k in best:
            if k == "vram" and not has_vram:
                continue
            lo = min(means[x][k] for x in models)
            ws = [m for m in models if means[m][k] <= lo + 1e-15]
            for m in ws:
                best[k][m] += 1.0 / len(ws)
    per = [{"model": m, "p_first": first[m] / n_boot, "mean_rank": float(np.mean(ranks[m])),
            "rank_p5": int(np.percentile(ranks[m], 5)), "rank_p95": int(np.percentile(ranks[m], 95)),
            "p_lowest": {k: (best[k][m] / n_boot if (k != "vram" or has_vram) else None) for k in best}}
           for m in models]
    return {"n_boot": n_boot, "seed": seed, "n_flows": n, "share_top_tied": top_ties / n_boot,
            "weights": {k: w.get(k) for k in ("latency", "vram", "tokens")}, "per_model": per,
            "method": (f"Paired bootstrap: {n_boot} resamples of the {n} flows (with replacement, the same flows for "
                       f"every model, seed {seed}); each resample's efficiency-only SAW uses the stored targets and "
                       "weights. A tie at the top splits the count. p_lowest: share of resamples where the model has "
                       "the lowest mean of that metric."),
            "note": ("The absolute SAW clamps at the study targets: models that all beat a target score the same "
                     "on it, so the top can tie in many resamples even when one model is faster (see p_lowest).")}


DRIVER_TOP, SCALING_POINTS = 6, 200


def drivers_block(sr: list[dict], am: list[dict], label_of: dict[str, str], run_dir: str | Path) -> dict[str, Any]:
    """What makes a flow slow or token-hungry? Per model, the Spearman rank correlation of each
    model-visible flow feature (session_scenarios.csv) with the flow's latency and tokens, the
    strongest first; and how latency grows with the prompt size (input tokens per flow): a linear fit.
    Correlation, not cause: a feature can stand in for another one."""
    from scipy.stats import spearmanr
    import pandas as pd
    path = Path(run_dir) / "session_scenarios.csv"
    feats = None
    if path.is_file():
        df = pd.read_csv(path)
        cols = [c for c in df.columns if c not in ("flow_id", "label") and pd.api.types.is_numeric_dtype(df[c])]
        feats = df.set_index(df["flow_id"].astype(str))[cols]
    tok_in: dict[tuple, float] = {}
    tok_all: dict[tuple, float] = {}
    for r in am:
        k = (r["model"], r["scenario_id"])
        tok_in[k] = tok_in.get(k, 0.0) + (r["input_tokens"] or 0)
        tok_all[k] = tok_all.get(k, 0.0) + (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
    per = []
    for model, dbm in label_of.items():
        rows = [r for r in sr if r["model"] == dbm and r["scenario_total_time_s"] is not None]
        sids = [str(r["scenario_id"]) for r in rows]
        lat = np.array([float(r["scenario_total_time_s"]) for r in rows])
        tok = np.array([tok_all.get((dbm, r["scenario_id"]), 0.0) for r in rows])
        tin = np.array([tok_in.get((dbm, r["scenario_id"]), 0.0) for r in rows])
        out: dict[str, Any] = {"model": model, "n_flows": len(rows)}
        if feats is not None and len(rows) >= 8:
            sub = feats.reindex(sids)
            corr = []
            for c in sub.columns:
                x = sub[c].to_numpy(dtype=float)
                ok = ~np.isnan(x)
                if ok.sum() < 8 or np.nanstd(x) == 0:
                    continue
                rl, pl = spearmanr(x[ok], lat[ok])
                rt, pt = spearmanr(x[ok], tok[ok]) if np.std(tok[ok]) > 0 else (float("nan"), float("nan"))
                if np.isnan(rl) and np.isnan(rt):
                    continue
                corr.append({"feature": c, "rho_latency": _f(rl), "p_latency": _f(pl), "rho_tokens": _f(rt), "p_tokens": _f(pt)})
            corr.sort(key=lambda d: -abs(d["rho_latency"] or 0))
            out["features"] = corr[:DRIVER_TOP]
            out["n_features_tested"] = len(corr)
        if len(rows) >= 5 and np.std(tin) > 0:
            slope, icept = np.polyfit(tin, lat, 1)
            r = float(np.corrcoef(tin, lat)[0, 1])
            idx = np.linspace(0, len(rows) - 1, min(len(rows), SCALING_POINTS)).round().astype(int)
            out["input_scaling"] = {"slope_s_per_1k_tokens": float(slope) * 1000, "intercept_s": float(icept), "r": r,
                                    "min_tokens": float(tin.min()), "max_tokens": float(tin.max()),
                                    "points": [[float(tin[i]), float(lat[i])] for i in sorted(set(idx.tolist()))]}
        per.append(out)
    return {"per_model": per, "has_features": feats is not None,
            "method": ("Spearman rank correlation of each model-visible flow feature with the flow's end-to-end latency "
                       f"and total tokens, per model (the {DRIVER_TOP} strongest for latency shown). Input scaling: a "
                       "least-squares line of latency against the flow's input tokens (all agents). Correlation is not "
                       "cause: features move together.")}


def cold_start_block(sr: list[dict], label_of: dict[str, str], loads: dict[str, Optional[dict]]) -> dict[str, Any]:
    """How long loading each model took, and whether its first flow was slower than the rest
    (a warm-up the measured mean includes). Model downloads are not part of the load time."""
    per = []
    for model, dbm in label_of.items():
        xs = [float(r["scenario_total_time_s"]) for r in sr if r["model"] == dbm and r["scenario_total_time_s"] is not None]
        ld = loads.get(model) or {}
        med = float(np.median(xs[1:])) if len(xs) > 1 else None
        per.append({"model": model, "load_s": ld.get("load_s"), "engine": ld.get("engine"),
                    "first_flow_s": xs[0] if xs else None, "median_rest_s": med,
                    "first_flow_penalty_s": (xs[0] - med) if xs and med is not None else None,
                    "first_flow_ratio": (xs[0] / med) if xs and med else None})
    return {"per_model": per, "measured": any(p["load_s"] is not None for p in per),
            "note": "Load time = loading the weights onto the GPU in a fresh worker (after any first-use "
                    "download, which is not counted). First-flow penalty = first flow minus the median of the rest."}


def read_texts(db: str | Path, run_id: str) -> dict[tuple, Optional[str]]:
    """{(db model, scenario, agent): answer text} — empty for runs from before texts were kept."""
    con = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)
    try:
        has = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_outputs'").fetchone()
        return {(m, sc, a): t for m, sc, a, t in con.execute(
            "SELECT model, scenario_id, agent_name, text FROM agent_outputs WHERE run_id=?", (run_id,))} if has else {}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# output reliability: is the pipeline's output usable? (not a detection score)
# ---------------------------------------------------------------------------
_TECHNIQUE = re.compile(r"\bT\d{4}(?:\.\d{3})?\b")


def _technique(text: Optional[str]) -> Optional[str]:
    m = _TECHNIQUE.search(text or "")
    return m.group(0) if m else None


def reliability_block(sr: list[dict], am: list[dict], label_of: dict[str, str], budgets: dict[str, int],
                      label_agent: str, verdict_agent: Optional[str], classes: list[str],
                      texts: dict[tuple, Optional[str]], labelled: bool,
                      mitre: Optional[dict[str, str]] = None,
                      repeat: Optional[dict[str, Optional[dict]]] = None) -> dict[str, Any]:
    """Per model, per flow: did the pipeline produce a usable answer?
      valid      the label agent's reply is one of the allowed labels AND no agent returned nothing
      truncated  some agent stopped at its token budget (its text may be cut; the answer can still be valid)
      consistent the verdict agent names the same class as the label agent (when there is one and its
                 text was recorded)
      reference  share of answers equal to the data set's reference label: a validity check that the
                 output is not noise — reported as one number, never ranked or scored here."""
    from ..pipeline.agents import normalize_class
    per = []
    for model, dbm in label_of.items():
        flows = [r for r in sr if r["model"] == dbm]
        rows: dict[str, list] = {}
        for r in am:
            if r["model"] == dbm:
                rows.setdefault(r["scenario_id"], []).append(r)
        n = len(flows)
        unparseable = {f["scenario_id"] for f in flows if f["predicted_label"] == "Unparseable"}
        empty = {sid for sid, rs in rows.items() if any((r["output_tokens"] or 0) == 0 for r in rs)}
        capped = {sid for sid, rs in rows.items()
                  if any(budgets.get(r["agent_name"]) and (r["output_tokens"] or 0) >= budgets[r["agent_name"]] for r in rs)}
        valid = [f for f in flows if f["scenario_id"] not in unparseable and f["scenario_id"] not in empty]
        cons = None
        if verdict_agent:
            pairs = [(f["predicted_label"], texts.get((dbm, f["scenario_id"], verdict_agent))) for f in valid]
            pairs = [(p, t) for p, t in pairs if t]
            if pairs:
                agree = sum(1 for p, t in pairs if normalize_class(t, classes) == p)
                cons = {"checked": len(pairs), "agree": agree, "rate": agree / len(pairs)}
        ref = None
        if labelled and n:
            ok = sum(1 for f in flows if f["correct"])
            ref = {"n": n, "match": ok, "rate": ok / n}
        # format: did the label agent reply with just a label, or did its reply need cleaning?
        fmt = None
        raw = [(f["scenario_id"], texts.get((dbm, f["scenario_id"], label_agent))) for f in flows]
        raw = [(sid, t) for sid, t in raw if t is not None]
        if raw:
            low = {c.lower(): c for c in classes}
            exact = sum(1 for _, t in raw if t.strip().strip(".\"'` ").lower() in low)
            parsed = sum(1 for _, t in raw if normalize_class(t, classes) != "Unparseable")
            fmt = {"checked": len(raw), "exact": exact, "exact_rate": exact / len(raw),
                   "cleaned": parsed - exact, "unparseable": len(raw) - parsed,
                   "mean_chars": float(np.mean([len(t) for _, t in raw]))}
        # MITRE: does the verdict agent name the same technique for the same label, and the reference one?
        mit = None
        if verdict_agent:
            ref_ids = {c: _technique(v) for c, v in (mitre or {}).items()}
            by_label: dict[str, list] = {}
            for f in valid:
                t = texts.get((dbm, f["scenario_id"], verdict_agent))
                if t is not None:
                    by_label.setdefault(f["predicted_label"], []).append(_technique(t))
            got = sum(len(v) for v in by_label.values())
            if got:
                modal = sum(max(v.count(x) for x in set(v)) for v in by_label.values())
                named = sum(1 for v in by_label.values() for x in v if x)
                match = sum(1 for lab, v in by_label.items() for x in v if x == ref_ids.get(lab))
                mit = {"checked": got, "named": named, "consistent": modal, "consistent_rate": modal / got,
                       "matches_reference_map": match, "match_rate": match / got,
                       "per_label": {lab: {"n": len(v), "techniques": {str(x): v.count(x) for x in set(v)}}
                                     for lab, v in sorted(by_label.items())}}
        # repeat check (opt-in): the same flows answered a second time by the same loaded model
        stab = None
        rc = (repeat or {}).get(model)
        if rc and rc.get("flows"):
            measured = {f["scenario_id"]: f["predicted_label"] for f in flows}
            pairs = [(measured.get(r["scenario_id"]), r.get("predicted")) for r in rc["flows"]
                     if r.get("predicted") is not None and r["scenario_id"] in measured]
            if pairs:
                same = sum(1 for a, b in pairs if a == b)
                stab = {"checked": len(pairs), "same": same, "rate": same / len(pairs)}
        per.append({"model": model, "n_flows": n,
                    "valid": len(valid), "valid_rate": (len(valid) / n) if n else None,
                    "unparseable": len(unparseable), "empty": len(empty), "truncated": len(capped),
                    "truncated_rate": (len(capped) / n) if n else None,
                    "verdict_consistency": cons, "reference_agreement": ref,
                    "format": fmt, "mitre": mit, "answer_stability": stab})
    return {
        "label_agent": label_agent, "verdict_agent": verdict_agent, "labelled": labelled,
        "per_model": per,
        "definitions": {
            "valid": f"the {label_agent} agent's reply is one of the allowed labels and no agent returned an empty answer",
            "truncated": "at least one agent stopped at its output-token budget (its text may be cut short)",
            "verdict_consistency": (f"the {verdict_agent} agent names the same class as the {label_agent} agent"
                                    if verdict_agent else "no verdict agent in this pipeline"),
            "format": f"the {label_agent} agent's reply is exactly one allowed label (else AgentMeter had to clean it up)",
            "mitre": ("the verdict agent names an ATT&CK technique (T####); consistent = the same technique for the "
                      "same label; reference map = the configured technique for that label"),
            "answer_stability": "opt-in repeat check: the first flows answered again by the same model, same answer",
            "reference_agreement": ("share of answers equal to the data set's reference label — a check that the "
                                    "output is meaningful, not a detection score; AgentMeter does not rank detectors"
                                    if labelled else "the flows have no reference labels"),
        },
    }


def latency_trend_block(sr: list[dict], label_of: dict[str, str]) -> dict[str, Any]:
    """Latency per flow in execution order (rowid), a rolling median and a drift test, per model."""
    per = []
    for model, dbm in label_of.items():
        xs = [float(r["scenario_total_time_s"]) for r in sr if r["model"] == dbm and r["scenario_total_time_s"] is not None]
        n = len(xs)
        window = max(3, (n // 10) | 1)
        med = rolling_median(xs, window) if xs else []
        idx = list(range(n))
        if n > TREND_MAX_POINTS:                       # evenly thinned for storage; stats use every flow
            idx = sorted({round(i * (n - 1) / (TREND_MAX_POINTS - 1)) for i in range(TREND_MAX_POINTS)})
        per.append({"model": model, "n": n, "window": window, "thinned": n > TREND_MAX_POINTS,
                    "points": [[i + 1, xs[i], med[i]] for i in idx], "drift": drift_test(xs)})
    return {"metric": "end-to-end latency per flow (s), in run order",
            "point_fields": ["flow", "latency_s", "rolling_median_s"],
            "method": (f"Rolling median over ~10% of the flows (odd window, at least 3). Drift: Spearman rank "
                       f"correlation of flow order vs latency after the first {WARMUP_N} flows (warm-up is "
                       f"checked separately); called a trend when p < {TREND_ALPHA} and the last-quarter median "
                       f"differs from the first-quarter median by more than {round((TREND_RATIO - 1) * 100)}%."),
            "per_model": per}


def _flow_tokens(am: list[dict], dbm: str) -> list[float]:
    tok: dict[str, float] = {}
    for r in am:
        if r["model"] == dbm:
            tok[r["scenario_id"]] = tok.get(r["scenario_id"], 0) + (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
    return list(tok.values())


def effect_block(sr: list[dict], am: list[dict], label_of: dict[str, str],
                 statistics: Optional[dict] = None) -> dict[str, Any]:
    """Two models: A vs B. Three or four: every pair under "pairs", and the top level is the
    pair of the two fastest models (the closest race on latency)."""
    models = list(label_of)
    if len(models) < 2:
        return {"skipped": "effect sizes compare two models (single-model session)"}
    if len(models) > 2:
        lat = {m: float(np.mean([r["scenario_total_time_s"] for r in sr if r["model"] == label_of[m]] or [np.inf]))
               for m in models}
        pairs = [_effect_pair(sr, am, label_of, statistics, a, b) for a, b in itertools.combinations(models, 2)]
        f1, f2 = sorted(models, key=lambda m: (lat[m], models.index(m)))[:2]
        main = next(p for p in pairs if {p["model_a"], p["model_b"]} == {f1, f2})
        return {**main, "pairs": pairs, "pair_basis": "the two fastest models (lowest mean latency)",
                "note": "Kruskal-Wallis p is the session's test across all the models; pairwise "
                        "significance is the Dunn post-hoc test (Statistics)."}
    return _effect_pair(sr, am, label_of, statistics, *models)


def _effect_pair(sr: list[dict], am: list[dict], label_of: dict[str, str], statistics: Optional[dict],
                 a: str, b: str) -> dict[str, Any]:
    da, db_ = label_of[a], label_of[b]
    stats = statistics or {}
    series = {
        "latency_s": ([r["scenario_total_time_s"] for r in sr if r["model"] == da],
                      [r["scenario_total_time_s"] for r in sr if r["model"] == db_], "scenario_total_time_s"),
        "peak_vram_mb": ([r["scenario_peak_vram_mb"] for r in sr if r["model"] == da and r["scenario_peak_vram_mb"] is not None],
                         [r["scenario_peak_vram_mb"] for r in sr if r["model"] == db_ and r["scenario_peak_vram_mb"] is not None],
                         "scenario_peak_vram_mb"),
        "tokens_per_flow": (_flow_tokens(am, da), _flow_tokens(am, db_), None),
    }
    rows = []
    for metric, (xa, xb, stat_key) in series.items():
        kw = stats.get(stat_key) if stat_key else None
        p = kw.get("p_value") if isinstance(kw, dict) else None
        if not xa or not xb:
            rows.append({"metric": metric, "available": False,
                         "reason": "no VRAM readings (CPU/mock run)" if metric == "peak_vram_mb" else "no data",
                         "kruskal_wallis_p": p})
            continue
        d = cliffs_delta(xa, xb)
        ci = bootstrap_mean_diff_ci(xa, xb)
        rows.append({"metric": metric, "available": True, "n_a": len(xa), "n_b": len(xb),
                     "mean_a": float(np.mean(xa)), "mean_b": float(np.mean(xb)),
                     "cliffs_delta": d, "magnitude": cliffs_magnitude(d),
                     "diff_mean_a_minus_b": ci["diff"], "ci95_low": ci["ci_low"], "ci95_high": ci["ci_high"],
                     "ci_excludes_zero": (ci["ci_low"] is not None and (ci["ci_low"] > 0 or ci["ci_high"] < 0)),
                     "kruskal_wallis_p": p})
    n_min = min(len(series["latency_s"][0]), len(series["latency_s"][1]))
    return {"model_a": a, "model_b": b,
            "method": (f"Cliff's delta (P(A>B) - P(A<B); negative = {a} lower) with Romano et al. magnitude; "
                       f"percentile bootstrap 95% CI of mean(A) - mean(B), {N_BOOT} resamples, seed {BOOT_SEED}. "
                       "The Kruskal-Wallis p is the session's existing test (not recomputed)."),
            "metrics": rows,
            "small_sample_note": SMALL_NOTE.format(n=n_min) if n_min < MIN_STABLE_N else None}


# ---------------------------------------------------------------------------
# B. cost and energy
# ---------------------------------------------------------------------------
def _ts(s: Optional[str]) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


def model_windows(sr: list[dict], label_of: dict[str, str]) -> dict[str, Optional[tuple[float, float]]]:
    """[first flow start, last flow end] per model from completed_at (1 s resolution:
    each stamp is taken as the middle of its second)."""
    out = {}
    for model, dbm in label_of.items():
        rows = [r for r in sr if r["model"] == dbm and _ts(r.get("completed_at")) is not None]
        if not rows:
            out[model] = None
            continue
        t0 = min(_ts(r["completed_at"]) + 0.5 - float(r["scenario_total_time_s"] or 0) for r in rows)
        t1 = max(_ts(r["completed_at"]) + 0.5 for r in rows)
        out[model] = (t0, t1)
    return out


def cost_energy_block(payload: dict[str, Any], sr: list[dict], label_of: dict[str, str],
                      *, real: bool, price: Optional[dict] = None,
                      energy: Optional[dict] = None) -> dict[str, Any]:
    price = price or {"usd_per_hour": None, "source": None, "reason": "no price configured"}
    energy = energy or {}
    windows = model_windows(sr, label_of)
    per = []
    for pm in payload.get("per_model") or []:
        m = pm["model"]
        e2e = (pm.get("efficiency") or {}).get("end_to_end") or {}
        n = (pm.get("efficiency") or {}).get("n_flows") or 0
        row: dict[str, Any] = {"model": m, "n_flows": n, "mean_latency_s": e2e.get("mean_latency_s")}
        if not real:
            row.update(cost_per_1k_flows_usd=None, cost_status="not measured",
                       cost_reason="mock/CPU run: the time is not a model measurement",
                       wh_per_flow=None, wh_per_1k_flows=None, energy_status="not measured",
                       energy_reason="mock/CPU run: no GPU power to sample")
        else:
            c = cost_per_1k_flows(e2e.get("mean_latency_s"), price.get("usd_per_hour"))
            row.update(cost_per_1k_flows_usd=c, cost_status="measured" if c is not None else "no_data",
                       cost_reason=None if c is not None else price.get("reason") or "no GPU price")
            win = windows.get(m)
            samples = energy.get("samples") or []
            wh = integrate_energy_wh(samples, *win) if (win and samples) else None
            if wh is not None and n:
                inwin = [w for t, w in samples if win[0] <= t <= win[1]]
                row.update(wh_per_flow=wh / n, wh_per_1k_flows=wh / n * 1000.0, energy_wh_total=wh,
                           mean_power_w=float(np.mean(inwin)) if inwin else None,
                           window_s=win[1] - win[0], n_samples=len(inwin), energy_status="measured",
                           energy_reason=None)
                # net of idle: the idle board power sampled right before this model's worker
                iw = (energy.get("idle") or {}).get(label_of.get(m, m)) or (energy.get("idle") or {}).get(m)
                idle = [w for t, w in samples if iw and iw["t0"] <= t <= iw["t1"]]
                if idle:
                    idle_w = float(np.mean(idle))
                    net = max(0.0, wh - idle_w * (win[1] - win[0]) / 3600.0)
                    row.update(idle_power_w=idle_w, idle_window_s=iw["t1"] - iw["t0"], idle_samples=len(idle),
                               wh_net_idle_total=net, wh_per_flow_net_idle=net / n,
                               wh_per_1k_flows_net_idle=net / n * 1000.0)
                else:
                    row.update(idle_power_w=None, wh_per_flow_net_idle=None, wh_per_1k_flows_net_idle=None,
                               idle_reason="no idle samples before this model's run" if iw else
                               "idle power was not sampled (energy.idle_sample_s = 0 or a session from before it)")
            else:
                row.update(wh_per_flow=None, wh_per_1k_flows=None, energy_status="no_data",
                           energy_reason=energy.get("error") or
                           ("no power samples in this model's window" if samples else "GPU power was not sampled"))
        per.append(row)
    return {
        "price": {k: price.get(k) for k in ("usd_per_hour", "source", "fetched_at", "instance_id", "reason", "error")
                  if price.get(k) is not None},
        "energy_sampling": {**{k: energy.get(k) for k in ("method", "interval_s", "n_samples", "gpu_index", "error")
                               if energy.get(k) is not None},
                            **({"idle_windows": len(energy["idle"])} if energy.get("idle") else {})},
        "per_model": per,
        "notes": {
            "cost": "Cost per 1,000 flows = mean end-to-end latency per flow x 1,000 / 3,600 x the GPU's "
                    "hourly price. Pipeline time only (model loading excluded).",
            "energy": "GPU board power sampled from the parent process (NVML, or nvidia-smi) while the "
                      "workers ran, integrated over each model's window (first flow start -> last flow end, "
                      "1 s timestamp resolution) and divided by its flows. Whole-board power, including "
                      "idle power between agent calls; model loading excluded. Sampling runs in a separate "
                      "process from the measured worker, so it does not add to the measured latency.",
            "energy_net": "Net of idle = the same energy minus the idle board power (mean of the samples "
                          "taken for a few seconds right before the model's worker started, GPU idle) x the "
                          "window length. The GPU can still be leaving a high-power state when the idle "
                          "sample starts, so the net figure is a lower bound on the model's own energy share.",
        },
    }


# ---------------------------------------------------------------------------
# GPU memory: working (per agent call, excludes weights) vs total peak (weights + working)
# ---------------------------------------------------------------------------
def gpu_timeline_block(sr: list[dict], label_of: dict[str, str], *, real: bool,
                       energy: Optional[dict] = None) -> dict[str, Any]:
    """Board power (W), GPU utilisation (%) and device memory (MB) over the session, as sampled
    by the parent process (power.py), thinned to bucket means; times are seconds from the first
    sample. Each model's measured window (first flow start .. last flow end) and its idle window
    are marked. Nothing here changes a stored number."""
    energy = energy or {}
    samples = energy.get("samples") or []
    if not real or not samples:
        reason = ("mock/CPU run: no GPU to sample" if not real else
                  energy.get("error") or "no GPU power samples were recorded")
        return {"sampled": False, "reason": reason, "points": [], "windows": [], "idle": [], "per_model": []}
    t0 = min(t for t, _ in samples)
    util = dict(energy.get("util_samples") or [])
    mem = dict(energy.get("mem_samples") or [])
    rows = sorted((t, w, util.get(t), mem.get(t)) for t, w in samples)
    n = len(rows)
    buckets = min(n, TIMELINE_MAX_POINTS)

    def mean(vs):
        vs = [v for v in vs if v is not None]
        return float(np.mean(vs)) if vs else None
    points = []
    for b in range(buckets):
        chunk = rows[b * n // buckets:(b + 1) * n // buckets]
        if chunk:
            points.append([round(mean([r[0] for r in chunk]) - t0, 3), mean([r[1] for r in chunk]),
                           mean([r[2] for r in chunk]), mean([r[3] for r in chunk])])
    wins = model_windows(sr, label_of)
    windows = [{"model": m, "from_s": w[0] - t0, "to_s": w[1] - t0} for m, w in wins.items() if w]
    idle = []
    for m, dbm in label_of.items():
        iw = (energy.get("idle") or {}).get(dbm) or (energy.get("idle") or {}).get(m)
        if iw:
            idle.append({"model": m, "from_s": iw["t0"] - t0, "to_s": iw["t1"] - t0})
    per = []
    for m, w in wins.items():
        if not w:
            continue
        inw = [r for r in rows if w[0] <= r[0] <= w[1]]
        per.append({"model": m, "n_samples": len(inw), "mean_power_w": mean([r[1] for r in inw]),
                    "peak_power_w": max((r[1] for r in inw), default=None),
                    "mean_util_pct": mean([r[2] for r in inw]), "peak_mem_mb": max((r[3] for r in inw if r[3] is not None), default=None)})
    return {"sampled": True, "method": energy.get("method"), "interval_s": energy.get("interval_s"),
            "n_samples": n, "thinned": n > TIMELINE_MAX_POINTS, "has_util": bool(util), "has_mem": bool(mem),
            "point_fields": ["t_s", "power_w", "util_pct", "mem_mb"], "points": points,
            "windows": windows, "idle": idle, "per_model": per}


def load_scaling_block(db_path: str | Path, run_id: str, label_of: dict[str, str]) -> dict[str, Any]:
    """The opt-in load-scaling results of this run (one file per model), or why there are none."""
    from ..run.load_test import read_results
    found = read_results(db_path, run_id, label_of)
    per = [{**r, "model": m} for m, r in found.items() if r]
    if not per:
        return {"measured": False, "reason": "not requested for this session (opt-in: \"Also measure load scaling\")",
                "per_model": []}
    return {"measured": True, "method": per[0].get("method"), "per_model": per,
            "note": ("Separate from the measured flows: uninstrumented, run after them in the same worker. "
                     "On one GPU, flows in flight share it; throughput that does not grow with concurrency "
                     "means the model is already using the GPU fully one flow at a time.")}


def read_model_memory(db: str | Path, run_id: str) -> dict[str, dict]:
    """model_memory rows (Phase 46 follow-up) by DB model label; {} for older sessions."""
    con = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM model_memory WHERE run_id=?", (run_id,)).fetchall()
    except sqlite3.OperationalError:            # table absent: the session predates it
        rows = []
    finally:
        con.close()
    return {r["model"]: dict(r) for r in rows}


def memory_block(payload: dict[str, Any], mem_rows: dict[str, dict], label_of: dict[str, str], *,
                 real: bool, agents: Optional[dict] = None, sr: Optional[list[dict]] = None,
                 energy: Optional[dict] = None) -> dict[str, Any]:
    agent_rows = {m["model"]: m for m in ((agents or {}).get("per_model") or [])}
    windows = model_windows(sr or [], label_of)
    msamp = (energy or {}).get("mem_samples") or []
    per = []
    for pm in payload.get("per_model") or []:
        m = pm["model"]
        e2e = (pm.get("efficiency") or {}).get("end_to_end") or {}
        peaks = [a.get("max_peak_vram_mb") for a in (agent_rows.get(m) or {}).get("agents", [])
                 if a.get("max_peak_vram_mb") is not None]
        row: dict[str, Any] = {"model": m, "working_mean_mb": e2e.get("mean_peak_vram_mb"),
                               "working_peak_mb": max(peaks) if peaks else None}
        r = mem_rows.get(label_of.get(m, m)) or mem_rows.get(m)
        win = windows.get(m)
        nv = [mb for t, mb in msamp if win and win[0] <= t <= win[1]]
        if r:
            cands = [x for x in (r.get("peak_reserved_mb"), max(nv) if nv else None) if x is not None]
            row.update(weights_mb=r.get("weights_allocated_mb"), weights_reserved_mb=r.get("weights_reserved_mb"),
                       device_after_load_mb=r.get("weights_nvml_mb"), device_before_load_mb=r.get("device_before_load_mb"),
                       total_peak_allocated_mb=r.get("peak_allocated_mb"), total_peak_reserved_mb=r.get("peak_reserved_mb"),
                       device_peak_nvml_mb=max(nv) if nv else None,
                       total_peak_mb=max(cands) if cands else None,
                       total_basis=("max(torch peak reserved, NVML device-used peak sampled during the run)"
                                    if nv else "torch peak reserved over all agent calls"),
                       status="measured" if cands else "no_data")
        else:
            row.update(weights_mb=None, total_peak_mb=None, total_peak_allocated_mb=None,
                       total_peak_reserved_mb=None, device_peak_nvml_mb=None,
                       status="not measured" if not real else "no_data",
                       reason=("mock/CPU run: no GPU memory to measure" if not real else
                               "total peak memory was not recorded (a session from before it was added)"))
        per.append(row)
    return {
        "per_model": per,
        "notes": {
            "working": "Working VRAM (excludes model weights) = the torch allocator peak ABOVE the memory "
                       "held before each agent call; the mean and the highest over all calls.",
            "total": "Total peak = loaded weights + working memory: the larger of torch's peak reserved "
                     "memory over all agent calls and the NVML device-used peak sampled during the model's "
                     "run (which also counts the CUDA context). Weights = torch allocated right after load. "
                     "The decision helper's VRAM limit uses the total peak.",
        },
    }


# ---------------------------------------------------------------------------
# identity (Compare / Leaderboard)
# ---------------------------------------------------------------------------
SETTINGS_KEYS = (("model", "provider"), ("model", "hf"), ("pipeline", "agents"), ("pipeline", "max_new_tokens"),
                 ("classes",), ("dataset", "max_feature_chars"))


PREPARED_SET_HASH_VERSION = 2


def prepared_set_content_hash(features_csv: str, labels_csv: Optional[str],
                              feature_columns: list[str]) -> str:
    """Phase 47 follow-up — hash of WHAT THE MODELS SEE AND ARE SCORED AGAINST: the
    model-visible feature columns (input.json feature_columns), row by row in benchmark order,
    plus each row's ground-truth label (labels.csv `label`) when the set is labelled.

    Identification columns (IPs, ports, protocol, timestamp), the hidden flow id and the
    selection-audit columns are NOT part of it, so the backend's full copy and the control
    plane's stripped copy of the same prepared set hash the same. Values are hashed as the
    exact strings in the file (no float re-formatting)."""
    import csv
    import io

    rows = list(csv.reader(io.StringIO(features_csv)))
    header, body = rows[0], [r for r in rows[1:] if r]
    idx = [header.index(c) for c in feature_columns]          # KeyError-free: ValueError if missing
    labels: Optional[list[str]] = None
    if labels_csv:
        lrows = list(csv.reader(io.StringIO(labels_csv)))
        li = lrows[0].index("label")
        labels = [r[li] for r in lrows[1:] if r]
        if len(labels) != len(body):
            raise ValueError("labels.csv does not align with the features")
    h = hashlib.sha256(b"agentmeter-prepared-set-v2\0")
    h.update("\x1f".join(feature_columns).encode() + b"\n")
    for i, r in enumerate(body):
        h.update("\x1f".join(r[j] for j in idx).encode())
        h.update(b"\x1e" + (labels[i].encode() if labels is not None else b"") + b"\n")
    return h.hexdigest()


def prepared_set_sha256(run_dir: str | Path) -> Optional[str]:
    """Hash of the flows actually benchmarked — see prepared_set_content_hash (version 2).
    None when the run dir has no flows."""
    run_dir = Path(run_dir)
    sel, meta = run_dir / "selected_flows.csv", run_dir / "input.json"
    if not sel.is_file() or not meta.is_file():
        return None
    try:
        m = json.loads(meta.read_text(encoding="utf-8"))
        lab = run_dir / "labels.csv"
        return prepared_set_content_hash(sel.read_text(encoding="utf-8"),
                                         lab.read_text(encoding="utf-8") if lab.is_file() else None,
                                         list(m["feature_columns"]))
    except (OSError, ValueError, KeyError):
        return None


def settings_of(config_data: dict[str, Any]) -> dict[str, Any]:
    out = {}
    custom = (config_data.get("pipeline") or {}).get("custom")
    if custom:        # a user-defined pipeline: its agents are part of what was measured
        out["pipeline.custom"] = custom
    engine = (config_data.get("model") or {}).get("engine")
    if engine and engine != "transformers":     # another inference engine measures differently
        out["model.engine"] = engine
    for path in SETTINGS_KEYS:
        v: Any = config_data
        for k in path:
            v = v.get(k) if isinstance(v, dict) else None
        if path == ("model", "hf") and isinstance(v, dict):
            v = {k: x for k, x in v.items() if k not in ("name", "cache_dir", "token")}
        out[".".join(path)] = v
    return out


def settings_fingerprint(settings: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True, default=str).encode()).hexdigest()[:16]


def identity_block(run_dir: str | Path, config_data: dict[str, Any], environment: dict[str, Any]) -> dict[str, Any]:
    st = settings_of(config_data)
    env = environment or {}
    return {"prepared_set_sha256": prepared_set_sha256(run_dir),
            "prepared_set_hash_version": PREPARED_SET_HASH_VERSION,
            "gpu": env.get("gpu_name") if env.get("provider") == "real" else None,
            "gpu_label": (env.get("gpu_name") or "unknown GPU") if env.get("provider") == "real" else "Demo (mock, no GPU)",
            "provider": env.get("provider"),
            "settings": st, "settings_fingerprint": settings_fingerprint(st)}


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------
def add_analyses(payload: dict[str, Any], *, db_path: str | Path, run_id: str, run_dir: str | Path,
                 config_data: dict[str, Any], price: Optional[dict] = None,
                 energy: Optional[dict] = None) -> dict[str, Any]:
    """Attach the Phase-46 blocks to a scored payload (in place) and return it."""
    from .constraints import evaluate_constraints

    label_of = {pm["model"]: pm.get("db_label") or pm["model"] for pm in payload.get("per_model") or []}
    sr, am = read_rows(db_path, run_id)
    budgets = {k: int(v) for k, v in ((config_data.get("pipeline") or {}).get("max_new_tokens") or {}).items() if v}
    env = payload.get("environment") or {}
    real = env.get("provider") == "real"
    from ..pipeline.spec import describe, label_agent
    pl = config_data.get("pipeline") or {}
    custom = bool(pl.get("custom"))
    payload["agents"] = agents_block(sr, am, label_of, budgets, decide_agent=label_agent(pl) or "decide",
                                     pipeline=describe(pl) if custom else None)
    verdict = next((a["name"] for a in describe(pl) if a["kind"] == "verdict"), None)
    from ..run.extras import read_all as read_extras
    labels = {m: label_of[m] for m in label_of}
    payload["output_reliability"] = reliability_block(
        sr, am, label_of, budgets, label_agent(pl) or "decide", verdict, list(config_data.get("classes") or []),
        read_texts(db_path, run_id), bool((payload.get("session") or {}).get("accuracy_available")),
        mitre=dict(config_data.get("mitre") or {}), repeat=read_extras(db_path, run_id, labels, "repeat_check"))
    payload["drivers"] = drivers_block(sr, am, label_of, run_dir)
    payload["cold_start"] = cold_start_block(sr, label_of, read_extras(db_path, run_id, labels, "model_load"))
    payload["rank_stability"] = rank_stability_block(sr, am, label_of, config_data.get("scoring"))
    payload["latency_distribution"] = latency_block(sr, label_of)
    payload["latency_trend"] = latency_trend_block(sr, label_of)
    payload["effect_sizes"] = effect_block(sr, am, label_of, payload.get("statistics"))
    payload["cost_energy"] = cost_energy_block(payload, sr, label_of, real=real, price=price, energy=energy)
    payload["gpu_timeline"] = gpu_timeline_block(sr, label_of, real=real, energy=energy)
    payload["load_scaling"] = load_scaling_block(db_path, run_id, label_of)
    payload["memory"] = memory_block(payload, read_model_memory(db_path, run_id), label_of, real=real,
                                     agents=payload["agents"], sr=sr, energy=energy)
    payload["session_identity"] = identity_block(run_dir, config_data, env)
    payload["decision_helper"] = evaluate_constraints(payload)
    return payload
