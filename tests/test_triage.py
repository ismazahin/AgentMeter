"""Triage: the policy table, the verdict's priority and the triage metrics."""
from pathlib import Path

import pytest
import yaml

from agentmeter.config import PROJECT_ROOT, load_config
from agentmeter.pipeline.agents import make_verdict
from agentmeter.pipeline.triage import (DEFAULT_TRIAGE, DISMISS, ESCALATE, INVESTIGATE, priority_of,
                                        triage_block, triage_table, validate_triage)
from agentmeter.session.analyses import settings_fingerprint, settings_of

CLASSES = ["Brute Force", "Volumetric DDoS", "Port Scanning", "DoS Hulk", "Benign"]


def test_every_config_has_the_same_complete_policy():
    for p in [PROJECT_ROOT / "config.yaml", *sorted((PROJECT_ROOT / "configs").glob("*.yaml"))]:
        data = yaml.safe_load(p.read_text())
        if "classes" not in data:
            continue
        assert data.get("triage") == DEFAULT_TRIAGE, p.name     # code fallback == configs
        validate_triage(data["triage"], data["classes"])


def test_the_agreed_policy():
    assert DEFAULT_TRIAGE["Brute Force"] == ESCALATE
    assert DEFAULT_TRIAGE["Volumetric DDoS"] == DEFAULT_TRIAGE["DoS Hulk"] == ESCALATE
    assert DEFAULT_TRIAGE["Port Scanning"] == INVESTIGATE and DEFAULT_TRIAGE["Benign"] == DISMISS


def test_an_unknown_or_unparseable_answer_is_never_dismissed():
    assert priority_of("Unparseable", DEFAULT_TRIAGE) == INVESTIGATE
    assert priority_of(None, DEFAULT_TRIAGE) == INVESTIGATE
    assert priority_of("Something else", DEFAULT_TRIAGE) == INVESTIGATE


@pytest.mark.parametrize("table, msg", [
    ({c: ESCALATE for c in CLASSES[:-1]}, "no priority for class"),
    ({**DEFAULT_TRIAGE, "Benign": "Ignore"}, "priority must be one of"),
    (["Escalate"], "must map"),
])
def test_a_bad_policy_is_refused(table, msg):
    with pytest.raises(ValueError, match=msg):
        validate_triage(table, CLASSES)


def test_a_config_with_a_bad_policy_does_not_load(tmp_path):
    data = yaml.safe_load((PROJECT_ROOT / "configs" / "run_full_mock.yaml").read_text())
    data["triage"].pop("Benign")
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="Benign"):
        load_config(p)


def test_the_verdict_carries_the_policy_priority():
    cfg = load_config(PROJECT_ROOT / "configs" / "run_full_mock.yaml")
    assert make_verdict("Port Scanning", "", cfg)["priority"] == INVESTIGATE
    assert make_verdict("Benign", "", cfg)["priority"] == DISMISS
    assert make_verdict("", "no idea", cfg)["priority"] == INVESTIGATE      # Unparseable


def test_a_config_without_a_table_uses_the_default():
    assert triage_table({}) == DEFAULT_TRIAGE
    assert triage_table({"triage": {"Benign": DISMISS}}) == {"Benign": DISMISS}


def _rows(pairs, model="mock:a"):
    return [{"model": model, "scenario_id": f"s{i}", "predicted_label": p, "held_out_label": t}
            for i, (t, p) in enumerate(pairs)]


def test_triage_metrics_by_hand():
    sr = _rows([("Brute Force", "Brute Force"),      # Escalate, right
                ("Brute Force", "Benign"),           # attack dismissed: missed
                ("Port Scanning", "Port Scanning"),  # Investigate, right
                ("Benign", "Benign"),                # Dismiss, right
                ("Benign", "DoS Hulk"),              # benign escalated: false escalation
                ("Benign", "Unparseable")])          # Investigate (never dismissed silently)
    cost = {"per_model": [{"model": "a", "cost_per_1k_flows_usd": 0.6, "wh_per_flow": 2.0}]}
    m = triage_block(sr, {"a": "mock:a"}, DEFAULT_TRIAGE, labelled=True, cost=cost)["per_model"][0]
    assert m["queue"] == {ESCALATE: 2, INVESTIGATE: 2, DISMISS: 2}
    assert (m["n_attacks"], m["n_benign"]) == (3, 3)
    assert m["missed_attacks"] == 1 and m["missed_attack_rate"] == pytest.approx(1 / 3)
    assert m["false_escalations"] == 1 and m["false_escalation_rate"] == pytest.approx(1 / 3)
    assert m["attacks_caught"] == 2
    assert m["priority_accuracy"] == pytest.approx(3 / 6)
    assert m["confusion"][ESCALATE][DISMISS] == 1 and m["confusion"][DISMISS][ESCALATE] == 1
    # $0.6 per 1,000 flows x 6 flows / 2 attacks caught; 2 Wh x 6 / 2
    assert m["cost_per_attack_caught_usd"] == pytest.approx(0.0018)
    assert m["wh_per_attack_caught"] == pytest.approx(6.0)


