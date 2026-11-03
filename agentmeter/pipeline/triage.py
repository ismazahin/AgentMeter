"""Triage priority: the SOC policy that turns a predicted class into what an analyst does next.

The model never chooses the priority. It classifies the flow; this table (``triage:`` in the
config, next to ``mitre:``) maps the class to Escalate, Investigate or Dismiss, the same way the
MITRE technique is looked up. Every model is therefore judged under the same policy, and the
priority can be recomputed for any stored session from its predicted and true labels.
"""
from __future__ import annotations

from typing import Any, Optional

ESCALATE, INVESTIGATE, DISMISS = "Escalate", "Investigate", "Dismiss"
PRIORITIES = (ESCALATE, INVESTIGATE, DISMISS)

# The policy used when a config has no ``triage:`` block (sessions prepared before triage).
# Keep it identical to config.yaml.
DEFAULT_TRIAGE: dict[str, str] = {
    "Brute Force": ESCALATE,
    "Volumetric DDoS": ESCALATE,
    "DoS Hulk": ESCALATE,
    "Port Scanning": INVESTIGATE,
    "Benign": DISMISS,
}
# An attack outside the 5-class taxonomy (the 6-class scheme): known to be an attack, type unknown.
OTHER_ATTACK_PRIORITY = INVESTIGATE
# A class the table does not name, or an answer that could not be parsed: never dismissed silently.
FALLBACK_PRIORITY = INVESTIGATE
BENIGN = "Benign"


def triage_table(config_data: dict[str, Any]) -> dict[str, str]:
    """The config's triage table, or the default policy when the config has none."""
    t = config_data.get("triage")
    return dict(t) if isinstance(t, dict) and t else dict(DEFAULT_TRIAGE)


def validate_triage(table: Any, classes: list[str]) -> None:
    """Raise ValueError unless every class has exactly one allowed priority."""
    if not isinstance(table, dict):
        raise ValueError("triage must map each class to Escalate, Investigate or Dismiss")
    bad = {c: p for c, p in table.items() if p not in PRIORITIES}
    if bad:
        raise ValueError(f"triage: priority must be one of {', '.join(PRIORITIES)}; got {bad}")
    missing = [c for c in classes if c not in table]
    if missing:
        raise ValueError(f"triage: no priority for class(es) {', '.join(missing)}")


def priority_of(label: Optional[str], table: dict[str, str]) -> str:
    return table.get(label or "", FALLBACK_PRIORITY)


def triage_block(sr: list[dict], label_of: dict[str, str], table: dict[str, str], *,
                 labelled: bool, cost: Optional[dict] = None) -> dict[str, Any]:
    """Per model: the analyst queue (Escalate / Investigate / Dismiss) and, for labelled input,
    the triage errors that matter in a SOC: attacks dismissed and benign flows escalated.

    Context for the efficiency results, never part of the efficiency score or the ranking."""
    cost_of = {r["model"]: r for r in ((cost or {}).get("per_model") or [])}
    model_of = {dbm: m for m, dbm in label_of.items()}
    rows: dict[str, list[dict]] = {}
    for r in sr:
        if r["model"] in model_of:
            rows.setdefault(model_of[r["model"]], []).append(r)
    per = []
    for m in label_of:
        rs = rows.get(m, [])
        n = len(rs)
        pred = [priority_of(r.get("predicted_label"), table) for r in rs]
        queue = {p: pred.count(p) for p in PRIORITIES}
        row: dict[str, Any] = {"model": m, "n_flows": n, "queue": queue,
                               "queue_share": {p: (queue[p] / n if n else None) for p in PRIORITIES}}
        if labelled and n:
            truth = [priority_of(r.get("held_out_label"), table) for r in rs]
            attack = [r.get("held_out_label") != BENIGN for r in rs]
            n_att = sum(attack)
            n_ben = n - n_att
            missed = sum(1 for a, p in zip(attack, pred) if a and p == DISMISS)
            false_esc = sum(1 for a, p in zip(attack, pred) if not a and p == ESCALATE)
            caught = n_att - missed
            confusion = {t: {p: 0 for p in PRIORITIES} for t in PRIORITIES}
            for t, p in zip(truth, pred):
                confusion[t][p] += 1
            row.update(
                n_attacks=n_att, n_benign=n_ben,
                missed_attacks=missed, missed_attack_rate=(missed / n_att) if n_att else None,
                attacks_caught=caught,
                false_escalations=false_esc, false_escalation_rate=(false_esc / n_ben) if n_ben else None,
                priority_accuracy=sum(1 for t, p in zip(truth, pred) if t == p) / n,
                confusion=confusion)
            c = (cost_of.get(m) or {}).get("cost_per_1k_flows_usd")
            row["cost_per_attack_caught_usd"] = (c * n / 1000.0 / caught) if (c is not None and caught) else None
            wh = (cost_of.get(m) or {}).get("wh_per_flow")
            row["wh_per_attack_caught"] = (wh * n / caught) if (wh is not None and caught) else None
        per.append(row)
    return {
        "labelled": labelled,
        "policy": {c: table[c] for c in table},
        "priorities": list(PRIORITIES),
        "fallback": FALLBACK_PRIORITY,
        "per_model": per,
        "notes": {
            "policy": "The priority comes from this table (the SOC's policy), never from the model: the model "
                      "classifies the flow, the table says what an analyst does with that class. A class the "
                      "table does not name, or an answer that could not be parsed, goes to Investigate.",
            "missed": "Missed attacks = flows whose true class is an attack but whose predicted class maps to "
                      "Dismiss: the costliest triage error.",
            "false_escalation": "False escalations = benign flows whose predicted class maps to Escalate: "
                                "analyst time spent on nothing.",
            "cost": "Cost (energy) per attack caught = the model's cost (energy) for all its flows divided by "
                    "the attacks it did not dismiss. Measured runs only.",
            "context": "Triage quality is context for the efficiency results. It does not change the efficiency "
                       "score or the ranking.",
            "flows": "The flows are network flows, not IDS alerts: this is flow triage.",
        },
    }
