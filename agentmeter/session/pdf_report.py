"""One-to-two-page PDF benchmark report for a finished service run (Phase 42).

Every number is read VERBATIM from the job's session_results.json (plus the
run's own input.json / selection_audit.json for the rule-base section, which
session_results.json does not carry). Nothing is recomputed. The verdict is the
session's own `relative_comparison.verdict` / `comparison.statement`; the
recommendation text reuses brief.recommendation() / brief.optimisation_hint(),
the parity-tested mirror of dashboard/report.js.

Framing: a resource-efficiency benchmark (+ accuracy where labelled), not a
threat-detection product and not a deployment sign-off.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Optional

from . import brief

# Characters outside the PDF base fonts' WinAnsi set -> safe equivalents.
_SAFE = {"→": "->", "←": "<-", "≥": ">=", "≤": "<=", "≠": "!=", "×": "x", "�": "?",
         "✓": "yes", "✗": "no", "…": "...", "≈": "~", "µ": "u"}


def _safe(text: Any) -> str:
    s = "" if text is None else str(text)
    for k, v in _SAFE.items():
        s = s.replace(k, v)
    return s.encode("cp1252", "replace").decode("cp1252")


def _x(text: Any) -> str:
    """Safe + escaped for reportlab Paragraph markup."""
    return _safe(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_time(v) -> str:
    if v is None:
        return "-"
    return f"{v * 1000:.{2 if v < 0.01 else 1}f} ms" if v < 1 else f"{v:.2f} s"


def fmt_int(v) -> str:
    return "-" if v is None else f"{v:,.0f}"


def fmt_mb(v) -> str:
    return "not measured" if v is None else f"{v:,.0f} MB"


def fmt_pct(v) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def recommendation_lines(res: dict[str, Any]) -> dict[str, Any]:
    """Plain-language recommendation from the session's own verdicts + report.js logic."""
    s, rel, cmp_ = res["session"], res.get("relative_comparison"), res.get("comparison")
    labelled = bool(s.get("accuracy_available"))
    by_label = {m["db_label"]: m["model"] for m in res["per_model"]}
    to_label = {m["model"]: m["db_label"] for m in res["per_model"]}

    # Which model leads on resource efficiency: the head-to-head verdict (2 models),
    # never an arbitrary SAW rank-1 inside a tie.
    lines: list[str] = []
    lead: Optional[str] = None
    if rel:
        ev = rel["efficiency_verdict"]
        if ev["verdict"] == "more_efficient":
            lead = ev["model"]
            other = next(m for m in rel["models"] if m != lead)
            lines.append(f"{lead} is the more resource-efficient of the two models on this input "
                         f"(it wins on {len(ev['wins'][lead])} efficiency metric(s) and loses none to {other}).")
        elif ev["verdict"] == "mixed":
            a, b = rel["models"]
            lines.append(f"Neither model is more resource-efficient overall: {a} and {b} each win on "
                         "different efficiency metrics (see the comparison table) - choose by the resource "
                         "you are most constrained on.")
        else:
            lines.append("The two models are equally resource-efficient on this input: every efficiency "
                         "difference is within the tie threshold or not statistically significant.")
    else:
        m = res["per_model"][0]
        lines.append(f"Single-model run: {m['model']} scored SAW {m['saw']['composite_100']} "
                     f"({m['saw']['tier']}). Add a second model for a head-to-head comparison.")

    # report.js recommendation(): ranked by the head-to-head leader when there is one,
    # otherwise by the SAW table; its headline is used only when SAW has a clear top.
    ranked = None
    saw_top = (cmp_ or {}).get("saw_top")
    if lead:
        rows = (res.get("phase8") or {}).get("saw_table") or []
        ranked = [{"model": to_label[lead]}] + [{"model": r["model"]} for r in rows
                                                if r["model"] != to_label[lead]]
    elif rel and saw_top in (None, "tie"):
        # No clear leader anywhere: rank-1 inside a SAW tie is arbitrary, so give
        # recommendation() no top model -> it names none (same behaviour in report.js).
        ranked = [{"model": None}]
    rec = brief.recommendation(res, ranked=ranked)
    if rec["headline"] and (lead or saw_top not in (None, "tie")):
        lines.append(_relabel(rec["headline"], by_label))

    # Accuracy caveat — honest, never softened.
    if labelled:
        if rec["allCritical"]:
            lines.append(_relabel(rec["caveat"], by_label))
        else:
            lines.append(rec["caveat"])
        best = max((m["accuracy"]["accuracy"] for m in res["per_model"]), default=None)
        target = rec.get("accTarget", 80.0)
        if best is not None and best * 100 < target:
            lines.append(f"Highest accuracy on this sample is {best * 100:.1f}% (below the {target:g}% "
                         "target): this report ranks resource efficiency and is NOT an endorsement of "
                         "either model as a threat detector.")
    else:
        lines.append("No ground-truth labels: accuracy was not measured. This report ranks resource "
                     "efficiency only and is not an endorsement of detection quality.")

    hint = brief.optimisation_hint(res, to_label.get(lead) if lead else None)
    return {"lines": lines, "hint": _relabel(hint["text"], by_label) if hint["text"] else ""}


