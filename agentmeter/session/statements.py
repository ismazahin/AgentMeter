"""Checked statements: every fact a section of the session's explanations may use, worked out by
code (design: docs/superpowers/specs/2026-11-11-checked-explanations-design.md, part 1).

The explainer model only chooses among these, orders them and words them, and tags each sentence
with the statement ids it used. A sentence is then checked against ITS statements: every number
must be one of their values and every model or step it names must be one of their subjects.

  build_statements(results, digest)  ->  {section: [statement, ...]}
  statement = {"id": "perf.3", "text": "...", "values": [37.0, ...], "subjects": ["Mistral 7B", "Perceive"]}

Numbers are rounded the way the page shows them (web/insights.js, web/analysis.html), so a
statement reads like the page next to it. Statement texts use no colon or semicolon, so a model
that copies one still passes the style check.
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

TABS = ("ranking", "reliability", "performance", "agents", "cost", "recommendations")
_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![\w])")


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def fmt_time(v: Any) -> Optional[str]:
    """Seconds as the page shows them (web/insights.js time())."""
    f = _num(v)
    if f is None:
        return None
    if f < 1:
        return f"{f * 1000:.{2 if f < 0.01 else 1}f} ms"
    return f"{f:.2f} s"


def fmt_int(v: Any) -> Optional[str]:
    f = _num(v)
    return None if f is None else f"{round(f):,}"


def fmt_pct(fraction: Any, d: int = 0) -> Optional[str]:
    f = _num(fraction)
    return None if f is None else f"{100 * f:.{d}f}%"


def fmt_gb(mb: Any) -> Optional[str]:
    f = _num(mb)
    return None if f is None else f"{f / 1024:.1f} GB"


def values_of(text: str, names: list[str]) -> list[float]:
    """The numbers in a statement (or sentence), ignoring those inside model names and words like
    4-bit or four-step."""
    t = text
    for n in sorted(names, key=len, reverse=True):
        t = t.replace(n, " ")
    t = re.sub(r"\b\d+-(bit|step|agent|class|flow)s?\b", " ", t, flags=re.I)
    out = []
    for m in _NUM.finditer(t):
        out.append(float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else "")))
    return out


class _Sec:
    def __init__(self, prefix: str, names: list[str]):
        self.prefix, self.names, self.items = prefix, names, []

    def add(self, text: Optional[str], subjects: list[str]) -> None:
        if not text or "None" in text:
            return
        self.items.append({"id": f"{self.prefix}.{len(self.items) + 1}", "text": text,
                           "values": values_of(text, self.names), "subjects": [s for s in subjects if s]})


def build_statements(res: dict, digest: dict) -> dict[str, list[dict]]:
    """Every statement each section may use, from this session's results and the explainer digest
    (agentmeter/session/explain.py build_digest: per-step budget verdicts, slowest steps)."""
    pms = digest.get("per_model") or []
    names = [p["name"] for p in pms if p.get("name")]
    one = len(names) == 1
    sec = {k: _Sec(k[:4] if k != "recommendations" else "rec", names) for k in TABS}
    dep, opt = _Sec("dep", names), _Sec("opt", names)
    p8 = res.get("phase8") or {}
    targets = p8.get("targets") or {}
    tiers = p8.get("tiers") or {"healthy_min": 80, "degraded_min": 60}
    flows = (digest.get("session") or {}).get("flows")
    raw_by = {_bare(r.get("model")): r for r in (p8.get("saw_table") or p8.get("raw_criteria") or [])}
    name_of = {_bare(pm.get("model")): pm_name for pm, pm_name in
               zip(res.get("per_model") or [], names)} if len(res.get("per_model") or []) == len(names) else {}
    mem_by = {_bare(m.get("model")): m for m in ((res.get("memory") or {}).get("per_model") or [])}
    pa_by: dict[str, dict[str, dict]] = {}
    for r in ((res.get("per_agent") or {}).get("table") or []):
        pa_by.setdefault(_bare(r.get("model")), {})[str(r.get("agent_name") or "").capitalize()] = r
    model_of = {v: k for k, v in name_of.items()}

    # ---------------- ranking
    ranked = sorted([p for p in pms if _num(p.get("efficiency_score_out_of_100")) is not None],
                    key=lambda p: (-float(p["efficiency_score_out_of_100"]), p.get("rank") or 99))
    if one:
        sec["ranking"].add(f"Only one model ran, {names[0]}, so nothing is ranked or compared between models.", [names[0]])
    for p in ranked:
        sec["ranking"].add(f"{p['name']} scores {p['efficiency_score_out_of_100']:.1f} of 100 on the efficiency score, "
                           f"in the {p.get('tier')} tier.", [p["name"]])
    if len(ranked) > 1:
        a, b = ranked[0], ranked[1]
        gap = float(a["efficiency_score_out_of_100"]) - float(b["efficiency_score_out_of_100"])
        if gap < 0.05:
            sec["ranking"].add(f"{a['name']} and {b['name']} tie on the efficiency score.", [a["name"], b["name"]])
        else:
            sec["ranking"].add(f"{a['name']} ranks first, {gap:.1f} points ahead of {b['name']}.", [a["name"], b["name"]])

    # ---------------- performance, agents, reliability, cost (per model)
    for p in pms:
        n = p["name"]
        lat, p95 = p.get("mean_latency_s"), p.get("p95_latency_s")
        if lat is not None:
            sec["performance"].add(f"{n} takes {fmt_time(lat)} per flow on average.", [n])
        if p95 is not None:
            sec["performance"].add(f"On its slowest 5% of flows, {n} takes {fmt_time(p95)} or more.", [n])
        ss = p.get("slowest_step") or {}
        if ss.get("step"):
            sec["performance"].add(f"The slowest step for {n} is {ss['step']}, at {ss['share_of_each_flows_time_pct']:.0f}% "
                                   f"of each flow's time.", [n, ss["step"]])
        for st in p.get("steps") or []:
            s = st.get("step")
            if st.get("share_of_each_flows_time_pct") is not None:
                sec["agents"].add(f"{s} takes {st['share_of_each_flows_time_pct']:.0f}% of each flow's time for {n}"
                                  + (f", {fmt_time(st['mean_time_s'])} per flow." if st.get("mean_time_s") is not None else "."), [n, s])
            if st.get("share_of_all_tokens_pct") is not None:
                sec["agents"].add(f"{s} uses {st['share_of_all_tokens_pct']:.0f}% of all tokens for {n}.", [n, s])
            if st.get("answer_tokens") is not None:
                sec["agents"].add(f"{s} answers from {n} are {fmt_int(st['answer_tokens'])} tokens long on average.", [n, s])
            if st.get("budget_advice"):
                sec["agents"].add(f"For {s} with {n}, {st['budget_advice']}.", [n, s])
            hit, of, cap = st.get("answers_cut_off_at_budget") or 0, st.get("flows"), st.get("token_budget")
            if hit and of and cap:
                sec["reliability"].add(f"{hit} of {of} {s} answers from {n} were cut off at {cap} tokens.", [n, s])
            if st.get("empty_answers"):
                sec["reliability"].add(f"{st['empty_answers']} {s} answers from {n} came back empty.", [n, s])
        if p.get("valid_answer_pct") is not None:
            sec["reliability"].add(f"{n} gave a usable answer for {p['valid_answer_pct']:.1f}% of flows.", [n])
        if p.get("unreadable_labels"):
            sec["reliability"].add(f"{p['unreadable_labels']} answers from {n} had a label that could not be read.", [n])
        if p.get("accuracy_pct") is not None:
            sec["reliability"].add(f"{n}'s answers matched the reference label for {p['accuracy_pct']:.1f}% of flows.", [n])
        if p.get("cost_per_1k_flows_usd") is not None:
            sec["cost"].add(f"{n} costs ${p['cost_per_1k_flows_usd']:.4f} per 1,000 flows.", [n])
        elif p.get("cost_status") or p.get("cost_reason"):
            sec["cost"].add(f"Cost was not measured for {n} in this run.", [n])
        if p.get("wh_per_1k_flows") is not None:
            sec["cost"].add(f"{n} uses {p['wh_per_1k_flows']:.1f} Wh per 1,000 flows.", [n])
        else:
            sec["cost"].add(f"Energy was not measured for {n} in this run.", [n])
    h2h = digest.get("head_to_head") or {}
    for mt in h2h.get("metrics") or []:
        if mt.get("measured") is False or not mt.get("better") or mt.get("better") == "tie":
            continue
        others = [k for k in (mt.get("values") or {}) if k != mt["better"]]
        if not others or mt.get("pct_better") is None:
            continue
        chance = "beyond chance" if mt.get("beyond_chance") else "which could be chance"
        sec["performance"].add(f"{mt['better']} is {mt['pct_better']:.1f}% better than {others[0]} on {mt.get('metric')}, {chance}.",
                               [mt["better"], others[0]])

    # ---------------- deployment and optimisation (the top model, as the page's panels)
    top = ranked[0] if ranked else (pms[0] if pms else None)
    if top:
        n = top["name"]
        mid = model_of.get(n)
        raw = raw_by.get(mid) or {}
        score = _num(top.get("efficiency_score_out_of_100"))
        if one:
            dep.add(f"Only one model ran, {n}, so this is a reading of one model, not a choice between models.", [n])
        if score is not None:
            dep.add(f"{n} scores {score:.1f} of 100, in the {top.get('tier')} tier.", [n])
            nxt = ("Degraded", tiers.get("degraded_min", 60)) if score < tiers.get("degraded_min", 60) else \
                  ("Healthy", tiers.get("healthy_min", 80)) if score < tiers.get("healthy_min", 80) else None
            if nxt:
                dep.add(f"{n} needs {nxt[1] - score:.1f} more points to reach the {nxt[0]} tier at {nxt[1]:g}.", [n])
        pts, w = top.get("points") or {}, digest.get("weights_pct") or {}
        word = {"latency": "speed", "vram": "GPU memory", "tokens": "tokens"}
        raw_key = {"latency": "latency_s", "vram": "vram_mb", "tokens": "tokens_total"}
        missing = [k for k in pts if raw and _num(raw.get(raw_key.get(k))) is None]
        lost = sorted(((float(w[k]) - float(v), k, float(w[k])) for k, v in pts.items()
                       if k in w and v is not None and k not in missing), reverse=True)
        if lost and lost[0][0] > 0.5:
            dep.add(f"Most points are lost on {word.get(lost[0][1], lost[0][1])}, {lost[0][0]:.1f} of its {lost[0][2]:.1f}.", [n])
        for k in missing:
            wk = word.get(k, k)
            dep.add(f"{wk[:1].upper() + wk[1:]} was not measured in this run, so its {float(w.get(k, 0)):.1f} points are missing.", [n])
        lat = _num(raw.get("latency_s")) or _num(top.get("mean_latency_s"))
        if lat and targets.get("latency_s"):
            dep.add(f"{n} takes {fmt_time(lat)} per flow against a target of {fmt_time(targets['latency_s'])}.", [n])
        if lat and lat >= 0.05:
            dep.add(f"At {fmt_time(lat)} per flow, one GPU answers about {fmt_int(86400 / lat)} flows a day, one at a time.", [n])
        mem = _num((mem_by.get(mid) or {}).get("total_peak_mb")) or _num(raw.get("total_device_vram_mb"))
        if mem:
            need = mem * 1.2 / 1024
            size = next((g for g in (8, 12, 16, 24, 40, 48, 80) if g >= need), None)
            dep.add(f"{n} needed {fmt_gb(mem)} of GPU memory at its peak"
                    + (f", so a GPU with {size} GB or more leaves room for longer prompts." if size else "."), [n])
        tok, ttok = _num(raw.get("tokens_total")), _num(targets.get("tokens_total"))
        if tok and ttok:
            dep.add(f"{n} uses {fmt_int(tok)} tokens per flow against a target of {fmt_int(ttok)}.", [n])
        if top.get("accuracy_pct") is not None and targets.get("accuracy_pct") is not None and _num(w.get("accuracy")):
            dep.add(f"{n}'s accuracy is {top['accuracy_pct']:.1f}% against a target of {float(targets['accuracy_pct']):.1f}%.", [n])
        if len(ranked) > 1:
            r2 = ranked[1]
            gap = float(top["efficiency_score_out_of_100"]) - float(r2["efficiency_score_out_of_100"])
            if gap < 0.05:
                dep.add(f"{n} and {r2['name']} tie on the efficiency score.", [n, r2["name"]])
            else:
                dep.add(f"{r2['name']} is next with {float(r2['efficiency_score_out_of_100']):.1f} points, {gap:.1f} behind {n}.", [n, r2["name"]])
        for st in top.get("steps") or []:
            if str(st.get("budget_advice") or "").startswith("raise"):
                dep.add(f"{st.get('answers_cut_off_at_budget')} of {st.get('flows')} {st['step']} answers from {n} were cut off at "
                        f"{st.get('token_budget')} tokens.", [n, st["step"]])

        # optimisation: the page's per-step numbers (web/analysis.html renderOptimDetail)
        rows = pa_by.get(mid) or {}
        flow_t = lat or sum(_num(r.get("mean_wall_s")) or 0 for r in rows.values())
        ss = top.get("slowest_step") or {}
        if ss.get("step"):
            opt.add(f"{ss['step']} is the slowest step for {n}, at {ss['share_of_each_flows_time_pct']:.0f}% of each flow's time.", [n, ss["step"]])
            r = rows.get(ss["step"]) or {}
            wall, ttft, out_t = _num(r.get("mean_wall_s")), _num(r.get("mean_ttft_s")) or 0.0, _num(r.get("mean_output_tokens"))
            if wall and out_t and out_t >= 20:
                per = max(0.0, wall - ttft) / out_t
                cut = round(out_t * 0.75)
                save = (out_t - cut) * per
                if save >= 0.01:
                    opt.add(f"Capping {ss['step']} answers at {fmt_int(cut)} tokens would save about {fmt_time(save)} per flow"
                            + (f", {100 * save / flow_t:.1f}% of the {fmt_time(flow_t)} total." if flow_t else "."), [n, ss["step"]])
        readers = sorted(((_num(r.get("mean_ttft_s")) or 0, s, r) for s, r in rows.items()), key=lambda x: -x[0])
        if readers and readers[0][0] > 0 and _num(readers[0][2].get("mean_wall_s")):
            t, s, r = readers[0]
            share = t / float(r["mean_wall_s"])
            if share >= 0.1:
                opt.add(f"{s} spends {fmt_time(t)} reading its {fmt_int(r.get('mean_input_tokens'))}-token prompt, "
                        f"{100 * share:.0f}% of its time.", [n, s])
        heavy = sorted(((_num(r.get("mean_vram_delta_mb")) or 0, s) for s, r in rows.items()), reverse=True)
        if heavy and heavy[0][0] > 0:
            opt.add(f"{heavy[0][1]} adds {fmt_int(heavy[0][0])} MB of working memory, the most of any step.", [n, heavy[0][1]])
        for st in top.get("steps") or []:
            if st.get("budget_advice") and st["budget_advice"] != "the budget fits":
                opt.add(f"For {st['step']}, {st['budget_advice']}.", [n, st["step"]])
        opt.add("A bigger token budget makes cut-off answers complete, but it adds time and tokens to every flow.", [])
        opt.add("Shorter instructions or fewer flow features shorten a step's prompt and the time it spends reading it.", [])

    # ---------------- recommendations (the tab's headline): the deployment facts in short
    for it in dep.items[:4] + opt.items[:2]:
        sec["recommendations"].add(it["text"], it["subjects"])

    out = {k: s.items for k, s in sec.items()}
    out["deployment"], out["optimisation"] = dep.items, opt.items
    # summary: the lead facts of each section, ids kept so a summary sentence is checked against them
    summ = []
    for k in ("ranking", "performance", "reliability", "agents", "cost"):
        summ += out[k][:2]
    summ += dep.items[:2] + opt.items[:1]
    seen, uniq = set(), []
    for it in summ:
        if it["text"] not in seen:
            seen.add(it["text"])
            uniq.append(dict(it, id="sum." + it["id"]))
    out["summary"] = uniq
    return out


def _bare(m: Any) -> str:
    return re.sub(r"^mock:", "", str(m or ""))
