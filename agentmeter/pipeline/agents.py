"""The pipeline agents (the TEST SUBJECT — keep minimal and generic).

A strictly linear chain: the standard Perceive -> Reason -> Decide -> Act, or another size of the
ladder (LADDERS: 1, 2, 3, 4, 6, 8 or 10 agents). No loops, no governance, no correlation, no
retries. Each agent is one system+user prompt and one model call. The agent set is config-driven
(pipeline.agents) so the harness stays agent-count agnostic.

Each agent function has the signature:
    agent(state, provider, config) -> dict   # partial state update

Prompts here enforce OUTPUT FORMAT only (so the harness can parse verdicts
reliably) — they deliberately do NOT add detection intelligence. Making the
pipeline "smarter" at detection is out of scope (Tahap 1 guardrail).
"""
from __future__ import annotations

import re
from typing import Callable

from ..config import Config
from ..providers.base import ModelProvider
from .triage import priority_of, severity_of, severity_table, triage_table

# The wording of the prompts below. Changing any prompt changes what is measured (tokens, time),
# so a session records this version and sessions with different versions are not compared.
# "triage-1": the agents are described as a network traffic triage pipeline.
# "triage-2": every agent reads the flow's features and the outputs of all earlier agents; the
#             ladder of pipeline sizes (1 to 10 agents).
PROMPT_VERSION = "triage-2"

# System prompts define each agent's role. The mock keys off the role word;
# the real HF model is steered by them too. "Be concise" bounds the free-text
# agents so they finish via EOS instead of being truncated at the token cap.
SYSTEM_PROMPTS = {
    "perceive": (
        "You are the Perceive agent in a network traffic triage pipeline. "
        "Read the raw flow features and produce a SHORT factual summary of the "
        "flow (at most 3 sentences). Do not classify yet."
    ),
    "reason": (
        "You are the Reason agent in a network traffic triage pipeline. Given "
        "the flow summary, briefly reason (at most 4 sentences) about which "
        "threat behaviours it is consistent with. Do not give a final label yet."
    ),
    "decide": (
        "You are the Decide agent in a network traffic triage pipeline. Choose "
        "EXACTLY ONE label from the allowed "
        "list. Reply with ONLY that label, copied verbatim, and nothing else — "
        "no punctuation, no explanation, no quotes."
    ),
    "act": (
        "You are the Act agent in a network traffic triage pipeline. Produce the "
        "final triage verdict: the predicted class, its MITRE ATT&CK technique, "
        "and a one-line justification."
    ),
    # the other sizes of the pipeline (LADDERS): the same format-only rule, no detection hints
    "triage": (
        "You are the Triage agent in a network traffic triage pipeline. You are the only agent: read "
        "the flow features, think briefly, then give the final triage verdict in the format asked."
    ),
    "analyse": (
        "You are the Analyse agent in a network traffic triage pipeline. Summarise the flow in at most "
        "3 sentences, then reason briefly (at most 3 sentences) about which threat behaviours it is "
        "consistent with. Do not give a final label yet."
    ),
    "conclude": (
        "You are the Conclude agent in a network traffic triage pipeline. Give the final triage "
        "verdict in the format asked: the label, its MITRE ATT&CK technique and a one-line justification."
    ),
    "enrich": (
        "You are the Enrich agent in a network traffic triage pipeline. Restate in plain words what the "
        "protocol, port, packet and timing fields of this flow mean (at most 3 sentences). Do not classify."
    ),
    "hypothesise": (
        "You are the Hypothesise agent in a network traffic triage pipeline. List the two or three "
        "behaviours this flow could be, one short line each. Do not choose one yet."
    ),
    "compare": (
        "You are the Compare agent in a network traffic triage pipeline. Weigh the listed behaviours "
        "against each other in at most 4 sentences. Do not give a final label yet."
    ),
    "verify": (
        "You are the Verify agent in a network traffic triage pipeline. Check the reasoning so far "
        "against the feature values and point out any contradiction, in at most 3 sentences."
    ),
    "critique": (
        "You are the Critique agent in a network traffic triage pipeline. Name the weakest step of the "
        "reasoning so far, in at most 2 sentences."
    ),
    "justify": (
        "You are the Justify agent in a network traffic triage pipeline. Write one sentence that "
        "justifies the most likely label, citing feature values. Write nothing else."
    ),
}

