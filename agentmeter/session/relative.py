"""Relative (head-to-head) comparison of the models in a session (2 to 4).

The absolute SAW (scoring.py) normalises each model against FIXED targets from
the L4 study, so two models that are both under a target both score the
maximum on it and can tie even when one is clearly better. This module adds a
direct A-vs-B view over the SAME per-model metrics Phase 38 already computed.
Nothing is re-measured, and the absolute SAW output is left unchanged — both
are reported.

Per metric (lower is better for latency, VRAM, tokens; higher for accuracy):
  * winner, or "tie" when within the threshold (2%; 2 pp for accuracy). Latency
    and VRAM are per-flow samples, so a winner must ALSO be significant in the
    session's existing Kruskal-Wallis test (p < 0.05) — a gap that is only timing
    noise is reported as a tie with its p-value, never as a false winner.
  * pct_better: how much better the winner is, relative to the other model
    ((loser - winner) / loser for costs), and ratio = loser / winner
  * accuracy: difference in percentage points (labelled runs only)

Overall efficiency verdict (transparent dominance rule, no weights):
  * one model wins at least one efficiency metric and loses none -> it is
    "more resource-efficient"
  * each wins at least one -> "mixed" (the trade-off is spelled out)
  * all ties / unavailable -> "tie"
Accuracy, when labelled, is reported beside the efficiency verdict, never
folded into it, so a trade-off (faster but less accurate) stays visible.

3 or 4 models: every pair is compared exactly as above (pairwise significance from the
session's Dunn post-hoc test instead of the overall Kruskal-Wallis p), and
  * per metric, the best model against the runner-up (same tie rules);
  * a model that is "more resource-efficient" than EACH other model in its pair is the
    most efficient overall; otherwise "mixed" (or "tie" when every pair ties);
  * ranking: pairwise wins - losses (a view of the pair verdicts, no weights).
"""
from __future__ import annotations

from itertools import combinations
from typing import Any, Optional

# the verdict is about resource efficiency; agreement with the data set's reference label is
# reported beside it as a validity check of the output (not a detection score, never ranked)
VALIDITY = "Validity check (answers matching the data set's reference label):"
NO_LABELS = "No reference labels in this data: efficiency only."
REL_TIE = 0.02        # efficiency metrics within 2% of each other: tie
ACC_TIE_PP = 2.0      # accuracy within 2 percentage points: tie
ALPHA = 0.05          # latency/VRAM winners must also be significant (per-flow Kruskal-Wallis)

EFFICIENCY_METRICS = [
    # key in efficiency.end_to_end, label, phrase when better, unit
    ("mean_latency_s", "mean end-to-end latency per flow", "lower latency", "s"),
    ("mean_peak_vram_mb", "mean peak working VRAM", "less VRAM", "MB"),
    ("mean_tokens_per_flow", "mean tokens per flow", "fewer tokens per flow", "tokens"),
]


def _cost_metric(key: str, label: str, phrase: str, unit: str,
                 a: str, b: str, va: Optional[float], vb: Optional[float],
                 p_value: Optional[float] = None) -> dict[str, Any]:
    out = {"metric": key, "label": label, "lower_is_better": True, "unit": unit,
           "values": {a: va, b: vb}}
    if va is None or vb is None:
        return {**out, "available": False, "winner": None,
                "note": "not measured for both models (e.g. no VRAM on CPU/mock)"}
    worse, better = max(va, vb), min(va, vb)
    if worse == 0 or (worse - better) / worse < REL_TIE:
        return {**out, "available": True, "winner": "tie", "pct_better": None, "ratio": None,
                "note": f"within {REL_TIE:.0%}"}
    if p_value is not None and p_value != p_value:
        # NaN: the test could not be computed (e.g. a flow without a reading) — claim nothing
        return {**out, "available": True, "winner": "tie", "pct_better": None, "ratio": None,
                "p_value": None,
                "note": f"{(worse - better) / worse:.0%} apart, but significance could not be tested "
                        "(some flows have no reading)"}
    if p_value is not None and p_value >= ALPHA:
        # Large-looking gap, but per-flow values overlap too much to call it (noise).
        return {**out, "available": True, "winner": "tie", "pct_better": None, "ratio": None,
                "p_value": p_value,
                "note": f"{(worse - better) / worse:.0%} apart but not statistically significant "
                        f"(Kruskal-Wallis p={p_value:.3f} >= {ALPHA})"}
    winner = a if va < vb else b
    return {**out, "available": True, "winner": winner, "p_value": p_value,
            "pct_better": round((worse - better) / worse * 100, 1),
            "ratio": round(worse / better, 2) if better else None,
            "phrase": phrase}


