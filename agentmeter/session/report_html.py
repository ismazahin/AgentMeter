"""The session PDF report: a short story first, then every session tab in full (2026-11-24 design).

Design: docs/superpowers/specs/2026-11-24-session-report-design.md. The page look follows the
approved mockup in docs/superpowers/specs/2026-11-24-report-mockup/.

Rules that keep the report honest:
- every number is read from the stored session_results.json (plus input.json, selection_audit.json
  and the prepared-set manifest, as the old report did). Nothing is measured or scored again.
- the plain sentences come from the web app's own rules (web/insights.js, run through QuickJS by
  report_js.Insights), so the PDF says what the session pages say. The few sentences written here
  only restate a stored number or the session's own comparison result.
- "Best" appears only where the session's own head-to-head comparison found a real difference.
  Measures the comparison does not test carry an outlined "not tested" tag.
- a missing value says "not measured" with its reason, never zero and never a guess.

The HTML is turned into a PDF by WeasyPrint. When WeasyPrint or the JS engine is missing, the
caller (server.jobs_api.report_pdf_bytes) falls back to the plain ReportLab report.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Optional

from . import report_charts as ch
from .report_js import Insights

_DIR = Path(__file__).resolve().parent / "report_templates"


# ---------------------------------------------------------------- number formats (as the web shows them)
def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def ftime(v: Any) -> str:
    v = _num(v)
    if v is None:
        return "not measured"
    a = abs(v)
    if 0 < a < 0.0001:
        return f"{v * 1e6:.1f} µs"
    if a < 1:
        return f"{v * 1000:.{2 if a < 0.01 else 1}f} ms" if a >= 0.001 or a == 0 else f"{v * 1000:.2f} ms"
    return f"{v:.2f} s"


def fint(v: Any) -> str:
    v = _num(v)
    return "–" if v is None else f"{v:,.0f}"


def fnum(v: Any, d: int = 1) -> str:
    v = _num(v)
    return "–" if v is None else f"{v:,.{d}f}"


def fmb(v: Any) -> str:
    v = _num(v)
    return "not measured" if v is None else f"{v:,.0f} MB"


def fpct(v: Any, d: int = 1) -> str:
    v = _num(v)
    return "–" if v is None else f"{v * 100:.{d}f}%"


def fusd(v: Any) -> str:
    v = _num(v)
    if v is None:
        return "not measured"
    return f"${v:.4f}" if v < 0.01 else f"${v:,.2f}"


def fwh(v: Any) -> str:
    v = _num(v)
    return "not measured" if v is None else (f"{v:.3f} Wh" if v < 1 else f"{v:,.2f} Wh")


def fp(v: Any) -> str:
    v = _num(v)
    return "–" if v is None else (f"{v:.3f}" if v >= 0.001 else "< 0.001")


def e(s: Any) -> str:
    return escape("" if s is None else str(s))


# ---------------------------------------------------------------- the report
class Report:
    def __init__(self, res: dict[str, Any], *, job: Optional[dict] = None, input_meta: Optional[dict] = None,
                 audit: Optional[dict] = None, prepared: Optional[dict] = None, decision: Optional[dict] = None):
        self.res, self.job = res, job or {}
        self.input_meta, self.audit, self.prepared = input_meta or {}, audit or {}, prepared or {}
        self.decision = decision
        self.s = res.get("session") or {}
        self.pm = res.get("per_model") or []
        self.models = [m["model"] for m in self.pm]
        self.rel = res.get("relative_comparison")
        self.env = res.get("environment") or {}
        self.real = self.env.get("provider") == "real"
        self.I = Insights(res)
        self.color = ch.model_colors(self.models)
        self.answers = self.I.call("sectionAnswers", default={}) or {}
        self.summ = self.I.call("tabSummaries", default={}) or {}
        self._by = {}
        for key in ("cost_energy", "memory", "output_reliability", "latency_distribution", "triage", "cold_start"):
            self._by[key] = {self._bare(r.get("model")): r for r in ((res.get(key) or {}).get("per_model") or [])}

    # ---- helpers
    @staticmethod
    def _bare(m: Any) -> str:
        return re.sub(r"^mock:", "", str(m or ""))

    def nick(self, m: Any) -> str:
        return self.I.nick(self._bare(m))

    def plain(self, t: Any) -> str:
        # "(6% apart but could be chance (p = 0.168))" -> "(6% apart but could be chance, p = 0.168)"
        return re.sub(r"\(([^()]*?)\s*\(([^()]*)\)\)", r"(\1, \2)", self.I.plain(t))

    def row(self, key: str, model: str) -> dict:
        return self._by.get(key, {}).get(self._bare(model)) or {}

    def e2e(self, model: str) -> dict:
        m = next((x for x in self.pm if x["model"] == model), {})
        return ((m.get("efficiency") or {}).get("end_to_end")) or {}

    def saw(self, model: str) -> dict:
        m = next((x for x in self.pm if x["model"] == model), {})
        return m.get("saw_efficiency_only") or m.get("saw") or {}

    def metric(self, key: str) -> Optional[dict]:
        return next((m for m in (self.rel or {}).get("metrics") or [] if m.get("metric") == key), None)

    def swatch(self, m: str) -> str:
        return f'<i class="sw" style="background:{self.color.get(m, "#6B7690")}"></i>'

    def mname(self, m: str) -> str:
        return f'<span class="mn">{self.swatch(m)}{e(self.nick(m))}</span>'

    def table(self, head: list[str], rows: list[list[Any]], num: tuple[int, ...] = (), cls: str = "") -> str:
        if not rows:
            return ""
        th = "".join(f'<th class="{"num" if i in num else ""}">{h}</th>' for i, h in enumerate(head))
        body = "".join("<tr>" + "".join(f'<td class="{"num" if i in num else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>"
                       for r in rows)
        return f'<table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>'

    def card(self, q: str, body: str, *, intro: str = "", answer: Optional[str] = None, note: str = "",
             key: Optional[str] = None, keep: bool = True) -> str:
        """One web card: its question, the web's one-line answer, a short intro, then the content."""
        a = self.answers.get(key or q) if answer is None else answer
        if not body and not a and not intro:
            return ""
        return (f'<section class="q{" keep" if keep else ""}"><h3>{e(q)}</h3>'
                + (f'<p class="ans">{e(self.plain(a))}</p>' if a else "")
                + (f'<p class="intro">{e(self.plain(intro))}</p>' if intro else "")
                + body + (f'<p class="note">{e(self.plain(note))}</p>' if note else "") + "</section>")

    def lead(self, tab: str) -> str:
        sm = self.summ.get(tab)
        if not sm:
            return ""
        return f'<div class="lead"><p class="lh">{e(self.plain(sm.get("h")))}</p><p>{e(self.plain(sm.get("s")))}</p></div>'

    def part(self, title: str, what: str, tab: Optional[str], cards: list[str]) -> str:
        cards = [c for c in cards if c]
        if not cards:
            return ""
        return (f'<div class="part"><p class="kicker">Session tab</p><h2 class="ph">{e(title)}</h2>'
                f'<p class="pwhat">{e(what)}</p>' + (self.lead(tab) if tab else "") + "".join(cards) + "</div>")

    # ---- page furniture
    def head(self, first: bool = False) -> str:
        env, s = self.env, self.s
        gpu = env.get("gpu_name") if self.real else "Demo run (mock), no GPU"
        date = (self.job.get("finished_at") or self.job.get("created_at") or "")[:10]
        if first:
            meta = (f'<b>Session report</b>{" · " + e(date) if date else ""}<br>{e(gpu or "GPU unknown")} · '
                    f'{e("4-bit NF4" if self.real else "no quantisation (demo)")} · {fint(s.get("n_flows"))} flows<br>'
                    f'{e(self.job.get("job_id") or s.get("run_id") or "")}')
        else:
            meta = (f'<b>Session report</b> · {e(" vs ".join(self.nick(m) for m in self.models[:4]))}<br>'
                    f'{e(self.job.get("job_id") or s.get("run_id") or "")}')
        return ('<header class="top"><div class="brand"><i><b></b><b></b><b></b><b></b></i>AgentMeter</div>'
                f'<div class="meta">{meta}</div></header>')

    # ================================================================ FRONT
    def headline(self) -> str:
        rel = self.rel
        if not rel:
            return f"{self.nick(self.models[0])} on its own. One model, so no head-to-head comparison."
        ev = rel.get("efficiency_verdict") or {}
        n = len(rel.get("models") or [])
        if ev.get("verdict") == "more_efficient":
            w = ev["model"]
            wins = [self.metric_label(k) for k in (ev.get("wins") or {}).get(w) or []]
            if n > 2:
                return f"{self.nick(w)} is the most resource-efficient of the {n} models."
            other = next(m for m in rel["models"] if m != w)
            return (f"{self.nick(w)} is more resource-efficient than {self.nick(other)}"
                    + (f", ahead on {self.andlist(wins)}." if wins else "."))
        if ev.get("verdict") == "mixed":
            return "Mixed result. Each model is ahead on a different measure." if n == 2 else \
                f"Mixed result. No model beats all {n - 1} others."
        return ("The two models" if n == 2 else f"All {n} models") + " are tied on resource efficiency."

    _MLABEL = {"mean_latency_s": "speed", "mean_peak_vram_mb": "working memory", "mean_tokens_per_flow": "token use"}

    def metric_label(self, k: str) -> str:
        return self._MLABEL.get(k, k.replace("_", " "))

    @staticmethod
    def andlist(xs: list[str]) -> str:
        xs = [x for x in xs if x]
        return xs[0] if len(xs) < 2 else ", ".join(xs[:-1]) + " and " + xs[-1]

    def tag(self, key: Optional[str]) -> tuple[str, str]:
        """(tag html, plain meaning) from the session's own head-to-head result for this measure."""
        if not self.rel:
            return "", ""
        if key is None:
            return '<span class="tag nt">Shown for context, not tested</span>', ""
        m = self.metric(key)
        if not m or not m.get("available"):
            return '<span class="tag nt">Not measured</span>', self.plain((m or {}).get("note") or "")
        if m.get("winner") == "tie":
            return '<span class="tag tie">Tie</span>', self.plain(m.get("note") or "within the tie range")
        w = m.get("winner")
        pct = _num(m.get("pct_better"))
        txt = f"Best: {self.nick(w)}" + (f", {pct:.1f}% better" if pct is not None else "")
        ru = m.get("runner_up")
        return f'<span class="tag best">{e(txt)}</span>', (f"Compared with {self.nick(ru)}." if ru else "")

    def bars(self, vals: list[tuple[str, Optional[float], str]], higher_better: bool = False,
             target: Optional[tuple[str, float, str]] = None) -> str:
        """vals: (model, value, text). One bar per model, scaled to the largest value."""
        nums = [v for _, v, _ in vals if v is not None] + ([target[1]] if target else [])
        mx = max(nums) if nums else 0
        out = []
        rows = list(vals) + ([("__target__", target[1], target[2])] if target else [])
        for m, v, t in rows:
            w = 0 if v is None or not mx else max(1.5, 100 * v / mx)
            if m == "__target__":
                out.append(f'<div class="bar tgt"><span class="n">{e(target[0])}</span><span class="t"><i style="width:{w:.1f}%"></i></span><span class="v">{e(t)}</span></div>')
            else:
                out.append(f'<div class="bar"><span class="n">{e(self.nick(m))}</span><span class="t"><i style="width:{w:.1f}%;background:{self.color[m]}"></i></span>'
                           f'<span class="v{" na" if v is None else ""}">{e(t)}</span></div>')
        return "".join(out)

    def measures(self) -> list[dict]:
        """The eight front-page measures, each with its values, tag and one plain line."""
        out = []
        ms = self.models
        cp = self.I.call("criteriaProfile", default={}) or {}
        tg = cp.get("targets") or {}

        def add(title, better, why, getter, fmt, key, means, target=None):
            vals = [(m, _num(getter(m)), fmt(getter(m))) for m in ms]
            tag, tmeans = self.tag(key) if len(ms) > 1 else ("", "")
            if all(v is None for _, v, _ in vals):
                reason = next((r for r in (self.na_reason(title),) if r), "not recorded in this run")
                means = f"Not measured in this run ({reason})."
                tag = '<span class="tag nt">Not measured</span>' if len(ms) > 1 else ""
            out.append(dict(title=title, better=better, why=why, vals=vals, tag=tag,
                            means=means(vals, tmeans) if callable(means) else means,
                            target=target if len(ms) == 1 else None))

        def leader(vals, lower=True):
            got = [(m, v) for m, v, _ in vals if v is not None]
            if len(got) < 2:
                return None, None, None
            got.sort(key=lambda x: x[1], reverse=not lower)
            return got[0][0], got[1][0], (got[1][1] - got[0][1]) if lower else (got[0][1] - got[1][1])

        lat = self.metric("mean_latency_s")

        def lat_means(vals, tm):
            if len(ms) == 1:
                v = vals[0][1]
                return f"A flow took {ftime(v)} on average, against the score's target of {ftime(tg.get('latency_s'))}."
            a, b, gap = leader(vals)
            if lat and lat.get("winner") not in (None, "tie"):
                return f"{self.nick(a)} answered a flow {ftime(gap)} quicker on average, and the gap passed the test."
            return f"{self.nick(a)} was {ftime(gap)} quicker on average, but {tm or 'the gap is within the tie range'}."
        add("Latency per flow", "lower is better", "Mean time for all agents to answer one flow",
            lambda m: self.e2e(m).get("mean_latency_s"), ftime, "mean_latency_s", lat_means,
            ("Target", tg.get("latency_s"), ftime(tg.get("latency_s"))) if tg.get("latency_s") else None)

        def p95_means(vals, tm):
            if len(ms) == 1:
                return f"Only 1 flow in 20 took longer than {ftime(vals[0][1])}."
            a, b, gap = leader(vals)
            return (f"On its slowest flows {self.nick(a)} finished {ftime(gap)} sooner. The slow tail is not tested "
                    "on its own, so treat this as a hint.")
        add("Slowest 5% of flows", "lower is better", "p95 latency, the wait on a hard flow",
            lambda m: self.e2e(m).get("p95_latency_s"), ftime, None, p95_means)

        vr = self.metric("mean_peak_vram_mb")

        def vram_means(vals, tm):
            if len(ms) == 1:
                return f"Working memory is what answering adds on top of the model weights, {fmb(vals[0][1])} here."
            a, b, gap = leader(vals)
            if vr and vr.get("winner") not in (None, "tie"):
                return f"{self.nick(a)} leaves {fmb(gap)} more room on the GPU for longer inputs or more agents."
            return f"The gap of {fmb(gap)} is {tm or 'within the tie range'}."
        add("Working GPU memory", "lower is better", "Memory used while answering, on top of the weights",
            lambda m: self.e2e(m).get("mean_peak_vram_mb"), fmb, "mean_peak_vram_mb", vram_means)

        def tot_means(vals, tm):
            top = max((v for _, v, _ in vals if v is not None), default=None)
            fits = next((g for g in (8, 12, 16, 24, 40, 48, 80) if top is not None and top <= g * 1024), None)
            return ("Most of this is the model weights. " +
                    (f"{'Every model fits' if len(ms) > 1 else 'It fits'} on a {fits} GB GPU." if fits else ""))
        add("Total peak GPU memory", "lower is better", "Weights plus working memory, the GPU size you need",
            lambda m: self.row("memory", m).get("total_peak_mb"), fmb, None, tot_means,
            ("Target", tg.get("vram_mb"), fmb(tg.get("vram_mb"))) if tg.get("vram_mb") else None)

        tk = self.metric("mean_tokens_per_flow")

        def tok_means(vals, tm):
            if len(ms) == 1:
                return f"The agents read and wrote {fint(vals[0][1])} tokens per flow, against a target of {fint(tg.get('tokens_total'))}."
            a, b, gap = leader(vals)
            if tk and tk.get("winner") not in (None, "tie"):
                return f"{self.nick(a)} used {fint(gap)} fewer tokens per flow, a real gap."
            return f"Both use about the same amount of text per flow, {tm or 'within the tie range'}."
        add("Tokens per flow", "lower is better", "Text read and written by all agents",
            lambda m: self.e2e(m).get("mean_tokens_per_flow"), fint, "mean_tokens_per_flow", tok_means,
            ("Target", tg.get("tokens_total"), fint(tg.get("tokens_total"))) if tg.get("tokens_total") else None)

        def cost_means(vals, tm):
            if len(ms) == 1:
                return f"Running 1,000 flows costs {fusd(vals[0][1])} at the GPU price paid for this run."
            a, b, gap = leader(vals)
            return f"Cost is GPU rental time, so it follows speed. The gap is {fusd(gap)} per 1,000 flows."
        add("Cost per 1,000 flows", "lower is better", "GPU rental time at the price paid for this run",
            lambda m: self.row("cost_energy", m).get("cost_per_1k_flows_usd"), fusd, None, cost_means)

        def en_means(vals, tm):
            if len(ms) == 1:
                return f"The GPU board used {fwh(vals[0][1])} per flow."
            a, b, gap = leader(vals)
            hi = max(v for _, v, _ in vals if v is not None)
            rel_gap = gap / hi if hi else 0
            return ("Both draw about the same energy for the same work." if rel_gap < 0.05 else
                    f"{self.nick(a)} used {fwh(gap)} less per flow. Energy is not tested on its own.")
        add("Energy per flow", "lower is better", "GPU board energy, sampled during the run",
            lambda m: self.row("cost_energy", m).get("wh_per_flow"), fwh, None, en_means)

        def saw_means(vals, tm):
            return ("One number out of 100 that sums up speed, memory and tokens. It is handy for ranking, "
                    "so check the single measures before deciding.")
        add("Efficiency score", "higher is better", "Weighted mix of latency, memory and tokens, 0 to 100",
            lambda m: self.saw(m).get("composite_100"), lambda v: fnum(v, 2), None, saw_means)
        out[-1]["higher"] = True
        return out

    def na_reason(self, title: str) -> str:
        src = {"Cost per 1,000 flows": ("cost_energy", "cost_reason"), "Energy per flow": ("cost_energy", "energy_reason"),
               "Total peak GPU memory": ("memory", "reason"), "Working GPU memory": ("memory", "reason")}.get(title)
        if not src:
            return ""
        r = next((self.row(src[0], m).get(src[1]) for m in self.models if self.row(src[0], m).get(src[1])), "")
        return self.plain(r)

    def takeaways(self) -> str:
        items = []
        for tab, label in (("performance", "Speed"), ("ranking", "Efficiency score"), ("reliability", "Usable answers"),
                           ("cost", "Cost and energy")):
            sm = self.summ.get(tab)
            if not sm:
                continue
            chips = sm.get("chips") or []
            big = chips[0]["value"] if chips else ""
            items.append(f'<div><p class="tk">{e(label)}</p><span class="big">{e(big)}</span>'
                         f'<h4>{e(self.plain(sm.get("h")))}</h4><p>{e(self.plain(sm.get("s")))}</p></div>')
        return f'<div class="takes">{"".join(items)}</div>' if items else ""

    _PRIO = [("speed", "speed per flow"), ("tail", "a short slow tail"), ("cost", "cost"), ("memory", "GPU memory"),
             ("tokens", "token use"), ("validity", "usable answers")]

    def picks(self) -> str:
        if len(self.models) < 2:
            fit = self.I.call("deploymentFit", default={}) or {}
            m0 = (fit.get("models") or [{}])[0]
            n = len(fit.get("profiles") or [])
            line = (f"{self.nick(self.models[0])} fits {m0.get('passes')} of {n} common deployment setups (memory, speed, "
                    "daily volume and cost). The Recommendations part lists each one.") if n else ""
            return ('<p class="kicker">Is this model a fit</p><div class="pick one"><div>'
                    f'<p>{e(line)} Add a second model to the next benchmark for a head-to-head comparison.</p></div></div>')
        best: dict[str, list[str]] = {}
        # first, what the session's own head-to-head test decided (e.g. working memory)
        for m in (self.rel or {}).get("metrics") or []:
            if m.get("available") and m.get("winner") not in (None, "tie") and m.get("metric") in self._MLABEL:
                best.setdefault(m["winner"], []).append(self._MLABEL[m["metric"]])
        for key, label in self._PRIO:
            r = self.I.call("recommend", key, 0.9, default=None)
            if r and r.get("best") and not r.get("tie") and r["best"].get("model"):
                if label == "GPU memory" and "working memory" in sum(best.values(), []):
                    continue                          # already said, from the tested measure
                best.setdefault(r["best"]["model"], []).append(label)
        cells = []
        for m in self.models:
            ws = best.get(m)
            if not ws:
                continue
            cells.append(f'<div><h4>{self.swatch(m)}Pick {e(self.nick(m))} if</h4><p>{e(self.andlist(ws)).capitalize()} '
                         f'{"matter" if len(ws) > 1 else "matters"} most to you.</p></div>')
        if not cells:
            cells = ['<div><p>No model is clearly ahead on any single priority. Choose by what else matters to you, '
                     'such as the model licence or the answers it gives.</p></div>']
        lat = self.metric("mean_latency_s")
        note = ""
        if lat and lat.get("winner") == "tie":
            ss = self.I.call("sampleSize", default={}) or {}
            need = next((x.get("flows") for x in ss.get("needed") or [] if abs((x.get("target") or 0) - 0.05) < 1e-9), None)
            note = ("The speed gap did not pass the test, so the speed pick is a lean, not a result. "
                    + (f"Telling apart a 5% gap takes about {fint(need)} flows per model." if need else ""))
        return ('<p class="kicker">Which one to pick</p><div class="pick">' + "".join(cells) + "</div>"
                + (f'<p class="pick-note">{e(note)}</p>' if note else ""))

    def caveats(self) -> str:
        cav = list((self.rel or {}).get("caveats") or self.s.get("caveats") or [])
        rules = [(r"^Relative view", "Fair comparison", False), (r"^Measured on (.+?): absolute", "Different GPU from the study", True),
                 (r"^NON-VALIDATED", "Exploratory run", True), (r"^Efficiency only", "No labels, efficiency only", True),
                 (r"^PCAP features", "PCAP features approximate", True), (r"^Balanced sample", "Balanced sample", False),
                 (r"^6-class run", "6 classes", False), (r"^No VRAM readings", "No memory readings", True),
                 (r"^MOCK provider", "Demo data", True), (r"^Small sample \((\d+) flows", "Small sample", True),
                 (r"^SAW targets are placeholders", "Score targets provisional", True)]
        items = []
        for c in cav:
            lab, warn = next(((l, w) for p, l, w in rules if re.search(p, c)), (re.split(r"[:.;]", c)[0][:40], False))
            first = re.split(r"(?<=[.])\s", self.plain(c), maxsplit=1)[0]       # the first sentence; the full text is in Overview
            items.append(f'<div class="{"warn" if warn else ""}"><b>{e(lab)}</b>{e(first)}</div>')
        return ('<p class="kicker">Keep in mind</p><div class="cav">' + "".join(items) + "</div>") if items else ""

    def page1(self) -> str:
        demo = ("" if self.real else '<p class="demo">Demo run. The mock provider is a fixed heuristic, not a language model. '
                'The numbers show how the report works and are not model measurements.</p>')
        sub = (f"{'Each model' if len(self.models) > 1 else 'The model'} ran the same "
               f"{len((self.res.get('agents') or {}).get('per_model', [{}])[0].get('agents') or []) or 4}-agent triage on the same "
               f"{fint(self.s.get('n_flows'))} network flows, one model at a time on the same "
               f"{'GPU' if self.real else 'machine'}. A difference only counts when it holds up statistically.")
        how = ('<p class="kicker">How to read the charts</p><div class="howto">'
               '<div><span class="ex"><i style="width:46px;background:#0B86A6"></i><i style="width:30px;background:#2E5BD8"></i></span>'
               '<b>Shorter bar wins</b>Each chart says if lower or higher is better. Most measures are costs, so the shorter bar is the better result.</div>'
               f'<div><span class="tag best">Best</span><b>The gap is real</b>One model was more than {self.tie_pct()} better and, where the session tests it, the gap passed the statistical test.</div>'
               f'<div><span class="tag tie">Tie</span> <span class="tag nt">Not tested</span><b>The gap may be noise</b>A Tie is within {self.tie_pct()} or could be chance. An outlined tag marks a number shown for context only.</div></div>')
        return (f'<section class="page front">{self.head(True)}{demo}<p class="kicker">Verdict</p>'
                f'<h1>{e(self.headline())}</h1><p class="sub">{e(sub)}</p>'
                f'<p class="kicker">In plain words</p>{self.takeaways()}{self.picks()}{how}{self.caveats()}</section>')

    def tie_pct(self) -> str:
        t = _num(((self.rel or {}).get("thresholds") or {}).get("efficiency_tie_rel"))
        return f"{t * 100:g}%" if t is not None else "the tie range"

    def page2(self) -> str:
        charts = []
        for m in self.measures():
            charts.append(f'<div class="chart"><h3>{e(m["title"])} <small>{e(m["better"])}</small></h3>'
                          f'<p class="why">{e(m["why"])}</p>{self.bars(m["vals"], m.get("higher", False), m.get("target"))}'
                          f'{m["tag"]}<p class="means">{e(m["means"])}</p></div>')
        legend = "".join(f'<span>{self.swatch(m)}{e(self.nick(m))}</span>' for m in self.models)
        return (f'<section class="page">{self.head()}<h2>Every measure, one chart each</h2>'
                f'<p class="h2p">Each chart compares {"the models" if len(self.models) > 1 else "the model with the score target"} on one measure. '
                f'The line under it says what the result means in practice.</p>'
                f'<div class="legend">{legend}<span class="note">Tags explained on page 1</span></div>'
                f'<div class="grid">{"".join(charts)}</div>{self.together() if len(self.models) <= 2 else ""}</section>')

    def together(self) -> str:
        if not self.rel:
            return ""
        ms = [m for m in self.rel.get("metrics") or [] if m.get("metric") != "accuracy"]
        won = [m for m in ms if m.get("available") and m.get("winner") not in (None, "tie")]
        tie = [m for m in ms if m.get("available") and m.get("winner") == "tie"]
        na = [m for m in ms if not m.get("available")]
        parts = []
        if won:
            parts.append("Tested and decided, " + self.andlist([f"{self.metric_label(m['metric'])} ({self.nick(m['winner'])} ahead)" for m in won]) + ".")
        if tie:
            parts.append("Tested and tied, " + self.andlist([self.metric_label(m["metric"]) for m in tie]) + ".")
        if na:
            parts.append("Not measured, " + self.andlist([self.metric_label(m["metric"]) for m in na]) + ".")
        parts.append("The slow tail, total memory, cost, energy and the score are shown for context and not tested on their own.")
        return f'<div class="sum"><b class="h">Putting it together</b>{e(" ".join(parts))}</div>'

    def page3(self) -> str:
        ef = self.I.call("efficiencyFrontier", default={}) or {}
        costs = {m: _num(self.row("cost_energy", m).get("cost_per_1k_flows_usd")) for m in self.models}
        if len(self.models) > 1 and all(v is not None for v in costs.values()):
            pts = [dict(label=self.nick(m), x=costs[m], y=_num(self.e2e(m).get("mean_latency_s")), color=self.color[m]) for m in self.models]
            svg = ch.scatter(pts, x_label="Cost per 1,000 flows", y_label="Latency per flow", x_fmt=lambda v: f"${v:.2f}",
                             y_fmt=ftime, good_corner="fast and cheap", labels=len(self.models) <= 2)
            title, expl = "Speed against cost", "Each dot is one model. The best place to be is the lower left, which is fast and cheap."
        elif len(self.models) > 1 and ef.get("points"):
            pts = [dict(label=self.nick(p["model"]), x=_num(p["x"]), y=_num(p["y"]), color=self.color.get(p["model"], "#6B7690"))
                   for p in ef["points"]]
            svg = ch.scatter(pts, x_label="Latency per flow", y_label=ef.get("y_label") or "Tokens per flow", x_fmt=ftime,
                             y_fmt=lambda v: f"{v:,.0f}", good_corner="fast and lean", labels=len(self.models) <= 2)
            title, expl = "Speed against token use", "Each dot is one model. Cost was not measured, so tokens stand in for it. Lower left is best."
        else:
            svg, title, expl = "", "", ""
        glance = self.glance_table()
        top = ""
        if svg and len(self.models) > 2:
            legend = "".join(f'<span>{self.swatch(m)}{e(self.nick(m))}</span>' for m in self.models)
            top = (f'<h2>{e(title)}</h2><p class="h2p">{e(expl)}</p><div class="two"><div class="panel">{svg}</div>'
                   f'<div class="panel"><div class="legend col">{legend}</div><p class="explain">{e(self.plain(self.answers.get("Which models are never beaten on two costs at once?") or ""))}</p></div></div>'
                   f'<h2>Each model at a glance</h2><div class="panel">{glance}</div>')
        elif svg:
            top = (f'<h2>{e(title)}</h2><p class="h2p">{e(expl)}</p><div class="two"><div class="panel">{svg}'
                   f'<p class="explain">{e(self.plain(self.answers.get("Which models are never beaten on two costs at once?") or ""))}</p></div>'
                   f'<div class="panel">{glance}</div></div>')
        else:
            top = f'<h2>Each model at a glance</h2><div class="panel">{glance}</div>'
        many = len(self.models) > 2
        return (f'<section class="page">{self.head()}{self.together() if many else ""}{top}{self.agent_split()}'
                + ("" if many else f'<h2>Words used in this report</h2>{self.glossary()}') + "</section>")

    def glance_table(self) -> str:
        rows = []
        dec = {m.get("metric"): m.get("winner") for m in (self.rel or {}).get("metrics") or []}

        def b(v, metric, m):
            return f"<b>{v}</b>" if dec.get(metric) == m else v
        for m in self.models:
            x = self.e2e(m)
            rows.append([self.mname(m), b(ftime(x.get("mean_latency_s")), "mean_latency_s", m), ftime(x.get("p95_latency_s")),
                         b(fmb(x.get("mean_peak_vram_mb")), "mean_peak_vram_mb", m), fmb(self.row("memory", m).get("total_peak_mb")),
                         b(fint(x.get("mean_tokens_per_flow")), "mean_tokens_per_flow", m),
                         fusd(self.row("cost_energy", m).get("cost_per_1k_flows_usd")), fwh(self.row("cost_energy", m).get("wh_per_flow")),
                         fpct(self.row("output_reliability", m).get("valid_rate")), fnum(self.saw(m).get("composite_100"), 2)])
        # transpose: measures down, models across (reads like the mockup)
        heads = ["Latency per flow", "Slowest 5% (p95)", "Working memory", "Total peak memory", "Tokens per flow",
                 "Cost per 1,000 flows", "Energy per flow", "Usable answers", "Efficiency score"]
        trows = [[h] + [r[i + 1] for r in rows] for i, h in enumerate(heads)]
        t = self.table([""] + [self.mname(m) for m in self.models], trows, num=tuple(range(1, len(self.models) + 1)), cls="glance")
        return t + '<p class="explain">Bold marks a result that passed the session\'s test. The rest are within noise, not tested or not measured.</p>'

    def agent_split(self) -> str:
        ag = (self.res.get("agents") or {}).get("per_model") or []
        if not ag:
            return ""
        blocks = []
        for m in ag:
            parts = [dict(label=f'{a["agent"]} {ftime(a.get("mean_latency_s"))}', value=_num(a.get("mean_latency_s")) or 0,
                          color=ch.AGENT[j % len(ch.AGENT)]) for j, a in enumerate(m.get("agents") or [])]
            o = m.get("overhead") or {}
            ans = self.answers.get("@agent:" + self._bare(m["model"]))
            if len(ag) > 2:
                blocks.append(f'<div class="ag"><p class="agm">{self.mname(m["model"])} <span>{e(self.plain(ans or ""))}</span></p>{ch.stacked(parts, height=16)}</div>')
                continue
            blocks.append(f'<div class="ag"><p class="agm">{self.mname(m["model"])} <span>{e(self.plain(ans or ""))}</span></p>'
                          f'{ch.stacked(parts)}' + (f'<p class="small">Hand-offs, where an earlier answer is sent again to the next agent, are '
                                                    f'{fpct(o.get("handoff_share_of_total"))} of all tokens, about {fint(o.get("handoff_tokens_per_flow"))} per flow (an estimate).</p>'
                                                    if o.get("handoff_tokens_per_flow") is not None else "") + "</div>")
        keys = "".join(f'<span><i class="sw" style="background:{ch.AGENT[j % len(ch.AGENT)]}"></i>{e(a["agent"].capitalize())}</span>'
                       for j, a in enumerate(ag[0].get("agents") or []))
        return ('<h2>Where the time goes, agent by agent</h2><p class="h2p">Each flow passes through the agents in turn. '
                f'Each bar splits the mean time per flow between them.</p><div class="panel">{"".join(blocks)}<div class="akeys">{keys}</div></div>')

    def glossary(self) -> str:
        g = [("Flow", "one network connection the agents read and judge."), ("p95", "the time 95% of flows finished within."),
             ("Working memory", "GPU memory used while answering, beyond the weights."),
             ("4-bit NF4", "a way to shrink a model so it uses less memory."),
             ("Efficiency score", "one 0 to 100 number mixing speed, memory and tokens (SAW)."),
             ("Significant", "unlikely to be chance, by a statistical test."),
             ("Hand-off tokens", "an earlier agent's answer sent again inside a later prompt."),
             ("Tie", f"a gap within {self.tie_pct()}, or one that could be chance.")]
        return '<dl class="gloss">' + "".join(f"<div><dt>{e(a)}</dt><dd>{e(b)}</dd></div>" for a, b in g) + "</dl>"

    def page4(self) -> str:
        th = (self.rel or {}).get("thresholds") or {}
        w = ((self.res.get("phase8_efficiency_only") or {}).get("weights")) or (self.res.get("rank_stability") or {}).get("weights") or {}
        tg = (self.I.call("criteriaProfile", default={}) or {}).get("targets") or {}
        wl = self.andlist([f"{k.replace('vram', 'memory')} {float(v):.2f}" for k, v in w.items() if k != "accuracy" and _num(v)])
        alpha = _num(th.get("significance_alpha")) or 0.05
        mt = self.table(["Measure", "How it is measured", "Why it matters"], [
            ["<b>Latency per flow</b>", "Clock time from a flow entering the first agent to the last agent's answer. Mean, median, p95 and p99 over all flows.", "How long someone waits for each flow to be handled."],
            ["<b>Working memory</b>", "PyTorch's peak memory counter, reset before each agent step and read after it.", "Room left for longer inputs or more agents on one GPU."],
            ["<b>Total peak memory</b>", "Working memory plus the loaded model weights.", "The size of GPU you need to rent or buy."],
            ["<b>Tokens per flow</b>", "Input and output tokens of all agents, counted by each model's own tokenizer. Hand-offs count too.", "More tokens means more time, and more money on paid APIs."],
            ["<b>Energy per flow</b>", "GPU board power sampled through the run, added up over each model's time and divided by its flows.", "The power bill, and the carbon behind it."],
            ["<b>Cost per 1,000 flows</b>", "Mean latency × the GPU's hourly price ÷ 3,600 × 1,000.", "What a real workload would cost on this GPU."],
            ["<b>Usable answers</b>", "Share of flows whose final answer had the required format. A validity check, never part of the score.", "Fast but unusable answers are not efficient."]], cls="mtab")
        lat = self.metric("mean_latency_s")
        latp = _num((lat or {}).get("p_value"))
        ex = ""
        if latp is not None:
            one_in = round(1 / latp) if latp > 0 else None
            ex = (f" Latency here got p = {latp:.3f}" + (f", so a gap this size would turn up by chance about 1 time in {one_in}" if one_in and latp >= alpha else "")
                  + (". That is too often to call a winner." if latp >= alpha else ", below the cut-off, so the gap counts."))
        win = (f'<div class="two2"><p class="explain">A model wins a measure only when it is more than {self.tie_pct()} better and, for latency and memory, '
               f'the gap passes the Kruskal-Wallis test with p below {alpha:g}. The test asks if one model\'s flows really ran differently, '
               f'or if the gap could come from normal variation between flows.</p><p class="explain">{e(ex.strip())} '
               f'{e(self.plain(self.answers.get("How many flows are needed to tell models apart?") or ""))}</p></div>') if self.rel else \
            '<p class="explain">One model was benchmarked, so no winner is decided. Its numbers are compared with the score targets instead.</p>'
        steps = (f'<div class="steps"><div><span class="sn">1</span><b>Scale each measure</b>Each value is compared with a fixed target, '
                 f'{ftime(tg.get("latency_s"))} latency, {fmb(tg.get("vram_mb"))} memory and {fint(tg.get("tokens_total"))} tokens per flow. '
                 'Meeting the target scores 1, and twice the target scores 0.5.</div>'
                 f'<div><span class="sn">2</span><b>Weigh them</b>{e(wl.capitalize() if wl else "The stored weights apply")}. '
                 'The app can re-rank with other weights as a view only.</div>'
                 '<div><span class="sn">3</span><b>Add up</b>The weighted parts are added and multiplied by 100. Answer accuracy is never part '
                 'of the score, and a stored score never changes later.</div></div>')
        sid = self.res.get("session_identity") or {}
        sha = (sid.get("prepared_set_sha256") or "")[:12]
        held = (f"{'All models' if len(self.models) > 1 else 'The model'} answered the exact same flows"
                + (f", checked by a fingerprint of the prepared files (SHA-256 {sha}…)" if sha else "")
                + f". They ran on the same {'GPU' if self.real else 'machine'} with the same settings, "
                + ("4-bit NF4" if self.real else "no quantisation (demo)")
                + ", the same token limits and the same agent pipeline. So any difference belongs to the model, not the setup.")
        return (f'<section class="page{" tight" if len(self.models) > 2 else ""}">{self.head()}<h2>How each number is measured</h2>'
                '<p class="h2p">Every number in this report was recorded during the run. Hand-off tokens are an estimate and are marked as one.</p>'
                f'{mt}<h2>How a winner is decided</h2>{win}<h2>How the efficiency score works</h2>'
                '<p class="h2p">The score turns three measures into one number out of 100, so models can be ranked.</p>'
                f'{steps}<h2>What was held equal</h2><p class="explain">{e(held)}</p>'
                f'<h2>What to do next</h2>{self.next_steps()}'
                + (f'<h2>Words used in this report</h2>{self.glossary()}' if len(self.models) > 2 else "") + "</section>")

    def next_steps(self) -> str:
        from . import brief
        items = []
        ss = self.I.call("sampleSize", default={}) or {}
        if self.rel and ss and not ss.get("gap_detectable"):
            need = next((x.get("flows") for x in ss.get("needed") or [] if abs((x.get("target") or 0) - 0.05) < 1e-9), None)
            items.append(("Settle speed.", f"Run the same models again on about {fint(need)} flows per model." if need else "Run the same models again on more flows."))
        hint = (brief.optimisation_hint(self.res, None) or {}).get("text")
        if hint:
            items.append(("Optimise.", self.plain(hint)))
        if not self.real:
            items.append(("Measure for real.", "Run the benchmark on a GPU. This demo run has no real timings, memory or energy."))
        items.append(("Compare fairly.", f"Check these numbers only against runs on the same {'GPU' if self.real else 'setup'}, on Consistency in the app."))
        return '<ol class="next">' + "".join(f"<li><b>{e(a)}</b> {e(b)}</li>" for a, b in items) + "</ol>"

    # ================================================================ DETAIL PARTS
    def overview(self) -> str:
        s, im, pr = self.s, self.input_meta, self.prepared
        fm = s.get("feature_match") or {}
        kv = [["Source", e(f"{str(s.get('source_type', '')).upper()} ({im.get('source_file', '–')})")],
              ["Labels", "Labelled, so answers are also checked against reference labels" if s.get("accuracy_available") else "Unlabelled, efficiency and validity only"],
              ["Flows benchmarked", fint(s.get("n_flows"))],
              ["Candidate pool", fint(im.get("rows_total"))],
              ["Labels the agents choose from", e(", ".join(s.get("class_set") or []))],
              ["Feature match", e(self.plain(f"{fm.get('status', '–')}" + (f". {fm['note']}" if fm.get("note") else "")))],
              ["Selection mode", e(str(s.get("selection_mode") or "–").replace("_", " "))]]
        samp = pr.get("sampling") or {}
        if samp.get("description"):
            kv.append(["Sampling", e(self.plain(samp["description"]))])
        if pr.get("id"):
            kv.append(["Prepared set", e(pr["id"])])
        env = self.env
        kv.append(["Measured on", e(f"{env.get('gpu_name')}, driver {env.get('driver_version') or '–'}, CUDA {env.get('cuda_runtime_version') or '–'}, "
                                     f"torch {env.get('torch') or '–'}, transformers {env.get('transformers') or '–'}" if self.real
                                     else "Demo (mock provider), no GPU")])
        kv.append(["Models", e(", ".join(m["model"] + ("" if m.get("canonical", True) else " (extra model, not in the validated baseline)") for m in self.pm))])
        inp = self.card("What went into this run?", self.table(["", ""], kv, cls="kv"), answer="")
        rules = self.audit.get("rules") or []
        sel = ""
        if rules:
            rr = [[e(r["id"]), e("constraint" if r.get("kind") == "constraint" else r.get("type")),
                   "yes" if r.get("fired") else ("disabled" if not r.get("enabled", True) else "no"),
                   "–" if r.get("kind") == "constraint" else fint(r.get("admitted"))] for r in rules]
            sel = self.card("How were the flows picked?", self.table(["Rule", "Type", "Fired", "Flows admitted"], rr, num=(3,)),
                            answer="", intro=f"{len(self.audit.get('rules_fired') or [])} of {len(rules)} rules fired. "
                                             f"{fint(self.audit.get('selected'))} of {fint(self.audit.get('total_flows'))} flows were selected, "
                                             f"with a budget of {fint(self.audit.get('max_flows'))}.")
        kf = self.I.call("keyFindings", default=[]) or []
        find = self.card("Key findings", '<ul class="find">' + "".join(f"<li>{e(self.plain(x.get('text')))}</li>" for x in kf) + "</ul>",
                         answer="", note="Each line restates one stored number of this session.") if kf else ""
        sc = self.I.call("scorecard", default={}) or {}
        score = ""
        if sc.get("dims") and len(self.models) > 1:
            rows = []
            for d in sc["dims"]:
                cells = []
                for c in d.get("cells") or []:
                    v = c.get("value")
                    t = self.fmt_unit(v, d.get("unit"))
                    g = c.get("grade")
                    cells.append(f'<span class="g-{e(g)}">{e(t)}</span>' + (f' <small>+{fpct(c.get("gap"), 0)}</small>' if g not in ("best", None) and c.get("gap") else ""))
                rows.append([e(d.get("label"))] + cells)
            score = self.card("How do the models compare, criterion by criterion?", self.table(["Measure"] + [self.mname(m) for m in sc.get("models") or []], rows),
                              note=f"Best is bold. A value within {int(round((sc.get('band') or 0.05) * 100))}% of the best counts as close.")
        cp = self.I.call("criteriaProfile", default={}) or {}
        prof = ""
        if cp.get("rows"):
            crit = cp.get("criteria") or []
            rows = [[self.mname(r["model"])] + [fnum((r.get("scores") or {}).get(c), 2) if not (cp.get("missing") or {}).get(c) else "no data" for c in crit]
                    for r in cp["rows"]]
            prof = self.card("How does each model score on each criterion?", self.table(["Model"] + [e(c.replace("vram", "memory").capitalize()) for c in crit], rows, num=tuple(range(1, len(crit) + 1))),
                             intro="Each criterion scaled from 0 to 1 against its target, where 1 meets the target.")
        trust = self.card("Can these results be trusted?", self.gpu_health_html(), answer=None)
        cav = list((self.rel or {}).get("caveats") or self.s.get("caveats") or [])
        good = self.card("Good to know", '<ul class="find">' + "".join(f"<li>{e(self.plain(c))}</li>" for c in cav) + "</ul>", answer="") if cav else ""
        verdict = self.card("The session's full verdict", f'<p class="explain">{e(self.plain(self.rel["verdict"]))}</p>', answer="") if self.rel else ""
        return self.part("Overview", "What was run, the headline numbers and how far to trust them.", None,
                         [inp, verdict, sel, find, score, prof, trust, good])

    def fmt_unit(self, v, unit):
        return {"s": ftime, "MB": fmb, "tokens": fint, "USD": fusd, "Wh": fwh, "%": fpct}.get(unit, lambda x: fnum(x, 2))(v)

    def gpu_health_html(self) -> str:
        g = self.res.get("gpu_health") or {}
        if not g.get("available"):
            return f'<p class="small">{e(self.plain(g.get("reason") or "GPU state was not recorded."))}</p>'
        rows = [[self.mname(m.get("model")), e(m.get("status")), fpct(m.get("clock_share"), 0), fnum(m.get("max_temp_c"), 0) + " °C" if m.get("max_temp_c") is not None else "–",
                 fpct(m.get("throttled_share"), 0), e(", ".join(m.get("other_processes") or []) or "none")] for m in g.get("per_model") or []]
        return self.table(["Model", "GPU state", "Clock (share of max)", "Highest temperature", "Slowed down", "Other processes"], rows, num=(2, 3, 4)) + \
            (f'<p class="note">{e(self.plain(g.get("rule")))}</p>' if g.get("rule") else "")

    def ranking(self) -> str:
        pm = sorted(self.pm, key=lambda m: -(_num(self.saw(m["model"]).get("composite_100")) or 0))
        bars = self.bars([(m["model"], _num(self.saw(m["model"]).get("composite_100")), fnum(self.saw(m["model"]).get("composite_100"), 2)) for m in pm], True)
        p8 = self.res.get("phase8_efficiency_only") or {}
        rows = []
        for r in p8.get("saw_table") or []:
            rows.append([e(self.nick(r.get("model"))), fnum(r.get("composite_100") or r.get("saw_100"), 2), e(r.get("tier") or "–"), fint(r.get("rank"))])
        scores = self.card("What is each model's efficiency score?", '<div class="bars">' + bars + "</div>",
                           answer=(self.summ.get("ranking") or {}).get("h") or "",
                           intro="The stored efficiency score, out of 100. It mixes latency, memory and tokens against fixed targets.")
        w = p8.get("weights") or {}
        weights = self.card("How much does each criterion count?",
                            self.table(["Criterion", "Weight"], [[e(k.replace("vram", "memory").capitalize()), f"{float(v):.2f}"] for k, v in w.items() if k != "accuracy"], num=(1,)))
        rs = self.res.get("rank_stability") or {}
        rk = ""
        if rs.get("per_model"):
            rows = [[self.mname(r["model"]), fpct(r.get("p_first"), 0), fnum(r.get("mean_rank"), 2), f'{fint(r.get("rank_p5"))} to {fint(r.get("rank_p95"))}',
                     fpct((r.get("p_lowest") or {}).get("latency"), 0), fpct((r.get("p_lowest") or {}).get("tokens"), 0)] for r in rs["per_model"]]
            rk = self.card("How sure is the ranking?", self.table(["Model", "First", "Mean rank", "Rank (90% of resamples)", "Fastest", "Fewest tokens"], rows, num=(1, 2, 3, 4, 5)),
                           note=rs.get("method"))
        ws = self.I.call("weightStability", default={}) or {}
        wsr = ""
        if ws.get("rows"):
            rows = []
            for r in ws["rows"]:
                if r.get("no_data"):
                    rows.append([e(r["criterion"].replace("vram", "memory").capitalize()), f'{fnum(r.get("stored"), 2)}', "no data", "–"])
                    continue
                st = r.get("stable") or [None, None]
                rows.append([e(r["criterion"].replace("vram", "memory").capitalize()), fnum(r.get("stored"), 2),
                             e(self.nick(r["leader"]) if r.get("leader") else "tie"), f"{fnum(st[0], 2)} to {fnum(st[1], 2)}"])
            wsr = self.card("How far can the weights move?", self.table(["Criterion", "Stored weight", "First at stored weight", "Stays first for weights"], rows, num=(1, 3)))
        ef = self.I.call("efficiencyFrontier", default={}) or {}
        fr = ""
        if ef.get("points") and len(self.models) > 1:
            pts = [dict(label=self.nick(p["model"]), x=_num(p["x"]), y=_num(p["y"]), color=self.color.get(p["model"], "#6B7690")) for p in ef["points"]]
            svg = ch.scatter(pts, x_label="Latency per flow", y_label=ef.get("y_label") or "", x_fmt=ftime, y_fmt=lambda v: f"{v:,.0f}", good_corner=None, height=220, labels=len(self.models) <= 2)
            rows = [[self.mname(p["model"]), ftime(p["x"]), fint(p["y"]), "yes" if p.get("frontier") else "no, beaten by " + e(", ".join(self.nick(x) for x in p.get("dominated_by") or []))] for p in ef["points"]]
            fr = self.card("Which models are never beaten on two costs at once?", f'<div class="two"><div>{svg}</div><div>' + self.table(["Model", "Latency / flow", e(ef.get("y_label") or ""), "On the frontier"], rows, num=(1, 2)) + "</div></div>")
        return self.part("Ranking", "Which model ranks first on the efficiency score, and how robust that order is.", "ranking",
                         [scores, weights, rk, wsr, fr])

    def reliability(self) -> str:
        orl = self.res.get("output_reliability") or {}
        cards = []
        ids = self.res.get("ids")
        if ids:
            i, lab = ids.get("ids") or {}, bool(ids.get("labelled"))
            rows = [[self.mname(r["model"]), fint(r.get("alerts"))] + ([f'{fint(r.get("false_alarms_dismissed"))} of {fint(r.get("false_alarms"))}',
                    f'{fint(r.get("attacks_dismissed_by_llm"))} ({fpct(r.get("attacks_dismissed_by_llm_rate"))})',
                    f'{fint(r.get("missed_total"))} ({fpct(r.get("missed_total_rate"))})'] if lab else []) +
                    [ftime((r.get("per_1k_incoming") or {}).get("time_s")), fusd((r.get("per_1k_incoming") or {}).get("cost_usd"))] for r in ids.get("per_model") or []]
            cards.append(self.card("What did the IDS pass on to the agents?", self.table(["Model", "Alerts"] + (["False alarms dismissed", "Attacks it dismissed", "Missed end to end"] if lab else []) + ["Time / 1,000 incoming", "Cost / 1,000 incoming"], rows, num=tuple(range(1, 8))),
                                   answer="", intro=f"The IDS flagged {fint(ids.get('n_alerts'))} of {fint(ids.get('n_flows_in'))} flows, so {fpct(ids.get('llm_calls_saved_share'))} of LLM calls were saved."
                                   + (f" IDS recall {fpct(i.get('recall'))}, precision {fpct(i.get('precision'))}." if lab else "")))
        if orl.get("per_model"):
            rows = [[self.mname(r["model"]), fpct(r.get("valid_rate")), fint(r.get("unparseable")), fint(r.get("empty")), fint(r.get("truncated")),
                     fpct((r.get("verdict_consistency") or {}).get("rate"))] + ([fpct((r.get("reference_agreement") or {}).get("rate"))] if orl.get("labelled") else [])
                    for r in orl["per_model"]]
            cards.append(self.card("Did every flow get a usable answer?", self.table(["Model", "Valid answers", "Not a label", "Empty answer", "Hit token budget", "Verdict agrees"] + (["Matches reference label"] if orl.get("labelled") else []), rows, num=tuple(range(1, 7))),
                                   intro="Each flow should end with one usable answer, a label from the allowed list and no empty reply. A validity check, never a detection score."))
        acc = self.res.get("accuracy_stats") or {}
        if acc.get("labelled") and acc.get("per_model"):
            rows = [[self.mname(r["model"]), fpct(r.get("accuracy")), f'{fpct((r.get("ci95") or [None, None])[0], 0)} to {fpct((r.get("ci95") or [None, None])[1], 0)}', f'{fint(r.get("correct"))} of {fint(r.get("n"))}'] for r in acc["per_model"]]
            prs = [[e(f'{self.nick(p["a"])} vs {self.nick(p["b"])}'), fpct(p.get("difference")), f'{fint(p.get("only_a_right"))} / {fint(p.get("only_b_right"))}', fp(p.get("p_holm")), e(self.plain(p.get("verdict")))] for p in acc.get("pairs") or []]
            cards.append(self.card("Are the accuracy differences real?", self.table(["Model", "Accuracy", "95% range", "Correct"], rows, num=(1, 2, 3)) + self.table(["Pair", "Difference", "Only A right / only B right", "p (Holm)", "Verdict"], prs, num=(1, 2, 3)),
                                   note=acc.get("method")))
        cm = self.I.call("classMetrics", default={}) or {}
        if cm.get("available"):
            blocks = []
            for m in cm.get("models") or []:
                rows = [[e(c["cls"]), fint(c.get("support")), fpct(c.get("precision"), 0), fpct(c.get("recall"), 0), fnum(c.get("f1"), 2)] for c in m.get("classes") or []]
                mix = "; ".join(f'{x["truth"]} answered as {x["predicted"]} ({fint(x["count"])})' for x in (m.get("mixups") or [])[:3])
                blocks.append(f'<div class="sub"><p class="agm">{self.mname(m["model"])}</p>' + self.table(["Class", "Flows", "Precision", "Recall", "F1"], rows, num=(1, 2, 3, 4))
                              + (f'<p class="small">Most common mix-ups, {e(self.plain(mix))}.</p>' if mix else "") + "</div>")
            cards.append(self.card("Which classes does each model mix up?", '<div class="cols">' + "".join(blocks) + "</div>"))
        tri = self.res.get("triage") or {}
        if tri.get("per_model"):
            from ..pipeline.triage import effective_gate
            from .pdf_report import gate_text
            pri, lab = list(tri.get("priorities") or []), bool(tri.get("labelled"))
            rows = [[self.mname(r["model"]), e(self.plain(gate_text(effective_gate(r))))] + [fint((r.get("queue") or {}).get(p)) for p in pri]
                    + ([f'{fint(r.get("missed_attacks"))} ({fpct(r.get("missed_attack_rate"))})', f'{fint(r.get("false_escalations"))} ({fpct(r.get("false_escalation_rate"))})'] if lab else [])
                    for r in tri["per_model"]]
            pol = ", ".join(f"{c} to {p}" for c, p in (tri.get("policy") or {}).items())
            g = tri.get("gate") or {}
            cards.append(self.card("What would an analyst have to do?", self.table(["Model", "Triage gate"] + pri + (["Missed attacks", "False escalations"] if lab else []), rows, num=tuple(range(2, 2 + len(pri) + 2))),
                                   intro=f"The priority comes from the session's policy table ({pol}), not from the model. Context only, it does not change the score.",
                                   note=(f"Gate, recommended only if missed attacks are at most {fpct(g.get('max_missed_attack_rate'))}, false escalations at most "
                                         f"{fpct(g.get('max_false_escalation_rate'))} and valid answers at least {fpct(g.get('min_valid_rate'))}.") if g else ""))
        socs = [(r["model"], self.I.call("socProjection", r, 0.99, 10000, with_res=False)) for r in tri.get("per_model") or []]
        socs = [(m, x) for m, x in socs if x]
        if socs:
            rows = [[self.mname(m), fint(x.get("escalations")), fint(x.get("escalate_attacks")), fint(x.get("investigations")), fint(x.get("analyst_queue")), fint(x.get("missed_attacks"))] for m, x in socs]
            cards.append(self.card("What would this look like in a real SOC?", self.table(["Model", "Escalated", "of which real attacks", "To investigate", "Analyst queue", "Missed attacks"], rows, num=(1, 2, 3, 4, 5)),
                                   intro="Out of 10,000 flows with 1% attacks, using each model's measured triage rates."))
        if orl.get("per_model") and any(r.get("format") or r.get("mitre") for r in orl["per_model"]):
            rows = [[self.mname(r["model"]), fpct((r.get("format") or {}).get("exact_rate")), fpct((r.get("mitre") or {}).get("consistent_rate")), fpct((r.get("mitre") or {}).get("match_rate"))] for r in orl["per_model"]]
            cards.append(self.card("Are the answers consistent?", self.table(["Model", "Reply is just a label", "Same technique per label", "Technique as configured"], rows, num=(1, 2, 3))))
        cards.append(self.card("Do the explanations match the verdict?", "", intro=""))
        fails = []
        for m in (self.res.get("agents") or {}).get("per_model") or []:
            for a in m.get("agents") or []:
                f = a.get("failures") or {}
                fails.append([self.mname(m["model"]), e(a["agent"]), fint(f.get("empty_output")), fint(f.get("hit_token_cap")), fint(f.get("unparseable_label")) if f.get("unparseable_label") is not None else "–", fpct(a.get("failure_rate"))])
        if fails:
            cards.append(self.card("Where do answers fail?", self.table(["Model", "Agent", "Empty", "At token budget", "Not a label", "Failure rate"], fails, num=(2, 3, 4, 5))))
        return self.part("Output reliability", "Whether the answers are usable, how they compare with reference labels, and what an analyst would face.", "reliability", cards)

    def performance(self) -> str:
        cards = []
        rel = self.rel
        if rel:
            rows = []
            for m in rel.get("metrics") or []:
                if m.get("metric") == "accuracy":
                    continue
                f = {"mean_latency_s": ftime, "mean_peak_vram_mb": fmb}.get(m["metric"], fint)
                res_t = ("not measured" if not m.get("available") else "tie, " + self.plain(m.get("note") or "") if m.get("winner") == "tie"
                         else f'{self.nick(m["winner"])}, {fnum(m.get("pct_better"), 1)}% better')
                rows.append([e(self.plain(m.get("label")).capitalize().replace("vram", "VRAM"))] + [f(m["values"].get(x)) for x in self.models] + [e(res_t)])
            cards.append(self.card("Which model is faster, and by how much?", self.table(["Measure"] + [self.mname(x) for x in self.models] + ["Result"], rows, num=tuple(range(1, len(self.models) + 1)))))
        pc = self.res.get("per_class") or {}
        if pc.get("table"):
            rows = [[e(self.nick(r.get("model"))), e(r.get("class")), fint(r.get("n")), ftime(r.get("mean_latency_s")), fint(r.get("mean_tokens")), fmb(r.get("mean_vram_mb")), fpct(r.get("accuracy"), 0)] for r in pc["table"]]
            cards.append(self.card("Does the attack class change the cost?", self.table(["Model", "Class", "Flows", "Latency", "Tokens", "Working memory", "Accuracy"], rows, num=(2, 3, 4, 5, 6)), answer="", note=pc.get("note"), keep=False))
        ld = self.res.get("latency_distribution") or {}
        if ld.get("per_model"):
            h = self.I.call("latencyHistogram", default={}) or {}
            svg = ch.histograms([dict(label=self.nick(m["model"]), color=self.color.get(m["model"], "#6B7690"), bins=[b.get("count", 0) if isinstance(b, dict) else b for b in m.get("bins") or []],
                                      marks=[dict(label=k["key"], x=k["value"]) for k in m.get("marks") or [] if k["key"] in ("p50", "p95")]) for m in h.get("models") or []],
                                edges=h.get("edges") or [], x_fmt=ftime)
            rows = [[self.mname(r["model"]), fint(r.get("n")), ftime(r.get("p50")), ftime(r.get("p95")), ftime(r.get("p99")), ftime(r.get("mean_s")),
                     ftime((r.get("warmup") or {}).get("mean_steady_s")), e(self.plain((r.get("warmup") or {}).get("note") or "–"))] for r in ld["per_model"]]
            cards.append(self.card("How fast is each model?", svg + self.table(["Model", "Flows", "p50", "p95", "p99", "Mean", "Mean without the first 5", "Warm-up check"], rows, num=(1, 2, 3, 4, 5, 6)),
                                   intro="How the time per flow spreads. Dashed lines mark the typical flow (p50) and the slowest 5% (p95)."))
        lt = self.res.get("latency_trend") or {}
        if lt.get("per_model"):
            series = [dict(color=self.color.get(m["model"], "#6B7690"), points=[(p[0], p[2]) for p in m.get("points") or []],
                           faint_points=[(p[0], p[1]) for p in m.get("points") or []]) for m in lt["per_model"]]
            svg = ch.lines(series, y_fmt=ftime, x_label="Flow, in run order", y_label="latency")
            rows = [[self.mname(m["model"]), e({"stable": "steady", "slows_down": "slows down", "speeds_up": "speeds up"}.get((m.get("drift") or {}).get("verdict"), "not tested")), fnum((m.get("drift") or {}).get("ratio"), 2) + "×", e(self.plain((m.get("drift") or {}).get("note") or ""))] for m in lt["per_model"]]
            cards.append(self.card("Does it slow down during the run?", svg + self.table(["Model", "Trend", "Last quarter against first", "Detail"], rows, num=(2,)),
                                   intro="Each dot is one flow. The line is a rolling median."))
        cov = (self.res.get("advanced") or {}).get("latency_cov") or []
        if cov:
            rows = [[e(self.nick(r["model"])), ftime(r.get("mean_latency_s")), ftime(r.get("std_latency_s")), fnum(r.get("cov"), 3)] for r in cov]
            cards.append(self.card("How predictable is it?", self.table(["Model", "Mean", "Spread (SD)", "Coefficient of variation"], rows, num=(1, 2, 3)), answer="",
                                   intro="The coefficient of variation is the spread divided by the mean. Lower means steadier timings."))
        pdx = ((self.res.get("advanced") or {}).get("prefill_decode") or {}).get("per_model") or []
        if pdx:
            rows = [[e(self.nick(r["model"])), ftime(r.get("mean_prefill_s")), ftime(r.get("mean_decode_s"))] for r in pdx]
            cards.append(self.card("Reading or writing, where does the time go?", self.table(["Model", "Reading the prompt (prefill)", "Writing the answer (decode)"], rows, num=(1, 2)), answer=""))
        q = self.I.call("queueSim", 60, default={}) or {}
        if q.get("models"):
            rows = [[self.mname(m["model"]), fnum(m.get("capacity_per_min"), 1), fpct(min(1, m.get("utilisation") or 0)), "yes" if m.get("keeps_up") else "no",
                     ftime(m.get("mean_wait_s")), ftime(m.get("p95_wait_s")), fint(m.get("max_queue"))] for m in q["models"]]
            cards.append(self.card("How many flows can one GPU handle?", self.table(["Model", "Capacity / min", "GPU busy", "Keeps up?", "Mean wait", "p95 wait", "Longest queue"], rows, num=(1, 2, 4, 5, 6)),
                                   answer="", intro=f"At {fint(q.get('flows_per_min'))} incoming flows a minute, the web's default. One GPU handles one flow at a time, as measured.",
                                   note=q.get("method")))
        cs = self.res.get("cold_start") or {}
        if cs.get("per_model"):
            rows = [[self.mname(r["model"]), ftime(r.get("load_s")), ftime(r.get("first_flow_s")), ftime(r.get("median_rest_s")), ftime(r.get("first_flow_penalty_s"))] for r in cs["per_model"]]
            cards.append(self.card("How much does the first flow cost?", self.table(["Model", "Load time", "First flow", "Median of the rest", "First-flow penalty"], rows, num=(1, 2, 3, 4)), note=cs.get("note")))
        dr = self.res.get("drivers") or {}
        if dr.get("per_model"):
            blocks = []
            for m in dr["per_model"]:
                rows = [[e(f["feature"]), fnum(f.get("rho_latency"), 2), fnum(f.get("rho_tokens"), 2), fp(f.get("p_latency"))] for f in (m.get("features") or [])[:6]]
                blocks.append(f'<div class="sub"><p class="agm">{self.mname(m["model"])}</p>' + self.table(["Flow feature", "With latency", "With tokens", "p (latency)"], rows, num=(1, 2, 3)) + "</div>")
            cards.append(self.card("Which flows are slow, and why?", '<div class="cols">' + "".join(blocks) + "</div>",
                                   intro="Which flow features go together with a longer time or more tokens (Spearman correlation, +1 always more, -1 always less, 0 no link). Correlation, not cause."))
        mem = self.res.get("memory") or {}
        if mem.get("per_model"):
            rows = [[self.mname(r["model"]), fmb(r.get("weights_mb")), fmb(r.get("working_mean_mb")), fmb(r.get("working_peak_mb")), fmb(r.get("total_peak_mb")), fmb(r.get("device_peak_nvml_mb"))] for r in mem["per_model"]]
            cards.append(self.card("How much GPU memory does it need?", self.table(["Model", "Model weights", "Working, mean", "Working, highest", "Total peak", "Device peak (NVML)"], rows, num=(1, 2, 3, 4, 5)),
                                   note=" ".join(v for v in (mem.get("notes") or {}).values() if v)))
        tp = (self.res.get("advanced") or {}).get("throughput") or []
        if tp:
            rows = [[e(self.nick(r["model"])), fnum(r.get("tokens_per_s"), 1), fint(r.get("mean_output_tokens"))] for r in tp]
            cards.append(self.card("How fast does it write?", self.table(["Model", "Output tokens per second", "Output tokens per flow"], rows, num=(1, 2)), answer=""))
        ef = self.res.get("effect_sizes") or {}
        if ef.get("metrics") and not ef.get("skipped"):
            lab = {"latency_s": "Latency per flow", "peak_vram_mb": "Working memory", "tokens_per_flow": "Tokens per flow"}
            rows = []
            for r in ef["metrics"]:
                if not r.get("available"):
                    rows.append([e(lab.get(r["metric"], r["metric"])), "–", "–", "–", e(self.plain(r.get("reason") or "not available")), "–"])
                    continue
                f = ftime if r["metric"] == "latency_s" else (fmb if "vram" in r["metric"] else lambda v: fnum(v, 1))
                rows.append([e(lab.get(r["metric"], r["metric"])), f"{r['cliffs_delta']:+.2f}", e(r.get("magnitude")), f(r.get("diff_mean_a_minus_b")),
                             f"{f(r.get('ci95_low'))} to {f(r.get('ci95_high'))}", fp(r.get("kruskal_wallis_p"))])
            cards.append(self.card("Is the difference real?", self.table(["Measure", "Cliff's delta", "Size", "Mean gap A minus B", "95% range", "Kruskal-Wallis p"], rows, num=(1, 3, 4, 5)),
                                   intro=f"Compared, A = {self.nick(ef.get('model_a'))} and B = {self.nick(ef.get('model_b'))}.", note=ef.get("method")))
        ss = self.I.call("sampleSize", default={}) or {}
        if ss.get("needed") and len(self.models) > 1:
            rows = [[fpct(x.get("target"), 0), fint(x.get("flows")), fint(self.s.get("n_flows"))] for x in ss["needed"]]
            cards.append(self.card("How many flows are needed to tell models apart?", self.table(["To detect a latency difference of", "Flows per model", "This session"], rows, num=(1, 2)),
                                   note="80% power at the 5% level, from each model's measured spread."))
        return self.part("Performance", "How fast each model is, how steady, what drives it, and whether the gaps are real.", "performance", cards)

    def agents(self) -> str:
        ag = (self.res.get("agents") or {}).get("per_model") or []
        cards = []
        for m in ag:
            rows = [[e(a["agent"]), ftime(a.get("mean_latency_s")), fpct(a.get("latency_share")), fint(a.get("mean_input_tokens")), fint(a.get("mean_output_tokens")),
                     fpct(a.get("token_share")), fmb(a.get("mean_peak_vram_mb"))] for a in m.get("agents") or []]
            parts = [dict(label=a["agent"], value=_num(a.get("mean_latency_s")) or 0, color=ch.AGENT[j % len(ch.AGENT)]) for j, a in enumerate(m.get("agents") or [])]
            cards.append(self.card(f"{self.nick(m['model'])}: where does the time go?", ch.stacked(parts) + self.table(["Agent", "Latency", "Share of flow time", "Tokens in", "Tokens out", "Token share", "Working memory"], rows, num=(1, 2, 3, 4, 5, 6)),
                                   key="@agent:" + self._bare(m["model"])))
        hg = self.I.call("handoffGraph", default={}) or {}
        if hg.get("per_model"):
            rows = [[self.mname(m["model"]), e(x["from"]), e(x["to"]), fint(x.get("tokens"))] for m in hg["per_model"] for x in m.get("edges") or []]
            cards.append(self.card("What do agents pass to each other?", self.table(["Model", "From", "To", "Tokens re-sent per flow"], rows, num=(3,))))
        pc = self.res.get("prompt_composition") or {}
        if pc.get("per_model"):
            rows = [[self.mname(m["model"]), e(a["agent"]), fint(a.get("input_tokens")), fint(a.get("system_tokens")), fint(a.get("feature_tokens")), fint(a.get("handoff_tokens")),
                     fint(a.get("other_tokens")), fpct(a.get("cacheable_share"), 0)] for m in pc["per_model"] for a in m.get("agents") or []]
            cards.append(self.card("What are the prompts made of?", self.table(["Model", "Agent", "Input", "System", "Features", "Hand-off", "Other", "Cacheable"], rows, num=(2, 3, 4, 5, 6, 7)), note=pc.get("note")))
        ra = self.I.call("removeAgent", default={}) or {}
        if ra.get("per_model"):
            rows = [[self.mname(m["model"]), e(a["agent"]), ftime(a.get("saves_s")), fpct(a.get("saves_share"), 0), fint(a.get("saves_tokens")),
                     e(", ".join(a.get("readers") or []) or "nobody")] for m in ra["per_model"] for a in m.get("agents") or [] if a.get("agent") != ra.get("label_agent")]
            cards.append(self.card("What if one agent were removed?", self.table(["Model", "Remove", "Time saved / flow", "Share of flow time", "Tokens saved / flow", "Who loses its answer"], rows, num=(2, 3, 4)),
                                   intro=f"The {ra.get('label_agent')} agent gives the answer, so it cannot be removed."))
        bt = self.I.call("budgetTuning", default=[]) or []
        if bt:
            rows = [[self.mname(m["model"]), e(a["agent"]), fint(a.get("budget")), fpct(a.get("hit_rate"), 0), f'{fint(a.get("p50"))}, {fint(a.get("p95"))}, {fint(a.get("max"))}',
                     e(("lower to about " + fint(a.get("suggest"))) if a.get("advice") == "lower" else ("raise it" if a.get("advice") == "raise" else "keep"))] for m in bt for a in m.get("agents") or []]
            cards.append(self.card("Are the token budgets right?", self.table(["Model", "Agent", "Budget", "Hits it", "Writes (median, p95, max)", "Advice"], rows, num=(2, 3))))
        ca = self.I.call("classAgentTime", default={}) or {}
        if ca.get("per_model"):
            agents = ca.get("agents") or []
            rows = [[self.mname(m["model"]), e(r["cls"])] + [ftime(next((p["s"] for p in r.get("parts") or [] if p["agent"] == a), None)) for a in agents] + [ftime(r.get("total")), e(r.get("slowest_agent"))]
                    for m in ca["per_model"] for r in m.get("rows") or []]
            cards.append(self.card("Which attack classes take longest, agent by agent?", self.table(["Model", "Class"] + [e(a) for a in agents] + ["Total", "Slowest step"], rows, num=tuple(range(2, 3 + len(agents)))), keep=False))
        return self.part("Agents", "How each agent in the pipeline spends time and tokens, and where the pipeline could be trimmed.", "agents", cards)

    def cost(self) -> str:
        ce = self.res.get("cost_energy") or {}
        cards = []
        if ce.get("per_model"):
            rows = [[self.mname(r["model"]), fusd(r.get("cost_per_1k_flows_usd")) if r.get("cost_per_1k_flows_usd") is not None else e(self.plain(r.get("cost_status"))),
                     fwh(r.get("wh_per_flow")) if r.get("wh_per_flow") is not None else e(self.plain(r.get("energy_status"))), fwh(r.get("wh_per_flow_net_idle")),
                     fwh(r.get("wh_per_1k_flows")), (fnum(r.get("mean_power_w"), 0) + " W") if r.get("mean_power_w") is not None else "–",
                     (fnum(r.get("idle_power_w"), 0) + " W") if r.get("idle_power_w") is not None else "–"] for r in ce["per_model"]]
            pr = ce.get("price") or {}
            pl = (f"GPU price ${pr['usd_per_hour']:.3f} an hour, from {pr.get('source')}, fetched {pr.get('fetched_at')}." if pr.get("usd_per_hour") is not None
                  else f"No GPU price ({self.plain(pr.get('reason') or 'not set')}).")
            cards.append(self.card("What does it cost per 1,000 flows?", self.table(["Model", "Cost / 1,000 flows", "Energy / flow (board)", "Energy / flow (net of idle)", "Energy / 1,000 flows", "Mean power", "Idle power"], rows, num=(1, 2, 3, 4, 5, 6)),
                                   intro=pl, note=" ".join(v for k, v in (ce.get("notes") or {}).items() if v)))
        ee = self.I.call("energyEfficiency", default={}) or {}
        if ee.get("measured"):
            rows = [[self.mname(m["model"]), fwh(m.get("wh_per_flow")), fint(m.get("flows_per_kwh")), fnum(m.get("tokens_per_wh"), 0)] for m in ee.get("models") or []]
            cards.append(self.card("How energy-efficient is each model?", self.table(["Model", "Energy / flow", "Flows per kWh", "Tokens per Wh"], rows, num=(1, 2, 3))))
        gt = self.I.call("gpuTimeline", default=None)
        if gt and gt.get("sampled") and gt.get("points"):
            pts = gt["points"]
            series = [dict(color="#E8742A", points=[(p["t"], p["power_w"]) for p in pts])]
            svg = ch.lines(series, y_fmt=lambda v: f"{v:.0f} W", x_label="Seconds into the run", y_label="board power", y_min=0)
            wins = "; ".join(f'{self.nick(w["model"])} {fnum(w["from_s"], 0)} to {fnum(w["to_s"], 0)} s' for w in gt.get("windows") or [])
            cards.append(self.card("What did the GPU do during the run?", svg, answer="", intro=f"Board power over the whole run, sampled every {fnum(gt.get('interval_s'), 1)} s. Each model's turn, {wins}."))
        ic = self.I.call("idleCost", default={}) or {}
        if ic.get("measured") and self.answers.get("How much does an idle GPU cost?"):
            cards.append(self.card("How much does an idle GPU cost?", f'<p class="small">{e(self.plain(self.answers.get("How much does an idle GPU cost?") or ""))}</p>', answer=""))
        wp = self.I.call("workloadProjection", {"flows_per_day": 10000}, default={}) or {}
        if wp.get("models"):
            rows = [[self.mname(m["model"]), fnum(m.get("gpu_hours_per_day"), 2), fint(m.get("capacity_per_gpu_day")), fint(m.get("gpus_needed")), fusd(m.get("cost_per_day")), fusd(m.get("cost_per_month"))] for m in wp["models"]]
            cards.append(self.card("What would my volume cost?", self.table(["Model", "GPU-hours / day", "Max flows / GPU / day", "GPUs needed", "Cost / day", "Cost / 30 days"], rows, num=(1, 2, 3, 4, 5)),
                                   answer="", intro="At 10,000 flows a day, the web's default, at this run's measured speed and GPU price."))
        cv = self.I.call("costPerValid", default=[]) or []
        if cv:
            rows = [[self.mname(r["model"]), fpct(r.get("valid_rate")), fnum(r.get("flows_per_valid"), 2), ftime(r.get("gpu_s_per_valid")), fusd(r.get("usd_per_1k_valid")), fwh(r.get("wh_per_valid"))] for r in cv]
            cards.append(self.card("What does each usable answer cost?", self.table(["Model", "Valid answers", "Flows per valid answer", "GPU time / valid", "Cost / 1,000 valid", "Energy / valid"], rows, num=(1, 2, 3, 4, 5))))
        return self.part("Cost and energy", "What the run cost in money and energy, and what a real workload would cost.", "cost", cards)

    def recommendations(self) -> str:
        from .pdf_report import recommendation_lines
        cards = []
        rec = recommendation_lines(self.res)
        cards.append(self.card("Which model should I deploy?", '<ul class="find">' + "".join(f"<li>{e(self.plain(l))}</li>" for l in rec["lines"]) + "</ul>"
                               + (f'<p class="small">{e(self.plain(rec["hint"]))}</p>' if rec.get("hint") else ""), answer=""))
        rows = []
        for key, label in self._PRIO:
            r = self.I.call("recommend", key, 0.9, default=None)
            if not r or not r.get("best"):
                continue
            rows.append([e(label[0].upper() + label[1:]), e(self.nick(r["best"]["model"])), self.fmt_unit(r["best"].get("value"), r.get("unit")),
                         e(self.nick((r.get("runner_up") or {}).get("model")) if r.get("runner_up") else "–"), "yes" if r.get("tie") else "no"])
        if rows:
            cards.append(self.card("Which model is best for my priority?", self.table(["Priority", "Best", "Value", "Next", "Too close to call"], rows),
                                   answer="", intro="Among models with at least 90% usable answers."))
        fit = self.I.call("deploymentFit", default={}) or {}
        if fit.get("profiles"):
            rows = []
            for i, p in enumerate(fit["profiles"]):
                cells = []
                for m in fit.get("models") or []:
                    c = (m.get("cells") or [])[i] if i < len(m.get("cells") or []) else {}
                    r = c.get("result")
                    cells.append({"pass": "fits", "fail": "does not fit", "no_data": "no data"}.get(r, e(r or "–")))
                rows.append([e(p.get("label"))] + cells)
            cards.append(self.card("Which deployment setups does each model fit?", self.table(["Setup"] + [self.mname(m["model"]) + f'<br><small>{fint(m.get("passes"))} of {len(fit["profiles"])} fit</small>' for m in fit.get("models") or []], rows)))
        dh = self.decision or self.res.get("decision_helper")
        if dh:
            if not dh.get("limits_set"):
                body = '<p class="small">No limits were set for this PDF. Set them on the session\'s Recommendations page and download the PDF from there to include them.</p>'
            else:
                lims = ", ".join(f"{v.get('label')} {v['value']:g} {v.get('unit', '')}".strip() for v in (dh.get("inputs") or {}).values() if v.get("value") is not None)
                rows = [[self.mname(m["model"]), e(m.get("result")), e(self.plain(", ".join(f"{r['rule']} {r['result']}" for r in m.get("rows") or [] if r["result"] != "not_set")))] for m in dh.get("per_model") or []]
                body = f'<p class="small">Limits, {e(lims)}.</p>' + self.table(["Model", "Result", "Details"], rows)
            cards.append(self.card("Does a model fit your limits?", body, answer="", note=dh.get("framing")))
        s3 = self.res.get("recommendation_stage3") or {}
        if s3:
            allr = s3.get("rules") or []
            rows = [[e(r["rule"]), e(self.nick(r["model"])), e(r.get("condition")), e(r["result"]), e(", ".join(r.get("sources") or [])), e(r.get("fetched_at") or "–")]
                    for r in allr if r.get("result") != "no_data"]
            nod = len(allr) - len(rows)
            notes = "".join(f"<li>{e(self.plain(n.get('note')))}</li>" for n in s3.get("notes") or [])
            ids = sorted({r["rule"] for r in allr if r.get("result") == "no_data"})
            nd = (f'<p class="small">{nod} of {len(allr)} context rule checks had no data'
                  + (f" (Hugging Face metadata {e(s3.get('metadata_status'))})" if s3.get("metadata_status") not in (None, "ok") else "")
                  + f'. Rules without data, {e(", ".join(ids))}.</p>') if nod else ""
            cards.append(self.card("What is known about these models?", (f'<ul class="find">{notes}</ul>' if notes else "") + self.table(["Rule", "Model", "Condition", "Result", "Source", "Fetched at"], rows) + nd,
                                   answer="", intro="Context from the Hugging Face model pages. It never changes a score, a rank or the verdict.", keep=False))
        return self.part("Recommendations", "Which model to deploy for which need, and what is known about each model.", "recommendations", cards)

    def data(self) -> str:
        sid = self.res.get("session_identity") or {}
        pr = self.prepared
        cc = pr.get("class_counts") or {}
        kv = [["Prepared set fingerprint (SHA-256)", f'<span class="mono">{e(sid.get("prepared_set_sha256") or "–")}</span>'],
              ["Flows", fint(self.s.get("n_flows"))], ["Classes", e(", ".join(self.s.get("class_set") or []))]]
        if cc.get("available"):
            lost = (cc.get("lost_in_sampling") or []) + (cc.get("lost_in_selection") or [])
            kv.append(["Classes lost in sampling", e(", ".join(lost) if lost else "none")])
        cards = [self.card("Which flows were used?", self.table(["", ""], kv, cls="kv"), answer="",
                           note="The full per-flow list is in the CSV download on the session's Data page.")]
        return self.part("Data", "The exact flows this session measured, so it can be repeated.", None, cards)

    def method_back(self) -> str:
        p = (Path(__file__).resolve().parents[2] / "web" / "app.html")
        faq = ""
        try:
            html = p.read_text(encoding="utf-8")
            sec = html[html.index('id="help-method"'):]
            nxt = sec.find('id="help-', 20)
            sec = sec[:nxt] if nxt > 0 else sec[:12000]
            blocks = re.findall(r"<(h3|h4|p|li)[^>]*>(.*?)</\1>", sec, flags=re.S)
            items = []
            for tag, inner in blocks:
                txt = re.sub(r"<[^>]+>", "", inner)
                txt = re.sub(r"\s+", " ", txt).strip()
                if not txt:
                    continue
                txt = self.plain(txt.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
                items.append(f"<h4>{e(txt)}</h4>" if tag in ("h3", "h4") else f"<p>{e(txt)}</p>")
            faq = "".join(items)
        except (OSError, ValueError):
            faq = ""
        if not faq:
            return ""
        return f'<div class="part back"><p class="kicker">Reference</p><h2 class="ph">Methodology</h2><div class="cols2">{faq}</div></div>'

    # ================================================================ assemble
    def html(self) -> str:
        css = (_DIR / "report.css").read_text(encoding="utf-8").replace("{FONTS}", (_DIR / "fonts").as_uri())
        gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        foot = f"AgentMeter session report · {self.job.get('job_id') or self.s.get('run_id') or ''} · generated {gen}"
        body = (self.page1() + self.page2() + self.page3() + self.page4()
                + '<section class="detail">' + self.overview() + self.ranking() + self.reliability() + self.performance()
                + self.agents() + self.cost() + self.recommendations() + self.data() + self.method_back() + "</section>")
        title = f"AgentMeter session report {self.job.get('job_id') or ''}".strip()
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{e(title)}</title>'
                f'<style>{css}</style><style>@page {{ @bottom-left {{ content: "{e(foot)}"; }} }}</style></head><body>{body}</body></html>')


def build_report_html(res: dict[str, Any], **kw: Any) -> str:
    return Report(res, **kw).html()


def build_report_pdf(res: dict[str, Any], **kw: Any) -> bytes:
    from weasyprint import HTML
    html = build_report_html(res, **kw)
    return HTML(string=html, base_url=str(_DIR)).write_pdf()
