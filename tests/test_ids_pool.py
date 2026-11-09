"""The traffic analysis without a GPU: the IDS scores every candidate-pool flow at preparation.

Design: docs/superpowers/specs/2026-11-09-gpu-free-analyse-traffic-design.md. CPU only; uses the
bundled demo IDS model (models/ids/demo_ids.*).
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from agentmeter.config import PROJECT_ROOT
from agentmeter.ids.stage import (IDS_IDENTITY_CSV, IDS_POOL_JSON, IDS_SCORES_CSV, apply_ids,
                                  score_pool)
from agentmeter.server import prepare as P

DEMO = PROJECT_ROOT / "models" / "ids" / "demo_ids.joblib"
SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
SAMPLE_PCAP = PROJECT_ROOT / "data" / "sample_pcaps" / "sample_small.pcap"
LIMITS = {"max_csv_rows": 500000, "max_pcap_packets": 100000, "max_pcap_flows": 20000, "pcap_windows": 10}


def _prepare(tmp_path, src, name, ids):
    sid = P.prepare_file(src, source={"filename": src.name}, results_root=tmp_path / "results", name=name,
                         max_flows=10, other_attack=False, limits=LIMITS, ids=ids)
    return tmp_path / "results" / sid


@pytest.fixture(scope="module")
def csv_set(tmp_path_factory):
    return _prepare(tmp_path_factory.mktemp("csv"), SAMPLE_CSV, "with_ids", DEMO)


def test_every_pool_flow_is_scored_and_the_selected_ones_are_marked(csv_set):
    scores = pd.read_csv(csv_set / IDS_SCORES_CSV, dtype={"flow_id": str})
    info = json.loads((csv_set / IDS_POOL_JSON).read_text())
    selected = pd.read_csv(csv_set / "features.csv", dtype={"flow_id": str})["flow_id"]
    pool_size = json.loads((csv_set / "input.json").read_text())["rows_total"]
    assert len(scores) == pool_size == info["n_flows_in"] > len(selected)
    assert set(scores.loc[scores["selected"], "flow_id"]) == set(selected)
    assert info["n_selected"] == len(selected) and info["scope"] == "pool"
    assert scores["score"].between(0, 1).all() and scores["label"].notna().all()     # labelled CSV
    assert info["n_flagged"] == int((scores["score"] >= info["threshold"]).sum())


def test_the_selected_flows_score_exactly_as_the_session_gate_scores_them(csv_set, tmp_path):
    """One scoring, never two that disagree: the pool scores equal apply_ids's for the same flows."""
    scores = pd.read_csv(csv_set / IDS_SCORES_CSV, dtype={"flow_id": str}).set_index("flow_id")
    sel = pd.read_csv(csv_set / "features.csv", dtype={"flow_id": str})
    feats = json.loads((csv_set / "input.json").read_text())["feature_columns"]
    scen = sel[["flow_id"] + feats].assign(label="x")
    apply_ids(tmp_path, scen, feats, {"threshold": 1e-9}, labelled=False, model_path=DEMO)
    gate = pd.read_csv(tmp_path / "ids_results.csv", dtype={"flow_id": str}).set_index("flow_id")
    assert (gate["score"] - scores.loc[gate.index, "score"]).abs().max() < 1e-12


def test_the_identity_file_holds_addresses_and_nothing_else(csv_set):
    ident = pd.read_csv(csv_set / IDS_IDENTITY_CSV, dtype=str)
    assert ident.columns[0] == "flow_id" and "src_ip" in ident.columns and "dst_ip" in ident.columns
    assert set(ident.columns) <= {"flow_id", *P.IDENTIFICATION_COLUMNS}
    assert len(ident) == len(pd.read_csv(csv_set / IDS_SCORES_CSV))