def _accuracy_metric(a: str, b: str, va: float, vb: float) -> dict[str, Any]:
    diff_pp = (va - vb) * 100
    out = {"metric": "accuracy", "label": "accuracy on this sample", "lower_is_better": False,
           "unit": "%", "values": {a: va, b: vb}, "available": True,
           "diff_pp": round(abs(diff_pp), 1)}
    if abs(diff_pp) < ACC_TIE_PP:
        return {**out, "winner": "tie", "note": f"within {ACC_TIE_PP:g} percentage points"}
    return {**out, "winner": a if va > vb else b}


def relative_comparison(models: list[str], per_model: dict[str, dict], labelled: bool,
                        caveats: list[str], hardware: Optional[str] = None,
                        p_values: Optional[dict[str, float]] = None,
                        pair_p: Optional[dict[str, dict[tuple, float]]] = None) -> Optional[dict[str, Any]]:
    """Head-to-head over Phase 38's per-model metrics (None for a single-model session).

    p_values: per-flow significance already computed by the session's statistics
    (keys = efficiency metric keys); a latency/VRAM gap with p >= ALPHA is a tie.
    pair_p (3-4 models): {metric key: {(model, model): Dunn p}} for the pairwise tests."""
    if len(models) < 2:
        return None
    if len(models) > 2:
        return _multi(models, per_model, labelled, caveats, hardware, pair_p or {})
    return _pair(models[0], models[1], per_model, labelled, caveats, hardware, p_values)


def _pair(a: str, b: str, per_model: dict[str, dict], labelled: bool, caveats: list[str],
          hardware: Optional[str], p_values: Optional[dict[str, float]]) -> dict[str, Any]:
    ea = per_model[a]["efficiency"]["end_to_end"]
    eb = per_model[b]["efficiency"]["end_to_end"]
    p_values = p_values or {}
    eff = [_cost_metric(k, lbl, ph, u, a, b, ea.get(k), eb.get(k), p_values.get(k))
           for k, lbl, ph, u in EFFICIENCY_METRICS]

    wins = {a: [m for m in eff if m["winner"] == a], b: [m for m in eff if m["winner"] == b]}
    if wins[a] and not wins[b]:
        eff_verdict, winner, loser = "more_efficient", a, b
    elif wins[b] and not wins[a]:
        eff_verdict, winner, loser = "more_efficient", b, a
    elif wins[a] and wins[b]:
        eff_verdict, winner, loser = "mixed", None, None
    else:
        eff_verdict, winner, loser = "tie", None, None

    def because(ms: list[dict]) -> str:
        return ", ".join(f"{m['pct_better']}% {m['phrase']}" for m in ms)

    ties = [m["label"] for m in eff if m["winner"] == "tie"]
    missing = [m["label"] for m in eff if not m["available"]]
    if eff_verdict == "more_efficient":
        sentence = f"{winner} is more resource-efficient than {loser}: {because(wins[winner])}"
    elif eff_verdict == "mixed":
        sentence = (f"Mixed efficiency: {a} has {because(wins[a])}; "
                    f"{b} has {because(wins[b])}")
    else:
        sentence = f"{a} and {b} are tied on resource efficiency"
    if ties:
        sentence += f" (tie on {', '.join(ties)})"
    if missing:
        sentence += f" ({', '.join(missing)}: not available)"
    sentence += "."

    acc = None
    if labelled:
        acc = _accuracy_metric(a, b, per_model[a]["accuracy"]["accuracy"],
                               per_model[b]["accuracy"]["accuracy"])
        va, vb = acc["values"][a], acc["values"][b]
        sentence += f" {VALIDITY} {a} {va:.1%}, {b} {vb:.1%}."
    else:
        sentence += f" {NO_LABELS}"

    rel_caveats = [
        "Relative view: both models were measured in this session on the same hardware and the "
        "same flows, so the A-vs-B differences are like-for-like; it does not use the fixed L4 "
        "targets (the absolute SAW does).",
    ]
    if hardware and "L4" not in hardware:
        rel_caveats.append(f"Measured on {hardware}: absolute numbers are not comparable with the "
                           "locked study (NVIDIA L4); only the A-vs-B relation is.")
    rel_caveats += caveats

    return {
        "models": [a, b],
        "basis": "direct A-vs-B on this session's measured metrics (no fixed targets)",
        "thresholds": {"efficiency_tie_rel": REL_TIE, "accuracy_tie_pp": ACC_TIE_PP,
                       "significance_alpha": ALPHA},
        "metrics": eff + ([acc] if acc else []),
        "efficiency_verdict": {
            "verdict": eff_verdict, "model": winner,
            "wins": {a: [m["metric"] for m in wins[a]], b: [m["metric"] for m in wins[b]]},
            "rule": "dominance: wins at least one efficiency metric and loses none",
        },
        "accuracy_verdict": ({"model": acc["winner"], "diff_pp": acc["diff_pp"]} if acc else None),
        "accuracy_included": labelled,
        "verdict": sentence,
        "caveats": rel_caveats,
    }


def _pp(pair_p: dict[str, dict[tuple, float]], key: str, a: str, b: str) -> Optional[float]:
    d = pair_p.get(key) or {}
    return d.get((a, b), d.get((b, a)))


