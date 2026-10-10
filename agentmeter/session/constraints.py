"""Decision helper — stage-2 fit scoring against user limits (configs/constraint_rules.yaml).

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product.
Each measured model is checked against the limits the user enters (max mean / p95
latency, max peak VRAM, max cost per 1,000 flows): meets / fails (which limit) /
no_data. It reads a copy of the scored payload and never changes a score, rank, SAW
value or the verdict.
"""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from ..config import PROJECT_ROOT

DEFAULT_RULES = PROJECT_ROOT / "configs" / "constraint_rules.yaml"
OPS = {"le": lambda a, b: a <= b, "lt": lambda a, b: a < b,
       "ge": lambda a, b: a >= b, "gt": lambda a, b: a > b}
FIELDS = ("measured.mean_latency_s", "measured.p95_latency_s", "measured.peak_vram_mb",
          "measured.cost_per_1k_flows_usd")
_TPL = re.compile(r"\{(value|limit)(?::([^{}]+))?\}")


class ConstraintError(ValueError):
    """A malformed rule file or an invalid limit."""


def load_rules(path: Optional[str | Path] = None) -> dict[str, Any]:
    p = Path(path or DEFAULT_RULES)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConstraintError(f"cannot read {p}: {e}") from e
    inputs = data.get("inputs") or {}
    rules = data.get("rules")
    if not isinstance(inputs, dict) or not inputs:
        raise ConstraintError(f"{p.name}: 'inputs' must be a non-empty mapping")
    if not isinstance(rules, list) or not rules:
        raise ConstraintError(f"{p.name}: 'rules' must be a non-empty list")
    ids = set()
    for r in rules:
        rid = r.get("id")
        if not rid or rid in ids:
            raise ConstraintError(f"{p.name}: every rule needs a unique id ({rid!r})")
        ids.add(rid)
        if r.get("op") not in OPS:
            raise ConstraintError(f"{p.name}: rule {rid}: unknown op {r.get('op')!r} (use {sorted(OPS)})")
        if r.get("field") not in FIELDS:
            raise ConstraintError(f"{p.name}: rule {rid}: unknown field {r.get('field')!r}")
        if r.get("limit") not in inputs:
            raise ConstraintError(f"{p.name}: rule {rid}: limit {r.get('limit')!r} is not declared under inputs")
    try:
        src = str(p.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        src = str(p)
    return {"source": src, "version": data.get("version"), "inputs": inputs, "rules": rules}


def parse_limits(raw: Mapping[str, Any], rules: dict[str, Any]) -> dict[str, Optional[float]]:
    """User limits (query string / form) -> floats >= 0; blank = not set. Unknown keys ignored."""
    out: dict[str, Optional[float]] = {}
    for key in rules["inputs"]:
        v = raw.get(key)
        if v is None or str(v).strip() == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError) as e:
            raise ConstraintError(f"{key} must be a number (got {v!r})") from e
        if math.isnan(f) or math.isinf(f) or f < 0:
            raise ConstraintError(f"{key} must be a finite number >= 0 (got {v!r})")
        out[key] = f
    return out


def facts_for(payload: dict[str, Any]) -> list[dict[str, Any]]:
    mem = {m["model"]: m for m in ((payload.get("memory") or {}).get("per_model") or [])}
    cost = {m["model"]: m for m in ((payload.get("cost_energy") or {}).get("per_model") or [])}
    out = []
    for pm in payload.get("per_model") or []:
        m = pm["model"]
        e2e = (pm.get("efficiency") or {}).get("end_to_end") or {}
        out.append({"model": m,
                    "measured.mean_latency_s": e2e.get("mean_latency_s"),
                    "measured.p95_latency_s": e2e.get("p95_latency_s"),
                    # TOTAL peak (weights + working). A session without it -> no_data, never "meets".
                    "measured.peak_vram_mb": (mem.get(m) or {}).get("total_peak_mb"),
                    "measured.cost_per_1k_flows_usd": (cost.get(m) or {}).get("cost_per_1k_flows_usd")})
    return out


def _render(tpl: str, value: Any, limit: Any) -> str:
    def sub(mt):
        v = value if mt.group(1) == "value" else limit
        if v is None:
            return "unknown"
        try:
            return format(v, mt.group(2)) if mt.group(2) else str(v)
        except (TypeError, ValueError):
            return str(v)
    return _TPL.sub(sub, tpl)


def evaluate_constraints(payload: dict[str, Any], limits: Optional[Mapping[str, Any]] = None,
                         rules: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Evaluate every measured model against the limits (default: the rule file's defaults)."""
    rb = rules or load_rules()
    snap = copy.deepcopy(payload)
    if limits is None:
        limits = {k: v.get("default") for k, v in rb["inputs"].items() if v.get("default") is not None}
    lim = parse_limits(limits, rb)
    per = []
    for f in facts_for(snap):
        rows, failed, nodata = [], [], []
        for r in rb["rules"]:
            limit = lim.get(r["limit"])
            have = f.get(r["field"])
            if limit is None:
                res, note = "not_set", None
            elif have is None:
                res, note = "no_data", f"no measurement for {r['field']}"
                nodata.append(r["id"])
            else:
                ok = OPS[r["op"]](float(have), float(limit))
                res, note = ("meets" if ok else "fails"), _render(r.get("note", ""), have, limit)
                if not ok:
                    failed.append(r["id"])
            rows.append({"rule": r["id"], "description": r.get("description", ""), "field": r["field"],
                         "op": r["op"], "input": r["limit"], "limit": limit, "value": have,
                         "result": res, "note": note})
        set_rows = [x for x in rows if x["result"] != "not_set"]
        overall = ("no_constraints" if not set_rows else "fails" if failed
                   else "no_data" if nodata else "meets")
        per.append({"model": f["model"], "result": overall, "failed": failed, "no_data": nodata, "rows": rows})
    return {
        "stage": "2 (fit scoring)", "rulebase": rb["source"], "version": rb["version"],
        "inputs": {k: {**v, "value": lim.get(k)} for k, v in rb["inputs"].items()},
        "limits_set": bool(lim),
        "framing": "Fit check of the measured numbers against your limits. It never changes a score, "
                   "rank, SAW value or the verdict.",
        "per_model": per,
    }
