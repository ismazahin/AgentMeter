"""Rule-based flow selection — an explicit, inspectable rule engine.

Purpose: a large PCAP can yield tens of thousands of flows; the benchmark must keep
analysis time bounded. This engine picks a REPRESENTATIVE subset using rules over
purely STATISTICAL flow properties (counts, durations, volumes, protocol, port).
It makes NO threat judgement: "notable" means "statistically unusual in this
capture", never "malicious".

The rule-base is data, not code: rules live in a YAML file (configs/flow_rules.yaml)
and are instances of a small, fixed set of operator TYPES implemented below:

  per_group      coverage — up to `per_group` flows from each value of `group_by`
                 (e.g. each protocol, each of the busiest destination ports)
  top_percentile notable — flows whose `field` is at/above its p-th percentile in
                 this capture (and above the median, so a flat capture fires nothing)
  threshold      notable — flows where `field <op> value` (absolute, declared limit)
  typical_band   baseline — flows whose `fields` all lie inside the [lo, hi]
                 percentile band (typical flows, kept for balance)
  random_fill    seeded random fill of whatever budget is left

and one CONSTRAINT type, which admits nothing itself but bounds every rule above:

  class_balance  label-aware (labelled CSV path only): per-class caps by water-
                 filling — each class present gets an equal share of the budget;
                 a class with fewer rows keeps them all and its unused share is
                 re-split among the rest. Rules still decide WHICH rows; the cap
                 only stops a class from exceeding its share. Labels arrive as a
                 separate argument and never join the table the operators read,
                 nor the selected output. With no labels (PCAP, unlabelled CSV,
                 or a label-blind run) it is skipped and says so in the audit.

Adding or re-tuning a rule = editing the YAML. Only a brand-new operator TYPE
needs code (one function registered in RULE_TYPES).

Selection semantics (deterministic for a given seed):
  * Rules run in declared order; a flow is admitted at most once, by the first
    rule that admits it. Every rule that MATCHED a selected flow is still
    recorded, so the audit shows all reasons, not just the winning one.
  * `budget.max_flows` caps the total. `quota` caps one rule's admissions.
  * `reserve` guarantees a rule slots: while an earlier rule runs, enough budget
    is held back to satisfy every later rule's reserve (bounded by how many
    not-yet-selected candidates that later rule actually has). A rule can always
    use its own reserve, so earlier reserves win when budget is short.
  * Quotas and reserves are tuned for `budget.max_flows`; a run-time override of
    the budget scales them by the same factor (ceil, min 1) to keep the rule mix.
"""
from __future__ import annotations

import math
import operator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd
import yaml

from ..config import PROJECT_ROOT

DEFAULT_RULES_PATH = PROJECT_ROOT / "configs" / "flow_rules.yaml"

_OPS: dict[str, Callable[[Any, Any], Any]] = {
    ">": operator.gt, ">=": operator.ge, "<": operator.lt,
    "<=": operator.le, "==": operator.eq, "!=": operator.ne,
}


class RuleConfigError(ValueError):
    """The rule-base file is malformed (unknown type, bad parameter, unknown field)."""


# ---------------------------------------------------------------------------
# Rule-base model + loading/validation
# ---------------------------------------------------------------------------
@dataclass
class Rule:
    id: str
    type: str
    description: str
    params: dict[str, Any]
    enabled: bool = True
    quota: Optional[int] = None
    reserve: int = 0


@dataclass
class RuleBase:
    rules: list[Rule]
    max_flows: int
    seed: int = 42
    derived: dict[str, list[str]] = field(default_factory=dict)
    source: str = ""


_REQUIRED: dict[str, tuple[str, ...]] = {
    "per_group": ("group_by", "per_group"),
    "top_percentile": ("field", "percentile"),
    "threshold": ("field", "op", "value"),
    "typical_band": ("fields", "band"),
    "random_fill": (),
    "class_balance": (),
}
_COMMON_KEYS = {"id", "type", "description", "enabled", "quota", "reserve"}
CONSTRAINT_TYPES = {"class_balance"}
_BALANCE_MODES = ("equal",)


def _fail(rule_id: str, msg: str) -> None:
    raise RuleConfigError(f"rule {rule_id!r}: {msg}")


