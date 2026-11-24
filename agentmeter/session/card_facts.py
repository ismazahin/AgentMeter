"""The facts behind every reading paragraph of the PDF report (design:
docs/superpowers/specs/2026-11-24-why-each-model-design.md, "Extension").

For each report section ("card") and for each model's "why" block, code works out the facts a
paragraph may use. The explainer model only chooses, orders and words them, tagging each sentence
with the fact ids it used, and every sentence is checked against those facts (explain.py).

  fact = {"id", "text", "values", "subjects", "kind"}
  kind "measured"  a value recorded in this session
       "design"    a likely reason from the model's published design (model_design.py). Offered only
                   when it points the same way as the result (the direction rule), otherwise as
                   "would usually ..., but here ...". Sentences citing one must be hedged.
       "context"   a general, always-true statement that lets a sentence state an impact without
                   inventing one ("the GPU is billed by the hour"). No numbers.

Every text uses everyday model names (explain.nick) and no colon or semicolon, so a sentence that
copies one still passes the style check.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from .statements import values_of


def slug(text: str) -> str:
    """The key of a report section, from its question (the PDF uses the same)."""
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60]


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


class Facts:
    def __init__(self, key: str, names: list[str], steps: list[str]):
        self.key, self.names, self.steps, self.items = key, names, steps, []

    def _add(self, kind: str, text: Optional[str], subjects: list[str]) -> None:
        if not text or "None" in text or "not measured" in text and kind == "design":
            return
        text = text.replace(":", ",").replace(";", ",")
        self.items.append({"id": f"{self.key}.{len(self.items) + 1}", "text": text, "kind": kind,
                           "values": values_of(text, self.names) if kind != "context" else [],
                           "subjects": [s for s in subjects if s]})

    def m(self, text: Optional[str], *subjects: str) -> None:
        self._add("measured", text, list(subjects))

    def d(self, text: Optional[str], *subjects: str) -> None:
        self._add("design", text, list(subjects))

    def c(self, text: str) -> None:
        self._add("context", text, [])


class Ctx:
    """Everything the builders read, with the report's number formats."""

    def __init__(self, res: dict, design: Optional[dict] = None, insights: Any = None):
        from . import report_html as rh
        from .explain import nick
        self.res, self.rh = res, rh
        self.models = [m["model"] for m in res.get("per_model") or []]
        self.nick = {m: nick(m) for m in self.models}
        self.design = {re.sub(r"^mock:", "", k): v for k, v in (design or {}).items()}
        self.I = insights
        steps = []
        for m in (res.get("agents") or {}).get("per_model") or []:
            for a in m.get("agents") or []:
                s = str(a.get("agent") or "").capitalize()
                if s and s not in steps:
                    steps.append(s)
        self.steps = steps

    def call(self, fn: str, *args: Any, default: Any = None) -> Any:
        return self.I.call(fn, *args, default=default) if self.I else default

    def by(self, key: str) -> dict[str, dict]:
        return {re.sub(r"^mock:", "", str(r.get("model"))): r for r in ((self.res.get(key) or {}).get("per_model") or [])}

    def e2e(self, m: str) -> dict:
        pm = next((x for x in self.res.get("per_model") or [] if x["model"] == m), {})
        return ((pm.get("efficiency") or {}).get("end_to_end")) or {}

    def n(self, m: Any) -> str:
        m = re.sub(r"^mock:", "", str(m))
        return self.nick.get(m) or next((v for k, v in self.nick.items() if k.endswith(m) or m.endswith(k)), m)

    def metric(self, key: str) -> Optional[dict]:
        return next((x for x in (self.res.get("relative_comparison") or {}).get("metrics") or [] if x.get("metric") == key), None)


