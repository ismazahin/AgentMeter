"""Phase 46 — additive efficiency analyses for a benchmark session.

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product.
Everything here READS the session's own DB (the same rows scoring.py read) and the
already-scored payload, and ADDS keys to session_results.json. Nothing here feeds
back into any score, SAW value, rank or verdict (tests/test_scored_keys_golden.py).

  agents                A. per-agent breakdown: latency / tokens and their share of
                           the flow, per-agent peak working VRAM, agentic overhead
                           (hand-off tokens), output failures, per-flow timeline
  latency_distribution  D. p50/p95/p99, histogram, warm-up check (first N flows)
  effect_sizes          D. Cliff's delta + bootstrap 95% CI of the A-B difference
                           (latency, VRAM, tokens), beside the existing Kruskal-Wallis p
  memory                working VRAM (excludes weights) vs total peak (weights + working)
  cost_energy           B. cost per 1,000 flows (measured time x GPU $/h) and energy
                           (sampled GPU board power) — "not measured" on mock/CPU
  decision_helper       C. constraint rules (configs/constraint_rules.yaml), evaluated
                           with the rule file's defaults; the UI/PDF re-evaluate with
                           the user's limits
  session_identity      prepared-set hash, GPU, settings fingerprint (Compare /
                           Leaderboard validity checks)
"""
from __future__ import annotations

import hashlib
import json
import math
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

    def at(t: float) -> float:
        for (ta, wa), (tb, wb) in zip(pts, pts[1:]):
            if ta <= t <= tb:
                return wa if tb == ta else wa + (wb - wa) * (t - ta) / (tb - ta)
        return pts[0][1] if t < pts[0][0] else pts[-1][1]

    xs = [t0] + [t for t, _ in pts if t0 < t < t1] + [t1]
    ys = [at(t) for t in xs]
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
    return [a for a in AGENT_ORDER if a in seen] + [a for a in seen if a not in AGENT_ORDER]


# ---------------------------------------------------------------------------
# A. agents
# ---------------------------------------------------------------------------
def agents_block(sr: list[dict], am: list[dict], label_of: dict[str, str],
                 budgets: dict[str, int], decide_agent: str = "decide") -> dict[str, Any]:
    order = _agents_in_order(am)
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
            fails = fail["empty_output"] + (fail["unparseable_label"] or 0)
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
                h = sum((fl[src]["output_tokens"] or 0) for src in HANDOFFS.get(ag, []) if src in fl)
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
        "handoffs": {a: HANDOFFS.get(a, []) for a in order},
        "notes": {
            "latency": "Per-agent latency = the agent's own model call (perf_counter); shares are of "
                       "the summed agent time, which is the flow's end-to-end latency.",
            "vram": "Per-agent peak working VRAM = torch allocator peak above the pre-call baseline "
                    "(excludes the loaded weights); null on CPU/mock.",
            "overhead": "Agentic overhead = tokens of earlier agents' outputs re-sent in later prompts "
                        "(hand-offs: " + ", ".join(f"{a} <- {'+'.join(s)}" for a, s in HANDOFFS.items() if s)
                        + "). Estimated from the recorded output-token counts; the rest of the input "
                          "(system prompt, flow features, instructions) is the task input.",
            "failures": "empty_output = 0 output tokens; hit_token_cap = output reached the agent's "
                        "max_new_tokens (likely truncated); unparseable_label = Decide's reply matched no "
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


def _flow_tokens(am: list[dict], dbm: str) -> list[float]:
    tok: dict[str, float] = {}
    for r in am:
        if r["model"] == dbm:
            tok[r["scenario_id"]] = tok.get(r["scenario_id"], 0) + (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
    return list(tok.values())


def effect_block(sr: list[dict], am: list[dict], label_of: dict[str, str],
                 statistics: Optional[dict] = None) -> dict[str, Any]:
    models = list(label_of)
    if len(models) != 2:
        return {"skipped": "effect sizes compare two models (single-model session)"}
    a, b = models
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


def prepared_set_sha256(run_dir: str | Path) -> Optional[str]:
    """Hash of the flows actually benchmarked (selected_flows.csv + labels.csv if any)."""
    run_dir = Path(run_dir)
    h = hashlib.sha256()
    found = False
    for name in ("selected_flows.csv", "labels.csv"):
        p = run_dir / name
        if p.is_file():
            h.update(name.encode() + b"\0" + p.read_bytes())
            found = True
    return h.hexdigest() if found else None


def settings_of(config_data: dict[str, Any]) -> dict[str, Any]:
    out = {}
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
    payload["agents"] = agents_block(sr, am, label_of, budgets)
    payload["latency_distribution"] = latency_block(sr, label_of)
    payload["effect_sizes"] = effect_block(sr, am, label_of, payload.get("statistics"))
    payload["cost_energy"] = cost_energy_block(payload, sr, label_of, real=real, price=price, energy=energy)
    payload["memory"] = memory_block(payload, read_model_memory(db_path, run_id), label_of, real=real,
                                     agents=payload["agents"], sr=sr, energy=energy)
    payload["session_identity"] = identity_block(run_dir, config_data, env)
    payload["decision_helper"] = evaluate_constraints(payload)
    return payload
