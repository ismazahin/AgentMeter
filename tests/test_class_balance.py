"""Phase 37: "Other Attack" for user CSV runs + label-aware class-balanced selection.

pandas only. Builds imbalanced / out-of-taxonomy CSVs from rows already in
data/cicids_full_300.csv (read-only). Proves the locked baseline is untouched and
that labels never reach the operators' table or the model-visible output.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math

import pandas as pd
import pytest

from agentmeter.config import PROJECT_ROOT, load_config
from agentmeter.ingest import rules, run
from agentmeter.ingest.csv_input import OTHER_ATTACK, load_csv

DATASET = PROJECT_ROOT / "data" / "cicids_full_300.csv"
SAMPLE = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
# Locked baseline dataset fingerprint (data/cicids_full_300.csv as committed).
DATASET_SHA256 = "ef1b13787e2c87602033f54d985355515408b4f930c9648cd1fb29cb540392b5"
STUDY_CLASSES = ["Brute Force", "Volumetric DDoS", "Port Scanning", "DoS Hulk", "Benign"]
RAW = {"Benign": "BENIGN", "Brute Force": "SSH-Patator", "DoS Hulk": "DoS Hulk",
       "Volumetric DDoS": "DDoS", "Port Scanning": "PortScan"}


def _sha(p) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _db_hashes() -> dict[str, str]:
    return {str(p): _sha(p) for p in (PROJECT_ROOT / "results").glob("*.db")}


def imbalanced_csv(tmp_path, other: dict[str, int] | None = None, name="imbalanced.csv"):
    """~86% BENIGN, like a real CIC-IDS2017 day file, with raw labels."""
    df = pd.read_csv(DATASET)
    parts = [pd.concat([df[df.label == "Benign"]] * 5)]                       # 300 BENIGN
    parts += [df[df.label == c].head(12) for c in RAW if c != "Benign"]        # 12 per attack class
    for raw_label, n in (other or {}).items():                                # out-of-5-class attacks
        parts.append(df[df.label == "DoS Hulk"].tail(n).assign(label=raw_label))
    out = pd.concat(parts, ignore_index=True)
    out["label"] = out["label"].map(lambda v: RAW.get(v, v))
    p = tmp_path / name
    out.rename(columns={"label": " Label"}).to_csv(p, index=False)
    return p


def _share(dist: dict[str, int]) -> float:
    return max(dist.values()) / sum(dist.values())


def _evenness(dist: dict[str, int]) -> float:
    n = sum(dist.values())
    h = -sum(v / n * math.log(v / n) for v in dist.values() if v)
    return h / math.log(len(dist)) if len(dist) > 1 else 1.0


# --- water-filling -------------------------------------------------------------------
def test_water_fill_gives_equal_shares_and_redistributes_small_classes():
    caps = rules.water_fill({"Benign": 900, "DoS": 40, "Bot": 5, "PS": 200}, 100)
    assert sum(caps.values()) == 100 and caps["Bot"] == 5
    assert max(caps.values()) - min(c for k, c in caps.items() if k != "Bot") <= 1
    assert rules.water_fill({"a": 3, "b": 2}, 100) == {"a": 3, "b": 2}       # budget > rows
    assert rules.water_fill({"a": 10, "b": 10, "c": 10}, 2) == {"a": 1, "b": 1, "c": 0}


# --- class-balanced selection ---------------------------------------------------------
def test_class_balance_evens_out_an_imbalanced_csv(tmp_path):
    p = imbalanced_csv(tmp_path)
    blind = run.process_csv(p, max_flows=50, label_blind=True, write=False)
    bal = run.process_csv(p, max_flows=50, out_root=tmp_path / "out")
    d_blind = blind["input"].metadata["label_distribution_selected"]
    d_bal = bal["input"].metadata["label_distribution_selected"]
    assert sum(d_blind.values()) == sum(d_bal.values()) == 50
    assert _share(d_blind) > 0.5 and max(d_blind, key=d_blind.get) == "Benign"   # dominated
    assert d_bal == {c: 10 for c in STUDY_CLASSES}                                # balanced
    assert _evenness(d_bal) > _evenness(d_blind)

    meta = bal["input"].metadata
    assert meta["selection_mode"] == "label_aware_balanced"
    assert blind["input"].metadata["selection_mode"] == "label_blind_statistical"
    audit = bal["audit"]
    assert audit["label_aware"] and "class_balance" in audit["rules_fired"]
    cb = next(r for r in audit["rules"] if r["id"] == "class_balance")
    assert cb["kind"] == "constraint" and cb["computed"]["caps"] == {c: 10 for c in STUDY_CLASSES}
    assert cb["blocked_by_class_cap"] > 0                       # BENIGN picks were capped
    assert len(audit["flows"]) == 50 and all(f["admitted_by"] and f["reason"] for f in audit["flows"])
    for r in audit["rules"]:                                    # accounting still exact
        assert r["admitted"] + r["already_selected"] + r["not_taken_quota_or_budget"] == r["matched"] \
            or r["kind"] == "constraint"
    skipped = next(r for r in blind["audit"]["rules"] if r["id"] == "class_balance")
    assert skipped["fired"] is False and "label-blind" in skipped["computed"]["skipped"]


def test_class_balance_is_a_yaml_edit_away(tmp_path):
    import yaml

    data = yaml.safe_load((PROJECT_ROOT / "configs" / "flow_rules.yaml").read_text(encoding="utf-8"))
    for r in data["rules"]:
        if r["type"] == "class_balance":
            r["enabled"] = False
    rp = tmp_path / "rules.yaml"
    rp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    res = run.process_csv(imbalanced_csv(tmp_path), rules_path=rp, max_flows=50, write=False)
    assert res["input"].metadata["selection_mode"] == "label_blind_statistical"
    assert _share(res["input"].metadata["label_distribution_selected"]) > 0.5


@pytest.mark.parametrize("bad, msg", [
    ({"id": "cb", "type": "class_balance", "quota": 5}, "takes no quota"),
    ({"id": "cb", "type": "class_balance", "mode": "proportional"}, "mode must be"),
])
def test_bad_class_balance_configs_are_rejected(bad, msg):
    with pytest.raises(rules.RuleConfigError, match=msg):
        rules.parse_rulebase({"budget": {"max_flows": 10}, "rules": [bad]})
    with pytest.raises(rules.RuleConfigError, match="at most one"):
        rules.parse_rulebase({"budget": {"max_flows": 10}, "rules": [
            {"id": "a", "type": "class_balance"}, {"id": "b", "type": "class_balance"}]})


# --- label isolation --------------------------------------------------------------------
def test_labels_never_reach_operators_or_the_model_visible_output(tmp_path, monkeypatch):
    p = imbalanced_csv(tmp_path, other={"Heartbleed": 4, "Bot": 6})
    label_values = set(STUDY_CLASSES) | {OTHER_ATTACK} | set(RAW.values()) | {"Heartbleed", "Bot"}
    seen: list[pd.DataFrame] = []
    for name, fn in list(rules.RULE_TYPES.items()):
        def spy(df, p_, rng, _fn=fn):
            seen.append(df)
            return _fn(df, p_, rng)
        monkeypatch.setitem(rules.RULE_TYPES, name, spy)

    res = run.process_csv(p, max_flows=40, other_attack=True, out_root=tmp_path / "out")
    assert res["input"].metadata["selection_mode"] == "label_aware_balanced"
    assert seen, "operators were not called"
    for df in seen:                                    # what the rule operators read
        assert not any("label" in c.lower() for c in df.columns)
        for c in df.select_dtypes(exclude="number").columns:
            assert not set(df[c].astype(str)) & label_values, c
    out = tmp_path / "out" / "imbalanced"
    sel_text = (out / "selected_flows.csv").read_text()  # what the model phase reads
    assert not any(v in sel_text for v in label_values)
    labels = pd.read_csv(out / "labels.csv")
    assert list(labels["flow_id"]) == list(pd.read_csv(out / "selected_flows.csv")["flow_id"])


# --- Other Attack ---------------------------------------------------------------------
def test_out_of_taxonomy_attacks_become_other_attack_in_a_6_class_run(tmp_path):
    p = imbalanced_csv(tmp_path, other={"DoS GoldenEye": 5, "Heartbleed": 2, "Web Attack � XSS": 3})
    res = run.process_csv(p, other_attack=True, max_flows=60, out_root=tmp_path / "out")
    rep, meta = res["report"], res["input"].metadata
    assert rep["label"]["other_attack"] == {"enabled": True, "rows": 10, "sources": {
        "DoS GoldenEye": 5, "Web Attack � XSS": 3, "Heartbleed": 2}}
    assert rep["label"]["distribution"][OTHER_ATTACK] == 10
    assert rep["rows_excluded_out_of_taxonomy"] == 0
    assert meta["class_scheme"]["name"] == "6-class"
    assert meta["class_scheme"]["classes"] == load_config().get("classes") + [OTHER_ATTACK]
    assert meta["label_distribution_selected"][OTHER_ATTACK] == 10   # all kept: under its share
    labels = res["input"].labels
    other = labels[labels["label"] == OTHER_ATTACK]
    assert set(other["label_raw"]) == {"DoS GoldenEye", "Heartbleed", "Web Attack � XSS"}
    md = (tmp_path / "out" / "imbalanced" / "schema_report.md").read_text()
    assert "**6-class**" in md and "Other Attack: enabled — 10 rows" in md


def test_default_is_5_class_and_excludes_with_a_hint(tmp_path):
    p = imbalanced_csv(tmp_path, other={"Bot": 4})
    res = run.process_csv(p, write=False)
    rep = res["report"]
    assert rep["class_scheme"]["name"] == "5-class" and OTHER_ATTACK not in rep["label"]["distribution"]
    assert rep["label"]["excluded_out_of_taxonomy"] == {"Bot": 4}
    assert any("--other-attack" in n for n in rep["notes"])


def test_five_class_csv_stays_5_class_even_with_other_attack_on():
    res = run.process_csv(DATASET, other_attack=True, write=False)
    scheme = res["input"].metadata["class_scheme"]
    assert scheme["name"] == "5-class" and scheme["other_attack_enabled"] is True
    assert scheme["classes"] == load_config().get("classes")


def test_missing_labels_are_never_other_attack(tmp_path):
    df = pd.read_csv(DATASET).head(20)
    df.loc[[0, 1, 2], "label"] = ["", "nan", "NULL"]
    p = tmp_path / "gaps.csv"
    df.to_csv(p, index=False)
    rep = load_csv(p, other_attack=True).report
    assert rep["rows_excluded_missing_label"] == 3
    assert rep["label"]["other_attack"]["rows"] == 0


def test_labels_match_case_insensitively(tmp_path):
    df = pd.read_csv(DATASET).head(10).assign(label="benign")
    p = tmp_path / "case.csv"
    df.to_csv(p, index=False)
    assert load_csv(p).report["label"]["distribution"] == {"Benign": 10}


# --- the locked baseline is untouched ------------------------------------------------------
def test_locked_baseline_dataset_db_and_classes_are_untouched(tmp_path):
    before = _db_hashes()
    assert _sha(DATASET) == DATASET_SHA256
    run.process_csv(imbalanced_csv(tmp_path, other={"Bot": 3}), other_attack=True, out_root=tmp_path)
    run.process_csv(DATASET, other_attack=True, out_root=tmp_path)          # read-only use
    assert _sha(DATASET) == DATASET_SHA256
    assert _db_hashes() == before
    classes = load_config().get("classes")
    assert sorted(classes) == sorted(STUDY_CLASSES) and OTHER_ATTACK not in classes
    assert OTHER_ATTACK not in (load_config().get("data_prep.label_map") or {}).values()


def test_cli_flags(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("ingest_cli", PROJECT_ROOT / "scripts" / "ingest.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main([str(SAMPLE), "--other-attack", "--max-flows", "12", "--no-write"]) == 0
    out = capsys.readouterr().out
    assert "Classes   6-class" in out and "label_aware_balanced" in out
    assert "Other Attack 2" in out
    assert cli.main([str(SAMPLE), "--label-blind", "--no-write"]) == 0
    out = capsys.readouterr().out
    assert "Classes   5-class" in out and "label_blind_statistical" in out