# ------------------------------------------------------------------------------------------- cards
def _perf_spread(f: Facts, x: Ctx) -> None:
    t = x.rh.ftime
    for r in (x.res.get("latency_distribution") or {}).get("per_model") or []:
        n = x.n(r["model"])
        f.m(f"A typical flow for {n} took {t(r.get('p50'))}, and its slowest 5% took {t(r.get('p95'))} or more.", n)
        if _num(r.get("p99")) is not None:
            f.m(f"The slowest 1% of flows for {n} took {t(r.get('p99'))} or more.", n)
        if _num(r.get("max_s")) is not None:
            f.m(f"The single slowest flow for {n} took {t(r.get('max_s'))}.", n)
        p50, p95 = _num(r.get("p50")), _num(r.get("p95"))
        if p50 and p95:
            f.m(f"For {n} the slowest 5% of flows took {p95 / p50:.1f} times as long as a typical flow.", n)
    f.c("For a batch job only the total time matters. For an analyst waiting on each verdict, the slowest flows are what gets noticed.")


def _perf_trend(f: Facts, x: Ctx) -> None:
    for m in (x.res.get("latency_trend") or {}).get("per_model") or []:
        d, n = m.get("drift") or {}, x.n(m["model"])
        if not d.get("tested"):
            continue
        word = {"stable": "kept a steady pace", "slows_down": "slowed down", "speeds_up": "sped up"}.get(d.get("verdict"), "")
        if word and _num(d.get("ratio")) is not None:
            f.m(f"{n} {word} over the run, its last quarter of flows took {d['ratio']:.2f} times as long as its first.", n)
    gh = x.res.get("gpu_health") or {}
    if gh.get("available"):
        f.m("The GPU ran normally for every model." if gh.get("status") != "check" else "The GPU was not in a normal state during the run.")
    f.c("A model that slows down as a run goes on would fall further behind on a long shift of traffic.")


