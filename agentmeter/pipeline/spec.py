"""User-defined pipelines: which agents run, in which order, and what each one reads.

A benchmark session may replace the default Perceive -> Reason -> Decide -> Act chain
with its own list of agents. Each entry is either one of the four built-in agents (run
exactly as in agents.py, so a default pipeline measures the same thing as before) or a
custom agent the user wrote:

    {"name": "verify", "title": "Verify", "kind": "text",
     "instructions": "Check the reasoning for mistakes ...",
     "reads": ["reason"], "features": true, "max_new_tokens": 160}

kind   text     free text, read by later agents
       label    picks exactly one class from the allowed list (the pipeline's answer)
       verdict  the final verdict (class + MITRE technique + one line), after the label
reads  earlier agents whose output this agent's prompt includes (the hand-off)

Rules (the harness stays a LINEAR chain, one model call per agent):
  - 1 to MAX_AGENTS agents, unique names;
  - exactly one label agent (built-in decide, or a custom label agent);
  - at most one verdict agent, and it comes after the label agent;
  - an agent reads only agents that run before it.

The harness only adds the output-format lines a label or verdict needs to be parsed;
what the agents say is up to the user's instructions.
"""
from __future__ import annotations

import re
from typing import Any, Optional

MAX_AGENTS = 8
KINDS = ("text", "label", "verdict")
BUILTIN_KIND = {"perceive": "text", "reason": "text", "decide": "label", "act": "verdict"}
DEFAULT_AGENTS = ["perceive", "reason", "decide", "act"]
# PipelineState keys an agent name may not take
RESERVED = {"scenario_id", "feature_prompt", "predicted_class", "verdict", "outputs"}
DEFAULT_BUDGET = {"text": 192, "label": 16, "verdict": 160}
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,23}$")
MIN_INSTR, MAX_INSTR, MAX_TITLE = 10, 1500, 40


class SpecError(ValueError):
    """The pipeline the user asked for is not valid."""


def _custom(raw: dict, name: str, earlier: list[str]) -> dict[str, Any]:
    kind = raw.get("kind", "text")
    if kind not in KINDS:
        raise SpecError(f"agent {name!r}: kind must be one of {', '.join(KINDS)}")
    title = str(raw.get("title") or name.replace("_", " ").capitalize()).strip()
    if not title or len(title) > MAX_TITLE:
        raise SpecError(f"agent {name!r}: the title must be 1 to {MAX_TITLE} characters")
    instr = raw.get("instructions")
    if not isinstance(instr, str) or not (MIN_INSTR <= len(instr.strip()) <= MAX_INSTR):
        raise SpecError(f"agent {name!r}: write its instructions ({MIN_INSTR} to {MAX_INSTR} characters)")
    reads = raw.get("reads", [])
    if not isinstance(reads, list) or not all(isinstance(r, str) for r in reads):
        raise SpecError(f"agent {name!r}: 'reads' must be a list of agent names")
    reads = list(dict.fromkeys(reads))
    later = [r for r in reads if r not in earlier]
    if later:
        raise SpecError(f"agent {name!r} can only read agents that run before it (not {', '.join(later)})")
    features = raw.get("features", True)
    if not isinstance(features, bool):
        raise SpecError(f"agent {name!r}: 'features' must be true or false")
    if not features and not reads:
        raise SpecError(f"agent {name!r} has no input: let it read the flow features or an earlier agent")
    budget = raw.get("max_new_tokens", DEFAULT_BUDGET[kind])
    if isinstance(budget, bool) or not isinstance(budget, int) or not (8 <= budget <= 1024):
        raise SpecError(f"agent {name!r}: max_new_tokens must be a whole number from 8 to 1024")
    return {"name": name, "title": title, "kind": kind, "instructions": instr.strip(),
            "reads": reads, "features": features, "max_new_tokens": budget}