def parse_rulebase(data: dict[str, Any], source: str = "<dict>") -> RuleBase:
    """Validate a parsed YAML mapping into a RuleBase (raises RuleConfigError)."""
    if not isinstance(data, dict):
        raise RuleConfigError(f"{source}: top level must be a mapping")
    budget = data.get("budget") or {}
    max_flows = budget.get("max_flows")
    if not isinstance(max_flows, int) or max_flows <= 0:
        raise RuleConfigError(f"{source}: budget.max_flows must be a positive integer")
    derived = data.get("derived") or {}
    if not isinstance(derived, dict) or not all(
            isinstance(v, list) and v and all(isinstance(c, str) for c in v) for v in derived.values()):
        raise RuleConfigError(f"{source}: derived must map name -> non-empty list of column names")

    rules: list[Rule] = []
    seen: set[str] = set()
    for raw in data.get("rules") or []:
        rid = str(raw.get("id") or "").strip()
        if not rid:
            raise RuleConfigError(f"{source}: every rule needs an id")
        if rid in seen:
            _fail(rid, "duplicate id")
        seen.add(rid)
        rtype = raw.get("type")
        if rtype not in RULE_TYPES and rtype not in CONSTRAINT_TYPES:
            _fail(rid, f"unknown type {rtype!r} (known: {', '.join([*RULE_TYPES, *CONSTRAINT_TYPES])})")
        if rtype in CONSTRAINT_TYPES and (raw.get("quota") is not None or raw.get("reserve")):
            _fail(rid, f"{rtype} is a constraint; it takes no quota/reserve")
        missing = [k for k in _REQUIRED[rtype] if k not in raw]
        if missing:
            _fail(rid, f"type {rtype} requires {missing}")
        params = {k: v for k, v in raw.items() if k not in _COMMON_KEYS}
        quota = raw.get("quota")
        reserve = raw.get("reserve", 0) or 0
        if quota is not None and (not isinstance(quota, int) or quota < 0):
            _fail(rid, "quota must be a non-negative integer or null")
        if not isinstance(reserve, int) or reserve < 0:
            _fail(rid, "reserve must be a non-negative integer")
        if quota is not None and reserve > quota:
            _fail(rid, "reserve cannot exceed quota")
        _check_params(rid, rtype, params)
        rules.append(Rule(id=rid, type=rtype, description=str(raw.get("description", "")),
                          params=params, enabled=bool(raw.get("enabled", True)),
                          quota=quota, reserve=reserve))
    if not rules:
        raise RuleConfigError(f"{source}: no rules declared")
    if sum(1 for r in rules if r.enabled and r.type == "class_balance") > 1:
        raise RuleConfigError(f"{source}: at most one enabled class_balance rule")
    total_reserve = sum(r.reserve for r in rules if r.enabled)
    if total_reserve > max_flows:
        raise RuleConfigError(
            f"{source}: reserves ({total_reserve}) exceed budget.max_flows ({max_flows})")
    return RuleBase(rules=rules, max_flows=max_flows, seed=int(data.get("seed", 42)),
                    derived=derived, source=source)


def _check_params(rid: str, rtype: str, p: dict[str, Any]) -> None:
    if rtype == "per_group":
        if not isinstance(p["per_group"], int) or p["per_group"] <= 0:
            _fail(rid, "per_group must be a positive integer")
        if "max_groups" in p and (not isinstance(p["max_groups"], int) or p["max_groups"] <= 0):
            _fail(rid, "max_groups must be a positive integer")
    elif rtype == "top_percentile":
        if not (isinstance(p["percentile"], (int, float)) and 0 < p["percentile"] < 100):
            _fail(rid, "percentile must be between 0 and 100 (exclusive)")
    elif rtype == "threshold":
        if p["op"] not in _OPS:
            _fail(rid, f"op must be one of {list(_OPS)}")
        if not isinstance(p["value"], (int, float)):
            _fail(rid, "value must be a number")
    elif rtype == "typical_band":
        band = p["band"]
        if not (isinstance(band, list) and len(band) == 2 and 0 <= band[0] < band[1] <= 100):
            _fail(rid, "band must be [lo, hi] percentiles with 0 <= lo < hi <= 100")
        if not (isinstance(p["fields"], list) and p["fields"]):
            _fail(rid, "fields must be a non-empty list")
    elif rtype == "class_balance":
        if p.get("mode", "equal") not in _BALANCE_MODES:
            _fail(rid, f"mode must be one of {list(_BALANCE_MODES)}")
    order = p.get("order_by")
    if order is not None and not isinstance(order, str):
        _fail(rid, "order_by must be a column name or 'random'")


