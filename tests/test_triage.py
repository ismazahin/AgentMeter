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