def _relabel(text: str, by_label: dict[str, str]) -> str:
    """DB labels (e.g. 'mock:mock-a') -> the model names the user picked."""
    for db, name in sorted(by_label.items(), key=lambda kv: -len(kv[0])):
        if db != name:
            text = text.replace(db, name)
    return text


def build_report_pdf(res: dict[str, Any], *, job: Optional[dict] = None,
                     input_meta: Optional[dict] = None, audit: Optional[dict] = None,
                     prepared: Optional[dict] = None, decision: Optional[dict] = None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    s = res["session"]
    rel = res.get("relative_comparison")
    cmp_ = res.get("comparison")
    labelled = bool(s.get("accuracy_available"))
    mock = s.get("provider") == "mock"
    pm = res["per_model"]
    names = [m["model"] for m in pm]
    input_meta = input_meta or {}
    audit = audit or {}
    prepared = prepared or {}           # manifest.json prepared_set block (Phase 43b), if any
    run_name = (job or {}).get("run_name") or s.get("run_dir", "").rstrip("/").split("/")[-1]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    ss = getSampleStyleSheet()
    ink, muted, line = colors.HexColor("#0a0a0a"), colors.HexColor("#5b5b63"), colors.HexColor("#d4d4d8")
    st = {
        "title": ParagraphStyle("t", parent=ss["Title"], fontName="Times-Roman", fontSize=21, leading=25,
                                alignment=TA_LEFT, spaceAfter=2, textColor=ink),
        "sub": ParagraphStyle("s", parent=ss["Normal"], fontSize=8.5, leading=11, textColor=muted),
        "h": ParagraphStyle("h", parent=ss["Heading2"], fontName="Times-Roman", fontSize=13.5,
                            leading=17, spaceBefore=9, spaceAfter=4, textColor=ink),
        "p": ParagraphStyle("p", parent=ss["Normal"], fontSize=9, leading=12.2, textColor=ink),
        "verdict": ParagraphStyle("v", parent=ss["Normal"], fontName="Times-Roman", fontSize=12,
                                  leading=15.5, textColor=ink, spaceAfter=4),
        "small": ParagraphStyle("sm", parent=ss["Normal"], fontSize=7.8, leading=10, textColor=muted),
        "cell": ParagraphStyle("c", parent=ss["Normal"], fontSize=8, leading=10),
        "banner": ParagraphStyle("b", parent=ss["Normal"], fontSize=8.8, leading=11.5,
                                 textColor=colors.HexColor("#8a1c1c")),
    }
    P = lambda t, k="p": Paragraph(_x(t), st[k])                    # noqa: E731
    Pm = lambda markup, k="p": Paragraph(markup, st[k])             # noqa: E731

    def table(rows, widths, num_cols=(), head=True):
        data = [[c if not isinstance(c, str) else Paragraph(_x(c), st["cell"]) for c in r] for r in rows]
        t = Table(data, colWidths=widths, repeatRows=1 if head else 0, hAlign="LEFT")
        style = [("FONTSIZE", (0, 0), (-1, -1), 8), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("LINEBELOW", (0, 0), (-1, -1), 0.4, line),
                 ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                 ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
        if head:
            style += [("TEXTCOLOR", (0, 0), (-1, 0), muted), ("LINEBELOW", (0, 0), (-1, 0), 0.8, ink)]
        t.setStyle(TableStyle(style))
        return t

    W = A4[0] - 30 * mm
    story: list = []
    story.append(P("AgentMeter — Benchmark Report", "title"))
    story.append(P(f"Run {run_name}  ·  generated {generated}  ·  session run id {s.get('run_id')}"
                   + (f"  ·  job {job['job_id']}" if job else ""), "sub"))
    story.append(P("Resource-efficiency benchmark of LLMs on a 4-agent threat-analysis pipeline "
                   "(+ accuracy where labels exist). Not a threat-detection product. NON-VALIDATED user run.",
                   "sub"))
    env = res.get("environment") or {}
    if env:
        if env.get("provider") == "real":
            mem = f", {env['gpu_vram_total_mb'] / 1024:.0f} GB" if env.get("gpu_vram_total_mb") else ""
            story.append(P(f"Measured on: REAL GPU {env.get('gpu_name') or 'unknown'}{mem}; NVIDIA driver "
                           f"{env.get('driver_version') or '-'}, CUDA {env.get('cuda_runtime_version') or '-'}; "
                           f"torch {env.get('torch') or '-'}, transformers {env.get('transformers') or '-'}, "
                           f"bitsandbytes {env.get('bitsandbytes') or '-'}; provider real (4-bit NF4); "
                           f"host {env.get('host') or '-'}.", "sub"))
        else:
            story.append(P("Measured on: no GPU — provider mock (demo).", "sub"))
    if mock:
        story.append(Spacer(1, 4))
        story.append(P("DEMO RUN — mock provider: a deterministic heuristic, not a language model. "
                       "The numbers below demonstrate the report, they are not model measurements.", "banner"))

    # --- input ---------------------------------------------------------------------------------
    story.append(P("Input", "h"))
    fm = s.get("feature_match") or {}
    kv = [
        ["Source", f"{str(s.get('source_type', '')).upper()}  ({input_meta.get('source_file', '-')})"],
        ["Labels", "Labelled -> accuracy + efficiency" if labelled else "Unlabelled -> efficiency only"],
        ["Candidate pool (after sampling + validation)", fmt_int(input_meta.get("rows_total"))],
        ["Flows selected and benchmarked", fmt_int(s.get("n_flows"))],
        ["Class set (scored)" if labelled else "Labels the model chooses from (not scored)",
         f"{s.get('class_scheme')}: {', '.join(s.get('class_set') or [])}"],
        ["Feature match", f"{fm.get('status', '-')}" + (f" - {fm['note']}" if fm.get("note") else "")],
        ["Selection mode", str(s.get("selection_mode") or "-").replace("_", " ")],
    ]
    samp = prepared.get("sampling") or {}
    if samp.get("description"):
        kv.append(["Sampling", samp["description"]
                   + (f"; pool: {samp['pool_method']}" if samp.get("pool_capped") else "")])
    if prepared.get("id"):
        kv.append(["Prepared set", prepared["id"]])
    cc = prepared.get("class_counts") or {}
    lost = (cc.get("lost_in_sampling") or []) + (cc.get("lost_in_selection") or [])
    if cc.get("available"):
        kv.append(["Classes absent after sampling", ", ".join(lost) if lost else "none"])
    story.append(table(kv, [52 * mm, W - 52 * mm], head=False))

    rules = audit.get("rules") or []
    if rules:
        story.append(P("Flow selection (rule-base)", "h"))
        story.append(P(f"{len(audit.get('rules_fired') or [])} of {len(rules)} rules fired; "
                       f"{fmt_int(audit.get('selected'))} of {fmt_int(audit.get('total_flows'))} flows selected "
                       f"(budget {fmt_int(audit.get('max_flows'))}).", "small"))
        rr = [["Rule", "Type", "Fired", "Flows admitted"]]
        for r in rules:
            constraint = r.get("kind") == "constraint"
            rr.append([r["id"], "constraint" if constraint else r["type"],
                       "yes" if r.get("fired") else ("disabled" if not r.get("enabled", True) else "no"),
                       "-" if constraint else fmt_int(r.get("admitted"))])
        story.append(table(rr, [52 * mm, 38 * mm, 22 * mm, 30 * mm]))

    # --- models ----------------------------------------------------------------------------------
    story.append(P("Models evaluated", "h"))
    story.append(P("; ".join(f"{m['model']}" + ("" if m.get("canonical") else " (not in the 5-model study set)")
                             for m in pm)
                   + f".  Provider {s.get('provider')}, hardware {s.get('hardware')}, quantisation {s.get('quant')}; "
                     "executed sequentially, one model at a time.", "p"))

    # --- comparison table --------------------------------------------------------------------------
    story.append(P("Per-metric comparison", "h"))
    if rel:
        rows = [["Metric", names[0], names[1], "Winner"]]
        for m in rel["metrics"]:
            is_acc = m["metric"] == "accuracy"
            f = fmt_pct if is_acc else fmt_time if m["metric"] == "mean_latency_s" else \
                fmt_mb if m["metric"] == "mean_peak_vram_mb" else fmt_int
            if not m["available"]:
                win = "not available"
            elif m["winner"] == "tie":
                win = "tie" + (f" ({m['note']})" if m.get("note") else "")
            else:
                win = f"{m['winner']} - " + (f"{m['diff_pp']} pp higher" if is_acc else f"{m['pct_better']}% better")
            rows.append([m["label"], f(m["values"][names[0]]), f(m["values"][names[1]]), win])
        story.append(table(rows, [44 * mm, 32 * mm, 32 * mm, W - 108 * mm]))
    hdr = ["Model", "Latency/flow", "p95", "Tokens/flow", "Peak VRAM"] + (["Accuracy"] if labelled else []) + \
          ["SAW" + ("" if labelled else " (eff.)"), "SAW eff.-only"]
    rows = [hdr]
    for m in pm:
        e = m["efficiency"]["end_to_end"]
        rows.append([m["model"], fmt_time(e["mean_latency_s"]), fmt_time(e["p95_latency_s"]),
                     fmt_int(e["mean_tokens_per_flow"]), fmt_mb(e["mean_peak_vram_mb"])]
                    + ([fmt_pct(m["accuracy"]["accuracy"])] if labelled else [])
                    + [f"{m['saw']['composite_100']} {m['saw']['tier']}",
                       str(m["saw_efficiency_only"]["composite_100"])])
    widths = [W - (6 + labelled) * 19 * mm] + [19 * mm] * (6 + labelled)
    story.append(Spacer(1, 5))
    story.append(table(rows, widths))

    # --- verdict -----------------------------------------------------------------------------------
    verdict = [P("Verdict", "h"),
               P(rel["verdict"] if rel else "Single-model run - no head-to-head comparison.", "verdict")]
    if cmp_:
        verdict.append(Pm("<b>Absolute SAW</b> (scored against fixed L4-study targets): " + _x(cmp_["statement"])
                        + "  Composites: " + _x(", ".join(f"{m['model']} {m['saw']['composite_100']} "
                                                          f"({m['saw']['tier']})" for m in pm)) + ".", "small"))
    story.append(KeepTogether(verdict))

    # --- recommendation ----------------------------------------------------------------------------
    rec = recommendation_lines(res)
    block = [P("Recommendation", "h")] + [P(l) for l in rec["lines"]]
    if rec["hint"]:
        block.append(P(rec["hint"], "small"))
    story.append(KeepTogether(block))

    # --- rule-base stage 3: measured verdict + external Hugging Face context --------------------
    s3 = res.get("recommendation_stage3")
    if s3:
        story.append(P("Recommendation — stage 3 (measured result + external model context)", "h"))
        story.append(P("Measured head-to-head verdict: " + str((s3.get("verdict") or {}).get("text") or "-")))
        mstat = s3.get("metadata_status")
        if s3.get("notes"):
            for n in s3["notes"]:
                story.append(P(f"- [{n['rule']}] {n['note']}  (source: {', '.join(n.get('sources') or [])}"
                               + (f"; {n['source_url']}, fetched {n['fetched_at']}" if n.get("fetched_at") else "")
                               + ")", "p"))
        else:
            story.append(P("No context rule fired" + ("" if mstat == "ok" else
                           f" (Hugging Face metadata {mstat}: the metadata rules had no data)") + ".", "p"))
        story.append(P("External metadata adds context only: it never changes any score, rank, SAW value "
                       f"or the verdict above. Rule set: {s3.get('rulebase')}.", "small"))
        rows = [["Rule", "Model", "Condition", "Result", "Source", "Fetched at"]]
        for r in s3.get("rules") or []:
            rows.append([r["rule"], r["model"].split("/")[-1], r["condition"],
                         r["result"] + ("" if r["result"] != "no_data" else f" ({r['detail']})"),
                         ", ".join(r.get("sources") or []), r.get("fetched_at") or "-"])
        story.append(P("Rules fired", "h"))
        story.append(table(rows, [28 * mm, 26 * mm, 46 * mm, 30 * mm, 24 * mm, W - 154 * mm]))

    # --- Phase 46: additive analyses (read verbatim from session_results.json) ---------------------
    short = lambda m: str(m).split("/")[-1]                          # noqa: E731
    ag = res.get("agents")
    if ag:
        story.append(P("Agents — per-agent breakdown", "h"))
        rows = [["Model", "Agent", "Latency", "Share", "Tokens in / out", "Token share", "Peak VRAM", "Failures"]]
        for m in ag.get("per_model") or []:
            for a in m.get("agents") or []:
                f = a.get("failures") or {}
                fl = [f"{f.get('empty_output', 0)} empty"]
                if f.get("hit_token_cap") is not None:
                    fl.append(f"{f['hit_token_cap']} at cap")
                if f.get("unparseable_label") is not None:
                    fl.append(f"{f['unparseable_label']} unparseable")
                rows.append([short(m["model"]), a["agent"], fmt_time(a.get("mean_latency_s")),
                             fmt_pct(a.get("latency_share")),
                             f"{fmt_int(a.get('mean_input_tokens'))} / {fmt_int(a.get('mean_output_tokens'))}",
                             fmt_pct(a.get("token_share")), fmt_mb(a.get("mean_peak_vram_mb")), ", ".join(fl)])
        story.append(table(rows, [30 * mm, 16 * mm, 18 * mm, 14 * mm, 26 * mm, 18 * mm, 24 * mm, W - 146 * mm]))
        for m in ag.get("per_model") or []:
            o = m.get("overhead") or {}
            story.append(P(f"{short(m['model'])}: agentic overhead {fmt_int(o.get('handoff_tokens_per_flow'))} "
                           f"hand-off tokens per flow = {fmt_pct(o.get('handoff_share_of_total'))} of all tokens "
                           f"({fmt_pct(o.get('handoff_share_of_input'))} of input).", "small"))
        story.append(P((ag.get("notes") or {}).get("overhead", "") + " " +
                       (ag.get("notes") or {}).get("failures", ""), "small"))

    ce = res.get("cost_energy")
    if ce:
        story.append(P("Cost and energy", "h"))
        rows = [["Model", "Cost / 1,000 flows", "Energy / flow", "Energy / 1,000 flows", "Mean GPU power"]]
        for r in ce.get("per_model") or []:
            cost = (f"${r['cost_per_1k_flows_usd']:.4f}" if r.get("cost_per_1k_flows_usd") is not None
                    else f"{r.get('cost_status')}: {r.get('cost_reason') or ''}".strip())
            wh = (f"{r['wh_per_flow']:.4f} Wh" if r.get("wh_per_flow") is not None
                  else f"{r.get('energy_status')}: {r.get('energy_reason') or ''}".strip())
            rows.append([short(r["model"]), cost, wh,
                         f"{r['wh_per_1k_flows']:.2f} Wh" if r.get("wh_per_1k_flows") is not None else "-",
                         f"{r['mean_power_w']:.0f} W" if r.get("mean_power_w") is not None else "-"])
        story.append(table(rows, [34 * mm, 42 * mm, 42 * mm, 32 * mm, W - 150 * mm]))
        pr, es = ce.get("price") or {}, ce.get("energy_sampling") or {}
        story.append(P("GPU price: " + (f"${pr['usd_per_hour']:.3f}/h, source {pr.get('source')}, fetched "
                                        f"{pr.get('fetched_at')}" if pr.get("usd_per_hour") is not None
                                        else (pr.get("reason") or "not applicable (mock/CPU run)"))
                       + ".  Power sampling: " + (f"{es.get('method')} every {es.get('interval_s')} s, "
                                                 f"{es.get('n_samples')} samples" if es.get("method")
                                                 else "not sampled (no GPU)") + ".", "small"))
        story.append(P((ce.get("notes") or {}).get("cost", "") + " " + (ce.get("notes") or {}).get("energy", ""),
                       "small"))

    dh = decision or res.get("decision_helper")
    if dh:
        story.append(P("Decision helper — your limits (stage-2 fit scoring)", "h"))
        if not dh.get("limits_set"):
            story.append(P("No limits were set. Enter them on the session's Recommendation tab; a PDF downloaded "
                           "from there includes them.", "p"))
        else:
            lims = "; ".join(f"{v.get('label')} {v['value']:g} {v.get('unit', '')}".strip()
                             for v in (dh.get("inputs") or {}).values() if v.get("value") is not None)
            story.append(P("Limits: " + lims + ".", "p"))
            rows = [["Model", "Result", "Details"]]
            for m in dh.get("per_model") or []:
                det = "; ".join(f"{r['rule']}: {r['result']}" + (f" ({r['note']})" if r.get("note") else "")
                                for r in m.get("rows") or [] if r["result"] != "not_set")
                rows.append([short(m["model"]), m["result"], det])
            story.append(table(rows, [34 * mm, 22 * mm, W - 56 * mm]))
        story.append(P(f"{dh.get('framing', '')} Rule set: {dh.get('rulebase')}.", "small"))

    ld, ef = res.get("latency_distribution"), res.get("effect_sizes")
    if ld:
        story.append(P("Statistical depth", "h"))
        rows = [["Model", "Flows", "p50", "p95", "p99", "Warm-up check"]]
        for r in ld.get("per_model") or []:
            rows.append([short(r["model"]), fmt_int(r.get("n")), fmt_time(r.get("p50")), fmt_time(r.get("p95")),
                         fmt_time(r.get("p99")), (r.get("warmup") or {}).get("note", "-")])
        story.append(table(rows, [30 * mm, 12 * mm, 18 * mm, 18 * mm, 18 * mm, W - 96 * mm]))
        if ef and not ef.get("skipped"):
            rows = [["Metric", "Cliff's delta", "Mean diff (A - B) [95% CI]", "Kruskal-Wallis p"]]
            for r in ef.get("metrics") or []:
                if not r.get("available"):
                    rows.append([r["metric"], "-", r.get("reason", "-"), "-"])
                    continue
                kp = r.get("kruskal_wallis_p")
                rows.append([r["metric"], f"{r['cliffs_delta']:+.2f} ({r['magnitude']})",
                             f"{r['diff_mean_a_minus_b']:.4g} [{r['ci95_low']:.4g}, {r['ci95_high']:.4g}]",
                             "-" if kp is None else f"{kp:.3g}"])
            story.append(P(f"A = {ef['model_a']}, B = {ef['model_b']}.", "small"))
            story.append(table(rows, [30 * mm, 34 * mm, 70 * mm, W - 134 * mm]))
            story.append(P(ef.get("method", ""), "small"))
        note = (ld.get("small_sample_note") or (ef or {}).get("small_sample_note"))
        if note:
            story.append(P(note, "p"))

    # --- accuracy (labelled only) ------------------------------------------------------------------
    if labelled:
        story.append(P("Accuracy by class", "h"))
        story.append(P(f"{s.get('class_scheme')} class set. Balanced sample - a per-class view, not real-world "
                       "prevalence.", "small"))
        rows = [["Class", "Flows"] + names]
        for c in s.get("class_set") or []:
            cells = []
            sup = None
            for m in pm:
                r = next((x for x in m["accuracy"]["per_class"] if x["class"] == c), {})
                sup = r.get("support", sup)
                cells.append(fmt_pct(r.get("accuracy")) if r.get("support") else "-")
            rows.append([c, fmt_int(sup)] + cells)
        story.append(table(rows, [44 * mm, 16 * mm] + [(W - 60 * mm) / len(names)] * len(names)))

    # --- caveats ------------------------------------------------------------------------------------
    cav = list((rel or {}).get("caveats") or s.get("caveats") or [])
    story.append(KeepTogether([P("Caveats", "h")] + [P("- " + c, "small") for c in cav] + [
        P("- Scope: this report measures LLM resource efficiency (and accuracy where labels exist) on a "
          "threat-analysis task. It is not a threat-detection product and not a deployment sign-off.", "small")]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(muted)
        canvas.drawString(15 * mm, 9 * mm, _safe(f"AgentMeter benchmark report - {run_name} - NON-VALIDATED"
                                                 + (" - DEMO (mock)" if mock else "")))
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=14 * mm, bottomMargin=16 * mm,
                            title=f"AgentMeter Benchmark Report - {run_name}", author="AgentMeter",
                            subject="LLM resource-efficiency benchmark (non-validated user run)")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
