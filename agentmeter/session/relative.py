"""Relative (head-to-head) comparison of the two models in a session.

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
"""
from __future__ import annotations

from typing import Any, Optional

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
                        p_values: Optional[dict[str, float]] = None) -> Optional[dict[str, Any]]:
    """Head-to-head A-vs-B over Phase 38's per-model metrics (None unless 2 models).

    p_values: per-flow significance already computed by the session's statistics
    (keys = efficiency metric keys); a latency/VRAM gap with p >= ALPHA is a tie."""
    if len(models) != 2:
        return None
    a, b = models
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
        if acc["winner"] == "tie":
            sentence += f" Accuracy: tie ({a} {va:.1%} vs {b} {vb:.1%})."
        else:
            sentence += (f" Accuracy: {acc['winner']} is higher by {acc['diff_pp']} percentage points "
                         f"({a} {va:.1%} vs {b} {vb:.1%}).")
        if winner and acc["winner"] not in ("tie", winner):
            sentence += (f" Trade-off: {winner} is more efficient, {acc['winner']} is more accurate.")
    else:
        sentence += " Accuracy: not available (no ground-truth labels) — efficiency only."

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
