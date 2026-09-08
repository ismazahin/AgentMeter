"""The four pipeline agents (the TEST SUBJECT — keep minimal and generic).

Perceive -> Reason -> Decide -> Act, a strictly linear chain. No loops, no
governance, no correlation, no retries. Each agent is one system+user prompt
and one model call. The agent set is config-driven (pipeline.agents) so the
harness stays agent-count agnostic.

Each agent function has the signature:
    agent(state, provider, config) -> dict   # partial state update

so the instrumentation layer (Phase 3) can wrap it uniformly.
"""
from __future__ import annotations

from typing import Any, Callable

from .config import Config
from .providers.base import ModelProvider

# System prompts define each agent's role. The mock keys off the role word;
# the real HF model is steered by them too.
SYSTEM_PROMPTS = {
    "perceive": (
        "You are the Perceive agent in a network threat-detection pipeline. "
        "Read the raw flow features and produce a short, factual structured "
        "summary of the flow. Do not classify yet."
    ),
    "reason": (
        "You are the Reason agent. Given the flow summary, reason about which "
        "threat behaviours it is consistent with. Do not give a final label yet."
    ),
    "decide": (
        "You are the Decide agent. Classify the flow into EXACTLY ONE of the "
        "allowed classes. Answer with the class name only."
    ),
    "act": (
        "You are the Act agent. Produce the final verdict: the predicted class, "
        "its MITRE ATT&CK technique, and a one-line justification."
    ),
}


def normalize_class(text: str, classes: list[str]) -> str:
    """Map free-form model text to exactly one configured class.

    Picks the class whose name appears earliest in the text (case-insensitive).
    Returns 'Unparseable' if none is found — counted as incorrect downstream,
    never silently coerced to a real class.
    """
    low = text.lower()
    best: tuple[int, str] | None = None
    for cls in classes:
        idx = low.find(cls.lower())
        if idx != -1 and (best is None or idx < best[0]):
            best = (idx, cls)
    return best[1] if best else "Unparseable"


# --- individual agents --------------------------------------------------

def perceive_agent(state: dict, provider: ModelProvider, config: Config) -> dict:
    res = provider.generate(state["feature_prompt"], system=SYSTEM_PROMPTS["perceive"])
    return {"perceive": res.text, "_last_result": res}


def reason_agent(state: dict, provider: ModelProvider, config: Config) -> dict:
    user = (
        f"Flow summary:\n{state.get('perceive', '')}\n\n"
        f"Raw features:\n{state['feature_prompt']}"
    )
    res = provider.generate(user, system=SYSTEM_PROMPTS["reason"])
    return {"reason": res.text, "_last_result": res}


def decide_agent(state: dict, provider: ModelProvider, config: Config) -> dict:
    classes = list(config.get("classes", []) or [])
    user = (
        f"Allowed classes: {', '.join(classes)}\n\n"
        f"Reasoning:\n{state.get('reason', '')}\n\n"
        f"Summary:\n{state.get('perceive', '')}\n\n"
        f"Raw features:\n{state['feature_prompt']}\n\n"
        "Respond with exactly one class name."
    )
    res = provider.generate(user, system=SYSTEM_PROMPTS["decide"])
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
    res = provider.generate(user, system=SYSTEM_PROMPTS["act"])

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