def _perf_capacity(f: Facts, x: Ctx) -> None:
    q = x.call("queueSim", 60, default={}) or {}
    for m in q.get("models") or []:
        n = x.n(m["model"])
        if _num(m.get("capacity_per_min")) is None:
            continue
        f.m(f"One GPU running {n} clears about {m['capacity_per_min']:.1f} flows a minute.", n)
        f.m(f"At 60 incoming flows a minute, {n} {'keeps up' if m.get('keeps_up') else 'falls behind and its queue keeps growing'}.", n)
        if _num(m.get("capacity_per_min")) and not m.get("keeps_up"):
            need = int(-(-60 // max(m["capacity_per_min"], 1e-9)))
            f.m(f"Keeping up with 60 flows a minute would take about {need} GPUs running {n}.", n)
    f.c("One GPU answers one flow at a time in this pipeline, so a queue forms whenever flows arrive faster than they are answered.")
    f.c("Sending the agents only the flows an intrusion detection system flags lowers the volume they have to handle.")


def _perf_first(f: Facts, x: Ctx) -> None:
    t = x.rh.ftime
    for r in (x.res.get("cold_start") or {}).get("per_model") or []:
        n = x.n(r["model"])
        f.m(f"{n} took {t(r.get('load_s'))} to load and {t(r.get('first_flow_s'))} on its first flow, against {t(r.get('median_rest_s'))} for a typical later flow.", n)
        if _num(r.get("first_flow_ratio")) is not None:
            f.m(f"The first flow for {n} took {r['first_flow_ratio']:.1f} times as long as a typical one.", n)
    f.c("A service that keeps the model loaded pays the first-flow cost once. One that loads the model on demand pays it on every start.")


def _perf_predict(f: Facts, x: Ctx) -> None:
    for r in (x.res.get("advanced") or {}).get("latency_cov") or []:
        n = x.n(r["model"])
        if _num(r.get("cov")) is not None:
            f.m(f"The spread of {n}'s flow times is {r['cov'] * 100:.0f}% of its mean.", n)
    f.c("A smaller spread means the time for the next flow can be predicted more closely.")


def _perf_drivers(f: Facts, x: Ctx) -> None:
    for m in (x.res.get("drivers") or {}).get("per_model") or []:
        n = x.n(m["model"])
        fs = [z for z in m.get("features") or [] if _num(z.get("rho_latency")) is not None]
        if fs:
            top = fs[0]
            sig = _num(top.get("p_latency")) is not None and top["p_latency"] < 0.05
            dir_ = "longer" if top["rho_latency"] > 0 else "shorter"
            feat = str(top["feature"]).replace("/s", " per second").replace("/", " to ")
            f.m(f"For {n}, flows with a higher {feat} tended to take {dir_} (correlation {top['rho_latency']:+.2f}, "
                + ("beyond chance)." if sig else "which could be chance)."), n)
    f.c("A correlation shows which flows tend to be slower, not why they are slower.")


def _perf_memory(f: Facts, x: Ctx) -> None:
    mb = x.rh.fmb
    for r in (x.res.get("memory") or {}).get("per_model") or []:
        n = x.n(r["model"])
        if _num(r.get("total_peak_mb")) is None:
            f.m(f"GPU memory was not measured for {n} in this run.", n)
            continue
        f.m(f"{n} needed {mb(r.get('total_peak_mb'))} at its peak, {mb(r.get('weights_mb'))} of it the model weights.", n)
        f.m(f"While answering, {n} used {mb(r.get('working_mean_mb'))} of working memory on average and {mb(r.get('working_peak_mb'))} at most.", n)
        g = next((g for g in (8, 12, 16, 24, 40, 48, 80) if r["total_peak_mb"] * 1.2 <= g * 1024), None)
        if g:
            f.m(f"{n} fits a {g} GB GPU with room to spare.", n)
    f.c("Working memory grows with the length of each prompt, so longer inputs or more agents sharing one GPU need more of it.")


def _perf_class(f: Facts, x: Ctx) -> None:
    t = x.rh.ftime
    rows = (x.res.get("per_class") or {}).get("table") or []
    by: dict[str, list] = {}
    for r in rows:
        if _num(r.get("mean_latency_s")) is not None:
            by.setdefault(x.n(r.get("model")), []).append(r)
    for n, rs in by.items():
        rs.sort(key=lambda r: r["mean_latency_s"])
        f.m(f"For {n}, {rs[-1]['class']} flows were the slowest at {t(rs[-1]['mean_latency_s'])} and {rs[0]['class']} the quickest at {t(rs[0]['mean_latency_s'])}.", n)
    f.c("Traffic with more of the slower classes would take longer per flow than this balanced sample suggests.")


def _perf_throughput(f: Facts, x: Ctx) -> None:
    for r in (x.res.get("advanced") or {}).get("throughput") or []:
        n = x.n(r["model"])
        if _num(r.get("tokens_per_s")) is not None:
            f.m(f"{n} wrote about {r['tokens_per_s']:,.0f} output tokens a second.", n)
    f.c("Writing speed sets how long a long answer takes, so it matters most for the steps that write the most.")


def _perf_effects(f: Facts, x: Ctx) -> None:
    lab = {"latency_s": "time per flow", "peak_vram_mb": "working memory", "tokens_per_flow": "tokens per flow"}
    ef = x.res.get("effect_sizes") or {}
    for r in ef.get("metrics") or []:
        if not r.get("available") or _num(r.get("kruskal_wallis_p")) is None:
            continue
        p = r["kruskal_wallis_p"]
        f.m(f"On {lab.get(r['metric'], r['metric'])} the gap between {x.n(ef.get('model_a'))} and {x.n(ef.get('model_b'))} is "
            f"{r.get('magnitude')} and {'beyond chance' if p < 0.05 else 'could be chance'} (p = {p:.3f}).",
            x.n(ef.get("model_a")), x.n(ef.get("model_b")))
    f.c("A gap that could be chance may come from the particular flows sampled rather than from the models.")


def _perf_sample(f: Facts, x: Ctx) -> None:
    ss = x.call("sampleSize", default={}) or {}
    flows = (x.res.get("session") or {}).get("n_flows")
    for z in ss.get("needed") or []:
        if _num(z.get("flows")) is not None:
            f.m(f"Telling apart a {z['target'] * 100:.0f}% speed gap takes about {z['flows']:,.0f} flows per model, this session had {flows}.")
    f.c("More flows narrow the range a real difference can hide in.")


def _rel_usable(f: Facts, x: Ctx) -> None:
    for r in (x.res.get("output_reliability") or {}).get("per_model") or []:
        n = x.n(r["model"])
        f.m(f"{n} gave a usable answer on {r.get('valid_rate', 0) * 100:.1f}% of flows.", n)
        for k, w in (("empty", "came back empty"), ("truncated", "were cut off at the token budget"), ("unparseable", "did not end in a valid label")):
            if r.get(k):
                f.m(f"{r[k]} answers from {n} {w}.", n)
    f.c("A flow without a usable answer needs a person to look at it, however fast it was.")


def _rel_accuracy(f: Facts, x: Ctx) -> None:
    acc = x.res.get("accuracy_stats") or {}
    for r in acc.get("per_model") or []:
        n, ci = x.n(r["model"]), r.get("ci95") or [None, None]
        f.m(f"{n} matched the reference label on {r.get('correct')} of {r.get('n')} flows, {r.get('accuracy', 0) * 100:.0f}%, "
            f"likely between {(ci[0] or 0) * 100:.0f}% and {(ci[1] or 0) * 100:.0f}%.", n)
    for p in acc.get("pairs") or []:
        f.m(f"The accuracy gap between {x.n(p['a'])} and {x.n(p['b'])} {'is beyond chance' if p.get('differ') else 'could be chance'}.", x.n(p["a"]), x.n(p["b"]))
    f.c("Accuracy here is a check that the output is usable, not a measure of threat detection.")


def _rel_mixups(f: Facts, x: Ctx) -> None:
    cm = x.call("classMetrics", default={}) or {}
    for m in cm.get("models") or []:
        n = x.n(m["model"])
        mx = (m.get("mixups") or [None])[0]
        if mx:
            f.m(f"The most common mistake for {n} was {mx['truth']} flows answered as {mx['predicted']}, {mx['count']} times.", n)
    f.c("An attack answered as Benign is dismissed, so that mistake is the costly one in triage.")


def _rel_analyst(f: Facts, x: Ctx) -> None:
    for r in (x.res.get("triage") or {}).get("per_model") or []:
        n, q = x.n(r["model"]), r.get("queue") or {}
        f.m(f"{n} sent {q.get('Escalate', 0)} flows to escalation, {q.get('Investigate', 0)} to investigation and dismissed {q.get('Dismiss', 0)}.", n)
        if _num(r.get("missed_attack_rate")) is not None:
            f.m(f"{n} dismissed {r.get('missed_attacks')} of {r.get('n_attacks')} attacks, {r['missed_attack_rate'] * 100:.0f}%.", n)
    f.c("Every dismissed attack is one an analyst never sees.")


def _rel_failures(f: Facts, x: Ctx) -> None:
    for m in (x.res.get("agents") or {}).get("per_model") or []:
        n = x.n(m["model"])
        tot = sum(((a.get("failures") or {}).get("hit_token_cap") or 0) for a in m.get("agents") or [])
        emp = sum(((a.get("failures") or {}).get("empty_output") or 0) for a in m.get("agents") or [])
        f.m(f"Across its agents, {n} had {tot} answers cut off at the token budget and {emp} empty answers.", n)
    f.c("A cut-off answer from an early agent is passed on to the next one, so the damage can spread down the pipeline.")


def _ag_time(model: str) -> Callable[[Facts, Ctx], None]:
    def build(f: Facts, x: Ctx) -> None:
        t = x.rh.ftime
        for m in (x.res.get("agents") or {}).get("per_model") or []:
            if m["model"] != model:
                continue
            n = x.n(model)
            ag = [a for a in m.get("agents") or [] if _num(a.get("mean_latency_s")) is not None]
            for a in ag:
                s = a["agent"].capitalize()
                f.m(f"{s} took {t(a['mean_latency_s'])} per flow for {n}, {a.get('latency_share', 0) * 100:.0f}% of the flow's time, "
                    f"reading {a.get('mean_input_tokens', 0):,.0f} tokens and writing {a.get('mean_output_tokens', 0):,.0f}.", n, s)
            if ag:
                top = max(ag, key=lambda a: a["mean_latency_s"])
                f.m(f"{top['agent'].capitalize()} is the slowest step for {n}.", n, top["agent"].capitalize())
        f.c("The step that writes the longest answer usually takes the most time, because answers are written one token at a time.")
    return build


def _ag_handoff(f: Facts, x: Ctx) -> None:
    for m in (x.res.get("agents") or {}).get("per_model") or []:
        o, n = m.get("overhead") or {}, x.n(m["model"])
        if _num(o.get("handoff_tokens_per_flow")) is not None:
            f.m(f"For {n}, earlier answers re-sent to later agents add about {o['handoff_tokens_per_flow']:,.0f} tokens per flow, "
                f"{(o.get('handoff_share_of_total') or 0) * 100:.1f}% of all its tokens (an estimate).", n)
    f.c("Splitting the work between agents costs tokens, because each agent reads what the earlier ones wrote.")


def _ag_prompts(f: Facts, x: Ctx) -> None:
    for m in (x.res.get("prompt_composition") or {}).get("per_model") or []:
        pf, n = m.get("per_flow") or {}, x.n(m["model"])
        inp = _num(pf.get("input_tokens"))
        if inp:
            f.m(f"Each flow's prompts for {n} came to {inp:,.0f} tokens, {100 * (pf.get('feature_tokens') or 0) / inp:.0f}% of them flow features "
                f"and {100 * (pf.get('system_tokens') or 0) / inp:.0f}% instructions.", n)
    f.c("Flow features fill most of every prompt, so the number of features sent decides how much each agent reads.")


def _ag_remove(f: Facts, x: Ctx) -> None:
    ra = x.call("removeAgent", default={}) or {}
    for m in ra.get("per_model") or []:
        n = x.n(m["model"])
        cand = [a for a in m.get("agents") or [] if a.get("agent") != ra.get("label_agent") and _num(a.get("saves_s")) is not None]
        if cand:
            b = max(cand, key=lambda a: a["saves_s"])
            f.m(f"For {n}, removing {b['agent'].capitalize()} would save {x.rh.ftime(b['saves_s'])} per flow, {b.get('saves_share', 0) * 100:.0f}% of its time.", n, b["agent"].capitalize())
    if ra.get("label_agent"):
        f.m(f"{str(ra['label_agent']).capitalize()} gives the final label, so it cannot be removed.", str(ra["label_agent"]).capitalize())
    f.c("Removing a step also removes the answer later steps read from it, so it can change their answers.")


def _ag_budgets(f: Facts, x: Ctx) -> None:
    for m in x.call("budgetTuning", default=[]) or []:
        n = x.n(m["model"])
        for a in m.get("agents") or []:
            s = a["agent"].capitalize()
            f.m(f"{s} for {n} has a {a.get('budget')}-token budget, hit it on {(a.get('hit_rate') or 0) * 100:.0f}% of flows and wrote {a.get('p95')} tokens at the 95th percentile.", n, s)
    f.c("A bigger token budget lets cut-off answers finish, at the price of longer flows. A smaller one saves time and cuts off more answers.")


def _cost_1k(f: Facts, x: Ctx) -> None:
    ce = x.res.get("cost_energy") or {}
    pr = ce.get("price") or {}
    if _num(pr.get("usd_per_hour")) is not None:
        f.m(f"The GPU cost ${pr['usd_per_hour']:.3f} an hour.")
    for r in ce.get("per_model") or []:
        n = x.n(r["model"])
        if _num(r.get("cost_per_1k_flows_usd")) is not None:
            f.m(f"A thousand flows cost ${r['cost_per_1k_flows_usd']:.2f} on {n}.", n)
        else:
            f.m(f"Cost was not measured for {n} in this run.", n)
        if _num(r.get("wh_per_flow")) is not None:
            f.m(f"{n} used {r['wh_per_flow']:.3f} Wh per flow, drawing about {r.get('mean_power_w', 0):.0f} W.", n)
    f.c("The GPU is billed by the hour whatever it is doing, so the cost per flow follows the time per flow.")


def _cost_volume(f: Facts, x: Ctx) -> None:
    wp = x.call("workloadProjection", {"flows_per_day": 10000}, default={}) or {}
    for m in wp.get("models") or []:
        n = x.n(m["model"])
        if _num(m.get("gpus_needed")) is not None:
            f.m(f"Ten thousand flows a day would need {m['gpus_needed']} GPU{'s' if m['gpus_needed'] != 1 else ''} running {n}"
                + (f", about ${m['cost_per_day']:.2f} a day." if _num(m.get("cost_per_day")) is not None else "."), n)
    f.c("At a fixed daily volume, a faster model needs fewer GPU-hours, so speed differences turn straight into cost.")


def _cost_energy(f: Facts, x: Ctx) -> None:
    ee = x.call("energyEfficiency", default={}) or {}
    for m in ee.get("models") or []:
        if _num(m.get("flows_per_kwh")) is not None:
            n = x.n(m["model"])
            f.m(f"{n} handled about {m['flows_per_kwh']:,.0f} flows per kWh.", n)
    f.c("Two models drawing the same power differ in energy per flow only by how long each flow takes.")


def _rec_fit(f: Facts, x: Ctx) -> None:
    fit = x.call("deploymentFit", default={}) or {}
    n_prof = len(fit.get("profiles") or [])
    for m in fit.get("models") or []:
        n = x.n(m["model"])
        f.m(f"{n} fits {m.get('passes')} of {n_prof} common deployment setups.", n)
        fails = [p["label"] for p, c in zip(fit.get("profiles") or [], m.get("cells") or [], strict=False) if c.get("result") == "fail"]
        if fails:
            f.m(f"{n} does not fit {', '.join(fails[:3]).lower()}.", n)
    f.c("A setup that a model does not fit would need a bigger GPU, more GPUs or a lower volume.")


def _rank_scores(f: Facts, x: Ctx) -> None:
    for pm in x.res.get("per_model") or []:
        s = pm.get("saw_efficiency_only") or pm.get("saw") or {}
        if _num(s.get("composite_100")) is not None:
            f.m(f"{x.n(pm['model'])} scores {s['composite_100']:.1f} of 100 on the efficiency score.", x.n(pm["model"]))
    f.c("The efficiency score sums speed, memory and tokens into one number, so it hides which of them a model is good at.")


def _rank_sure(f: Facts, x: Ctx) -> None:
    rs = x.res.get("rank_stability") or {}
    for r in rs.get("per_model") or []:
        n = x.n(r["model"])
        if _num(r.get("p_first")) is not None:
            f.m(f"{n} came first in {r['p_first'] * 100:.0f}% of {rs.get('n_boot')} reshuffles of the flows.", n)
    f.c("A ranking that changes when the flows are reshuffled would likely change on a different sample of traffic.")


# (tab, question, builder). The questions are the report's section headings (report_html.py).
def card_specs(res: dict) -> list[tuple[str, str, Callable[[Facts, Ctx], None]]]:
    from .explain import nick
    specs = [
        ("ranking", "What is each model's efficiency score?", _rank_scores),
        ("ranking", "How sure is the ranking?", _rank_sure),
        ("reliability", "Did every flow get a usable answer?", _rel_usable),
        ("reliability", "Are the accuracy differences real?", _rel_accuracy),
        ("reliability", "Which classes does each model mix up?", _rel_mixups),
        ("reliability", "What would an analyst have to do?", _rel_analyst),
        ("reliability", "Where do answers fail?", _rel_failures),
        ("performance", "Does the attack class change the cost?", _perf_class),
        ("performance", "How fast is each model?", _perf_spread),
        ("performance", "Does it slow down during the run?", _perf_trend),
        ("performance", "How predictable is it?", _perf_predict),
        ("performance", "How many flows can one GPU handle?", _perf_capacity),
        ("performance", "How much does the first flow cost?", _perf_first),
        ("performance", "Which flows are slow, and why?", _perf_drivers),
        ("performance", "How much GPU memory does it need?", _perf_memory),
        ("performance", "How fast does it write?", _perf_throughput),
        ("performance", "Is the difference real?", _perf_effects),
        ("performance", "How many flows are needed to tell models apart?", _perf_sample),
    ]
    for pm in res.get("per_model") or []:
        specs.append(("agents", f"{nick(pm['model'])}: where does the time go?", _ag_time(pm["model"])))
    specs += [
        ("agents", "What do agents pass to each other?", _ag_handoff),
        ("agents", "What are the prompts made of?", _ag_prompts),
        ("agents", "What if one agent were removed?", _ag_remove),
        ("agents", "Are the token budgets right?", _ag_budgets),
        ("cost", "What does it cost per 1,000 flows?", _cost_1k),
        ("cost", "How energy-efficient is each model?", _cost_energy),
        ("cost", "What would my volume cost?", _cost_volume),
        ("recommendations", "Which deployment setups does each model fit?", _rec_fit),
    ]
    return specs


# --------------------------------------------------------------------------------------- why part
def _why_model(f: Facts, x: Ctx, m: str, other: Optional[str]) -> None:
    t, mb = x.rh.ftime, x.rh.fmb
    n, o = x.n(m), (x.n(other) if other else None)
    e = x.e2e(m)
    lat = x.metric("mean_latency_s")
    if _num(e.get("mean_latency_s")) is not None:
        txt = f"{n} averaged {t(e['mean_latency_s'])} per flow"
        if lat and other and _num(lat.get("p_value")) is not None:
            txt += (f", a gap to {o} that could be chance (p = {lat['p_value']:.3f})" if lat.get("winner") == "tie"
                    else f", a gap to {o} beyond chance (p = {lat['p_value']:.3f})")
        f.m(txt + ".", n, *( [o] if o else []))
    if _num(e.get("p95_latency_s")) is not None:
        f.m(f"The slowest 5% of {n}'s flows took {t(e['p95_latency_s'])} or more.", n)
    _ag_time(m)(f, x)
    for r in (x.res.get("advanced") or {}).get("throughput") or []:
        if x.n(r["model"]) == n and _num(r.get("tokens_per_s")) is not None:
            f.m(f"{n} wrote about {r['tokens_per_s']:,.0f} output tokens a second.", n)
    mem = x.by("memory").get(m) or {}
    if _num(mem.get("working_mean_mb")) is not None:
        f.m(f"While answering, {n} used {mb(mem['working_mean_mb'])} of working memory on average.", n)
    if _num(mem.get("total_peak_mb")) is not None:
        f.m(f"{n}'s total peak was {mb(mem['total_peak_mb'])}, {mb(mem.get('weights_mb'))} of it the weights.", n)
    if _num(e.get("mean_tokens_per_flow")) is not None:
        f.m(f"{n} read and wrote {e['mean_tokens_per_flow']:,.0f} tokens per flow.", n)
    pc = next((p for p in (x.res.get("prompt_composition") or {}).get("per_model") or [] if x.n(p["model"]) == n), None)
    if pc and _num((pc.get("per_flow") or {}).get("feature_tokens")) is not None:
        f.m(f"The same flow features came to {pc['per_flow']['feature_tokens']:,.0f} tokens per flow with {n}'s tokenizer.", n)
    # head-to-head results of this session
    for key, word in (("mean_peak_vram_mb", "working memory"), ("mean_tokens_per_flow", "tokens per flow"), ("mean_latency_s", "time per flow")):
        mt = x.metric(key)
        if mt and mt.get("available") and other:
            if mt.get("winner") == "tie":
                f.m(f"{n} and {o} tie on {word}.", n, o)
            elif mt.get("winner") and _num(mt.get("pct_better")) is not None:
                tested = key in ("mean_latency_s", "mean_peak_vram_mb")      # the per-flow measures the session tests
                f.m(f"{x.n(mt['winner'])} is {mt['pct_better']:.1f}% better on {word}"
                    + (", beyond chance." if tested else ", more than the tie range."), x.n(mt["winner"]))
    _why_design(f, x, m, other)
    f.c("Working memory during generation is mostly the attention cache, which grows with every token in the prompt.")
    f.c("Answers are written one token at a time, so the step that writes the most usually takes the longest.")


def _why_design(f: Facts, x: Ctx, m: str, other: Optional[str]) -> None:
    """Design facts as likely reasons, under the direction rule."""
    dm = x.design.get(re.sub(r"^mock:", "", m))
    if not dm:
        return
    n = x.n(m)
    if dm.get("layers") and dm.get("kv_heads"):
        f.d(f"{n} has {dm['layers']} layers with {dm['kv_heads']} key-value heads each.", n)
    if dm.get("vocab_size"):
        f.d(f"{n}'s vocabulary has {dm['vocab_size']:,} entries.", n)
    if dm.get("params_b"):
        f.d(f"{n} has about {dm['params_b']:.2f} billion parameters.", n)
    if dm.get("context_window"):
        f.d(f"{n} accepts prompts of up to {dm['context_window']:,} tokens.", n)
    do = x.design.get(re.sub(r"^mock:", "", other)) if other else None
    if not do:
        return
    o = x.n(other)
    mem = x.by("memory")
    wm, wo = _num((mem.get(m) or {}).get("working_mean_mb")), _num((mem.get(other) or {}).get("working_mean_mb"))
    km, ko = dm.get("kv_cache_per_token"), do.get("kv_cache_per_token")
    if km and ko and wm is not None and wo is not None and km != ko:
        ratio = max(km, ko) / min(km, ko)
        more = n if km > ko else o
        used_more = n if wm > wo else o
        if more == used_more:
            f.d(f"{more} likely needs more working memory because it caches about {ratio:.1f} times as much per token as "
                f"{o if more == n else n}, from its layers and key-value heads.", n, o)
        else:
            f.d(f"By design {more} would usually need more cache per token, about {ratio:.1f} times as much, "
                f"but here it used less working memory.", n, o)
    em, eo = x.e2e(m), x.e2e(other)
    vm, vo = dm.get("vocab_size"), do.get("vocab_size")
    tm, to = _num(em.get("mean_tokens_per_flow")), _num(eo.get("mean_tokens_per_flow"))
    if vm and vo and tm and to and vm != vo:
        bigger = n if vm > vo else o
        fewer = n if tm < to else o
        close = abs(tm - to) / max(tm, to) < 0.02
        if close:
            f.d(f"{bigger}'s larger vocabulary would usually turn the same text into fewer tokens, but here the two used about the same.", n, o)
        elif bigger == fewer:
            f.d(f"{bigger}'s larger vocabulary likely explains part of why it used fewer tokens.", bigger)
        else:
            f.d(f"{bigger}'s larger vocabulary would usually mean fewer tokens, but here it used more.", bigger)


def build_all(res: dict, design: Optional[dict] = None, insights: Any = None) -> dict[str, Any]:
    """{"cards": {key: {"tab", "question", "facts"}}, "why": {key: {"model", "facts"}}}."""
    x = Ctx(res, design, insights)
    names = list(x.nick.values())
    out: dict[str, Any] = {"cards": {}, "why": {}}
    for tab, q, build in card_specs(res):
        key = slug(q)
        f = Facts("c." + key[:24], names, x.steps)
        try:
            build(f, x)
        except Exception:                       # an older or partial block: the section keeps its one-line answer
            continue
        if any(i["kind"] == "measured" for i in f.items):
            out["cards"][key] = {"tab": tab, "question": q, "facts": f.items}
    ms = x.models
    for i, m in enumerate(ms):
        other = ms[1 - i] if len(ms) == 2 else None
        f = Facts("w" + str(i + 1), names, x.steps)
        try:
            _why_model(f, x, m, other)
        except Exception:
            continue
        out["why"][slug(x.n(m))] = {"model": x.n(m), "facts": f.items}
    return out