# Conservative alias table for FORMAT normalization only: maps the short forms
# a model commonly emits back to the canonical label. This is parsing hygiene,
# not detection logic — it never invents a class the model didn't name.
_ALIASES = {
    "Brute Force": ["brute force", "bruteforce", "brute-force"],
    "Volumetric DDoS": ["volumetric ddos", "ddos", "volumetric", "distributed denial"],
    "Port Scanning": ["port scanning", "port scan", "portscan", "port-scan", "scanning"],
    "DoS Hulk": ["dos hulk", "doshulk", "hulk"],  # keep 'dos' out: ambiguous vs DDoS
    "Benign": ["benign", "normal", "no threat", "not malicious", "legitimate"],
}


def normalize_class(text: str, classes: list[str]) -> str:
    """Map free-form model text to exactly one configured class.

    Format-only, deterministic, in priority order:
      1) exact class name appearing earliest in the text (case-insensitive)
      2) a conservative alias appearing earliest in the text
      3) 'Unparseable' if nothing matches (never silently coerced to a class)
    """
    low = text.lower().strip()

    # 1) exact class-name match, earliest position wins
    best: tuple[int, str] | None = None
    for cls in classes:
        idx = low.find(cls.lower())
        if idx != -1 and (best is None or idx < best[0]):
            best = (idx, cls)
    if best:
        return best[1]

    # 2) alias match on word boundaries (avoids e.g. 'syn' inside 'asynchronous'),
    #    earliest position wins, only for configured classes
    alias_best: tuple[int, str] | None = None
    for cls in classes:
        for alias in _ALIASES.get(cls, []):
            m = re.search(r"\b" + re.escape(alias) + r"\b", low)
            if m and (alias_best is None or m.start() < alias_best[0]):
                alias_best = (m.start(), cls)
    if alias_best:
        return alias_best[1]

    return "Unparseable"


# --- the agents -----------------------------------------------------------
# Every agent is one model call: its system prompt (role and output format only) and a user prompt
# made of the flow's features and the outputs of ALL earlier agents in the chain (the full history,
# as most agent frameworks pass context). So each extra agent re-reads more: the re-sent context
# grows with the number of agents, which is part of what is measured.
#
# kind: "text" (free text), "label" (one allowed label), "verdict" (class, MITRE technique and a
# justification, built on the label an earlier agent chose) or "both" (label and verdict in one
# reply, for the short pipelines).
ROLE_KIND = {
    "perceive": "text", "reason": "text", "decide": "label", "act": "verdict",
    "triage": "both", "analyse": "text", "conclude": "both", "enrich": "text", "hypothesise": "text",
    "compare": "text", "verify": "text", "critique": "text", "justify": "text",
}
# the output-token budget of a role the config does not list (format/hygiene, not detection logic)
DEFAULT_BUDGET = {"text": 200, "label": 12, "verdict": 160, "both": 220}

# The pipelines of each size (Experiment: "number of agents"). Every rung ends with the same final
# output: a label and a verdict. 4 is the standard pipeline.
LADDERS: dict[int, list[str]] = {
    1: ["triage"],
    2: ["analyse", "conclude"],
    3: ["perceive", "reason", "conclude"],
    4: ["perceive", "reason", "decide", "act"],
    6: ["perceive", "enrich", "reason", "verify", "decide", "act"],
    8: ["perceive", "enrich", "hypothesise", "reason", "verify", "critique", "decide", "act"],
    10: ["perceive", "enrich", "hypothesise", "reason", "compare", "verify", "critique", "decide", "justify", "act"],
}
AGENT_COUNTS = tuple(sorted(LADDERS))
STANDARD_AGENTS = 4


def ladder(n: int) -> list[str]:
    """The agents of the n-agent pipeline (ValueError for a size that has no pipeline)."""
    if int(n) not in LADDERS:
        raise ValueError(f"no {n}-agent pipeline; the sizes are {', '.join(map(str, AGENT_COUNTS))}")
    return list(LADDERS[int(n)])


def _max_tokens(config: Config, agent_name: str) -> int | None:
    """Per-agent max_new_tokens from config; a role it does not list gets its kind's default."""
    budgets = config.get("pipeline.max_new_tokens", {}) or {}
    val = budgets.get(agent_name)
    if val:
        return int(val)
    kind = ROLE_KIND.get(agent_name)
    return DEFAULT_BUDGET[kind] if kind and budgets else None


def _history(state: dict, order: list[str], name: str) -> str:
    """The outputs of every agent before `name`, in order, each under its role name."""
    out = []
    for prev in order[: order.index(name)] if name in order else []:
        text = state.get(prev)
        if text:
            out.append(f"[{prev.capitalize()}]\n{text}")
    return "\n\n".join(out)


