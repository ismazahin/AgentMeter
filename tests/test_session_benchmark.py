"""Phase 38: input layer -> EXISTING 4-agent pipeline -> scoring + comparison.

CPU only, MOCK provider, no GPU/model/network. Sessions run through the real
runner (one `agentmeter.run.worker` subprocess per model, sequential); the
6-class and label-isolation checks also drive the real worker function in-process
with a scripted provider so prompts can be inspected.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3

import pytest
import yaml

from agentmeter.analysis import analyze
from agentmeter.config import PROJECT_ROOT, load_config
from agentmeter.ingest.csv_input import OTHER_ATTACK
from agentmeter.ingest.run import process_csv
from agentmeter.providers.base import GenerationResult
from agentmeter.providers.mock import MockProvider
from agentmeter.session import benchmark, scoring
from agentmeter.session.benchmark import SessionError, prepare_session, run_session

SAMPLE = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
DATASET = PROJECT_ROOT / "data" / "cicids_full_300.csv"
DATASET_SHA256 = "ef1b13787e2c87602033f54d985355515408b4f930c9648cd1fb29cb540392b5"
AGENTS = ["perceive", "reason", "decide", "act"]
BASE_CLASSES = load_config().get("classes")


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _db_hashes():
    return {str(p): _sha(p) for p in (PROJECT_ROOT / "results").glob("*.db")}


def _rows(db, sql):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


@pytest.fixture(scope="module")
def locked_before():
    return {"dbs": _db_hashes(), "dataset": _sha(DATASET)}


@pytest.fixture(scope="module")
def labelled(tmp_path_factory, locked_before):
    root = tmp_path_factory.mktemp("csv_runs")
    process_csv(SAMPLE, out_root=root, max_flows=15)
    run_dir = root / "cicids2017_sample"
    payload = run_session(run_dir, ["mock-a", "mock-b"], provider="mock")
    return run_dir, payload


@pytest.fixture(scope="module")
def six_class(tmp_path_factory, locked_before):
    root = tmp_path_factory.mktemp("csv6")
    process_csv(SAMPLE, out_root=root, other_attack=True, max_flows=12)
    run_dir = root / "cicids2017_sample"
    return run_dir, run_session(run_dir, ["mock-a", "mock-b"], provider="mock")


# --- labelled 5-class: the full flow through the existing runner --------------------------
def test_labelled_session_runs_both_models_through_the_existing_pipeline(labelled):
    run_dir, p = labelled
    db = run_dir / "session.db"
    assert db.exists() and (run_dir / "session_results.json").exists()
    assert _rows(db, "SELECT status FROM runs") == [("complete",)]
    assert _rows(db, "SELECT model, COUNT(*) FROM scenario_results GROUP BY model ORDER BY model") == [
        ("mock:mock-a", 15), ("mock:mock-b", 15)]
    # the existing instrumentation recorded all 4 agents for every flow and model
    assert _rows(db, "SELECT DISTINCT agent_name FROM agent_metrics ORDER BY agent_name") == sorted(
        (a,) for a in AGENTS)
    assert _rows(db, "SELECT COUNT(*) FROM agent_metrics") == [(2 * 15 * 4,)]

    s = p["session"]
    assert s["evaluation_mode"] == "accuracy_available" and s["labelled"] and s["non_validated"]
    assert s["class_set"] == BASE_CLASSES and s["class_scheme"] == "5-class"
    assert s["execution"].startswith("sequential")
    assert p["provenance"]["non_validated"] is True and p["provenance"]["run_kind"] == "user_run"
    for m in p["per_model"]:
        assert list(m["efficiency"]["per_agent"]) == AGENTS
        e = m["efficiency"]["end_to_end"]
        assert e["mean_latency_s"] > 0 and e["mean_tokens_per_flow"] > 0
        assert e["mean_peak_vram_mb"] is None                 # CPU: never fabricated
        assert m["accuracy"]["n"] == 15 and 0 <= m["accuracy"]["accuracy"] <= 1
        assert list(m["accuracy"]["confusion"]) == BASE_CLASSES
        assert m["saw"]["tier"] in ("Healthy", "Degraded", "Critical")
    assert set(p["phase7"]) == {"per_model", "per_class", "confusion"}
    assert {r["model"] for r in p["phase8"]["saw_table"]} == {"mock:mock-a", "mock:mock-b"}


def test_two_model_comparison_is_complete_and_honest(labelled):
    _, p = labelled
    c = p["comparison"]
    assert c["models"] == ["mock-a", "mock-b"]
    assert c["more_resource_efficient"]["model"] in ("mock-a", "mock-b", "tie")
    assert c["saw_basis"].startswith("full SAW") and len(c["saw_ranking"]) == 2
    assert c["saw_top"] in ("mock-a", "mock-b", "tie")
    assert set(c["accuracy"]["values"]) == {"mock-a", "mock-b"}
    assert {m["metric"] for m in c["metrics"]} == {"mean_latency_s", "mean_tokens_per_flow",
                                                   "mean_peak_vram_mb"}
    vram = next(m for m in c["metrics"] if m["metric"] == "mean_peak_vram_mb")
    assert vram["available"] is False                          # no fake VRAM winner on CPU
    caveats = " ".join(p["session"]["caveats"])
    for phrase in ("NON-VALIDATED", "Balanced sample", "MOCK provider", "No VRAM readings"):
        assert phrase in caveats
    from pathlib import Path
    json.loads((Path(p["session"]["run_dir"]) / "session_results.json").read_text())  # strict JSON


def test_five_class_session_config_is_the_base_config_classes(labelled):
    run_dir, _ = labelled
    cfg = yaml.safe_load((run_dir / "session_config.yaml").read_text())
    assert cfg["classes"] == BASE_CLASSES and OTHER_ATTACK not in cfg.get("mitre", {})
    assert cfg["storage"]["sqlite_path"] == str(run_dir / "session.db")
    assert cfg["run"]["models"] == ["mock-a", "mock-b"]


def test_scoring_reuses_analyze_saw(labelled, monkeypatch):
    run_dir, p = labelled
    calls = {"composite": 0, "tier": 0, "phase7": 0}
    for name in calls:
        real = getattr(analyze, {"composite": "_composite", "tier": "_tier", "phase7": "phase7"}[name])

        def spy(*a, _real=real, _n=name, **k):
            calls[_n] += 1
            return _real(*a, **k)
        monkeypatch.setattr(analyze, {"composite": "_composite", "tier": "_tier", "phase7": "phase7"}[name], spy)
    plan = prepare_session(run_dir, ["mock-a", "mock-b"], provider="mock")
    again = scoring.score_session(plan, run_id=p["session"]["run_id"])
    assert calls["composite"] > 0 and calls["tier"] > 0 and calls["phase7"] == 1
    assert again["per_model"][0]["saw"] == p["per_model"][0]["saw"]


# --- 6-class -------------------------------------------------------------------------
def test_six_class_session_scores_over_six_classes(six_class):
    run_dir, p = six_class
    six = BASE_CLASSES + [OTHER_ATTACK]
    cfg = yaml.safe_load((run_dir / "session_config.yaml").read_text())
    assert cfg["classes"] == six and cfg["mitre"][OTHER_ATTACK] == benchmark.OTHER_ATTACK_MITRE
    assert p["session"]["class_set"] == six and p["session"]["class_scheme"] == "6-class"
    for m in p["per_model"]:
        assert list(m["accuracy"]["confusion"]) == six
        oa = next(r for r in m["accuracy"]["per_class"] if r["class"] == OTHER_ATTACK)
        assert oa["support"] == 2
    assert "6-class run" in " ".join(p["session"]["caveats"])


class ScriptedProvider(MockProvider):
    """Mock that records every prompt and answers Decide with a fixed label."""

    prompts: list[tuple[str, str]] = []

    def __init__(self, config, decide_answer="Other Attack"):
        super().__init__(config)
        self.decide_answer = decide_answer

    def generate(self, prompt, system=None, max_new_tokens=None):
        role = next((r for r in AGENTS if r in (system or "").lower()), "?")
        ScriptedProvider.prompts.append((role, prompt))
        if role == "decide":
            return GenerationResult(text=self.decide_answer, input_tokens=5, output_tokens=2)
        return super().generate(prompt, system=system, max_new_tokens=max_new_tokens)


def _run_worker_in_process(plan, model, monkeypatch, answer):
    """Drive the EXISTING worker function in-process with a scripted provider."""
    import agentmeter.providers as providers
    from agentmeter.db.storage import Storage
    from agentmeter.run import worker

    ScriptedProvider.prompts = []
    monkeypatch.setattr(providers, "get_provider", lambda cfg: ScriptedProvider(cfg, answer))
    st = Storage(plan["db_path"])
    run_id = "run_inproc"
    st.create_run(run_id, "fp", "none", "cpu", notes="test")
    st.close()
    worker._run_model_sqlite(load_config(plan["config_path"]), model, run_id, None)
    st = Storage(plan["db_path"])
    st.finish_run(run_id)
    st.close()
    return run_id


def test_decide_offers_and_accepts_other_attack_in_a_6_class_run(tmp_path, monkeypatch):
    process_csv(SAMPLE, out_root=tmp_path, other_attack=True, max_flows=12)
    plan = prepare_session(tmp_path / "cicids2017_sample", ["scripted"], provider="mock")
    run_id = _run_worker_in_process(plan, "scripted", monkeypatch, answer="Other Attack")
    decide = [p for r, p in ScriptedProvider.prompts if r == "decide"]
    assert decide and all(OTHER_ATTACK in p.split("\n")[0] for p in decide)   # in the allowed list
    preds = _rows(plan["db_path"], "SELECT DISTINCT predicted_label FROM scenario_results")
    assert preds == [(OTHER_ATTACK,)]                                          # accepted, not Unparseable
    res = scoring.score_session(plan, run_id=run_id)
    pc = {r["class"]: r for r in res["per_model"][0]["accuracy"]["per_class"]}
    assert pc[OTHER_ATTACK]["accuracy"] == 1.0 and pc["Benign"]["accuracy"] == 0.0
    assert res["per_model"][0]["accuracy"]["accuracy"] == pytest.approx(2 / 12)


def test_five_class_decide_prompt_is_unchanged(tmp_path, monkeypatch):
    process_csv(SAMPLE, out_root=tmp_path, max_flows=10)
    plan = prepare_session(tmp_path / "cicids2017_sample", ["scripted"], provider="mock")
    _run_worker_in_process(plan, "scripted", monkeypatch, answer="Other Attack")
    decide = [p for r, p in ScriptedProvider.prompts if r == "decide"]
    assert decide and all(p.split("\n")[0] == "Allowed labels (copy one verbatim): "
                          + " | ".join(BASE_CLASSES) for p in decide)
    # In a 5-class run "Other Attack" is not a class: Decide's answer parses as
    # Unparseable and the existing Act fallback decides — it can never be stored.
    preds = {r[0] for r in _rows(plan["db_path"], "SELECT predicted_label FROM scenario_results")}
    assert OTHER_ATTACK not in preds and preds <= set(BASE_CLASSES) | {"Unparseable"}


def test_model_never_sees_labels_ids_or_identification_columns(tmp_path, monkeypatch):
    process_csv(SAMPLE, out_root=tmp_path, other_attack=True, max_flows=12)
    run_dir = tmp_path / "cicids2017_sample"
    plan = prepare_session(run_dir, ["scripted"], provider="mock")
    _run_worker_in_process(plan, "scripted", monkeypatch, answer="Benign")
    import pandas as pd
    labels = pd.read_csv(run_dir / "labels.csv")
    secrets = set(labels["label"]) | set(labels["label_raw"]) | set(labels["flow_id"])
    for role, prompt in ScriptedProvider.prompts:
        if role in ("perceive", "reason"):          # flow-only prompts (Decide lists allowed labels)
            assert not any(s in prompt for s in secrets), role
            assert "label" not in prompt.lower() and "src_ip" not in prompt
    scen = pd.read_csv(run_dir / "session_scenarios.csv")
    assert list(scen.columns) == ["flow_id"] + json.loads((run_dir / "input.json").read_text())[
        "feature_columns"] + ["label"]


# --- PCAP: efficiency only ---------------------------------------------------------------
def test_pcap_session_is_efficiency_only(tmp_path):
    pytest.importorskip("scapy")
    pytest.importorskip("cicflowmeter")
    from agentmeter.ingest.run import process_pcap
    from agentmeter.ingest.sample import write_sample_pcap

    cap = write_sample_pcap(tmp_path / "cap.pcap")
    process_pcap(cap, out_root=tmp_path / "pcap_runs", max_flows=8)
    run_dir = tmp_path / "pcap_runs" / "cap"
    p = run_session(run_dir, ["mock-a"], provider="mock")
    s = p["session"]
    assert s["evaluation_mode"] == "efficiency_only" and not s["accuracy_available"]
    assert s["source_type"] == "pcap" and "phase7" not in p
    m = p["per_model"][0]
    assert m["accuracy"] is None and list(m["efficiency"]["per_agent"]) == AGENTS
    assert m["saw"] == m["saw_efficiency_only"]
    assert p["phase8"]["weights"]["accuracy"] == 0.0
    assert abs(sum(p["phase8"]["weights"].values()) - 1.0) < 1e-9
    assert p["comparison"] is None and p["statistics"] == {"skipped": "statistics need 2 models"}
    assert any(c.startswith("Efficiency only") for c in s["caveats"])
    assert any("PCAP features are approximate" in c for c in s["caveats"])


# --- rejections + safety -------------------------------------------------------------------
@pytest.mark.parametrize("models, msg", [
    (["a", "b", "c"], "at most 2 models"),
    (["a", "a"], "duplicate"),
    ([], "choose 1 or 2"),
])
def test_model_count_is_enforced_before_anything_runs(labelled, models, msg):
    run_dir, _ = labelled
    with pytest.raises(SessionError, match=msg):
        run_session(run_dir, models, provider="mock")


def test_cli_rejects_three_models(labelled, capsys):
    spec = importlib.util.spec_from_file_location("bench_cli", PROJECT_ROOT / "scripts" / "benchmark.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    run_dir, _ = labelled
    assert cli.main([str(run_dir), "--models", "a", "b", "c", "--provider", "mock"]) == 2
    assert "at most 2 models" in capsys.readouterr().err


def test_refuses_to_write_into_the_locked_study_db(tmp_path, monkeypatch):
    process_csv(SAMPLE, out_root=tmp_path, max_flows=5)
    run_dir = tmp_path / "cicids2017_sample"
    monkeypatch.setattr(analyze, "LOCKED_STUDY_DB", (run_dir / "session.db").resolve())
    with pytest.raises(SessionError, match="locked study DB"):
        prepare_session(run_dir, ["mock-a"], provider="mock")


def test_canonical_models_are_recognised(tmp_path):
    process_csv(SAMPLE, out_root=tmp_path, max_flows=5)
    plan = prepare_session(tmp_path / "cicids2017_sample",
                           ["Qwen/Qwen2.5-7B-Instruct", "someone/pulled-model"], provider="mock")
    assert [m["canonical"] for m in plan["models"]] == [True, False]


def test_locked_study_untouched(labelled, six_class, locked_before):
    assert _db_hashes() == locked_before["dbs"]
    assert _sha(DATASET) == locked_before["dataset"] == DATASET_SHA256


# --- Phase 39: relative comparison beside the (unchanged) absolute SAW -----------------------
def test_relative_comparison_sits_beside_the_unchanged_absolute_output(labelled):
    _, p = labelled
    # absolute output: same keys as Phase 38
    assert set(p["comparison"]) == {"models", "metrics", "more_resource_efficient", "saw_basis",
                                    "saw_ranking", "accuracy", "saw_top", "statement"}
    assert {r["model"] for r in p["phase8"]["saw_table"]} == {"mock:mock-a", "mock:mock-b"}
    rel = p["relative_comparison"]
    assert rel["models"] == ["mock-a", "mock-b"] and rel["accuracy_included"] is True
    assert {m["metric"] for m in rel["metrics"]} == {"mean_latency_s", "mean_peak_vram_mb",
                                                     "mean_tokens_per_flow", "accuracy"}
    assert rel["efficiency_verdict"]["verdict"] in ("more_efficient", "mixed", "tie")
    assert rel["verdict"] and "Accuracy:" in rel["verdict"]
    # identical mock models: tokens and accuracy are exactly equal -> ties, never a winner
    tok = next(m for m in rel["metrics"] if m["metric"] == "mean_tokens_per_flow")
    acc = next(m for m in rel["metrics"] if m["metric"] == "accuracy")
    assert tok["winner"] == "tie" and acc["winner"] == "tie"
    assert next(m for m in rel["metrics"] if m["metric"] == "mean_peak_vram_mb")["available"] is False
    assert all(c in rel["caveats"] for c in p["session"]["caveats"])


def test_relative_comparison_absent_for_one_model(tmp_path):
    process_csv(SAMPLE, out_root=tmp_path, max_flows=5)
    p = run_session(tmp_path / "cicids2017_sample", ["mock-a"], provider="mock")
    assert p["relative_comparison"] is None and p["comparison"] is None

