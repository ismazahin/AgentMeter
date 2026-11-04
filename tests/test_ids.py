"""The IDS gate: training, loading, the session stage, the analysis and the job option."""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd
import pytest

from agentmeter.config import PROJECT_ROOT
from agentmeter.ids.analysis import ids_block
from agentmeter.ids.model import IdsError, load_model
from agentmeter.ids.stage import IDS_JSON, IDS_RESULTS_CSV, normalize_ids_option
from agentmeter.pipeline.triage import DEFAULT_TRIAGE

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import train_ids as T  # noqa: E402

DEMO = PROJECT_ROOT / "models" / "ids" / "demo_ids.joblib"
SAMPLE = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    out = tmp_path_factory.mktemp("ids") / "m"
    assert T.main([str(PROJECT_ROOT / "data" / "cicids_full_300.csv"), "--out", str(out), "--trees", "20",
                   "--holdout-max", "1000"]) == 0
    return out


def test_training_writes_a_model_its_metadata_hashes_and_a_holdout(trained):
    meta = json.loads(trained.with_suffix(".json").read_text())
    assert meta["algorithm"].startswith("RandomForestClassifier") and 0 < meta["threshold"] <= 1
    assert meta["metrics"]["validation"]["recall"] >= 0.99            # the threshold rule on validation
    d = meta["data"]
    assert d["dedupe"]["rows_after"] <= d["dedupe"]["rows"]
    hold = pd.read_csv(trained.parent / d["holdout_csv"])
    assert "Label" in hold.columns and len(hold) == sum(c["holdout"] for c in d["classes"].values())
    m = load_model(trained.with_suffix(".joblib"))
    assert m.overlap(hold) == 0                                      # hold-out flows are not training flows
    assert m.features == meta["features"]


def test_a_changed_model_file_is_refused(trained, tmp_path):
    import shutil
    for ext in (".joblib", ".json"):
        shutil.copy(trained.with_suffix(ext), (tmp_path / "x").with_suffix(ext))
    with open(tmp_path / "x.joblib", "ab") as fh:
        fh.write(b"tampered")
    with pytest.raises(IdsError, match="sha256"):
        load_model(tmp_path / "x.joblib")
    with pytest.raises(IdsError, match="no IDS model"):
        load_model(tmp_path / "missing.joblib")


def test_the_threshold_keeps_the_target_recall():
    scores = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95])
    attack = np.array([False, False, True, True, True, True, True, True, True, True])
    tau = T.choose_threshold(scores, attack, 0.99)
    assert tau == 0.3 and (scores[attack] >= tau).mean() == 1.0
    tau = T.choose_threshold(scores, attack, 0.75)                     # 2 of 8 attacks may fall below
    assert tau == 0.5 and (scores[attack] >= tau).mean() == 0.75


def test_ambiguous_and_duplicate_flows_are_removed():
    from agentmeter.ingest.feature_map import CIC_FEATURES
    row = {c: 1.0 for c in CIC_FEATURES}
    df = pd.DataFrame([{**row, "label": "Benign"}, {**row, "label": "DoS Hulk"},          # ambiguous
                       {**row, "Flow Duration": 2.0, "label": "Benign"},
                       {**row, "Flow Duration": 2.0, "label": "Benign"},                   # duplicate
                       {**row, "Flow Duration": 3.0, "label": "DoS Hulk"}])
    out, info = T.dedupe(df.assign(label_raw=df["label"]))
    assert len(out) == 2 and info["ambiguous_dropped"] == 2 and info["duplicates_dropped"] == 1


@pytest.mark.parametrize("opt, want", [(None, None), (False, None), (True, {"threshold": None}),
                                       ({"threshold": 0.4}, {"threshold": 0.4})])
def test_the_ids_option(opt, want):
    assert normalize_ids_option(opt) == want


@pytest.mark.parametrize("bad", [{"threshold": 0}, {"threshold": 1.5}, {"threshold": "x"}, "yes", 3])
def test_a_bad_ids_option_is_refused(bad):
    with pytest.raises(IdsError):
        normalize_ids_option(bad)


def test_the_bundled_demo_model_loads():
    m = load_model(DEMO)
    assert m.meta["demo"] is True and m.identity["sha256"]


