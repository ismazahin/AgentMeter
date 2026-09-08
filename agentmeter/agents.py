"""The four pipeline agents (the TEST SUBJECT — keep minimal and generic).

Perceive -> Reason -> Decide -> Act, a strictly linear chain. No loops, no
governance, no correlation, no retries. Each agent is one system+user prompt
and one model call. The agent set is config-driven (pipeline.agents) so the
harness stays agent-count agnostic.

Each agent function has the signature:
    agent(state, provider, config) -> dict   # partial state update

Prompts here enforce OUTPUT FORMAT only (so the harness can parse verdicts
reliably) — they deliberately do NOT add detection intelligence. Making the
pipeline "smarter" at detection is out of scope (Tahap 1 guardrail).
"""
from __future__ import annotations

import re
from typing import Callable

from .config import Config
from .providers.base import ModelProvider

# System prompts define each agent's role. The mock keys off the role word;
# the real HF model is steered by them too. "Be concise" bounds the free-text
# agents so they finish via EOS instead of being truncated at the token cap.
SYSTEM_PROMPTS = {
    "perceive": (
        "You are the Perceive agent in a network threat-detection pipeline. "
        "Read the raw flow features and produce a SHORT factual summary of the "
        "flow (at most 3 sentences). Do not classify yet."
    ),
    "reason": (
        "You are the Reason agent. Given the flow summary, briefly reason (at "
        "most 4 sentences) about which threat behaviours it is consistent with. "
        "Do not give a final label yet."
    ),
    "decide": (
        "You are the Decide agent. Choose EXACTLY ONE label from the allowed "
        "list. Reply with ONLY that label, copied verbatim, and nothing else — "
        "no punctuation, no explanation, no quotes."
    ),
    "act": (
        "You are the Act agent. Produce the final verdict: the predicted class, "
        "its MITRE ATT&CK technique, and a one-line justification."
    ),
}

# Conservative alias table for FORMAT normalization only: maps the short forms
# a model commonly emits back to the canonical label. This is parsing hygiene,
# not detection logic — it never invents a class the model didn't name.
_ALIASES = {
    "Brute Force": ["brute force", "bruteforce", "brute-force"],
    "Volumetric DDoS": ["volumetric ddos", "ddos", "volumetric", "distributed denial"],
    "Port Scanning": ["port scanning", "port scan", "portscan", "port-scan", "scanning"],
    "SYN Flood": ["syn flood", "syn-flood", "synflood", "syn"],
    "Data Exfiltration": ["data exfiltration", "exfiltration", "exfil", "data exfil"],
    "Benign": ["benign", "normal", "no threat", "not malicious", "legitimate"],
}


def _max_tokens(config: Config, agent_name: str) -> int | None:
    """Per-agent max_new_tokens override from config (None = provider default)."""
    budgets = config.get("pipeline.max_new_tokens", {}) or {}
    val = budgets.get(agent_name)
    return int(val) if val else None


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


# --- individual agents --------------------------------------------------

def perceive_agent(state: dict, provider: ModelProvider, config: Config) -> dict:
    res = provider.generate(
        state["feature_prompt"],
        system=SYSTEM_PROMPTS["perceive"],
        max_new_tokens=_max_tokens(config, "perceive"),
    )
    return {"perceive": res.text, "_last_result": res}


def reason_agent(state: dict, provider: ModelProvider, config: Config) -> dict:
    user = (
        f"Flow summary:\n{state.get('perceive', '')}\n\n"
        f"Raw features:\n{state['feature_prompt']}"
    )
    res = provider.generate(
        user, system=SYSTEM_PROMPTS["reason"], max_new_tokens=_max_tokens(config, "reason")
    )
    return {"reason": res.text, "_last_result": res}


def decide_agent(state: dict, provider: ModelProvider, config: Config) -> dict:
    classes = list(config.get("classes", []) or [])
    user = (
        f"Allowed labels (copy one verbatim): {' | '.join(classes)}\n\n"
        f"Reasoning:\n{state.get('reason', '')}\n\n"
        f"Summary:\n{state.get('perceive', '')}\n\n"
        f"Raw features:\n{state['feature_prompt']}\n\n"
        "Your entire answer must be exactly one label from the list above."
    )
    res = provider.generate(
        user, system=SYSTEM_PROMPTS["decide"], max_new_tokens=_max_tokens(config, "decide")
    )
    predicted = normalize_class(res.text, classes)
    return {"decide": res.text, "predicted_class": predicted, "_last_result": res}


def act_agent(state: dict, provider: ModelProvider, config: Config) -> dict:
    classes = list(config.get("classes", []) or [])
    mitre = dict(config.get("mitre", {}) or {})
    predicted = state.get("predicted_class", "Unparseable")
    user = (
        f"Chosen class: {predicted}\n"
        f"Reasoning:\n{state.get('reason', '')}\n\n"
        "Give: predicted class, its MITRE ATT&CK technique, one-line justification."
    )
    res = provider.generate(
        user, system=SYSTEM_PROMPTS["act"], max_new_tokens=_max_tokens(config, "act")
    )

    # Prefer the class Decide already committed to; fall back to parsing Act's text.
    final_class = predicted if predicted in classes else normalize_class(res.text, classes)
    verdict = {
        "predicted_class": final_class,
        "mitre_technique": mitre.get(final_class, "N/A"),
        "justification": res.text.strip().splitlines()[-1] if res.text.strip() else "",
        "raw": res.text,
    }
    return {"act": res.text, "verdict": verdict, "_last_result": res}


# Registry keeps the harness agent-count agnostic: config lists names, the
# graph builder wires whichever of these are named, in order.
AGENT_REGISTRY: dict[str, Callable[[dict, ModelProvider, Config], dict]] = {
    "perceive": perceive_agent,
    "reason": reason_agent,
    "decide": decide_agent,
    "act": act_agent,
}