def _multi(models: list[str], per_model: dict[str, dict], labelled: bool, caveats: list[str],
           hardware: Optional[str], pair_p: dict[str, dict[tuple, float]]) -> dict[str, Any]:
    n = len(models)
    pairs = [_pair(a, b, per_model, labelled, [], hardware,
                   {k: p for k in pair_p if (p := _pp(pair_p, k, a, b)) is not None})
             for a, b in combinations(models, 2)]
    eff = []
    for k, lbl, ph, u in EFFICIENCY_METRICS:
        vals = {m: per_model[m]["efficiency"]["end_to_end"].get(k) for m in models}
        known = sorted((v, m) for m, v in vals.items() if v is not None)
        if len(known) < n:
            eff.append({"metric": k, "label": lbl, "lower_is_better": True, "unit": u, "values": vals,
                        "available": False, "winner": None, "note": "not measured for every model (e.g. no VRAM on CPU/mock)"})
            continue
        (vb, best), (vr, runner) = known[0], known[1]
        m = _cost_metric(k, lbl, ph, u, best, runner, vb, vr, _pp(pair_p, k, best, runner))
        m.update(values=vals, runner_up=runner)
        eff.append(m)
    beats = {m: [o for o in models if o != m and any(
        p["efficiency_verdict"]["model"] == m and set(p["models"]) == {m, o} for p in pairs)] for m in models}
    losses = {m: sum(1 for p in pairs if m in p["models"] and p["efficiency_verdict"]["model"] not in (None, m))
              for m in models}
    top = [m for m in models if len(beats[m]) == n - 1]
    if top:
        eff_verdict, winner = "more_efficient", top[0]
    elif all(p["efficiency_verdict"]["verdict"] == "tie" for p in pairs):
        eff_verdict, winner = "tie", None
    else:
        eff_verdict, winner = "mixed", None
    ranking = sorted(({"model": m, "pair_wins": len(beats[m]), "pair_losses": losses[m]} for m in models),
                     key=lambda r: (-(r["pair_wins"] - r["pair_losses"]), models.index(r["model"])))
    for i, r in enumerate(ranking, 1):
        r["rank"] = i

    def best_line(m: dict) -> str:
        if not m["available"]:
            return f"{m['label']}: not available"
        if m["winner"] == "tie":
            return f"{m['label']}: no clear best ({m['note']})"
        return f"{m['phrase']}: {m['winner']} ({m['pct_better']}% ahead of {m['runner_up']})"

    if eff_verdict == "more_efficient":
        sentence = f"{winner} is more resource-efficient than each of the other {n - 1} models"
    elif eff_verdict == "mixed":
        sentence = f"Mixed efficiency across the {n} models: no model beats every other one"
    else:
        sentence = f"The {n} models are tied on resource efficiency"
    sentence += ". Best per metric: " + "; ".join(best_line(m) for m in eff) + "."

    acc = None
    if labelled:
        vals = {m: per_model[m]["accuracy"]["accuracy"] for m in models}
        (va, best), (vr, runner) = sorted(((v, m) for m, v in vals.items()), reverse=True)[:2]
        acc = _accuracy_metric(best, runner, va, vr)
        acc.update(values=vals, runner_up=runner)
        sentence += f" {VALIDITY} " + ", ".join(f"{m} {vals[m]:.1%}" for m in models) + "."
    else:
        sentence += f" {NO_LABELS}"

    rel_caveats = [
        f"Relative view: all {n} models were measured in this session on the same hardware and the same "
        "flows, so their differences are like-for-like; it does not use the fixed L4 targets (the "
        "absolute SAW does). Pairwise latency/VRAM differences must be significant in the session's "
        "Dunn post-hoc test (p < 0.05).",
    ]
    if hardware and "L4" not in hardware:
        rel_caveats.append(f"Measured on {hardware}: absolute numbers are not comparable with the "
                           "locked study (NVIDIA L4); only the relations between the models are.")
    rel_caveats += caveats
    return {
        "models": list(models),
        "mode": "multi",
        "basis": f"pairwise comparisons of the {n} models on this session's measured metrics (no fixed targets)",
        "thresholds": {"efficiency_tie_rel": REL_TIE, "accuracy_tie_pp": ACC_TIE_PP,
                       "significance_alpha": ALPHA},
        "metrics": eff + ([acc] if acc else []),
        "efficiency_verdict": {
            "verdict": eff_verdict, "model": winner,
            "wins": {m: [x["metric"] for x in eff if x.get("winner") == m] for m in models},
            "beats": beats,
            "rule": "more resource-efficient than each other model in its pair (pairwise dominance)",
        },
        "accuracy_verdict": ({"model": acc["winner"], "diff_pp": acc["diff_pp"]} if acc else None),
        "accuracy_included": labelled,
        "ranking": ranking,
        "pairs": pairs,
        "verdict": sentence,
        "caveats": rel_caveats,
    }