# ---------------------------------------------------------------- the analysis, by hand
def test_ids_block_by_hand():
    ids_df = pd.DataFrame({"flow_id": ["a", "b", "c", "d", "e", "f"],
                           "score": [0.9, 0.8, 0.7, 0.6, 0.2, 0.1],
                           "flagged": [True, True, True, True, False, False],
                           "label": ["DoS Hulk", "Brute Force", "Benign", "Benign", "Port Scanning", "Benign"]})
    sr = [{"model": "mock:x", "scenario_id": f, "predicted_label": p}
          for f, p in [("a", "DoS Hulk"), ("b", "Benign"), ("c", "Benign"), ("d", "Port Scanning")]]
    info = {"threshold": 0.5, "labelled": True, "scoring_s_per_flow": 0.001, "model": {"name": "t"}}
    cost = {"price": {"usd_per_hour": 3.6}, "per_model": [{"model": "x", "mean_latency_s": 2.0, "wh_per_flow": 0.5}]}
    b = ids_block(info, ids_df, sr, {"x": "mock:x"}, DEFAULT_TRIAGE, cost=cost)
    i = b["ids"]
    assert (i["tp"], i["fp"], i["fn"], i["tn"]) == (2, 2, 1, 1) and i["recall"] == pytest.approx(2 / 3)
    assert b["n_alerts"] == 4 and b["llm_calls_saved_share"] == pytest.approx(2 / 6)
    m = b["per_model"][0]
    assert m["false_alarms"] == 2 and m["false_alarms_dismissed"] == 1        # c dismissed, d investigated
    assert m["attacks_flagged"] == 2 and m["attacks_dismissed_by_llm"] == 1   # b dismissed
    assert m["missed_by_ids"] == 1 and m["missed_total"] == 2 and m["missed_total_rate"] == pytest.approx(2 / 3)
    assert m["analyst_queue"] == 2                                             # a escalated, d investigated
    p = m["per_1k_incoming"]
    assert p["time_s"] == pytest.approx(1000 * (0.001 + 4 / 6 * 2.0))
    assert p["cost_usd"] == pytest.approx(p["time_s"] / 3600 * 3.6)
    assert p["energy_wh"] == pytest.approx(1000 * 4 / 6 * 0.5)
    hi = next(c for c in m["tau_curve"] if c["threshold"] == 0.75)            # only a, b remain alerts
    assert hi["alerts"] == 2 and hi["missed_total_rate"] == pytest.approx(2 / 3)
    assert all(c["threshold"] >= 0.5 for c in b["ids_curve"])


# ---------------------------------------------------------------- a real (mock) session
@pytest.fixture(scope="module")
def session(tmp_path_factory):
    from agentmeter.ingest.run import process_csv
    from agentmeter.session.benchmark import run_session
    root = tmp_path_factory.mktemp("ids_runs")
    process_csv(SAMPLE, out_root=root, max_flows=30)
    run_dir = root / "cicids2017_sample"
    return run_dir, run_session(run_dir, ["mock-a"], provider="mock", ids=True)


def test_a_session_with_the_ids_triages_only_the_flagged_flows(session):
    import yaml
    run_dir, p = session
    ids_df = pd.read_csv(run_dir / IDS_RESULTS_CSV)
    assert len(ids_df) == 30 and p["ids"]["n_flows_in"] == 30
    assert p["session"]["n_flows"] == int(ids_df["flagged"].sum()) == p["ids"]["n_alerts"]
    cfg = yaml.safe_load((run_dir / "session_config.yaml").read_text())
    assert cfg["ids"]["demo"] is True and cfg["ids"]["threshold"] == p["ids"]["threshold"]
    assert p["session_identity"]["settings"]["ids"]["sha256"] == load_model(DEMO).identity["sha256"]
    assert any(c.startswith("Demo IDS") for c in p["session"]["caveats"])
    flows = json.loads(next(run_dir.glob("flows_*.json")).read_text())
    assert all(f["ids_score"] >= p["ids"]["threshold"] for f in flows["flows"])


def test_a_later_session_without_the_ids_drops_its_files(session):
    from agentmeter.session.benchmark import prepare_session
    run_dir, _ = session
    plan = prepare_session(run_dir, ["mock-a"], provider="mock")
    assert plan["ids"] is None and plan["n_flows"] == 30
    assert not (run_dir / IDS_JSON).exists() and not (run_dir / IDS_RESULTS_CSV).exists()


def test_an_ids_that_flags_nothing_is_refused(session, monkeypatch):
    from agentmeter.ids.model import IdsModel
    from agentmeter.session.benchmark import SessionError, prepare_session
    run_dir, _ = session
    monkeypatch.setattr(IdsModel, "score", lambda self, df: (np.zeros(len(df)), 0.0))
    with pytest.raises(SessionError, match="flagged none"):
        prepare_session(run_dir, ["mock-a"], provider="mock", ids=True)


# ---------------------------------------------------------------- the job API
def _app():
    import serve
    return serve.create_app(mode="mock")


def test_the_job_api_refuses_a_bad_option_and_a_missing_model(monkeypatch, tmp_path):
    c = _app().test_client()
    r = c.post("/api/jobs", json={"run": "csv_runs/x", "models": ["mock-a"], "ids": {"threshold": 7}})
    assert r.status_code == 400 and "threshold" in r.get_json()["error"]
    monkeypatch.setenv("AGENTMETER_IDS_MODEL", str(tmp_path / "none.joblib"))
    r = c.post("/api/jobs", json={"run": "csv_runs/x", "models": ["mock-a"], "ids": True})
    assert r.status_code == 409 and r.get_json()["code"] == "no_ids_model"
    assert c.get("/health").get_json()["ids"] is None
