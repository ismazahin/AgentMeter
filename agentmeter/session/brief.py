"""Decision-brief text logic — a FAITHFUL Python mirror of dashboard/report.js.

The PDF report (pdf_report.py) is built server-side, so it cannot call the
browser's report.js directly. Instead these functions mirror report.js's
`recommendation()` and `optimisationHint()` line for line — same inputs
(an analysis.json / session_results.json dict), same branches, same strings —
and tests/test_pdf_report.py runs report.js under node on the same payloads and
asserts identical output (the same parity approach as saw.js <-> analyze.py).
Change one, change the other.
"""
from __future__ import annotations

from typing import Any, Optional

_DS_PRIO = {"accuracy": "accuracy", "latency": "speed (latency)",
            "vram": "memory (VRAM)", "tokens": "token economy"}


def _js_num(x: Any) -> str:
    """Render a number the way JS string concatenation does (80.0 -> '80')."""
    f = float(x)
    return str(int(f)) if f.is_integer() else repr(f)


def _saw_rows(data: dict) -> list[dict]:
    p8 = (data or {}).get("phase8") or {}
    return p8.get("saw_table") or p8.get("raw_criteria") or []


def _ranked_models(data: dict, ranked: Optional[list[dict]]) -> list[str]:
    if ranked:
        return [r["model"] for r in ranked]
    rows = sorted(_saw_rows(data), key=lambda r: 99 if r.get("rank") is None else r["rank"])
    return [r["model"] for r in rows]


def recommendation(data: dict, ranked: Optional[list[dict]] = None,
                   weights: Optional[dict] = None) -> dict[str, Any]:
    """Mirror of report.js recommendation(data, ranked, weights)."""
    p8 = (data or {}).get("phase8") or {}
    rows = _saw_rows(data)
    if not rows:
        return {"headline": "", "caveat": "", "top": None, "allCritical": False}
    order = _ranked_models(data, ranked)
    top = order[0] if order else None
    w = weights or p8.get("weights") or {}
    keys = [k for k in _DS_PRIO if w.get(k) is not None]
    keys = sorted(keys, key=lambda k: -(float(w.get(k) or 0)))          # stable, like JS sort
    prios = [_DS_PRIO[k] for k in keys[:2]]
    pri_txt = ", then ".join(prios) if prios else "the configured weights"
    targets = p8.get("targets") or {}
    acc_target = float(targets["accuracy_pct"]) if targets.get("accuracy_pct") is not None else 80.0
    all_critical = all((r.get("tier") or "") == "Critical" for r in rows)
    headline = (f"Based on your current priorities ({pri_txt}), {top} is the most "
                f"resource-efficient choice.") if top else ""
    if all_critical:
        caveat = (f"All {len(rows)} models fall in the Critical tier (accuracy below the "
                  f"{_js_num(acc_target)}% target), so none is recommended for zero-shot deployment as-is"
                  + (f"; {top} is the best resource-efficient starting point." if top else "."))
    else:
        caveat = ("This is decision support, not a deployment sign-off — check each model's tier "
                  "and the accuracy target before deploying.")
    return {"headline": headline, "caveat": caveat, "top": top,
            "allCritical": all_critical, "accTarget": acc_target}


def optimisation_hint(data: dict, model: Optional[str] = None) -> dict[str, Any]:
    """Mirror of report.js optimisationHint(data, model): resource-only guidance."""
    dom = ((data or {}).get("per_agent") or {}).get("dominant") or []
    row = next((d for d in dom if d.get("model") == model), None) if model else None
    row = row or (dom[0] if dom else None)
    if not row:
        return {"text": "", "latency_agent": None, "vram_agent": None}
    la = row.get("latency_dominant_agent") or None
    va = row.get("vram_dominant_agent") or None
    where = []
    if la:
        where.append(f'the "{la}" step dominates latency')
    if va:
        where.append(f'"{va}" dominates working memory')
    text = ("Resource cost concentrates in the pipeline"
            + (": " + " and ".join(where) if where else "") + ". "
            "To cut resource cost, cap that step's token budget (lower its max_new_tokens) or run a "
            "lighter model for that one step — this reduces latency, memory and token use only; "
            "it does not change the model's verdicts.")
    return {"text": text, "latency_agent": la, "vram_agent": va}
