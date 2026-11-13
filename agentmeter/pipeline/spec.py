"""The agents of a stored pipeline configuration, for the analyses and the UI.

Every new run uses the built-in Perceive -> Reason -> Decide -> Act chain (or the subset a
base config lists). Sessions recorded before user-defined pipelines were removed may still
carry a `pipeline.custom` section in their stored config. describe() reads it, so such a
session still shows its agents, but nothing can start a run with one any more.
"""
from __future__ import annotations

from typing import Any, Optional

BUILTIN_KIND = {"perceive": "text", "reason": "text", "decide": "label", "act": "verdict"}
DEFAULT_AGENTS = ["perceive", "reason", "decide", "act"]


def describe(pipeline_cfg: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """The agents of a config's pipeline section, in order, each with kind, title and reads
    (built-ins from agents.HANDOFFS)."""
    from .agents import HANDOFFS
    pipeline_cfg = pipeline_cfg or {}
    names = list(pipeline_cfg.get("agents") or DEFAULT_AGENTS)
    custom = pipeline_cfg.get("custom") or {}          # only in sessions recorded before the removal
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