def test_unlabelled_input_shows_only_the_queue():
    sr = _rows([("UNLABELLED", "Benign"), ("UNLABELLED", "Brute Force")])
    t = triage_block(sr, {"a": "mock:a"}, DEFAULT_TRIAGE, labelled=False)
    m = t["per_model"][0]
    assert m["queue"][DISMISS] == 1 and m["queue"][ESCALATE] == 1
    assert "missed_attacks" not in m and t["labelled"] is False


def test_the_policy_and_prompt_version_enter_the_fingerprint_only_when_recorded():
    old = {"classes": CLASSES, "pipeline": {"agents": ["perceive"]}}
    new = {**old, "triage": DEFAULT_TRIAGE, "pipeline": {"agents": ["perceive"], "prompt_version": "triage-1"}}
    assert "triage" not in settings_of(old) and "pipeline.prompt_version" not in settings_of(old)
    assert settings_fingerprint(settings_of(old)) != settings_fingerprint(settings_of(new))
    other = {**new, "triage": {**DEFAULT_TRIAGE, "Brute Force": INVESTIGATE}}
    assert settings_fingerprint(settings_of(other)) != settings_fingerprint(settings_of(new))


# ---------------------------------------------------------------- a real (mock) session
def test_a_session_records_its_policy_and_reports_triage(tmp_path):
    import json

    from agentmeter.ingest.run import process_csv
    from agentmeter.session.benchmark import run_session
    sample = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
    process_csv(sample, out_root=tmp_path, max_flows=8)
    run_dir = tmp_path / "cicids2017_sample"
    p = run_session(run_dir, ["mock-a"], provider="mock")
    cfg = yaml.safe_load((run_dir / "session_config.yaml").read_text())
    assert cfg["pipeline"]["prompt_version"] == "triage-1" and cfg["triage"]["Benign"] == DISMISS
    t = p["triage"]
    m = t["per_model"][0]
    assert t["labelled"] is True and sum(m["queue"].values()) == m["n_flows"] == 8
    assert m["n_attacks"] + m["n_benign"] == 8 and m["missed_attacks"] <= m["n_attacks"]
    st = p["session_identity"]["settings"]
    assert st["pipeline.prompt_version"] == "triage-1" and st["triage"] == cfg["triage"]
    flows = json.loads(next(run_dir.glob("flows_*.json")).read_text())
    f = flows["flows"][0]
    assert f["truth_priority"] in (ESCALATE, INVESTIGATE, DISMISS)
    assert all(pm["priority"] in (ESCALATE, INVESTIGATE, DISMISS) for pm in f["per_model"].values())


# ---------------------------------------------------------------- severity and the triage gate
from agentmeter.pipeline.triage import (DEFAULT_GATE, DEFAULT_SEVERITY, SNORT_CLASSTYPE_PRIORITY,  # noqa: E402
                                        apply_gate, severity_of, validate_gate, validate_severity)


def test_every_config_has_the_same_severity_and_gate():
    for p in [PROJECT_ROOT / "config.yaml", *sorted((PROJECT_ROOT / "configs").glob("*.yaml"))]:
        data = yaml.safe_load(p.read_text())
        if "classes" not in data:
            continue
        assert data.get("severity") == DEFAULT_SEVERITY, p.name
        assert {k: data["triage_gate"][k] for k in data["triage_gate"]} == \
            {k: v for k, v in DEFAULT_GATE.items() if k != "basis"}, p.name
        assert data["scoring"]["targets_source"] == "placeholder", p.name


def test_severity_comes_from_the_snort_classtype_priority():
    assert severity_of("Brute Force", DEFAULT_SEVERITY) == {"classtype": "attempted-user", "priority": 1, "level": "High"}
    assert severity_of("DoS Hulk", DEFAULT_SEVERITY)["level"] == "Medium"
    assert severity_of("Port Scanning", DEFAULT_SEVERITY)["level"] == "Medium"
    assert severity_of("Benign", DEFAULT_SEVERITY)["level"] == "Low"
    assert severity_of("Unparseable", DEFAULT_SEVERITY) is None
    assert set(DEFAULT_SEVERITY.values()) <= set(SNORT_CLASSTYPE_PRIORITY)


def test_a_bad_severity_or_gate_is_refused():
    with pytest.raises(ValueError, match="classtype must be one of"):
        validate_severity({**DEFAULT_SEVERITY, "Benign": "harmless"}, CLASSES)
    with pytest.raises(ValueError, match="no classtype"):
        validate_severity({"Benign": "not-suspicious"}, CLASSES)
    with pytest.raises(ValueError, match="between 0 and 1"):
        validate_gate({"max_missed_attack_rate": 15})


def test_the_verdict_carries_severity():
    cfg = load_config(PROJECT_ROOT / "configs" / "run_full_mock.yaml")
    assert make_verdict("Brute Force", "", cfg)["severity"]["level"] == "High"