def load_rulebase(path: str | Path | None = None) -> RuleBase:
    p = Path(path) if path else DEFAULT_RULES_PATH
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return parse_rulebase(data, source=str(p))


# ---------------------------------------------------------------------------
# Operator types. Each returns an ORDERED list of (row index, reason) matches
# plus a dict of values it computed from this capture (shown in the audit).
# ---------------------------------------------------------------------------
Match = tuple[int, str]


def _fmt(v: Any) -> str:
    v = float(v)
    return f"{v:,.0f}" if abs(v) >= 100 or v == int(v) else f"{v:,.3g}"


def _ordered(df: pd.DataFrame, idx: pd.Index, order_by: Optional[str], descending: bool,
             rng: np.random.Generator) -> list[int]:
    if order_by in (None, "random"):
        arr = np.array(list(idx))
        rng.shuffle(arr)
        return [int(i) for i in arr]
    return [int(i) for i in df.loc[idx].sort_values(order_by, ascending=not descending,
                                                     kind="mergesort").index]


def _per_group(df, p, rng):
    col = p["group_by"]
    counts = df[col].value_counts(sort=True)  # biggest groups first
    groups = list(counts.index)
    if p.get("max_groups"):
        groups = groups[: p["max_groups"]]
    order_by, desc = p.get("order_by", "random"), bool(p.get("descending", True))
    how = "random (seeded)" if order_by in (None, "random") else f"{order_by} ({'desc' if desc else 'asc'})"
    out: list[Match] = []
    # Round-robin across groups so a tight quota still covers every group first.
    per = {g: _ordered(df, df.index[df[col] == g], order_by, desc, rng)[: p["per_group"]] for g in groups}
    for rank in range(p["per_group"]):
        for g in groups:
            if rank < len(per[g]):
                label = int(g) if isinstance(g, float) and g.is_integer() else g
                out.append((per[g][rank], f"{col}={label}: pick #{rank + 1} of up to {p['per_group']} "
                                          f"from a group of {int(counts[g])} flows, ordered by {how}"))
    info = {"groups_total": int(len(counts)), "groups_used": len(groups)}
    return out, info


def _top_percentile(df, p, rng):
    f, q = p["field"], float(p["percentile"])
    s = df[f]
    thr, med = float(s.quantile(q / 100.0)), float(s.median())
    hit = s[(s >= thr) & (s > med)].sort_values(ascending=False, kind="mergesort")
    out = [(int(i), f"{f} = {_fmt(v)} ≥ p{q:g} of this capture ({_fmt(thr)}); median {_fmt(med)}")
           for i, v in hit.items()]
    return out, {"threshold": thr, "median": med}


def _threshold(df, p, rng):
    f, op, val = p["field"], p["op"], p["value"]
    s = df[f]
    hit = s[_OPS[op](s, val)].sort_values(ascending=op in ("<", "<="), kind="mergesort")
    return [(int(i), f"{f} = {_fmt(v)} {op} {_fmt(val)}") for i, v in hit.items()], {}


def _typical_band(df, p, rng):
    lo, hi = p["band"]
    mask = pd.Series(True, index=df.index)
    bounds = {}
    for f in p["fields"]:
        a, b = float(df[f].quantile(lo / 100.0)), float(df[f].quantile(hi / 100.0))
        bounds[f] = [a, b]
        mask &= df[f].between(a, b)
    idx = _ordered(df, df.index[mask], "random", True, rng)
    names = ", ".join(p["fields"])
    return [(i, f"{names} all within p{lo}–p{hi} of this capture (typical flow)") for i in idx], \
        {"bounds": bounds}


def _random_fill(df, p, rng):
    idx = _ordered(df, df.index, "random", True, rng)
    return [(i, "budget left after the rules above; seeded random fill") for i in idx], {}


RULE_TYPES: dict[str, Callable[[pd.DataFrame, dict, np.random.Generator], tuple[list[Match], dict]]] = {
    "per_group": _per_group,
    "top_percentile": _top_percentile,
    "threshold": _threshold,
    "typical_band": _typical_band,
    "random_fill": _random_fill,
}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
@dataclass
class SelectionResult:
    selected: pd.DataFrame          # selected flows + selection_rule/selection_reason/matched_rules
    rule_summary: list[dict[str, Any]]
    per_flow: list[dict[str, Any]]  # audit trail, one entry per selected flow
    total_flows: int
    max_flows: int
    seed: int

    label_aware: bool = False

    def audit(self) -> dict[str, Any]:
        return {
            "framing": ("Statistical sampling — rules use flow statistics to keep analysis "
                        "bounded; no rule makes a threat judgement."
                        + (" Class balance read held-out labels for SELECTION only; the model "
                           "never sees them." if self.label_aware else "")),
            "label_aware": self.label_aware,
            "total_flows": self.total_flows, "max_flows": self.max_flows,
            "selected": len(self.selected), "seed": self.seed,
            "rules_fired": [r["id"] for r in self.rule_summary if r["fired"]],
            "rules": self.rule_summary, "flows": self.per_flow,
        }


