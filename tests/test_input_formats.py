"""CSV input formats: CICFlowMeter CSVs with other column names, and label-column detection.

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product. These
tests check that a CSE-CIC-IDS2018 / Python-CICFlowMeter / CICFlowMeter-V4 file yields the
same features as the CIC-IDS2017 file it was made from, that the label column is found (or
the run becomes efficiency-only), and that a CIC-IDS2017 file is read exactly as before.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agentmeter.ingest import csv_input, formats, run
from agentmeter.ingest.csv_input import CSV_META_SOURCES
from agentmeter.ingest.feature_map import CIC_FEATURES, FEATURE_MAP
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SAMPLE = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
FMT = formats.load_formats()


def sample() -> pd.DataFrame:
    df = pd.read_csv(SAMPLE, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    return df


def feats(path) -> np.ndarray:
    return csv_input.load_csv(path).features[CIC_FEATURES].to_numpy(float)


BASE = None


def base() -> np.ndarray:
    global BASE
    if BASE is None:
        BASE = feats(SAMPLE)
    return BASE


def write(tmp_path, df, name="in.csv"):
    p = tmp_path / name
    df.to_csv(p, index=False)
    return p


# ---------------------------------------------------------------------------
# the alias table itself
# ---------------------------------------------------------------------------
def test_aliases_are_consistent():
    valid = set(CIC_FEATURES) | set(CSV_META_SOURCES)
    seen: dict[str, str] = {}
    for key, f in FMT["formats"].items():
        for alias, target in f["columns"].items():
            assert target in valid, f"{key}: {alias!r} -> {target!r} is not a CIC-IDS2017 column"
            n = formats.norm(alias)
            assert seen.setdefault(n, target) == target, f"{alias!r} maps to two different columns"
    assert FMT["duplicates"] == {"Fwd Header Length.1": "Fwd Header Length"}
    assert FMT["label_columns"][0] == "Label"


# ---------------------------------------------------------------------------
# a CIC-IDS2017 file: unchanged
# ---------------------------------------------------------------------------
def test_cic_ids2017_file_is_read_exactly_as_before(tmp_path):
    r = csv_input.load_csv(SAMPLE)
    assert r.report["columns"]["renamed"] == {} and r.report["columns"]["format"] is None
    assert r.labelled and r.report["label"]["column"] == "Label"
    res = run.process_csv(SAMPLE, max_flows=10, write=False)
    assert res["input"].metadata["feature_match"]["status"] == "exact"


# ---------------------------------------------------------------------------
# other CICFlowMeter exports
# ---------------------------------------------------------------------------
def test_cse_cic_ids2018_file_gives_the_same_features_and_maps_its_labels(tmp_path):
    inv = {v: k for k, v in FMT["formats"]["cse_cic_ids2018"]["columns"].items()}
    df = sample().rename(columns=inv).drop(columns=["Fwd Header Length.1"])
    df["Label"] = df["Label"].map({"BENIGN": "Benign", "FTP-Patator": "FTP-BruteForce", "SSH-Patator": "SSH-Bruteforce",
                                   "DDoS": "DDoS attacks-LOIC-HTTP", "DoS Hulk": "DoS attacks-Hulk"}).fillna(df["Label"])
    p = write(tmp_path, df)
    r = csv_input.load_csv(p)
    c = r.report["columns"]
    assert c["format"] == "cse_cic_ids2018" and len(c["renamed"]) >= 40 and c["missing"] == []
    assert c["derived"] == {"Fwd Header Length.1": "Fwd Header Length"}           # CIC's own duplicate
    assert np.allclose(r.features[CIC_FEATURES].to_numpy(float), base(), equal_nan=True)
    assert r.report["label"]["distribution"] == csv_input.load_csv(SAMPLE).report["label"]["distribution"]
    meta = run.process_csv(p, max_flows=10, write=False)["input"].metadata
    assert meta["feature_match"]["status"] == "mapped" and meta["feature_match"]["format"] == "cse_cic_ids2018"
    assert meta["evaluation_mode"] == "accuracy_available"


def test_python_cicflowmeter_keys_are_recognised(tmp_path):
    keys = {cic: key for cic, key, status, _ in FEATURE_MAP if status != "duplicate"}
    df = sample().rename(columns=keys).drop(columns=["Fwd Header Length.1"])
    r = csv_input.load_csv(write(tmp_path, df))
    assert r.report["columns"]["format"] == "cicflowmeter_python"
    assert np.allclose(r.features[CIC_FEATURES].to_numpy(float), base(), equal_nan=True)


def test_case_and_punctuation_variants_match(tmp_path):
    df = sample().rename(columns=lambda c: c.upper().replace(" ", "_") if c in CIC_FEATURES else c)
    r = csv_input.load_csv(write(tmp_path, df))
    assert r.report["columns"]["missing"] == [] and r.report["columns"]["format"] is None   # name variants only
    assert np.allclose(r.features[CIC_FEATURES].to_numpy(float), base(), equal_nan=True)


def test_an_exact_name_wins_over_an_alias(tmp_path):
    df = sample()
    df["Tot Fwd Pkts"] = -1                                                    # alias of a column that is present
    r = csv_input.load_csv(write(tmp_path, df))
    assert "Tot Fwd Pkts" not in r.report["columns"]["renamed"]
    assert np.allclose(r.features[CIC_FEATURES].to_numpy(float), base(), equal_nan=True)


def test_an_unknown_format_is_still_refused_with_a_pointer(tmp_path):
    p = write(tmp_path, pd.DataFrame({"srcbytes": [1, 2], "dstbytes": [3, 4]}))
    with pytest.raises(csv_input.CsvValidationError, match="not a CICFlowMeter flow CSV.*PCAP"):
        csv_input.load_csv(p)


# ---------------------------------------------------------------------------
# label column detection
# ---------------------------------------------------------------------------
def test_a_binary_label_is_not_used_and_the_run_is_efficiency_only(tmp_path):
    df = sample()
    df["Label"] = (df["Label"] != "BENIGN").astype(int)                       # attack yes/no only
    p = write(tmp_path, df)
    r = csv_input.load_csv(p)
    assert not r.labelled and any("0/1" in n for n in r.report["notes"])
    meta = run.process_csv(p, max_flows=10, write=False)["input"].metadata
    assert meta["evaluation_mode"] == "efficiency_only" and meta["selection_mode"] == "label_blind_statistical"


def test_attack_cat_is_used_when_label_is_binary(tmp_path):
    df = sample()
    df["attack_cat"] = df["Label"].replace({"BENIGN": "Normal"})                 # UNSW-NB15 style
    df["Label"] = (df["Label"] != "BENIGN").astype(int)
    r = csv_input.load_csv(write(tmp_path, df))
    assert r.labelled and r.report["label"]["column"] == "attack_cat"
    assert r.report["label"]["distribution"].get("Benign") == 8                # "Normal" -> Benign
    assert "attack_cat" not in r.features.columns and "Label" not in r.features.columns


def test_no_label_column_means_efficiency_only(tmp_path):
    r = csv_input.load_csv(write(tmp_path, sample().drop(columns=["Label"])))
    assert not r.labelled and any("efficiency only" in n for n in r.report["notes"])
