"""The session explainer: the LAST step of a session (design: docs/superpowers/specs/
2026-11-10-ai-explanations-design.md).

An open-source LLM writes the interpretive text the app shows (an Overview summary, each tab's
headline and why line, the Deployment recommendation and Optimisation guidance openings) and
describes what the agents' answers show. It never measures, scores or ranks. Its output goes to
<run_dir>/explanation.json; session_results.json is never changed.

Checked explanations (docs/superpowers/specs/2026-11-11-checked-explanations-design.md):

  build_digest(results, run_dir)   facts worked out from this session (budget verdicts, slowest
                                   steps, everyday names), the input of the statements
  statements.build_statements      every statement each section may use, with its numbers and
                                   the models and steps it is about
  answer_patterns.build_groups     the groups of answers that went wrong, counted by code
  orchestrate(gen, ...)            one request per section, then one per answer group, with the
                                   model loaded once: each sentence is checked against the
                                   statements it is tagged with, each pattern against the answers;
                                   a section that fails is asked once more, then left to the
                                   standard text
  explain_session(...)             runs orchestrate in the model's OWN process (its GPU memory is
                                   freed when it exits), tries the next model when one fails, and
                                   writes explanation.json. Never raises.

Engines: "hf" (transformers), "vllm", and "fake" (tests only, AGENTMETER_TEST_EXPLAINER=1).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("agentmeter.explain")

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "configs" / "explainer.yaml"
PROMPT_PATH = Path(__file__).with_name("explain_prompt.md")
PATTERN_PROMPT_PATH = Path(__file__).with_name("explain_patterns_prompt.md")
OUT_NAME = "explanation.json"

TABS = ("ranking", "reliability", "performance", "agents", "cost", "recommendations")
# the keys the app reads from explanation.json "sections"
SECTIONS = ("summary",) + tuple(f"tab.{t}.{p}" for t in TABS for p in ("h", "s")) + ("deployment", "optimisation")
# what each request asks for: purpose, sentences (fewest, most), words per sentence at most
SECTION_SPECS = {
    **{f"tab.{t}": {"purpose": p, "sentences": (1, 3), "words": 30} for t, p in {
        "ranking": "the headline of the Model Ranking tab: who ranks first on the efficiency score and how solid that is, "
                   "then one or two sentences on why",
        "reliability": "the headline of the Output Reliability tab: whether the answers were usable and where they fail, "
                       "then one or two sentences on why",
        "performance": "the headline of the Performance tab: speed per flow and what drives it, then one or two sentences on why",
        "agents": "the headline of the Agent Analysis tab: which pipeline step costs the most, then one or two sentences on why",
        "cost": "the headline of the Cost and Energy tab: cost and energy per 1,000 flows or why they were not measured, "
                "then one or two sentences on why",
        "recommendations": "the headline of the Recommendations tab: what to deploy and under which condition, then one or "
                           "two sentences on why"}.items()},
    "deployment": {"purpose": "the opening above the checklist Before you deploy it. Say what matters most for this "
                              "session and how the checks relate", "sentences": (2, 4), "words": 35},
    "optimisation": {"purpose": "the opening above the list What you can change, and what it would save. Say which "
                                "change to make first, what it costs and what it fixes", "sentences": (2, 4), "words": 35},
    "summary": {"purpose": "the summary at the top of the session. What was compared, the main finding, the most "
                           "important caveat and the one thing to do next", "sentences": (3, 5), "words": 35},
}
ORDER = tuple(f"tab.{t}" for t in TABS) + ("deployment", "optimisation", "summary")


# ---------------------------------------------------------------------------- configuration
def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    import yaml
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        cfg = {}
    cfg.setdefault("enabled", False)
    cfg.setdefault("models", [])
    cfg.setdefault("max_new_tokens", 1800)
    cfg.setdefault("timeout_s", 480)
    cfg.setdefault("retries", 1)
    cfg.setdefault("excerpts", 20)
    cfg.setdefault("excerpt_chars", 300)
    cfg.setdefault("slow_flows_per_model", 5)
    cfg.setdefault("answer_patterns", {})
    for k, v in {"max_groups": 4, "min_group": 3, "sample": 8, "contrast": 8, "chars": 300}.items():
        cfg["answer_patterns"].setdefault(k, v)
    return cfg


def explainer_models(cfg: Optional[dict] = None) -> list[str]:
    """Every configured explainer model id (empty when the explainer is turned off)."""
    cfg = cfg or load_config()
    return [m["id"] for m in cfg.get("models") or [] if m.get("id")] if cfg.get("enabled") else []


def startup_models(total_vram_gb: Optional[float], cfg: Optional[dict] = None) -> list[str]:
    """The explainer models to download at rental start (scripts/vast_models.py): every model
    whose memory need fits this GPU (with 1 GB to spare), so the fallback can load when the first
    one fails during a session."""
    cfg = cfg or load_config()
    if not cfg.get("enabled"):
        return []
    return [m["id"] for m in cfg.get("models") or []
            if m.get("id") and (total_vram_gb is None or total_vram_gb - 1 >= float(m.get("min_free_vram_gb") or 0))]


# ---------------------------------------------------------------------------- names
def bare(m: str) -> str:
    return re.sub(r"^mock:", "", str(m or ""))


def nick(m: str) -> str:
    """The app's everyday model name (web/insights.js nick)."""
    b = bare(m).split("/")[-1]
    x = re.sub(r"^Meta-", "", b, flags=re.I)
    x = re.sub(r"[-_](instruct|chat|it)([-_].*)?$", "", x, flags=re.I)
    x = re.sub(r"[-_]\d+k$", "", x, flags=re.I)
    if x == b and not re.search(r"\d(\.\d+)?b\b", x, flags=re.I):
        return b
    x = re.sub(r"(\d(?:\.\d+)?)b\b", r"\1B", x, flags=re.I).replace("-", " ")
    x = re.sub(r"^Phi 3", "Phi-3", x, flags=re.I)
    return x[:1].upper() + x[1:]