def _user_prompt(name: str, state: dict, order: list[str], config: Config) -> str:
    kind = ROLE_KIND.get(name, "text")
    hist = _history(state, order, name)
    classes = list(config.get("classes", []) or [])
    parts = []
    if kind in ("label", "both"):
        parts.append(f"Allowed labels (copy one verbatim): {' | '.join(classes)}")
    if kind == "verdict":
        parts.append(f"Chosen class: {state.get('predicted_class', 'Unparseable')}")
    if hist:
        parts.append(f"Earlier agents:\n{hist}")
    parts.append(f"Raw features:\n{state['feature_prompt']}" if hist or kind != "text" else state["feature_prompt"])
    if kind == "label":
        parts.append("Your entire answer must be exactly one label from the list above.")
    elif kind == "verdict":
        parts.append("Give: predicted class, its MITRE ATT&CK technique, one-line justification.")
    elif kind == "both":
        parts.append("Give: on the first line 'Label: ' and one label from the list above, copied verbatim; then "
                     "its MITRE ATT&CK technique; then a one-line justification.")
    return "\n\n".join(parts)


def _label_of(text: str, classes: list[str]) -> str:
    """The label of a 'both' reply: its 'Label:' line first, else the earliest class it names."""
    m = re.search(r"^\s*label\s*:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    if m:
        got = normalize_class(m.group(1), classes)
        if got != "Unparseable":
            return got
    return normalize_class(text, classes)


def make_agent(name: str) -> Callable[[dict, ModelProvider, Config], dict]:
    """The agent function of one role, for a pipeline whose agent order is config pipeline.agents."""
    kind = ROLE_KIND[name]

    def agent(state: dict, provider: ModelProvider, config: Config) -> dict:
        order = list(config.get("pipeline.agents", []) or [])
        classes = list(config.get("classes", []) or [])
        system = SYSTEM_PROMPTS[name]
        user = _user_prompt(name, state, order, config)
        res = provider.generate(user, system=system, max_new_tokens=_max_tokens(config, name))
        update: dict = {name: res.text, "_last_result": res,
                        "_parts": {"system": system, "features": state["feature_prompt"],
                                   "handoff": _history(state, order, name)}}
        if kind == "label":
            update["predicted_class"] = normalize_class(res.text, classes)
        elif kind == "verdict":
            predicted = state.get("predicted_class", "Unparseable")
            final = predicted if predicted in classes else normalize_class(res.text, classes)
            update["verdict"] = make_verdict(final, res.text, config)
        elif kind == "both":
            label = _label_of(res.text, classes)
            update["predicted_class"] = label
            update["verdict"] = make_verdict(label, res.text, config)
        return update

    agent.__name__ = f"{name}_agent"
    return agent


# Which earlier agents' OUTPUTS each agent's prompt re-sends (the inter-agent hand-off): all of them.
# Documentation for the Agents analysis (session/analyses.py: "agentic overhead").
def handoffs(order: list[str]) -> dict[str, list[str]]:
    return {a: list(order[:i]) for i, a in enumerate(order)}


HANDOFFS: dict[str, list[str]] = handoffs(LADDERS[STANDARD_AGENTS])
# the hand-off of the first prompt version ("triage-1"), for sessions recorded with it
HANDOFFS_V1: dict[str, list[str]] = {"perceive": [], "reason": ["perceive"], "decide": ["reason", "perceive"],
                                     "act": ["reason"]}


def make_verdict(predicted: str, text: str, config: Config) -> dict:
    """The verdict a pipeline ends with. Without a verdict agent it is the label alone."""
    classes = list(config.get("classes", []) or [])
    mitre = dict(config.get("mitre", {}) or {})
    final_class = predicted if predicted in classes else (normalize_class(text, classes) if text else "Unparseable")
    return {
        "predicted_class": final_class,
        "mitre_technique": mitre.get(final_class, "N/A"),
        # the SOC's policy table, not the model, sets the priority (pipeline/triage.py)
        "priority": priority_of(final_class, triage_table(getattr(config, "data", config))),
        "severity": severity_of(final_class, severity_table(getattr(config, "data", config))),
        "justification": text.strip().splitlines()[-1] if text.strip() else "",
        "raw": text,
    }


# Registry keeps the harness agent-count agnostic: config lists names, the
# graph builder wires whichever of these are named, in order.
AGENT_REGISTRY: dict[str, Callable[[dict, ModelProvider, Config], dict]] = {n: make_agent(n) for n in ROLE_KIND}
