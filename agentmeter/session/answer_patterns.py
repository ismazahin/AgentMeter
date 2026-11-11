"""What the answers show: patterns in the agents' stored answers, strict (design:
docs/superpowers/specs/2026-11-11-checked-explanations-design.md, part 2).

Code groups the answers that went wrong and counts them; the explainer model names one pattern per
group with a key phrase and a quote; code keeps the pattern only when the answers support it:

  build_groups(results, flows, names, cfg)  groups of at least `min_group` answers, the largest
                                            `max_groups` first, with contrast answers of the same
                                            step (IP addresses and ports removed)
  check_pattern(reply, group)               (kept pattern, None) or (None, reason): the quote is
                                            word for word from one of the group's answers, the
                                            key phrase is in at least half of them and at most
                                            half as common in the contrast answers, and the
                                            pattern sentence has no number and passes the style rules
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

_ID_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\bport\s+\d+\b", re.I)
_STYLE = [(re.compile(r";"), "semicolon"), (re.compile(r"(?<!\d):|:(?!\d)"), "colon"),
          (re.compile(r"[—–]"), "dash"), (re.compile(r"\s-\s"), "dash")]


def scrub(text: Any) -> str:
    """An answer as the model may see it: IPv4 addresses and port numbers removed."""
    return re.sub(r"\s+", " ", _ID_RE.sub("[removed]", str(text or ""))).strip()


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[…\"'“”‘’]", " ", str(text or "").lower())).strip()


def _bare(m: Any) -> str:
    return re.sub(r"^mock:", "", str(m or ""))


def build_groups(res: dict, flows: dict, names: dict[str, str], cfg: dict) -> list[dict]:
    """The failure groups of this session, largest first. `names`: model id -> everyday name."""
    pc = cfg.get("answer_patterns") or {}
    min_group, max_groups = int(pc.get("min_group", 3)), int(pc.get("max_groups", 4))
    if not flows or not flows.get("has_text"):
        return []
    caps: dict[tuple, int] = {}
    for pm in ((res.get("agents") or {}).get("per_model") or []):
        for a in pm.get("agents") or []:
            cap = (a.get("failures") or {}).get("token_cap")
            if cap:
                caps[(_bare(pm.get("model")), str(a.get("agent") or ""))] = int(cap)
    label_agent = (res.get("output_reliability") or {}).get("label_agent") or "decide"
    order = list(flows.get("agent_order") or [])
    # the step whose reasoning the label agent read: the one right before it
    reason_step = order[order.index(label_agent) - 1] if label_agent in order and order.index(label_agent) > 0 else label_agent
    groups: dict[tuple, dict] = {}
    contrast: dict[tuple, list[str]] = {}

    def add(key, kind, model, step, text, **extra):
        g = groups.setdefault(key, {"kind": kind, "model": names.get(model, _bare(model).split("/")[-1]),
                                    "step": step.capitalize(), "texts": [], **extra})
        g["texts"].append(scrub(text))

    for f in flows.get("flows") or []:
        for m, pm in (f.get("per_model") or {}).items():
            if not isinstance(pm, dict):
                continue
            agents = {a.get("agent"): a for a in pm.get("agents") or [] if isinstance(a, dict)}
            for step, a in agents.items():
                txt = a.get("text")
                if txt is None:
                    continue
                cap = caps.get((_bare(m), str(step)))
                cut = cap is not None and (a.get("output_tokens") or 0) >= cap
                if not str(txt).strip():
                    add((m, step, "empty"), "empty", m, step, "")
                elif cut:
                    add((m, step, "cut_off"), "cut_off", m, step, txt, budget=cap)
                else:
                    contrast.setdefault((m, step), []).append(scrub(txt))
            if pm.get("correct") is False and pm.get("predicted") and f.get("truth"):
                r = agents.get(reason_step) or {}
                if r.get("text"):
                    add((m, reason_step, "mix", f["truth"], pm["predicted"]), "mix_up", m, reason_step, r["text"],
                        true=f["truth"], answered=pm["predicted"])
            elif pm.get("correct") is True:
                r = agents.get(reason_step) or {}
                if r.get("text"):
                    contrast.setdefault((m, reason_step, "right"), []).append(scrub(r["text"]))
    out = []
    multi = len({g["model"] for g in groups.values()}) > 1
    for key, g in groups.items():
        if g["kind"] == "empty" or len(g["texts"]) < min_group:
            continue                                   # empty answers have no text to read
        g["contrast"] = contrast.get((key[0], key[1], "right") if g["kind"] == "mix_up" else (key[0], key[1]), [])
        g["n"] = len(g["texts"])
        who = f" from {g['model']}" if multi else ""
        g["describe"] = (f"cut-off {g['step']} answers{who}" if g["kind"] == "cut_off" else
                         f"{g['true']} flows answered as {g['answered']}{who}, as {g['step']} described them")
        out.append(g)
    out.sort(key=lambda g: -g["n"])
    for i, g in enumerate(out[:max_groups], 1):
        g["id"] = f"g{i}"
    return out[:max_groups]


def check_pattern(reply: dict, group: dict) -> tuple[Optional[dict], Optional[str]]:
    """Keep a pattern only when the answers support it (see the module's docstring)."""
    if not isinstance(reply, dict):
        return None, "no pattern in the reply"
    pattern = str(reply.get("pattern") or "").strip()
    phrase = str(reply.get("key_phrase") or "").strip()
    quote = str(reply.get("quote") or "").strip().strip("\"'“”")
    if not pattern or not phrase or not quote:
        return None, "pattern, key phrase or quote missing"
    for rx, what in _STYLE:
        if rx.search(pattern):
            return None, f"the pattern uses a {what}"
    if re.search(r"\d", pattern):
        return None, "the pattern sentence contains a number"
    if len(pattern.split()) > 40:
        return None, "the pattern sentence is too long"
    if not 2 <= len(phrase.split()) <= 6:
        return None, "the key phrase is not 2 to 6 words"
    if len(quote.split()) < 4:
        return None, "the quote is shorter than 4 words"
    texts = [norm(t) for t in group.get("texts") or []]
    if not any(norm(quote) in t for t in texts):
        return None, "the quote is not word for word from one of the answers"
    p = norm(phrase)
    if group.get("kind") == "mix_up":                  # a pattern of the answers, not the label itself
        cls = {norm(group.get("true", "")), norm(group.get("answered", ""))}
        words = [w for w in p.split() if w not in {"classification", "class", "label", "flow", "flows", "the", "a", "is"}]
        if not words or " ".join(words) in cls or all(w in " ".join(cls) for w in words):
            return None, "the key phrase only repeats the class names"
    found = sum(1 for t in texts if p in t)
    if found * 2 < len(texts):
        return None, f"the key phrase is in only {found} of {len(texts)} answers"
    con = [norm(t) for t in group.get("contrast") or []]
    if con:
        rate_g, rate_c = found / len(texts), sum(1 for t in con if p in t) / len(con)
        if rate_c > rate_g / 2:
            return None, "the key phrase is as common in the other answers, so it does not explain this group"
    kept = {"group": group.get("id"), "kind": group.get("kind"), "model": group.get("model"), "step": group.get("step"),
            "pattern": pattern, "key_phrase": phrase, "quote": quote, "found_in": found, "of": len(texts),
            "label": f"Found in {found} of {len(texts)} {group.get('describe')}."}
    return kept, None


def most_common_phrase(group: dict) -> Optional[str]:
    """The test-only explainer's key phrase: the most common three-word run in the group's answers
    that is at most half as common in the contrast answers."""
    texts = [norm(t) for t in group.get("texts") or []]
    con = [norm(t) for t in group.get("contrast") or []]
    counts: dict[str, int] = {}
    for t in texts:
        ws = t.split()
        for g in {" ".join(ws[i:i + 3]) for i in range(len(ws) - 2)}:
            counts[g] = counts.get(g, 0) + 1
    for g, c in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        if c * 2 < len(texts):
            break
        if con and sum(1 for t in con if g in t) / len(con) > (c / len(texts)) / 2:
            continue
        if group.get("kind") == "mix_up" and any(norm(x) in g for x in (group.get("true", ""), group.get("answered", ""))):
            continue
        return g
    return None


def ceil_half(n: int) -> int:
    return int(math.ceil(n / 2))