def _with_derived(df: pd.DataFrame, rb: RuleBase) -> pd.DataFrame:
    work = df.copy()
    for name, cols in rb.derived.items():
        unknown = [c for c in cols if c not in work.columns]
        if unknown:
            raise RuleConfigError(f"derived {name!r}: unknown columns {unknown}")
        work[name] = work[cols].sum(axis=1)
    return work


def _fields_of(rule: Rule) -> list[str]:
    p = rule.params
    fs = [p.get("group_by"), p.get("field")] + list(p.get("fields") or [])
    if p.get("order_by") not in (None, "random"):
        fs.append(p["order_by"])
    return [f for f in fs if f]


def rulebase_fields(rb: RuleBase) -> set[str]:
    """Every source column the ENABLED rules read (derived fields expanded)."""
    fields: set[str] = set()
    for r in rb.rules:
        if r.enabled:
            for f in _fields_of(r):
                fields.update(rb.derived.get(f, [f]))
    return fields


def water_fill(counts: dict[str, int], budget: int) -> dict[str, int]:
    """Equal share per class; a class smaller than its share keeps all its rows and
    the spare is re-split among the others. Deterministic (ties: smaller class,
    then name, first)."""
    caps = {c: 0 for c in counts}
    remaining = budget
    open_ = sorted((c for c in counts if counts[c] > 0), key=lambda c: (counts[c], c))
    while open_ and remaining > 0:
        share = remaining // len(open_)
        if share == 0:
            for c in open_[:remaining]:
                caps[c] += 1
            break
        for c in open_:
            give = min(share, counts[c] - caps[c])
            caps[c] += give
            remaining -= give
        open_ = [c for c in open_ if caps[c] < counts[c]]
    return caps


