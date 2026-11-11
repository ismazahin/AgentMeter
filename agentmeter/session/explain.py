"""The session explainer: the LAST step of a session (design: docs/superpowers/specs/
2026-11-10-ai-explanations-design.md).

An open-source LLM reads this session's finished results and writes the interpretive text the
app shows: an Overview summary, each tab's headline and "why" line, and the Deployment
recommendation and Optimisation guidance paragraphs. It never measures, scores or ranks. Its
output goes to <run_dir>/explanation.json; session_results.json is never changed.

  build_digest(results, run_dir)   the facts the model may use, from this session only: the
                                   results, plus a few agent-answer excerpts and the slowest
                                   flows from its flows_<run>.json (no IPs, ports or timestamps)
  check_section(key, text, digest) the owner's rules: every number from the digest, no ; : or
                                   dash as punctuation, everyday model names, word limits
  explain_session(...)             runs the model in its OWN process (its GPU memory is freed when
                                   it exits), checks each section, retries the failed ones once,
                                   writes explanation.json. Never raises: a failure is recorded and
                                   the session still finishes.

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
OUT_NAME = "explanation.json"

TABS = ("ranking", "reliability", "performance", "agents", "cost", "recommendations")
SECTIONS = ("summary",) + tuple(f"tab.{t}.{p}" for t in TABS for p in ("h", "s")) + ("deployment", "optimisation")
WORD_LIMIT = {"summary": 120, "deployment": 120, "optimisation": 120}
for _t in TABS:
    WORD_LIMIT[f"tab.{_t}.h"] = 20
    WORD_LIMIT[f"tab.{_t}.s"] = 30
MIN_WORDS = {"summary": 25, "deployment": 20, "optimisation": 20}
for _t in TABS:
    MIN_WORDS[f"tab.{_t}.h"] = 6
    MIN_WORDS[f"tab.{_t}.s"] = 8


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
    cfg.setdefault("timeout_s", 300)
    cfg.setdefault("retries", 1)
    cfg.setdefault("excerpts", 20)
    cfg.setdefault("excerpt_chars", 300)
    cfg.setdefault("slow_flows_per_model", 5)
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
            "efficiency_score": _r(saw.get("composite_100"), 1),
            "tier": saw.get("tier"),
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
        steps = []
        for a in (ag.get(m) or {}).get("agents") or []:
            f = a.get("failures") or {}
            steps.append({"step": a.get("agent"), "time_share_pct": _pct(a.get("latency_share"), 0),
                          "mean_time_s": _secs(a.get("mean_latency_s")),
                          "prompt_tokens": _r(a.get("mean_input_tokens"), 0),
                          "answer_tokens": _r(a.get("mean_output_tokens"), 0),
                          "token_budget": f.get("token_cap"), "hit_budget": f.get("hit_token_cap"),
                          "empty": f.get("empty_output"), "failure_pct": _pct(a.get("failure_rate"))})
        row["steps"] = steps
        # the facts a small model tends to misread from the table, worked out here
        timed = [x for x in steps if x.get("time_share_pct") is not None]
        if timed:
            top = max(timed, key=lambda x: x["time_share_pct"])
            row["slowest_step"] = {"step": top["step"], "share_of_flow_pct": top["time_share_pct"],
                                   "mean_time_s": top.get("mean_time_s")}
        wordy = [x for x in steps if x.get("answer_tokens") is not None]
        if wordy:
            top = max(wordy, key=lambda x: x["answer_tokens"])
            row["longest_answer_step"] = {"step": top["step"], "answer_tokens": top["answer_tokens"]}
        capped = [x for x in steps if (x.get("hit_budget") or 0) > 0]
        if capped:
            top = max(capped, key=lambda x: x["hit_budget"])
            row["most_cut_off_step"] = {"step": top["step"], "answers_cut_off": top["hit_budget"],
                                        "token_budget": top.get("token_budget")}
        per_model.append({k: v for k, v in row.items() if v is not None})

    rc = res.get("relative_comparison") or {}
    gh = res.get("gpu_health") or {}
    digest: dict[str, Any] = {
        "session": {"models": [names[m] for m in models], "n_models": len(models),
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


# ---------------------------------------------------------------------------- checks
_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![\w])")
_STYLE = [(re.compile(r";"), "semicolon"), (re.compile(r"(?<!\d):|:(?!\d)"), "colon"),
          (re.compile(r"[—–]"), "dash"), (re.compile(r"\s-\s|^-\s|\s-$"), "dash")]


def _digest_numbers(o: Any, out: set) -> set:
    if isinstance(o, bool):
        return out
    if isinstance(o, (int, float)):
        out.add(float(o))
    elif isinstance(o, str):
        for m in _NUM_RE.finditer(o):
            out.add(float(m.group(0).replace(",", "")))
    elif isinstance(o, dict):
        for v in o.values():
            _digest_numbers(v, out)
    elif isinstance(o, list):
        for v in o:
            _digest_numbers(v, out)
    return out


def _matches(t: float, dec: int, allowed: set) -> bool:
    step = 10 ** -dec
    for v in allowed:
        for c in (v, v * 100, v * 1000, v / 1024, v / 1000):      # fraction→%, s→ms, MB→GB, per 1,000
            if abs(round(c, dec) - t) <= step / 2 + 1e-9 or (dec < 2 and abs(c - t) <= step):
                return True
    return False


def check_section(key: str, text: str, digest: dict, allowed: Optional[set] = None) -> Optional[str]:
    """None when the section passes, else the reason it fails."""
    if not isinstance(text, str):
        return "not text"
    t = text.strip()
    if not t:
        return None                                       # empty: the computed text shows
    for rx, what in _STYLE:
        if rx.search(t):
            return f"uses a {what}"
    if "/" in t and re.search(r"\b[\w.-]+/[\w.-]+\b", t):
        return "uses a full model id"
    limit = WORD_LIMIT.get(key, 120)
    n_words = len(t.split())
    if n_words > limit * 1.5:
        return f"too long ({n_words} words, limit {limit})"
    least = MIN_WORDS.get(key, 0)
    if n_words < least:
        return f"too short ({n_words} words, a full sentence needs at least {least})"
    if re.search(r"than (expected|anticipated|usual)", t, re.I):
        return "compares with an expectation that is not in the results"
    allowed = allowed if allowed is not None else _digest_numbers(digest, set())
    names = [n for n in (digest.get("session") or {}).get("models") or []]
    stripped = t
    for n in sorted(names, key=len, reverse=True):
        stripped = stripped.replace(n, " ")
    stripped = re.sub(r"\b\d+-(bit|step|agent|class|flow)s?\b", " ", stripped, flags=re.I)  # 4-bit, four-step
    for m in _NUM_RE.finditer(stripped):
        whole, frac = m.group(1).replace(",", ""), m.group(2) or ""
        val, dec = float(whole + ("." + frac if frac else "")), len(frac)
        if dec == 0 and val <= 12:                       # small counts: "3 runs", "top 2"
            continue
        if not _matches(val, dec, allowed):
            return f"number {m.group(0)} is not in the results"
    return None


# ---------------------------------------------------------------------------- running the model
def _json_reply(text: str) -> dict:
    """The JSON object in a model's reply (gpt-oss: the final channel; others: the whole text)."""
    if "<|channel|>final<|message|>" in text:
        text = text.split("<|channel|>final<|message|>")[-1]
    text = re.sub(r"<\|[^|]*\|>", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[a: b + 1])


