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

# --- severity -------------------------------------------------------------------------------
# ATT&CK has no severity and network flows carry no CVE, so severity is taken from a published
# IDS source: each class is mapped (``severity:`` in the config) to a Snort/Suricata classtype,
# whose default priority in classification.config (1 = most severe) gives the level.
SNORT_CLASSTYPE_PRIORITY: dict[str, int] = {
    "attempted-admin": 1,      # Attempted Administrator Privilege Gain
    "attempted-user": 1,       # Attempted User Privilege Gain
    "attempted-dos": 2,        # Attempted Denial of Service
    "attempted-recon": 2,      # Attempted Information Leak
    "not-suspicious": 3,       # Not Suspicious Traffic
    "unknown": 3,              # Unknown Traffic
}
SEVERITY_LEVEL = {1: "High", 2: "Medium", 3: "Low"}
DEFAULT_SEVERITY: dict[str, str] = {
    "Brute Force": "attempted-user",
    "Volumetric DDoS": "attempted-dos",
    "DoS Hulk": "attempted-dos",
    "Port Scanning": "attempted-recon",
    "Benign": "not-suspicious",
}
OTHER_ATTACK_CLASSTYPE = "unknown"
SEVERITY_SOURCE = ("Snort/Suricata classification.config default priorities "
                   "(1 = High, 2 = Medium, 3 = Low) of the classtype mapped to each class")

# --- triage gate ----------------------------------------------------------------------------
# A model is recommended for triage only when it meets every limit. Defaults: parity with a
# Tier-2 analyst as reported in research (85-90% of true positives detected, 70-80% of false
# positives rejected; SIR-Bench, arXiv 2604.12040), taken at the lower end. The valid-answer
# floor is this study's own choice (no published standard). Declare all three in the thesis.
DEFAULT_GATE: dict[str, Any] = {
    "max_missed_attack_rate": 0.15,
    "max_false_escalation_rate": 0.30,
    "min_valid_rate": 0.95,
    "basis": "Tier-2 analyst parity (SIR-Bench, arXiv 2604.12040: 85-90% true positives detected, "
             "70-80% false positives rejected); valid-answer floor chosen by this study",
}


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


def severity_table(config_data: dict[str, Any]) -> dict[str, str]:
    t = config_data.get("severity")
    return dict(t) if isinstance(t, dict) and t else dict(DEFAULT_SEVERITY)


def validate_severity(table: Any, classes: list[str]) -> None:
    if not isinstance(table, dict):
        raise ValueError("severity must map each class to a Snort/Suricata classtype")
    bad = {c: t for c, t in table.items() if t not in SNORT_CLASSTYPE_PRIORITY}
    if bad:
        raise ValueError(f"severity: classtype must be one of {', '.join(SNORT_CLASSTYPE_PRIORITY)}; got {bad}")
    missing = [c for c in classes if c not in table]
    if missing:
        raise ValueError(f"severity: no classtype for class(es) {', '.join(missing)}")


def severity_of(label: Optional[str], table: dict[str, str]) -> Optional[dict[str, Any]]:
    """{classtype, priority, level} for a class; None for an answer that is not a class."""
    ct = table.get(label or "")
    if ct is None:
        return None
    p = SNORT_CLASSTYPE_PRIORITY[ct]
    return {"classtype": ct, "priority": p, "level": SEVERITY_LEVEL[p]}


def gate_config(config_data: dict[str, Any]) -> dict[str, Any]:
    g = config_data.get("triage_gate")
    return {**DEFAULT_GATE, **(g if isinstance(g, dict) else {})}


def validate_gate(gate: Any) -> None:
    if not isinstance(gate, dict):
        raise ValueError("triage_gate must be a mapping")
    for k in ("max_missed_attack_rate", "max_false_escalation_rate", "min_valid_rate"):
        if k in gate and not (isinstance(gate[k], (int, float)) and 0 <= gate[k] <= 1):
            raise ValueError(f"triage_gate.{k} must be a share between 0 and 1, got {gate[k]!r}")


