"""Rule-based flow selection + CIC-IDS2017 feature map (pandas only — no scapy)."""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest
import yaml

from agentmeter.config import PROJECT_ROOT
from agentmeter.ingest import feature_map as fm
from agentmeter.ingest import rules


# --- feature map -------------------------------------------------------------
def test_feature_list_matches_the_pipeline_dataset_header():
    header = pd.read_csv(PROJECT_ROOT / "data" / "cicids_full_300.csv", nrows=0).columns
    assert fm.CIC_FEATURES == [c for c in header if c != "label"]
    assert len(fm.CIC_FEATURES) == 78


def test_every_mapping_has_a_known_status_and_gaps_are_explained():
    for cic, key, status, note in fm.FEATURE_MAP:
        assert status in fm.STATUSES, cic
        assert key, cic
        if status != "direct":
            assert note, f"{cic}: a non-direct mapping must say why"


def test_mapping_report_flags_missing_and_unmapped_keys():
    keys = {k for _, k, _, _ in fm.FEATURE_MAP} - {"idle_std"} | {"brand_new_key"}
    rep = fm.mapping_report(keys)
    assert rep["counts"]["missing"] == 1
    assert [r["cic_ids2017"] for r in rep["features"] if r["status"] == "missing"] == ["Idle Std"]
    assert rep["unmapped_extractor_keys"] == ["brand_new_key"]
    assert sum(rep["counts"].values()) == 78
    md = fm.mapping_markdown(rep)
    assert "NO — 1 missing" in md and "| Idle Std | `idle_std` | missing |" in md