def _messages(digest: dict, only: Optional[dict[str, str]] = None) -> list[dict]:
    system = PROMPT_PATH.read_text(encoding="utf-8")
    user = "Digest:\n" + json.dumps(digest, ensure_ascii=False, separators=(",", ":"))     # compact: fewer tokens
    if only:
        user += ("\n\nYour earlier reply broke a rule in these sections. Rewrite only these keys and "
                 "return a JSON object with just them.\n" + json.dumps(only, ensure_ascii=False, indent=1))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _fake_reply(digest: dict, only: Optional[dict] = None) -> dict:
    """Test-only stand-in (AGENTMETER_TEST_EXPLAINER=1): sentences built from the digest, with one
    section that breaks the style rule so the fallback can be tested."""
    pm = digest.get("per_model") or [{}]
    a = pm[0]
    name = a.get("name", "The model")
    lat = a.get("mean_latency_s")
    out = {
        "summary": f"This session compared {len(pm)} model{'s' if len(pm) != 1 else ''} on {digest.get('session', {}).get('flows', 0)} flows. "
                   f"{name} took {lat} s per flow on average, and every model gave usable answers on these flows. "
                   "Run it again on more flows before you rely on a small gap between the models.",
        "tab.ranking.h": f"{name} leads this session on the efficiency score.",
        "tab.ranking.s": "The order holds for these flows and weights.",
        "tab.performance.h": f"{name} answers a flow in {lat} s on average.",
        "tab.performance.s": "Speed here comes mostly from the longest step in the pipeline.",
        "tab.cost.h": "Cost was not measured; this is a demo run.",       # breaks the style rule, fixed on retry
        "tab.agents.h": "Slowest step: see the table below.",             # breaks it, still broken after the retry
        "deployment": f"Start from {name}, which leads this session. Check its speed against your alert volume before you deploy it, "
                      "and test it on your own traffic first.",
        "optimisation": "Shorten the answers of the slowest step first, because its answer length sets most of the time. "
                        "Then trim the prompt of the step that reads the most.",
    }
    if only:
        return {k: v.replace(";", ",") for k, v in out.items() if k in only}
    return out