def normalize(spec: Any) -> Optional[list[dict[str, Any]]]:
    """Validate a requested pipeline. None (or the default four, unchanged) -> None: the
    session runs the base config's pipeline exactly as before. Raises SpecError."""
    if spec is None:
        return None
    if not isinstance(spec, list) or not spec:
        raise SpecError("'pipeline' must be a list of 1 to %d agents" % MAX_AGENTS)
    if len(spec) > MAX_AGENTS:
        raise SpecError(f"at most {MAX_AGENTS} agents in a pipeline (got {len(spec)})")
    out: list[dict[str, Any]] = []
    for raw in spec:
        if isinstance(raw, str):
            raw = {"name": raw}
        if not isinstance(raw, dict):
            raise SpecError("each agent must be a built-in name or an object")
        name = raw.get("name")
        if not isinstance(name, str) or not _NAME.match(name) or name in RESERVED:
            raise SpecError(f"agent name {name!r}: use 2 to 24 lowercase letters, digits or _ "
                            "(starting with a letter)")
        if name in (a["name"] for a in out):
            raise SpecError(f"agent {name!r} appears twice")
        earlier = [a["name"] for a in out]
        if name in BUILTIN_KIND:
            if any(k in raw for k in ("instructions", "reads", "features")) or raw.get("kind", BUILTIN_KIND[name]) != BUILTIN_KIND[name]:
                raise SpecError(f"{name!r} is a built-in agent; give your own agent another name")
            out.append({"name": name, "builtin": True, "kind": BUILTIN_KIND[name]})
        else:
            out.append(_custom(raw, name, earlier))
    kinds = [a["kind"] for a in out]
    if kinds.count("label") != 1:
        raise SpecError("a pipeline needs exactly one label agent (Decide, or your own label agent): "
                        f"it has {kinds.count('label')}")
    if kinds.count("verdict") > 1:
        raise SpecError("a pipeline can have at most one verdict agent")
    if "verdict" in kinds and kinds.index("verdict") < kinds.index("label"):
        raise SpecError("the verdict agent must come after the label agent")
    if [a["name"] for a in out] == DEFAULT_AGENTS:
        return None
    return out


def apply_to_config(spec: Optional[list[dict[str, Any]]], data: dict[str, Any]) -> None:
    """Write a normalized pipeline into a config dict (in place). None leaves it unchanged."""
    if not spec:
        return
    pl = data.setdefault("pipeline", {})
    pl["agents"] = [a["name"] for a in spec]
    pl["custom"] = {a["name"]: {k: v for k, v in a.items() if k != "name"} for a in spec if not a.get("builtin")}
    budgets = dict(pl.get("max_new_tokens") or {})
    for a in spec:
        if not a.get("builtin"):
            budgets[a["name"]] = a["max_new_tokens"]
    pl["max_new_tokens"] = {k: v for k, v in budgets.items() if k in pl["agents"]}


def describe(pipeline_cfg: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """The agents of a config's pipeline section, in order, each with kind, title and reads
    (built-ins from agents.HANDOFFS). Used by the analyses and the UI."""
    from .agents import HANDOFFS
    pipeline_cfg = pipeline_cfg or {}
    names = list(pipeline_cfg.get("agents") or DEFAULT_AGENTS)
    custom = pipeline_cfg.get("custom") or {}
    out = []
    for i, n in enumerate(names):
        c = custom.get(n)
        if c:
            out.append({"name": n, "title": c.get("title") or n, "kind": c.get("kind", "text"), "builtin": False,
                        "reads": list(c.get("reads") or []), "features": bool(c.get("features", True)),
                        "instructions": c.get("instructions")})
        else:
            out.append({"name": n, "title": n.capitalize(), "kind": BUILTIN_KIND.get(n, "text"), "builtin": True,
                        "reads": [r for r in HANDOFFS.get(n, []) if r in names[:i]], "features": True})
    return out


def label_agent(pipeline_cfg: Optional[dict[str, Any]]) -> Optional[str]:
    for a in describe(pipeline_cfg):
        if a["kind"] == "label":
            return a["name"]
    return None