# --- rule engine ---------------------------------------------------------------
def make_flows(n_tcp=150, n_udp=45, seed=0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = n_tcp + n_udp
    df = pd.DataFrame(0.0, index=range(n), columns=fm.CIC_FEATURES)
    df.insert(0, "flow_id", [f"f{i:05d}" for i in range(n)])
    df["protocol_name"] = ["TCP"] * n_tcp + ["UDP"] * n_udp
    df["Destination Port"] = [80.0] * 100 + [443.0] * 50 + [53.0] * n_udp
    df["Total Fwd Packets"] = rng.integers(2, 10, n).astype(float)
    df["Total Backward Packets"] = rng.integers(1, 8, n).astype(float)
    df["Total Length of Fwd Packets"] = rng.integers(100, 900, n).astype(float)
    df["Total Length of Bwd Packets"] = rng.integers(100, 900, n).astype(float)
    df["Flow Duration"] = rng.integers(1_000, 50_000, n).astype(float)
    df.loc[[3, 7, 11], "Total Fwd Packets"] = [5000.0, 4000.0, 3000.0]       # packet outliers
    df.loc[[20], "Flow Duration"] = [90_000_000.0]                           # > 60 s
    return df


def rb_from(rule_list, max_flows=40, **extra) -> rules.RuleBase:
    data = {"seed": 7, "budget": {"max_flows": max_flows},
            "derived": {"total_packets": ["Total Fwd Packets", "Total Backward Packets"],
                        "total_bytes": ["Total Length of Fwd Packets", "Total Length of Bwd Packets"]},
            "rules": rule_list, **extra}
    return rules.parse_rulebase(data)


def test_default_rulebase_loads_and_is_statistical_only():
    rb = rules.load_rulebase()
    assert rb.max_flows == 500
    assert {r.type for r in rb.rules} <= set(rules.RULE_TYPES)
    text = (PROJECT_ROOT / "configs" / "flow_rules.yaml").read_text(encoding="utf-8").lower()
    assert "not a threat judgement" in text or "no rule judges" in text


def test_default_rules_bound_the_selection_and_explain_every_flow():
    df = make_flows()
    res = rules.select_flows(df, rules.load_rulebase(), max_flows=40)
    assert len(res.selected) == 40
    assert res.selected["flow_id"].is_unique
    audit = res.audit()
    assert audit["selected"] == 40 and len(audit["flows"]) == 40
    assert all(f["admitted_by"] and f["reason"] for f in audit["flows"])
    assert set(res.selected["selection_rule"]) <= {r.id for r in rules.load_rulebase().rules}
    # Coverage: both protocols present even though TCP dominates.
    assert set(res.selected["protocol_name"]) == {"TCP", "UDP"}
    # Notable flows are taken and the reason quotes the computed percentile.
    assert {"f00003", "f00007", "f00011"} <= set(res.selected["flow_id"])
    fired = audit["rules_fired"]
    assert "protocol_coverage" in fired and "typical_baseline" in fired
    assert "no rule makes a threat judgement" in audit["framing"]


def test_selection_is_deterministic_for_a_seed():
    df, rb = make_flows(), rules.load_rulebase()
    a = rules.select_flows(df, rb, max_flows=30).selected["flow_id"].tolist()
    b = rules.select_flows(df, rb, max_flows=30).selected["flow_id"].tolist()
    assert a == b
    rb2 = copy.deepcopy(rb)
    rb2.seed = 999
    assert rules.select_flows(df, rb2, max_flows=30).selected["flow_id"].tolist() != a


def test_top_percentile_reason_and_threshold_rule():
    rb = rb_from([
        {"id": "big", "type": "top_percentile", "field": "total_packets", "percentile": 98},
        {"id": "long", "type": "threshold", "field": "Flow Duration", "op": ">=", "value": 60_000_000},
    ])
    res = rules.select_flows(make_flows(), rb)
    by = dict(zip(res.selected["flow_id"], res.selected["selection_reason"]))
    assert "≥ p98 of this capture" in by["f00003"]
    assert list(res.selected["flow_id"][:3]) == ["f00003", "f00007", "f00011"]  # largest first
    assert by["f00020"].startswith("Flow Duration = 90,000,000 >= 60,000,000")


def test_flat_capture_fires_no_percentile_rule():
    df = make_flows()
    df["Total Fwd Packets"] = 4.0
    df["Total Backward Packets"] = 4.0
    rb = rb_from([{"id": "big", "type": "top_percentile", "field": "total_packets", "percentile": 95}])
    res = rules.select_flows(df, rb)
    assert len(res.selected) == 0 and res.audit()["rules_fired"] == []


def test_reserve_is_held_back_from_an_earlier_greedy_rule():
    rb = rb_from([
        {"id": "greedy", "type": "random_fill"},
        {"id": "baseline", "type": "typical_band", "fields": ["total_packets"],
         "band": [25, 75], "reserve": 8, "quota": 8},
    ], max_flows=20)
    res = rules.select_flows(make_flows(), rb)
    counts = res.selected["selection_rule"].value_counts().to_dict()
    assert counts == {"greedy": 12, "baseline": 8}


def test_quota_caps_a_rule_and_per_group_round_robins():
    rb = rb_from([{"id": "proto", "type": "per_group", "group_by": "protocol_name",
                   "per_group": 10, "quota": 4}])
    res = rules.select_flows(make_flows(), rb)
    assert len(res.selected) == 4
    assert res.selected["protocol_name"].tolist().count("UDP") == 2  # alternates groups


def test_budget_override_scales_quotas_and_reserves():
    res = rules.select_flows(make_flows(), rules.load_rulebase(), max_flows=15)
    assert len(res.selected) == 15
    summary = {r["id"]: r for r in res.rule_summary}
    assert summary["typical_baseline"]["reserve"] == 3      # ceil(100 * 15/500)
    assert summary["protocol_coverage"]["quota"] == 2       # ceil(50 * 15/500)
    assert summary["fill_remaining"]["quota"] is None
    assert len(res.audit()["rules_fired"]) >= 4             # the mix survives a tight budget
    for r in res.rule_summary:                              # every match is accounted for
        assert r["admitted"] + r["already_selected"] + r["not_taken_quota_or_budget"] == r["matched"]


def test_disabled_rule_is_listed_but_does_not_fire():
    rb = rb_from([
        {"id": "off", "type": "random_fill", "enabled": False},
        {"id": "big", "type": "top_percentile", "field": "total_packets", "percentile": 95},
    ])
    res = rules.select_flows(make_flows(), rb)
    summary = {r["id"]: r for r in res.rule_summary}
    assert summary["off"]["enabled"] is False and summary["off"]["fired"] is False
    assert [r["id"] for r in res.rule_summary] == ["off", "big"]  # declared order
    assert set(res.selected["selection_rule"]) == {"big"}


def test_also_matched_records_every_other_rule():
    rb = rb_from([
        {"id": "big", "type": "top_percentile", "field": "total_packets", "percentile": 98},
        {"id": "big_abs", "type": "threshold", "field": "Total Fwd Packets", "op": ">", "value": 1000},
    ])
    flow = next(f for f in rules.select_flows(make_flows(), rb).per_flow if f["flow_id"] == "f00003")
    assert flow["admitted_by"] == "big"
    assert [m["rule"] for m in flow["also_matched"]] == ["big_abs"]


def test_empty_flow_table_selects_nothing():
    res = rules.select_flows(make_flows().iloc[0:0], rules.load_rulebase())
    assert len(res.selected) == 0 and res.audit()["rules_fired"] == []


@pytest.mark.parametrize("bad, msg", [
    ({"id": "x", "type": "magic"}, "unknown type"),
    ({"id": "x", "type": "top_percentile", "field": "total_packets"}, "requires"),
    ({"id": "x", "type": "top_percentile", "field": "total_packets", "percentile": 100}, "percentile"),
    ({"id": "x", "type": "threshold", "field": "Flow Duration", "op": "=~", "value": 1}, "op must be"),
    ({"id": "x", "type": "typical_band", "fields": ["Flow Duration"], "band": [80, 20]}, "band"),
    ({"id": "x", "type": "random_fill", "quota": 2, "reserve": 5}, "reserve cannot exceed quota"),
    ({"id": "x", "type": "random_fill", "reserve": 999}, "exceed budget"),
])
def test_bad_rule_configs_are_rejected_with_a_clear_message(bad, msg):
    with pytest.raises(rules.RuleConfigError, match=msg):
        rb_from([bad])


def test_duplicate_ids_unknown_fields_and_bad_budget_are_rejected():
    with pytest.raises(rules.RuleConfigError, match="duplicate id"):
        rb_from([{"id": "a", "type": "random_fill"}, {"id": "a", "type": "random_fill"}])
    rb = rb_from([{"id": "a", "type": "per_group", "group_by": "No Such Column", "per_group": 1}])
    with pytest.raises(rules.RuleConfigError, match="unknown field"):
        rules.select_flows(make_flows(), rb)
    with pytest.raises(rules.RuleConfigError, match="max_flows"):
        rules.select_flows(make_flows(), rb_from([{"id": "a", "type": "random_fill"}]), max_flows=0)
    with pytest.raises(rules.RuleConfigError, match="max_flows"):
        rules.parse_rulebase({"budget": {"max_flows": 0}, "rules": [{"id": "a", "type": "random_fill"}]})


def test_adding_a_rule_is_a_config_edit_only(tmp_path):
    data = yaml.safe_load((PROJECT_ROOT / "configs" / "flow_rules.yaml").read_text(encoding="utf-8"))
    data["rules"].insert(0, {"id": "https_first", "type": "threshold", "description": "added in YAML",
                             "field": "Destination Port", "op": "==", "value": 443, "quota": 5})
    p = tmp_path / "rules.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    res = rules.select_flows(make_flows(), rules.load_rulebase(p), max_flows=500)
    taken = res.selected[res.selected["selection_rule"] == "https_first"]
    assert len(taken) == 5 and set(taken["Destination Port"]) == {443.0}