def select_flows(flows: pd.DataFrame, rb: RuleBase, max_flows: Optional[int] = None,
                 labels: Optional[Any] = None) -> SelectionResult:
    """Apply the rule-base to a flow table and return the selection + audit.

    `labels` (optional, aligned with `flows` rows) is consulted ONLY by an enabled
    class_balance constraint. It is never added to the table the operators read
    or to the selected output."""
    budget = int(rb.max_flows if max_flows is None else max_flows)
    if budget <= 0:
        raise RuleConfigError(f"max_flows must be a positive integer (got {budget})")
    work = _with_derived(flows.reset_index(drop=True), rb)
    for r in rb.rules:
        unknown = [f for f in _fields_of(r) if f not in work.columns]
        if unknown:
            _fail(r.id, f"unknown field(s) {unknown}")
    lab = None
    if labels is not None:
        lab = np.asarray(labels, dtype=object)
        if len(lab) != len(work):
            raise ValueError(f"labels length {len(lab)} != flows {len(work)}")

    balance = next((r for r in rb.rules if r.enabled and r.type in CONSTRAINT_TYPES), None)
    caps: Optional[dict[str, int]] = None
    class_counts: dict[str, int] = {}
    if balance is not None and lab is not None and len(work):
        class_counts = {str(k): int(v) for k, v in pd.Series(lab).value_counts().items()}
        caps = water_fill(class_counts, budget)
    taken: dict[str, int] = {c: 0 for c in (caps or {})}

    active = [r for r in rb.rules if r.enabled and r.type not in CONSTRAINT_TYPES]
    # Quotas/reserves are tuned for the configured budget. A run-time override
    # (e.g. --max-flows) scales them by the same factor so the rule MIX is kept.
    factor = budget / rb.max_flows

    def scaled(v: Optional[int]) -> Optional[int]:
        if v is None or factor == 1:
            return v
        return max(1, math.ceil(v * factor)) if v > 0 else 0

    quota = {r.id: scaled(r.quota) for r in active}
    reserve = {r.id: scaled(r.reserve) or 0 for r in active}
    matches: dict[str, list[Match]] = {}
    infos: dict[str, dict] = {}
    for i, r in enumerate(active):
        rng = np.random.default_rng(rb.seed + i)
        if len(work):
            matches[r.id], infos[r.id] = RULE_TYPES[r.type](work, r.params, rng)
        else:
            matches[r.id], infos[r.id] = [], {}
    reasons_by_flow: dict[int, list[dict[str, str]]] = {}
    for r in active:
        for idx, why in matches[r.id]:
            reasons_by_flow.setdefault(idx, []).append({"rule": r.id, "reason": why})

    selected: dict[int, tuple[str, str]] = {}   # idx -> (admitting rule, reason)
    order: list[int] = []
    summary: list[dict[str, Any]] = []
    for pos, r in enumerate(active):
        left = budget - len(selected)
        held_back = sum(
            min(reserve[later.id], sum(1 for i, _ in matches[later.id] if i not in selected))
            for later in active[pos + 1:])
        # A rule may always use its own reserve; beyond that it must leave room
        # for the reserves of the rules after it.
        room = min(left, max(reserve[r.id], left - held_back))
        cap = room if quota[r.id] is None else min(room, quota[r.id])
        admitted = already = blocked = 0
        for idx, why in matches[r.id]:
            if idx in selected:
                already += 1
                continue
            if admitted >= cap:
                continue  # keep scanning so already_selected is counted exactly
            if caps is not None:
                c = str(lab[idx])
                if taken[c] >= caps[c]:
                    blocked += 1          # class already holds its balanced share
                    continue
                taken[c] += 1
            selected[idx] = (r.id, why)
            order.append(idx)
            admitted += 1
        not_taken = len(matches[r.id]) - already - admitted
        summary.append({
            "id": r.id, "type": r.type, "kind": "admission", "description": r.description,
            "enabled": True, "params": r.params, "quota": quota[r.id], "reserve": reserve[r.id],
            "computed": infos[r.id], "matched": len(matches[r.id]), "admitted": admitted,
            "already_selected": already, "not_taken_quota_or_budget": not_taken,
            "blocked_by_class_cap": blocked, "fired": admitted > 0,
        })
    if balance is not None:
        applied = caps is not None
        summary.append({
            "id": balance.id, "type": balance.type, "kind": "constraint",
            "description": balance.description, "enabled": True, "params": balance.params,
            "quota": None, "reserve": 0,
            "computed": ({"applied": True, "class_counts": class_counts, "caps": caps,
                          "selected_per_class": dict(taken)} if applied else
                         {"applied": False,
                          "skipped": "no labels supplied (PCAP, unlabelled CSV, or label-blind run)"}),
            "matched": len(work) if applied else 0, "admitted": 0, "already_selected": 0,
            "not_taken_quota_or_budget": 0,
            "blocked_by_class_cap": sum(x.get("blocked_by_class_cap", 0) for x in summary),
            "fired": applied,
        })
    by_id = {row["id"]: row for row in summary}
    summary = []
    for r in rb.rules:  # report in declared order, disabled rules included
        if r.enabled:
            summary.append(by_id[r.id])
        else:
            summary.append({"id": r.id, "type": r.type,
                            "kind": "constraint" if r.type in CONSTRAINT_TYPES else "admission",
                            "description": r.description,
                            "enabled": False, "params": r.params, "quota": r.quota,
                            "reserve": r.reserve, "computed": {}, "matched": 0, "admitted": 0,
                            "already_selected": 0, "not_taken_quota_or_budget": 0,
                            "blocked_by_class_cap": 0, "fired": False})

    sel = flows.reset_index(drop=True).loc[order].copy()
    sel["selection_rule"] = [selected[i][0] for i in order]
    sel["selection_reason"] = [selected[i][1] for i in order]
    sel["matched_rules"] = [";".join(m["rule"] for m in reasons_by_flow[i]) for i in order]
    per_flow = [{
        "flow_id": str(work.at[i, "flow_id"]) if "flow_id" in work.columns else str(i),
        "admitted_by": selected[i][0], "reason": selected[i][1],
        "also_matched": [m for m in reasons_by_flow[i] if m["rule"] != selected[i][0]],
    } for i in order]
    return SelectionResult(selected=sel.reset_index(drop=True), rule_summary=summary,
                           per_flow=per_flow, total_flows=len(work), max_flows=budget,
                           seed=rb.seed, label_aware=caps is not None)