# ---------------------------------------------------------------------------- digest
def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _r(v: Any, d: int = 2) -> Optional[float]:
    f = _num(v)
    return None if f is None else round(f, d)


def _pct(v: Any, d: int = 1) -> Optional[float]:
    f = _num(v)
    return None if f is None else round(100 * f, d)


def _secs(v: Any) -> Optional[float]:
    """Seconds with the precision the app shows (milliseconds below a second)."""
    f = _num(v)
    if f is None:
        return None
    return round(f, 4) if f < 1 else round(f, 2)


def _by_model(rows: Any) -> dict[str, dict]:
    return {r.get("model"): r for r in rows or [] if isinstance(r, dict) and r.get("model")}


_ID_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\bport\s+\d+\b", re.I)


def _scrub(text: str, n: int) -> str:
    """An agent answer as a short excerpt: IPv4 addresses and port numbers removed."""
    t = _ID_RE.sub("[removed]", str(text or "")).replace("\n", " ").strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _flows_file(run_dir: Optional[Path], run_id: Optional[str]) -> Optional[dict]:
    if not run_dir:
        return None
    p = Path(run_dir) / f"flows_{run_id}.json" if run_id else None
    cands = [p] if p and p.is_file() else sorted(Path(run_dir).glob("flows_*.json"))
    for c in cands:
        try:
            return json.loads(c.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def build_digest(res: dict, run_dir: Optional[str | Path] = None, cfg: Optional[dict] = None) -> dict[str, Any]:
    """Everything the explainer may use, from this session only. Values are rounded the way the
    app shows them, so the number check can compare like with like."""
    cfg = cfg or load_config()
    sess = res.get("session") or {}
    models = [pm.get("model") for pm in res.get("per_model") or [] if pm.get("model")]
    names = {m: nick(m) for m in models}
    env = res.get("environment") or {}
    real = env.get("provider") == "real"
    p8 = res.get("phase8") or {}
    eff_w = (res.get("phase8_efficiency_only") or {}).get("weights") or p8.get("weights") or {}
    mem = _by_model((res.get("memory") or {}).get("per_model"))
    ce = _by_model((res.get("cost_energy") or {}).get("per_model"))
    rel = _by_model((res.get("output_reliability") or {}).get("per_model"))
    tri = _by_model((res.get("triage") or {}).get("per_model"))
    ag = _by_model((res.get("agents") or {}).get("per_model"))
    norm = {bare(k): v for k, v in _by_model(p8.get("normalised")).items()}    # keyed "mock:<id>" in demo runs

    per_model = []
    for pm in res.get("per_model") or []:
        m = pm.get("model")
        e2e = ((pm.get("efficiency") or {}).get("end_to_end")) or {}
        saw = pm.get("saw_efficiency_only") or pm.get("saw") or {}
        mm, cc, rr, tt = mem.get(m) or {}, ce.get(m) or {}, rel.get(m) or {}, tri.get(m) or {}
        row: dict[str, Any] = {
            "name": names[m],
            "mean_latency_s": _secs(e2e.get("mean_latency_s")),
            "p95_latency_s": _secs(e2e.get("p95_latency_s")),
            "tokens_per_flow": _r(e2e.get("mean_tokens_per_flow"), 0),
            "output_tokens_per_flow": _r(e2e.get("mean_output_tokens_per_flow"), 0),
            "efficiency_score_out_of_100": _r(saw.get("composite_100"), 1),
            "tier": saw.get("tier"),
            "rank": saw.get("rank"),           # the page's order, ties included
            "working_vram_mean_mb": _r(mm.get("working_mean_mb"), 0),
            "total_peak_vram_gb": _r((_num(mm.get("total_peak_mb")) or 0) / 1024, 1) if _num(mm.get("total_peak_mb")) else None,
            "memory_status": mm.get("status"),
            "valid_answer_pct": _pct(rr.get("valid_rate")),
            "empty_answers": rr.get("empty"), "cut_off_answers": rr.get("truncated"),
            "unreadable_labels": rr.get("unparseable"),
            "cost_per_1k_flows_usd": _r(cc.get("cost_per_1k_flows_usd"), 4),
            "cost_status": cc.get("cost_status") or None,
            "cost_reason": cc.get("cost_reason") or None,
            "wh_per_1k_flows": _r(cc.get("wh_per_1k_flows"), 1),
        }
        acc = pm.get("accuracy") or {}
        if sess.get("accuracy_available") and _num(acc.get("accuracy")) is not None:
            row["accuracy_pct"] = _pct(acc.get("accuracy"))
        if tt:
            row["triage"] = {"missed_attack_pct": _pct(tt.get("missed_attack_rate")),
                             "false_escalation_pct": _pct(tt.get("false_escalation_rate")),
                             "queue": tt.get("queue")}
        n = norm.get(bare(m)) or {}
        if eff_w:
            tot = sum(float(eff_w.get(k) or 0) for k in ("latency", "vram", "tokens")) or 1.0
            row["points"] = {k: _r(100 * float(eff_w.get(k) or 0) / tot * float(n.get(k) or 0), 1)
                             for k in ("latency", "vram", "tokens") if eff_w.get(k)}
        steps, pm_n = [], (ag.get(m) or {}).get("n_flows")
        for a in (ag.get(m) or {}).get("agents") or []:
            f = a.get("failures") or {}
            cap, hit, n = f.get("token_cap"), f.get("hit_token_cap") or 0, a.get("n") or pm_n
            mx = _num(a.get("output_tokens_max"))
            # the budget verdict, worked out here: cut off often means too SMALL a budget
            if cap and n and hit / n >= 0.10:
                up = int(math.ceil(max(cap * 1.25, (mx or 0) * 1.25) / 50.0) * 50)
                advice = f"raise the budget to about {up} tokens, {hit} of {n} answers were cut off at {cap} tokens"
            elif cap and mx is not None and mx < cap / 2:
                down = max(8, int(math.ceil(mx * 1.5 / 10.0) * 10))
                advice = f"the budget could be lowered to about {down} tokens, the longest answer used {int(mx)} of {cap} tokens"
            else:
                advice = "the budget fits"
            steps.append({"step": (a.get("agent") or "").capitalize(),
                          "share_of_each_flows_time_pct": _pct(a.get("latency_share"), 0),
                          "mean_time_s": _secs(a.get("mean_latency_s")),
                          "share_of_all_tokens_pct": _pct(a.get("token_share"), 0),
                          "prompt_tokens": _r(a.get("mean_input_tokens"), 0),
                          "answer_tokens": _r(a.get("mean_output_tokens"), 0),
                          "token_budget": cap, "answers_cut_off_at_budget": hit, "flows": n,
                          "budget_advice": advice.replace(":", ","),
                          "empty_answers": f.get("empty_output"), "failure_pct": _pct(a.get("failure_rate"))})
        row["steps"] = steps
        # the facts a small model tends to misread from the table, worked out here
        timed = [x for x in steps if x.get("share_of_each_flows_time_pct") is not None]
        if timed:
            top = max(timed, key=lambda x: x["share_of_each_flows_time_pct"])
            row["slowest_step"] = {"step": top["step"], "share_of_each_flows_time_pct": top["share_of_each_flows_time_pct"],
                                   "mean_time_s": top.get("mean_time_s")}
        wordy = [x for x in steps if x.get("answer_tokens") is not None]
        if wordy:
            top = max(wordy, key=lambda x: x["answer_tokens"])
            row["longest_answer_step"] = {"step": top["step"], "answer_tokens": top["answer_tokens"]}
        capped = [x for x in steps if (x.get("answers_cut_off_at_budget") or 0) > 0]
        if capped:
            top = max(capped, key=lambda x: x["answers_cut_off_at_budget"])
            row["most_cut_off_step"] = {"step": top["step"], "answers_cut_off": top["answers_cut_off_at_budget"],
                                        "of_flows": top.get("flows"), "token_budget": top.get("token_budget"),
                                        "budget_advice": top.get("budget_advice")}
        per_model.append({k: v for k, v in row.items() if v is not None})

    rc = res.get("relative_comparison") or {}
    gh = res.get("gpu_health") or {}
    digest: dict[str, Any] = {
        "session": {"models": [names[m] for m in models], "n_models": len(models),
                    "comparison": ("only one model ran, so nothing is ranked or compared between models"
                                   if len(models) == 1 else f"{len(models)} models compared on the same flows"),
                    "flows": sess.get("n_flows"), "input": sess.get("source_type"),
                    "labelled": bool(sess.get("accuracy_available")),
                    "gpu": env.get("gpu_name") if real else None, "demo": not real,
                    "pipeline_steps": len(((res.get("agents") or {}).get("agent_order")) or []) or None},
        "weights_pct": {k: _pct(float(eff_w.get(k) or 0) / (sum(float(eff_w.get(x) or 0) for x in ("latency", "vram", "tokens")) or 1), 0)
                        for k in ("latency", "vram", "tokens") if eff_w.get(k)},
        "targets": {k: v for k, v in (p8.get("targets") or {}).items() if k != "accuracy_pct" or sess.get("accuracy_available")},
        "per_model": per_model,
        "head_to_head": (_head_to_head(rc, names) if rc else None),
        "gpu_state": (("not normal" if gh.get("status") == "check" else "normal") if gh.get("available") else "not recorded"),
    }
    fl = _flows_file(Path(run_dir) if run_dir else None, sess.get("run_id"))
    if fl:
        digest["answer_excerpts"] = _excerpts(fl, names, cfg)
        digest["slowest_flows"] = _slowest(fl, names, cfg)
    return _clean(digest)


def _head_to_head(rc: dict, names: dict[str, str]) -> dict[str, Any]:
    out = []
    for mt in rc.get("metrics") or []:
        vals = {names.get(k, nick(k)): v for k, v in (mt.get("values") or {}).items()}
        if mt.get("available") is False:
            out.append({"metric": mt.get("label"), "measured": False})
            continue
        out.append({"metric": mt.get("label"), "lower_is_better": mt.get("lower_is_better"),
                    "values": {k: (_secs(v) if mt.get("unit") == "s" else _r(v, 1)) for k, v in vals.items()},
                    "unit": mt.get("unit"),
                    "better": names.get(mt.get("winner"), nick(mt.get("winner"))) if mt.get("winner") else "tie",
                    "pct_better": _r(mt.get("pct_better"), 1),
                    "p_value": _r(mt.get("p_value"), 4),
                    "beyond_chance": (None if _num(mt.get("p_value")) is None else bool(float(mt["p_value"]) < 0.05))})
    return {"verdict": _rename(rc.get("verdict"), names), "metrics": out}


def _rename(text: Any, names: dict[str, str]) -> Any:
    if not isinstance(text, str):
        return text
    for full in sorted(names, key=len, reverse=True):
        text = text.replace("mock:" + bare(full), names[full]).replace(bare(full), names[full]).replace(bare(full).split("/")[-1], names[full])
    return text


def _excerpts(fl: dict, names: dict[str, str], cfg: dict) -> list[dict]:
    n, chars = int(cfg.get("excerpts") or 20), int(cfg.get("excerpt_chars") or 300)
    fails, mixes = [], []
    for f in fl.get("flows") or []:
        for m, pm in (f.get("per_model") or {}).items():
            if not isinstance(pm, dict):
                continue
            for a in pm.get("agents") or []:
                txt = a.get("text")
                if txt is None:
                    continue
                if not str(txt).strip():
                    fails.append({"model": names.get(m, nick(m)), "step": a.get("agent"), "problem": "empty answer", "text": ""})
            if pm.get("correct") is False and pm.get("predicted"):
                last = (pm.get("agents") or [{}])[-1] if pm.get("agents") else {}
                decide = next((a for a in pm.get("agents") or [] if a.get("agent") in ("decide", "classify")), last)
                mixes.append({"model": names.get(m, nick(m)), "step": decide.get("agent"), "problem": "mix-up",
                              "true_class": f.get("truth"), "answered": pm.get("predicted"),
                              "text": _scrub(decide.get("text") or "", chars)})
    # one mix-up per (model, true, answered) pair, most common pairs first
    seen: dict[tuple, int] = {}
    for x in mixes:
        seen[(x["model"], x["true_class"], x["answered"])] = seen.get((x["model"], x["true_class"], x["answered"]), 0) + 1
    uniq, used = [], set()
    for x in sorted(mixes, key=lambda x: -seen[(x["model"], x["true_class"], x["answered"])]):
        k = (x["model"], x["true_class"], x["answered"])
        if k in used:
            continue
        used.add(k)
        uniq.append(dict(x, times=seen[k]))
    return (fails[: n // 2] + uniq)[:n]


def _slowest(fl: dict, names: dict[str, str], cfg: dict) -> list[dict]:
    k = int(cfg.get("slow_flows_per_model") or 5)
    rows: dict[str, list] = {}
    for f in fl.get("flows") or []:
        for m, pm in (f.get("per_model") or {}).items():
            if isinstance(pm, dict) and _num(pm.get("latency_s")) is not None:
                rows.setdefault(m, []).append((float(pm["latency_s"]), f, pm))
    out = []
    for m, lst in rows.items():
        for lat, f, pm in sorted(lst, key=lambda x: -x[0])[:k]:
            out.append({"model": names.get(m, nick(m)), "time_s": _secs(lat), "true_class": f.get("truth"),
                        "steps": [{"step": a.get("agent"), "time_s": _secs(a.get("wall_s")),
                                   "prompt_tokens": a.get("input_tokens"), "answer_tokens": a.get("output_tokens")}
                                  for a in pm.get("agents") or []]})
    return out


def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items() if v is not None and v != {} and v != []}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


# ---------------------------------------------------------------------------- the owner's rules
_EMPTY_CLAIMS = [
    (r"no (issues|problems) (noted|found|observed)|\bsolid\b|is reliable\b|(high|good|strong) reliability|reliability (is|was) (high|good|strong|excellent|fine|solid)",
     "an unsupported reassurance"),
    (r"\b(retrain|fine-?tun)", "advice outside what AgentMeter can change"),
    (r"score (of |was |is |at )?\d+(\.\d+)?\s?%|\d+(\.\d+)?%\s+(efficiency )?score", "the efficiency score is points out of 100, not a percent"),
]
_STYLE = [(re.compile(r";"), "semicolon"), (re.compile(r"(?<!\d):|:(?!\d)"), "colon"),
          (re.compile(r"[—–]"), "dash"), (re.compile(r"\s-\s|^-\s|\s-$"), "dash")]
_UP = re.compile(r"\b(increas|rais|expand|enlarg|extend)\w*\s+(?:\w+\s+){0,4}?budget|\b(higher|larger|bigger)\s+(?:token\s+)?budget", re.I)
_DOWN = re.compile(r"\b(reduc|lower|decreas|cut(?!\s+off)|shrink|trim)\w*\s+(?:\w+\s+){0,4}?budget|\b(smaller|lower|tighter)\s+(?:token\s+)?budget", re.I)


def rule_problem(t: str, digest: dict) -> Optional[str]:
    """The rules every written sentence follows, whatever its statements say."""
    for rx, what in _STYLE:
        if rx.search(t):
            return f"uses a {what}"
    if "/" in t and re.search(r"\b[\w.-]+/[\w.-]+\b", t):
        return "uses a full model id"
    if re.search(r"than (expected|anticipated|usual)", t, re.I):
        return "compares with an expectation that is not in the results"
    for rx, why in _EMPTY_CLAIMS:
        if re.search(rx, t, re.I):
            return why
    if re.search(r"['\"‘“][A-Za-z]+['\"’”]", t):
        return "puts quote marks around a name"
    why = _budget_contradiction(t, digest)
    if why:
        return why
    if (digest.get("session") or {}).get("n_models") == 1 and re.search(
            r"\b(ranks? (first|top|highest)|most efficient|best model|outperform|beats?)\b", t, re.I):
        return "ranks a model in a one-model session"
    return None


def _budget_contradiction(text: str, digest: dict) -> Optional[str]:
    """A sentence that tells a step's budget to go the other way than its worked-out verdict."""
    advice: dict[str, str] = {}
    for pm in digest.get("per_model") or []:
        for st in pm.get("steps") or []:
            if st.get("step") and st.get("budget_advice"):
                advice.setdefault(str(st["step"]).lower(), st["budget_advice"])
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if "budget" not in sent.lower():
            continue
        named = [n for n in advice if re.search(r"\b" + re.escape(n) + r"\b", sent, re.I)]
        if len(named) != 1:                                 # no step, or several: cannot tell which
            continue
        n, adv = named[0], advice[named[0]]
        if _UP.search(sent) and re.search(r"\b(efficien|faster|speed|quicker|cheaper)", sent, re.I):
            return f"says a bigger budget for {n.capitalize()} improves efficiency, but it costs time and tokens"
        if _UP.search(sent) and not adv.startswith("raise"):
            return f"advises a bigger budget for {n.capitalize()}, but {adv}"
        if _DOWN.search(sent) and adv.startswith("raise"):
            return f"advises a smaller budget for {n.capitalize()}, but {adv}"
    return None


def _matches(t: float, dec: int, allowed: list[float]) -> bool:
    """A sentence's number equals one of its statements' numbers, at the sentence's rounding."""
    step = 10 ** -dec
    return any(abs(round(v, dec) - t) <= step / 2 + 1e-9 for v in allowed)


def known_names(digest: dict) -> tuple[list[str], list[str]]:
    """The session's model names and step names, as the statements write them."""
    models = list((digest.get("session") or {}).get("models") or [])
    steps = sorted({str(st.get("step")) for pm in digest.get("per_model") or [] for st in pm.get("steps") or [] if st.get("step")})
    return models, steps


def check_sentence(text: str, uses: list[str], by_id: dict[str, dict], digest: dict) -> Optional[str]:
    """None when the sentence keeps to the statements it is tagged with (and the rules), else why not."""
    from .statements import values_of
    t = str(text or "").strip()
    if not t:
        return "empty"
    if len(t.split()) > 45:
        return "too long"
    if not uses:
        return "names no statement"
    unknown = [u for u in uses if u not in by_id]
    if unknown:
        return f"uses a statement that is not in this section ({', '.join(unknown)})"
    why = rule_problem(t, digest)
    if why:
        return why
    models, steps = known_names(digest)
    allowed = [v for u in uses for v in by_id[u]["values"]]
    for x in values_of(t, models):
        dec = len(str(x).split(".")[1]) if "." in str(x) and not float(x).is_integer() else 0
        if not _matches(x, dec, allowed):
            return f"the number {x:g} is not in its statements"
    subjects = {s for u in uses for s in by_id[u]["subjects"]}
    for n in models + steps:
        if re.search(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", t) and n not in subjects:
            return f"names {n}, which its statements are not about"
    return None


# ---------------------------------------------------------------------------- requests
def _json_reply(text: str) -> Any:
    """The JSON object in a model's reply (gpt-oss: its final channel), code fences allowed."""
    if "<|channel|>final<|message|>" in text:
        text = text.split("<|channel|>final<|message|>")[-1]
    text = re.sub(r"<\|[^|]*\|>", "", text)
    text = re.sub(r"```(?:json)?", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(text[a: b + 1])
        except ValueError:
            pass
    # cut off: keep every complete sentence object of a section reply
    got = []
    for m in re.finditer(r'\{\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"uses"\s*:\s*\[([^\]]*)\]\s*\}', text):
        try:
            got.append({"text": json.loads('"' + m.group(1) + '"'), "uses": json.loads("[" + m.group(2) + "]")})
        except ValueError:
            continue
    if got:
        return {"sentences": got}
    raise ValueError("no JSON object in the reply")


def section_messages(section: str, stmts: list[dict], digest: dict, retry: Optional[list[dict]] = None) -> list[dict]:
    spec = SECTION_SPECS[section]
    lo, hi = spec["sentences"]
    one = (digest.get("session") or {}).get("n_models") == 1
    ask = {"section": section, "purpose": spec["purpose"],
           "sentences": f"{lo} to {hi}", "words_per_sentence_at_most": spec["words"],
           "only_one_model_ran": one,
           "statements": [{"id": s["id"], "text": s["text"]} for s in stmts]}
    if section.startswith("tab."):
        ask["note"] = "The first sentence is the headline, at least 6 words. The next one or two explain it."
    user = "Write this section.\nINPUT\n" + json.dumps(ask, ensure_ascii=False)
    if retry:
        user += ("\n\nYour earlier sentences broke a rule. Write the section again and avoid these problems.\n"
                 + json.dumps(retry, ensure_ascii=False))
    return [{"role": "system", "content": PROMPT_PATH.read_text(encoding="utf-8")}, {"role": "user", "content": user}]


def pattern_messages(group: dict, cfg: dict, retry: Optional[str] = None) -> list[dict]:
    pc = cfg.get("answer_patterns") or {}
    n, k, chars = int(pc.get("sample", 8)), int(pc.get("contrast", 8)), int(pc.get("chars", 300))
    cut = lambda t: t if len(t) <= chars else t[: chars - 1] + "…"  # noqa: E731
    ask = {"group": group.get("describe"), "step": group.get("step"),
           "answers": [cut(t) for t in group.get("texts", [])[:n]],
           "contrast_answers": [cut(t) for t in group.get("contrast", [])[:k]]}
    user = "Describe this group.\nINPUT\n" + json.dumps(ask, ensure_ascii=False)
    if retry:
        user += "\n\nYour earlier reply was not kept, because " + retry + ". Try again or return an empty pattern."
    return [{"role": "system", "content": PATTERN_PROMPT_PATH.read_text(encoding="utf-8")}, {"role": "user", "content": user}]


def _norm_sentence(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def _assemble(section: str, kept: list[dict]) -> dict[str, str]:
    if section.startswith("tab."):
        out = {section + ".h": kept[0]["text"]}
        if len(kept) > 1:
            out[section + ".s"] = " ".join(x["text"] for x in kept[1:3])
        return out
    return {section: " ".join(x["text"] for x in kept)}


def orchestrate(gen: Callable[[list[dict]], tuple[str, int, int]], statements: dict[str, list[dict]],
                groups: list[dict], digest: dict, cfg: dict) -> dict:
    """One request per section, then per answer group, checked as described in the module's
    docstring. `gen(messages) -> (text, input tokens, output tokens)`."""
    from .answer_patterns import check_pattern
    t0 = time.time()
    tokens = {"input": 0, "output": 0}
    raws: list[str] = []

    def ask(msgs):
        text, n_in, n_out = gen(msgs)
        tokens["input"] += int(n_in or 0)
        tokens["output"] += int(n_out or 0)
        raws.append(text or "")
        return text or ""

    sections: dict[str, str] = {}
    detail: dict[str, dict] = {}
    dropped: dict[str, str] = {}
    seen: set[str] = set()
    for section in ORDER:
        stmts = statements.get(section.replace("tab.", "")) if section.startswith("tab.") else statements.get(section)
        if not stmts:
            continue
        by_id = {s["id"]: s for s in stmts}
        lo, _hi = SECTION_SPECS[section]["sentences"]
        retry, result = None, None
        for attempt in (1, 2):
            try:
                got = _json_reply(ask(section_messages(section, stmts, digest, retry)))
                sents = got.get("sentences") if isinstance(got, dict) else got
            except ValueError as e:
                sents, retry = [], [{"problem": str(e) + ", reply with the JSON object only"}]
                continue
            kept, bad = [], []
            for s in sents if isinstance(sents, list) else []:
                text, uses = str((s or {}).get("text") or "").strip(), [str(u) for u in (s or {}).get("uses") or []]
                why = check_sentence(text, uses, by_id, digest)
                if not why and section != "summary" and _norm_sentence(text) in seen:
                    why = "repeats a sentence of another section"
                (bad if why else kept).append({"text": text, "uses": uses, **({"why": why} if why else {})})
            first_ok = bool(kept) and (not section.startswith("tab.") or (sents and kept[0]["text"] == str((sents[0] or {}).get("text") or "").strip()
                                                                           and len(kept[0]["text"].split()) >= 6))
            if len(kept) >= lo and first_ok:
                result = (kept, bad)
                break
            retry = [{"sentence": b["text"], "problem": b["why"]} for b in bad] or [{"problem": "too few sentences"}]
        if result is None:
            dropped[section] = "no version passed the checks: " + "; ".join(r.get("problem", "") for r in (retry or []))[:300]
            continue
        kept, bad = result
        sections.update(_assemble(section, kept))
        detail[section] = {"sentences": kept, "dropped": bad}
        seen.update(_norm_sentence(x["text"]) for x in kept)

    patterns, patterns_dropped = [], []
    for g in groups:
        why = None
        for attempt in (1, 2):
            try:
                got = _json_reply(ask(pattern_messages(g, cfg, why)))
            except ValueError as e:
                why = str(e)
                continue
            if isinstance(got, dict) and not str(got.get("pattern") or "").strip():
                why = "no pattern fits"
                break
            kept, why = check_pattern(got, g)
            if kept:
                patterns.append(kept)
                break
        else:
            pass
        if not any(p["group"] == g.get("id") for p in patterns):
            patterns_dropped.append({"group": g.get("id"), "describe": g.get("describe"), "why": why})

    status = "ok" if sections and not dropped else "partial" if sections else "failed"
    out = {"status": status, "sections": sections, "detail": detail, "dropped": dropped,
           "patterns": patterns, "patterns_dropped": patterns_dropped,
           "statements": {s["id"]: s["text"] for v in statements.values() for s in v},
           "tokens": tokens, "seconds": round(time.time() - t0, 1)}
    if status == "failed":
        out["reason"] = "no section passed the checks"
    if status != "ok":
        out["raw_reply"] = "\n----- next reply -----\n".join(raws)[-6000:]
    return out


def _fake_gen(statements: dict[str, list[dict]], groups: list[dict]) -> Callable:
    """Test-only stand-in (AGENTMETER_TEST_EXPLAINER=1): sentences that copy their statements,
    one tagged with the wrong statement (dropped), and patterns from the answers, one with a made-up
    quote (dropped)."""
    from .answer_patterns import most_common_phrase
    used: set[str] = set()

    def gen(msgs):
        user = msgs[-1]["content"]
        req = json.loads(user.split("INPUT\n", 1)[1].split("\n\n", 1)[0])
        if "section" in req:
            st = req["statements"]
            fresh = [s for s in st if s["text"] not in used] if req["section"] != "summary" else st
            sents = [{"text": s["text"], "uses": [s["id"]]} for s in fresh[:3]]
            used.update(x["text"] for x in sents)
            if req["section"] == "tab.agents" and len(st) > 1 and "earlier" not in user:
                sents.insert(1, {"text": st[1]["text"], "uses": [st[0]["id"]]})       # wrong tag: dropped
            return json.dumps({"sentences": sents}), 0, 0
        g = next((x for x in groups if x.get("describe") == req.get("group")), None) or {}
        if g.get("id") == "g2":
            return json.dumps({"pattern": "These answers repeat the same wording.", "key_phrase": "the same wording",
                               "quote": "a sentence that no agent ever wrote"}), 0, 0
        phrase = most_common_phrase(g) or ""
        text = next((t for t in g.get("texts", []) if phrase and phrase in t.lower()), (g.get("texts") or [""])[0])
        quote = " ".join(text.split()[:10])
        return json.dumps({"pattern": f"These {g.get('step', '')} answers share the same wording before their conclusion.",
                           "key_phrase": phrase, "quote": quote}), 0, 0
    return gen


# ---------------------------------------------------------------------------- the model's process
def _load_generator(spec: dict) -> tuple[Callable, float]:
    """Load the model once; return gen(messages) -> (text, input tokens, output tokens)."""
    t0 = time.time()
    mid, mx = spec["model"], int(spec["max_new_tokens"])
    kw_chat = {"reasoning_effort": spec["reasoning"]} if spec.get("reasoning") else {}
    if spec["engine"] == "vllm":
        from vllm import LLM, SamplingParams
        llm = LLM(model=mid, gpu_memory_utilization=0.85, max_model_len=16384)
        sp = SamplingParams(max_tokens=mx, temperature=0.3)

        def gen(msgs):
            r = llm.chat(msgs, sp, **({"chat_template_kwargs": kw_chat} if kw_chat else {}))
            return r[0].outputs[0].text, len(r[0].prompt_token_ids or []), len(r[0].outputs[0].token_ids)
        return gen, round(time.time() - t0, 1)
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(mid)
    model = AutoModelForCausalLM.from_pretrained(mid, dtype="auto", device_map="cuda")

    def gen(msgs):
        # transformers 4.56+ and 5.x: ask for the mapping (input_ids, attention_mask)
        enc = tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=True, return_dict=True,
                                      return_tensors="pt", **kw_chat)
        if hasattr(enc, "keys"):
            inputs = {k: v.to(model.device) for k, v in enc.items() if hasattr(v, "to")}
        else:
            inputs = {"input_ids": enc.to(model.device)}
        n_in = int(inputs["input_ids"].shape[-1])
        with torch.no_grad():
            out = model.generate(**inputs, max_new_tokens=mx, do_sample=True, temperature=0.3)
        new = out[0][n_in:]
        return tok.decode(new, skip_special_tokens=False), n_in, int(new.shape[-1])
    return gen, round(time.time() - t0, 1)


def _worker_main(argv: list[str]) -> int:
    """Child process: load the model once, run every request with its checks, write the result,
    exit (frees the GPU). An error is written with its full traceback, for explanation.json."""
    import traceback
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    out = Path(argv[1])
    try:
        gen, load_s = _load_generator(spec)
        plan = spec["plan"]
        res = orchestrate(gen, plan["statements"], plan["groups"], plan["digest"], plan["cfg"])
        res["load_s"] = load_s
        out.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        return 0
    except BaseException as e:  # noqa: BLE001 — report it to the parent, then fail
        out.write_text(json.dumps({"error": f"{type(e).__name__}: {e}"[:400], "trace": traceback.format_exc()[-4000:]}),
                       encoding="utf-8")
        return 1


class ExplainerError(RuntimeError):
    """The explainer process failed; .trace holds its traceback for explanation.json."""
    trace = ""


def _run_child(mspec: dict, plan: dict, cfg: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="am-explain-") as td:
        spec_p, out_p = Path(td) / "in.json", Path(td) / "out.json"
        spec_p.write_text(json.dumps({"engine": mspec.get("engine", "hf"), "model": mspec["id"],
                                      "reasoning": mspec.get("reasoning"), "max_new_tokens": int(cfg["max_new_tokens"]),
                                      "plan": plan}, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, HF_HUB_OFFLINE=os.environ.get("HF_HUB_OFFLINE", "1"),
                   # reuse memory PyTorch reserved in pieces (a long prompt's attention needs one block)
                   PYTORCH_CUDA_ALLOC_CONF=os.environ.get("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True"))
        p = subprocess.run([sys.executable, "-m", "agentmeter.session.explain", "--worker", str(spec_p), str(out_p)],
                           cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=int(cfg["timeout_s"]))
        got = None
        if out_p.is_file():
            try:
                got = json.loads(out_p.read_text(encoding="utf-8"))
            except ValueError:
                got = None
        if p.returncode != 0 or not got or got.get("error"):
            trace = (got or {}).get("trace") or (p.stderr or "")[-4000:]
            err = ExplainerError("explainer process failed: " + ((got or {}).get("error") or
                                 (trace.strip().splitlines() or ["no output"])[-1])[:300])
            err.trace = trace
            raise err
        return got


def _free_vram_gb() -> Optional[float]:
    try:
        import torch
        if torch.cuda.is_available():
            free, _ = torch.cuda.mem_get_info()
            return free / 1024 ** 3
    except Exception:  # noqa: BLE001
        return None
    return None


MIN_TRANSFORMERS = (4, 56)        # gpt-oss needs 4.55, Apertus 4.56


def _engine_problem(engine: str) -> Optional[str]:
    """Why the engine cannot run the explainer on this machine, or None."""
    from importlib.metadata import PackageNotFoundError, version
    pkg = "vllm" if engine == "vllm" else "transformers"
    try:
        v = version(pkg)
    except PackageNotFoundError:
        return f"{pkg} is not installed on this server"
    if pkg == "transformers":
        parts = tuple(int(x) for x in re.findall(r"\d+", v)[:2])
        if parts < MIN_TRANSFORMERS:
            return f"transformers {v} is too old for the explainer (needs {MIN_TRANSFORMERS[0]}.{MIN_TRANSFORMERS[1]} or newer)"
    return None


def _fitting_models(cfg: dict, free_gb: Optional[float]) -> list[dict]:
    """The configured models whose memory need fits the free GPU memory, in config order."""
    return [m for m in cfg.get("models") or []
            if free_gb is None or free_gb >= float(m.get("min_free_vram_gb") or 0)]


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def plan_for(results: dict, run_dir: str | Path, cfg: dict) -> dict:
    """What the model's process needs: the statements, the answer groups, the digest, the config."""
    from .answer_patterns import build_groups
    from .statements import build_statements
    digest = build_digest(results, run_dir, cfg)
    names = {pm.get("model"): nick(pm.get("model")) for pm in results.get("per_model") or [] if pm.get("model")}
    flows = _flows_file(Path(run_dir) if run_dir else None, (results.get("session") or {}).get("run_id"))
    return {"statements": build_statements(results, digest), "groups": build_groups(results, flows or {}, names, cfg),
            "digest": digest, "cfg": cfg}


def explain_session(results: dict, run_dir: str | Path, *, real: bool, cfg: Optional[dict] = None,
                    runner: Optional[Callable] = None, free_vram_gb: Optional[float] = None) -> dict:
    """Write <run_dir>/explanation.json and return it. Never raises.
    `runner(mspec, messages, cfg) -> {"text", "tokens"}` replaces the model (tests)."""
    run_dir = Path(run_dir)
    out: dict[str, Any] = {"status": "skipped", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        cfg = cfg or load_config()
        fake = os.environ.get("AGENTMETER_TEST_EXPLAINER") == "1"
        if not cfg.get("enabled") and not fake:
            out["reason"] = "the explainer is turned off (configs/explainer.yaml)"
        elif not real and not fake:
            out["reason"] = "demo run: no GPU, so no explanation is written"
        else:
            plan = plan_for(results, run_dir, cfg)
            out.update(prompt_sha256=_sha(PROMPT_PATH.read_text(encoding="utf-8") + PATTERN_PROMPT_PATH.read_text(encoding="utf-8")),
                       statements_sha256=_sha(json.dumps(plan["statements"], sort_keys=True)))
            if fake:
                cands = [{"id": "test-explainer", "engine": "fake"}]
            else:
                cands = _fitting_models(cfg, free_vram_gb if free_vram_gb is not None else _free_vram_gb())
            if not cands:
                out["reason"] = "the GPU does not have enough free memory for any configured explainer model"
            else:
                out.update(_try_models(plan, cands, cfg, runner, fake))
        write(run_dir, out)
    except Exception as e:  # noqa: BLE001 — never fail the session
        log.exception("explanation failed")
        out.update(status="failed", reason=f"{type(e).__name__}: {e}"[:300])
        if getattr(e, "trace", ""):
            out["trace"] = e.trace
        try:
            write(run_dir, out)
        except OSError:
            pass
    return out


def _try_models(plan: dict, cands: list[dict], cfg: dict, runner: Optional[Callable], fake: bool) -> dict:
    """Each fitting model in turn until one writes something; every failed attempt is kept."""
    attempts: list[dict] = []
    for mspec in cands:
        problem = None if (runner is not None or fake) else _engine_problem(mspec.get("engine", "hf"))
        if problem:
            attempts.append({"model": mspec["id"], "reason": problem, "skipped": True})
            continue
        try:
            if fake:
                res = orchestrate(_fake_gen(plan["statements"], plan["groups"]), plan["statements"], plan["groups"], plan["digest"], cfg)
            elif runner is not None:
                def gen(msgs, _m=mspec):
                    r = runner(_m, msgs, cfg)
                    tk = r.get("tokens") or {}
                    return r.get("text") or "", tk.get("input", 0), tk.get("output", 0)
                res = orchestrate(gen, plan["statements"], plan["groups"], plan["digest"], cfg)
            else:
                res = _run_child(mspec, plan, cfg)
        except Exception as e:  # noqa: BLE001 — try the next model
            log.warning("explainer %s failed: %s", mspec["id"], e)
            a = {"model": mspec["id"], "reason": f"{type(e).__name__}: {e}"[:300]}
            if getattr(e, "trace", ""):
                a["trace"] = e.trace
            attempts.append(a)
            continue
        res.update(model=mspec["id"], engine=mspec.get("engine", "hf"))
        if res["status"] != "failed":
            if attempts:
                res["attempts"] = attempts
            return res
        attempts.append({"model": mspec["id"], "reason": res.get("reason") or "no section passed the checks",
                         "raw_reply": res.get("raw_reply")})
    last = attempts[-1] if attempts else {}
    out = {"status": "skipped" if attempts and all(a.get("skipped") for a in attempts) else "failed",
           "reason": "; ".join(f"{a['model']}: {a['reason']}" for a in attempts)[:600] or "no explainer model could run",
           "attempts": attempts}
    for k in ("trace", "raw_reply"):
        if last.get(k):
            out[k] = last[k]
    return out


def write(run_dir: str | Path, data: dict) -> Path:
    p = Path(run_dir) / OUT_NAME
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def read(run_dir: str | Path) -> Optional[dict]:
    p = Path(run_dir) / OUT_NAME
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--worker":
        sys.exit(_worker_main(sys.argv[2:]))
    print("usage: python -m agentmeter.session.explain --worker <in.json> <out.json>", file=sys.stderr)
    sys.exit(2)