def _not_applicable(m: dict[str, Any]) -> list[str]:
    """Checks with nothing to judge on labelled input: no attack reached the model (missed attacks),
    or no benign flow did (false escalations; e.g. the IDS let no false alarm through)."""
    out = []
    if m.get("n_attacks") == 0:
        out.append("missed_attack_rate")
    if m.get("n_benign") == 0:
        out.append("false_escalation_rate")
    return out


def effective_gate(m: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The gate result of a triage row, also for sessions stored before not-applicable checks
    existed (they read "not checked" when no benign flow reached the model)."""
    g = m.get("gate")
    if not g or g.get("passed") is not None or g.get("failed"):
        return g
    na = set(g.get("not_applicable") or _not_applicable(m))
    left = [c for c in g.get("not_checked") or [] if c not in na]
    return {**g, "passed": None if left else True, "not_checked": left, "not_applicable": sorted(na)}


def apply_gate(triage: dict[str, Any], reliability: Optional[dict[str, Any]], gate: dict[str, Any]) -> None:
    """Add each model's gate result to the triage block (in place). Unlabelled input cannot be
    checked for missed attacks or false escalations: those checks read "not checked". On labelled
    input, a check with nothing to judge (no benign flow reached the model, e.g. the IDS let no
    false alarm through) is "not applicable" and does not hold the gate back."""
    valid = {r["model"]: r.get("valid_rate") for r in ((reliability or {}).get("per_model") or [])}
    for m in triage.get("per_model") or []:
        checks = []

        def check(name, value, limit, ok_if_le):
            if value is None:
                checks.append({"check": name, "value": None, "limit": limit, "passed": None})
            else:
                checks.append({"check": name, "value": value, "limit": limit,
                               "passed": value <= limit if ok_if_le else value >= limit})
        check("missed_attack_rate", m.get("missed_attack_rate"), gate["max_missed_attack_rate"], True)
        check("false_escalation_rate", m.get("false_escalation_rate"), gate["max_false_escalation_rate"], True)
        check("valid_rate", valid.get(m["model"]), gate["min_valid_rate"], False)
        na = _not_applicable(m)
        for c in checks:
            if c["check"] in na:
                c["not_applicable"] = True
        failed = [c["check"] for c in checks if c["passed"] is False]
        unchecked = [c["check"] for c in checks if c["passed"] is None and c["check"] not in na]
        m["gate"] = {"passed": False if failed else (None if unchecked else True),
                     "failed": failed, "not_checked": unchecked, "not_applicable": na, "checks": checks}
    triage["gate"] = {k: gate[k] for k in ("max_missed_attack_rate", "max_false_escalation_rate",
                                           "min_valid_rate", "basis")}


def triage_block(sr: list[dict], label_of: dict[str, str], table: dict[str, str], *,
                 labelled: bool, cost: Optional[dict] = None,
                 severity: Optional[dict[str, str]] = None) -> dict[str, Any]:
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
        sev = severity or DEFAULT_SEVERITY
        levels = [(severity_of(r.get("predicted_label"), sev) or {}).get("level") or "None" for r in rs]
        row: dict[str, Any] = {"model": m, "n_flows": n, "queue": queue,
                               "queue_share": {p: (queue[p] / n if n else None) for p in PRIORITIES},
                               "severity_queue": {lv: levels.count(lv) for lv in ("High", "Medium", "Low", "None")}}
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
        "severity": {c: severity_of(c, severity or DEFAULT_SEVERITY) for c in (severity or DEFAULT_SEVERITY)},
        "severity_source": SEVERITY_SOURCE,
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
            "severity": "Severity = the default priority of the Snort/Suricata classtype mapped to the predicted "
                        "class (MITRE ATT&CK has no severity, and flows carry no CVE). \"None\" = an answer "
                        "that is not a class.",
            "gate": "A model is recommended for triage only if it meets every gate limit; a check that needs "
                    "labels is \"not checked\" on unlabelled input. The gate never changes the efficiency "
                    "score or the ranking.",
        },
    }