def _gated(pairs, valid):
    t = triage_block(_rows(pairs), {"a": "mock:a"}, DEFAULT_TRIAGE, labelled=True)
    apply_gate(t, {"per_model": [{"model": "a", "valid_rate": valid}]}, DEFAULT_GATE)
    return t


def test_the_gate_passes_a_model_within_every_limit():
    pairs = [("Brute Force", "Brute Force")] * 9 + [("Brute Force", "Benign")] + [("Benign", "Benign")] * 10
    g = _gated(pairs, 1.0)["per_model"][0]["gate"]
    assert g["passed"] is True and g["failed"] == []         # 10% missed <= 15%, 0 false escalations


def test_the_gate_names_every_limit_a_model_fails():
    pairs = [("Brute Force", "Benign")] * 5 + [("Brute Force", "Brute Force")] * 5 + [("Benign", "DoS Hulk")] * 5
    g = _gated(pairs, 0.90)["per_model"][0]["gate"]
    assert g["passed"] is False
    assert g["failed"] == ["missed_attack_rate", "false_escalation_rate", "valid_rate"]


def test_unlabelled_input_is_not_checked_by_the_gate():
    t = triage_block(_rows([("UNLABELLED", "Benign")]), {"a": "mock:a"}, DEFAULT_TRIAGE, labelled=False)
    apply_gate(t, {"per_model": [{"model": "a", "valid_rate": 1.0}]}, DEFAULT_GATE)
    g = t["per_model"][0]["gate"]
    assert g["passed"] is None and g["not_checked"] == ["missed_attack_rate", "false_escalation_rate"]


def test_the_pdf_recommendation_states_the_gate():
    from agentmeter.session.pdf_report import gate_line
    pairs = [("Brute Force", "Benign")] * 5 + [("Benign", "Benign")] * 5
    line = gate_line({"triage": _gated(pairs, 1.0)})
    assert "not recommended for triage as-is" in line and "missed attacks" in line


# ---------------------------------------------------------------- the corrected CIC-IDS2017
def test_attempted_flows_of_the_corrected_dataset_are_excluded(tmp_path):
    import pandas as pd

    from agentmeter.ingest.csv_input import is_attempted, load_csv
    assert is_attempted("DoS Hulk - Attempted") and is_attempted("FTP-Patator - attempted")
    assert not is_attempted("DoS Hulk") and not is_attempted("BENIGN")
    src = pd.read_csv(PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv")
    col = next(c for c in src.columns if c.strip() == "Label")
    src.loc[src.index[:3], col] = "DoS Hulk - Attempted"
    src.loc[src.index[3:5], col] = "Portscan"                 # the corrected set's spelling
    p = tmp_path / "corrected.csv"
    src.to_csv(p, index=False)
    for other in (False, True):                               # never "Other Attack", even when on
        r = load_csv(p, other_attack=other)
        assert r.report["label"]["excluded_attempted"] == 3
        assert not r.labels["label_raw"].str.contains("Attempted").any()
        assert (r.labels["label_raw"] == "Portscan").sum() >= 1          # read case-insensitively
        assert set(r.labels.loc[r.labels["label_raw"] == "Portscan", "label"]) == {"Port Scanning"}


# ---------------------------------------------------------------- calibrating the SAW targets
def test_calibration_proposes_and_writes_targets(tmp_path):
    import json
    import sys
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import calibrate_targets as C
    res = {"environment": {"provider": "real", "gpu_name": "NVIDIA L4"},
           "phase8": {"raw_criteria": [
               {"model": "a", "latency_s": 2.0, "vram_mb": 6000.0, "tokens_total": 900.0},
               {"model": "b", "latency_s": 4.0, "vram_mb": 9000.0, "tokens_total": 700.0},
               {"model": "c", "latency_s": 3.0, "vram_mb": float("nan"), "tokens_total": 800.0}]}}
    f = tmp_path / "session_results.json"
    f.write_text(json.dumps(res))
    per, gpus = C.collect([f])
    assert C.propose(per, "best") == {"latency_s": 2.0, "vram_mb": 6000.0, "tokens_total": 700.0}
    assert C.propose(per, "median") == {"latency_s": 3.0, "vram_mb": 7500.0, "tokens_total": 800.0}
    cfg = tmp_path / "config.yaml"
    cfg.write_text((PROJECT_ROOT / "configs" / "run_full_mock.yaml").read_text())
    assert C.main([str(f), "--write", str(cfg)]) == 0
    d = yaml.safe_load(cfg.read_text())
    assert d["scoring"]["targets"]["latency_s"] == 2.0 and d["scoring"]["targets"]["accuracy_pct"] == 80.0
    assert d["scoring"]["targets_source"].startswith("calibrated") and "NVIDIA L4" in d["scoring"]["targets_source"]
    load_config(cfg)                                           # still a valid config


def test_calibration_refuses_mock_sessions(tmp_path):
    import json
    import sys
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import calibrate_targets as C
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"environment": {"provider": "mock"}, "phase8": {"raw_criteria": []}}))
    with pytest.raises(SystemExit, match="not a real GPU session"):
        C.collect([f])