def _hf_generate(spec: dict, t0: float) -> tuple[str, int, int, float]:
    """transformers path. Works on 4.56+ and 5.x: apply_chat_template returns a mapping
    (input_ids, attention_mask) on 5.x and when asked to with return_dict=True on 4.x."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    mid, msgs = spec["model"], spec["messages"]
    tok = AutoTokenizer.from_pretrained(mid)
    model = AutoModelForCausalLM.from_pretrained(mid, dtype="auto", device_map="cuda")
    t_load = time.time() - t0
    kw = {"reasoning_effort": spec["reasoning"]} if spec.get("reasoning") else {}
    enc = tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=True, return_dict=True,
                                  return_tensors="pt", **kw)
    if hasattr(enc, "keys"):
        inputs = {k: v.to(model.device) for k, v in enc.items() if hasattr(v, "to")}
    else:                                            # an older tokenizer returning a bare tensor
        inputs = {"input_ids": enc.to(model.device)}
    n_in = int(inputs["input_ids"].shape[-1])
    with torch.no_grad():
        gen = model.generate(**inputs, max_new_tokens=spec["max_new_tokens"], do_sample=True, temperature=0.3)
    new_tokens = gen[0][n_in:]
    text = tok.decode(new_tokens, skip_special_tokens=False)
    return text, n_in, int(new_tokens.shape[-1]), round(t_load, 1)


def _worker_main(argv: list[str]) -> int:
    """Child process: load the model, answer one request, write the reply, exit (frees the GPU).
    Any error is written to the output file with its full traceback, for explanation.json."""
    import traceback
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    out = Path(argv[1])
    t0 = time.time()
    try:
        load_s = None
        if spec["engine"] == "vllm":
            from vllm import LLM, SamplingParams
            llm = LLM(model=spec["model"], gpu_memory_utilization=0.85, max_model_len=16384)
            load_s = round(time.time() - t0, 1)
            kw = {"chat_template_kwargs": {"reasoning_effort": spec["reasoning"]}} if spec.get("reasoning") else {}
            res = llm.chat(spec["messages"], SamplingParams(max_tokens=spec["max_new_tokens"], temperature=0.3), **kw)
            text, n_in, n_out = res[0].outputs[0].text, len(res[0].prompt_token_ids or []), len(res[0].outputs[0].token_ids)
        else:
            text, n_in, n_out, load_s = _hf_generate(spec, t0)
        out.write_text(json.dumps({"text": text, "tokens": {"input": n_in, "output": n_out},
                                   "load_s": load_s, "seconds": round(time.time() - t0, 1)}), encoding="utf-8")
        return 0
    except BaseException as e:  # noqa: BLE001 — report it to the parent, then fail
        out.write_text(json.dumps({"error": f"{type(e).__name__}: {e}"[:400],
                                   "trace": traceback.format_exc()[-4000:]}), encoding="utf-8")
        return 1


class ExplainerError(RuntimeError):
    """The explainer process failed; .trace holds its traceback for explanation.json."""
    trace = ""


def _run_model(mspec: dict, messages: list[dict], cfg: dict, runner: Optional[Callable] = None) -> dict:
    """Run one request in a child process with a time limit. Returns {text, tokens, seconds}."""
    if runner is not None:
        return runner(mspec, messages, cfg)
    with tempfile.TemporaryDirectory(prefix="am-explain-") as td:
        spec_p, out_p = Path(td) / "in.json", Path(td) / "out.json"
        spec_p.write_text(json.dumps({"engine": mspec.get("engine", "hf"), "model": mspec["id"],
                                      "reasoning": mspec.get("reasoning"), "messages": messages,
                                      "max_new_tokens": int(cfg["max_new_tokens"])}), encoding="utf-8")
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
            # the child's own report (full traceback) when it wrote one, else its stderr
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


def _pick_model(cfg: dict, free_gb: Optional[float]) -> Optional[dict]:
    fit = _fitting_models(cfg, free_gb)
    return fit[0] if fit else None


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def explain_session(results: dict, run_dir: str | Path, *, real: bool, cfg: Optional[dict] = None,
                    runner: Optional[Callable] = None, free_vram_gb: Optional[float] = None) -> dict:
    """Write <run_dir>/explanation.json and return it. Never raises."""
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
            digest = build_digest(results, run_dir, cfg)
            prompt = PROMPT_PATH.read_text(encoding="utf-8")
            out.update(prompt_sha256=_sha(prompt), digest_sha256=_sha(json.dumps(digest, sort_keys=True)))
            if fake:
                cands = [{"id": "test-explainer", "engine": "fake"}]
                runner = lambda ms, msgs, c: {"text": json.dumps(_fake_reply(digest, None)), "tokens": {"input": 0, "output": 0}, "seconds": 0.1}  # noqa: E731
            else:
                cands = _fitting_models(cfg, free_vram_gb if free_vram_gb is not None else _free_vram_gb())
            if not cands:
                out["reason"] = "the GPU does not have enough free memory for any configured explainer model"
            else:
                out.update(_try_models(digest, cands, cfg, runner, fake))
        write(run_dir, out)
    except Exception as e:  # noqa: BLE001 — never fail the session
        log.exception("explanation failed")
        out.update(status="failed", reason=f"{type(e).__name__}: {e}"[:300])
        if getattr(e, "trace", ""):
            out["trace"] = e.trace                       # the full error, for whoever fixes it
        try:
            write(run_dir, out)
        except OSError:
            pass
    return out


def _try_models(digest: dict, cands: list[dict], cfg: dict, runner: Optional[Callable], fake: bool) -> dict:
    """Each fitting model in turn until one writes something: a model that cannot run (engine
    missing, out of memory, a load error) or whose every section fails gives way to the next.
    Every failed attempt is kept in "attempts" with its reason and traceback."""
    attempts: list[dict] = []
    for mspec in cands:
        problem = None if (runner is not None or fake) else _engine_problem(mspec.get("engine", "hf"))
        if problem:
            attempts.append({"model": mspec["id"], "reason": problem, "skipped": True})
            continue
        try:
            res = _generate(digest, mspec, cfg, runner, fake)
        except Exception as e:  # noqa: BLE001 — try the next model
            log.warning("explainer %s failed: %s", mspec["id"], e)
            a = {"model": mspec["id"], "reason": f"{type(e).__name__}: {e}"[:300]}
            if getattr(e, "trace", ""):
                a["trace"] = e.trace
            attempts.append(a)
            continue
        if res["status"] != "failed":
            if attempts:
                res["attempts"] = attempts
            return res
        attempts.append({"model": mspec["id"], "reason": res.get("reason") or "no section passed the checks"})
    last = attempts[-1] if attempts else {}
    out = {"status": "skipped" if attempts and all(a.get("skipped") for a in attempts) else "failed",
           "reason": "; ".join(f"{a['model']}: {a['reason']}" for a in attempts)[:600] or "no explainer model could run",
           "attempts": attempts}
    if last.get("trace"):
        out["trace"] = last["trace"]
    return out


def _generate(digest: dict, mspec: dict, cfg: dict, runner: Optional[Callable], fake: bool) -> dict:
    t0 = time.time()
    allowed = _digest_numbers(digest, set())
    reply = _run_model(mspec, _messages(digest), cfg, runner)
    tokens = dict(reply.get("tokens") or {})
    try:
        got = _json_reply(reply.get("text") or "")
    except ValueError as e:
        got = {}
        first_err = str(e)
    else:
        first_err = None
    sections, dropped = {}, {}
    for k in SECTIONS:
        v = got.get(k)
        if v is None or not str(v).strip():
            continue
        why = check_section(k, str(v), digest, allowed)
        if why:
            dropped[k] = why
        else:
            sections[k] = str(v).strip()
    for _ in range(int(cfg.get("retries") or 0)):
        if not dropped:
            break
        ask = {k: dropped[k] for k in dropped}
        if fake:
            again = _fake_reply(digest, ask)
        else:
            r2 = _run_model(mspec, _messages(digest, ask), cfg, runner)
            for k2, v2 in (r2.get("tokens") or {}).items():
                tokens[k2] = (tokens.get(k2) or 0) + (v2 or 0)
            try:
                again = _json_reply(r2.get("text") or "")
            except ValueError:
                again = {}
        for k in list(dropped):
            v = again.get(k)
            if v and not check_section(k, str(v), digest, allowed):
                sections[k] = str(v).strip()
                del dropped[k]
    status = "ok" if sections and not dropped else "partial" if sections else "failed"
    res = {"status": status, "model": mspec["id"], "engine": mspec.get("engine", "hf"),
           "seconds": round(time.time() - t0, 1), "load_s": reply.get("load_s"), "tokens": tokens,
           "sections": sections, "dropped": dropped}
    if status == "failed":
        res["reason"] = first_err or "no section passed the checks"
    return res


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