def test_the_analysis_is_outside_the_prepared_set_hash_and_its_downloads(csv_set, tmp_path):
    plain = _prepare(tmp_path, SAMPLE_CSV, "with_ids", None)          # same name: same flow ids
    a = json.loads((csv_set / "manifest.json").read_text())["prepared_set"]
    b = json.loads((plain / "manifest.json").read_text())["prepared_set"]
    assert a["content_sha256"] == b["content_sha256"]
    assert a["ids_analysis"]["available"] and "ids_analysis" not in b
    assert not any((plain / f).exists() for f in (IDS_SCORES_CSV, IDS_POOL_JSON, IDS_IDENTITY_CSV))
    assert not set(P.DOWNLOADABLE) & {IDS_SCORES_CSV, IDS_POOL_JSON, IDS_IDENTITY_CSV}


def test_a_missing_model_prepares_the_set_and_says_why(tmp_path):
    d = _prepare(tmp_path, SAMPLE_CSV, "no_model", tmp_path / "missing.joblib")
    block = json.loads((d / "manifest.json").read_text())["prepared_set"]["ids_analysis"]
    assert block["available"] is False and "no IDS model" in block["reason"]
    assert (d / "features.csv").exists() and not (d / IDS_SCORES_CSV).exists()


def test_a_pcap_pool_is_scored_without_labels(tmp_path):
    pytest.importorskip("scapy")
    d = _prepare(tmp_path, SAMPLE_PCAP, "pcap_ids", DEMO)
    scores = pd.read_csv(d / IDS_SCORES_CSV, dtype={"flow_id": str})
    info = json.loads((d / IDS_POOL_JSON).read_text())
    assert len(scores) == info["n_flows_in"] > 0 and info["labelled"] is False
    assert scores["label"].isna().all()
    selected = pd.read_csv(d / "features.csv", dtype={"flow_id": str})["flow_id"]
    assert set(scores.loc[scores["selected"], "flow_id"]) == set(selected)


def test_score_pool_marks_selected_by_flow_id():
    pool = pd.read_csv(SAMPLE_CSV, nrows=5)
    pool.columns = [c.strip() for c in pool.columns]
    pool["flow_id"] = [f"f{i}" for i in range(len(pool))]
    out, info = score_pool(pool, ["f1", "f3"], labelled=False, model_path=DEMO)
    assert list(out["selected"]) == [False, True, False, True, False]
    assert info["n_flows_in"] == 5 and info["n_selected"] == 2


# --- the scores travel with a re-uploaded set (import_prepared) ----------------------------
def _files(d, *names):
    return {n: (d / n).read_bytes() for n in names}


def test_a_reuploaded_set_keeps_its_ids_scores_but_never_the_addresses(csv_set, tmp_path):
    files = _files(csv_set, "manifest.json", "features.csv", "labels.csv", IDS_SCORES_CSV, IDS_POOL_JSON,
                   IDS_IDENTITY_CSV)
    sid = P.import_prepared(files, tmp_path)
    out = tmp_path / sid
    assert (out / IDS_SCORES_CSV).read_bytes() == files[IDS_SCORES_CSV]
    assert (out / IDS_POOL_JSON).read_bytes() == files[IDS_POOL_JSON]
    assert not (out / IDS_IDENTITY_CSV).exists()


def test_a_reuploaded_set_without_scores_imports_as_before(csv_set, tmp_path):
    sid = P.import_prepared(_files(csv_set, "manifest.json", "features.csv", "labels.csv"), tmp_path)
    assert (tmp_path / sid / "features.csv").exists() and not (tmp_path / sid / IDS_SCORES_CSV).exists()


@pytest.mark.parametrize("case, msg", [
    ("only_scores", "both ids_scores.csv and ids_pool.json"),
    ("edited_scores", "does not match the manifest's IDS analysis"),
])
def test_bad_ids_files_are_refused(csv_set, tmp_path, case, msg):
    files = _files(csv_set, "manifest.json", "features.csv", "labels.csv", IDS_SCORES_CSV, IDS_POOL_JSON)
    if case == "only_scores":
        files.pop(IDS_POOL_JSON)
    else:
        files[IDS_SCORES_CSV] = files[IDS_SCORES_CSV].replace(b"0.", b"1.", 1)
    with pytest.raises(P.PrepareError, match=msg):
        P.import_prepared(files, tmp_path)
