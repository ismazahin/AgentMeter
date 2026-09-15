"""Data-prep tests for build_dataset (no GPU, no network).

Uses a small synthetic fixture with the CIC-IDS quirks the real files have:
leading-space column names (' Label'), Inf/NaN in flow features, droppable
labels, and BENIGN spread across all four files. Verifies the output is exactly
balanced, finite, reproducible, and still isolates the label for the model.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import yaml

from agentmeter.config import load_config
from agentmeter.dataprep import build_dataset
from agentmeter.dataset import DatasetLoader

# CIC-IDS-style schema: most feature names carry a leading space; label is ' Label'.
COLS = [" Destination Port", " Flow Duration", " Flow Bytes/s", "SYN Flag Count", " Label"]
FEATURES = ["Destination Port", "Flow Duration", "Flow Bytes/s", "SYN Flag Count"]


def _rows(rng, label, k, *, bad=0):
    """k valid rows for `label`, plus `bad` rows carrying an Inf/NaN feature."""
    recs = []
    for _ in range(k):
        recs.append([int(rng.integers(1, 65535)), int(rng.integers(1, 1_000_000)),
                     float(rng.random() * 1e5), int(rng.integers(0, 5)), label])
    for i in range(bad):
        val = np.inf if i % 2 == 0 else np.nan  # exercise BOTH non-finite kinds
        recs.append([80, 1234, val, 1, label])
    return recs


def _write_fixture(tmp_path):
    d = tmp_path / "cicids_raw"
    d.mkdir()
    rng = np.random.default_rng(0)  # fixes the fixture content (not the sampler)

    def dump(name, recs):
        pd.DataFrame(recs, columns=COLS).to_csv(d / name, index=False)

    # BENIGN is spread across ALL four files (20 each) to test pooled sampling.
    dump("Tuesday-WorkingHours_pcap_ISCX.csv",
         _rows(rng, "FTP-Patator", 40, bad=5) + _rows(rng, "SSH-Patator", 40, bad=5)
         + _rows(rng, "BENIGN", 20) + _rows(rng, "DoS GoldenEye", 10))       # droppable
    dump("Wednesday-workingHours_pcap_ISCX.csv",
         _rows(rng, "DoS Hulk", 80, bad=6) + _rows(rng, "BENIGN", 20)
         + _rows(rng, "DoS slowloris", 10))                                   # droppable
    dump("Friday-WorkingHours-Afternoon-DDos_pcap_ISCX.csv",
         _rows(rng, "DDoS", 80, bad=4) + _rows(rng, "BENIGN", 20)
         + _rows(rng, "Heartbleed", 3))                                       # droppable
    dump("Friday-WorkingHours-Afternoon-PortScan_pcap_ISCX.csv",
         _rows(rng, "PortScan", 80, bad=4) + _rows(rng, "BENIGN", 20))
    return d


def _write_config(tmp_path, input_dir, out_path, n_per_class=60, seed=42):
    cfg = {
        "run": {"mode": "pilot", "device": "cpu", "require_gpu": False, "seed": 42},
        "model": {"provider": "mock", "name": "mock/m", "mock": {"latency_s": 0.0}},
        # dataset section points at the produced file so DatasetLoader can read it.
        "dataset": {"path": str(out_path), "label_column": "label", "limit": None,
                    "max_feature_chars": 4000},
        "pipeline": {"agents": ["perceive", "reason", "decide", "act"]},
        "classes": ["Brute Force", "Volumetric DDoS", "Port Scanning", "DoS Hulk", "Benign"],
        "mitre": {"Benign": "N/A"},
        "scoring": {"weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15}},
        "data_prep": {
            "input_dir": str(input_dir),
            "files": [
                "Tuesday-WorkingHours_pcap_ISCX.csv",
                "Wednesday-workingHours_pcap_ISCX.csv",
                "Friday-WorkingHours-Afternoon-DDos_pcap_ISCX.csv",
                "Friday-WorkingHours-Afternoon-PortScan_pcap_ISCX.csv",
            ],
            "output_path": str(out_path),
            "label_column": "Label",
            "output_label_column": "label",
            "seed": seed,
            "n_per_class": n_per_class,
            "label_map": {
                "FTP-Patator": "Brute Force", "SSH-Patator": "Brute Force",
                "DoS Hulk": "DoS Hulk", "DDoS": "Volumetric DDoS",
                "PortScan": "Port Scanning", "BENIGN": "Benign",
            },
        },
    }
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return str(p)


CANON = ["Brute Force", "Volumetric DDoS", "Port Scanning", "DoS Hulk", "Benign"]


def test_build_dataset_balanced_finite_and_shaped(tmp_path):
    d = _write_fixture(tmp_path)
    out = tmp_path / "cicids_full_300.csv"
    cfg = _write_config(tmp_path, d, out)

    res = build_dataset(config_path=cfg)
    assert res["n_rows"] == 300
    assert res["dropped_non_finite"] > 0  # the injected Inf/NaN rows were removed

    df = pd.read_csv(out)
    # Exactly 300 rows, 60 per canonical class.
    assert len(df) == 300
    assert df["label"].value_counts().to_dict() == {c: 60 for c in CANON}
    # Output = feature columns + a single canonical 'label' column (no raw ' Label').
    assert list(df.columns) == FEATURES + ["label"]
    # No Inf/NaN anywhere in the feature columns.
    feats = df[FEATURES].apply(pd.to_numeric, errors="coerce")
    assert not np.isinf(feats.to_numpy()).any()
    assert not feats.isna().to_numpy().any()


def test_build_dataset_is_reproducible(tmp_path):
    d = _write_fixture(tmp_path)
    out = tmp_path / "out.csv"
    cfg = _write_config(tmp_path, d, out)

    build_dataset(config_path=cfg)
    first = out.read_bytes()
    build_dataset(config_path=cfg)   # same seed -> identical file
    assert out.read_bytes() == first


def test_build_dataset_fails_loudly_when_a_class_is_short(tmp_path):
    d = _write_fixture(tmp_path)
    out = tmp_path / "out.csv"
    # Ask for more rows than any class has -> must fail loudly, not unbalance.
    cfg = _write_config(tmp_path, d, out, n_per_class=100)
    with pytest.raises(ValueError, match="< 100 valid rows"):
        build_dataset(config_path=cfg)


def test_output_loads_with_isolation_intact(tmp_path):
    d = _write_fixture(tmp_path)
    out = tmp_path / "cicids_full_300.csv"
    cfg_path = _write_config(tmp_path, d, out)
    build_dataset(config_path=cfg_path)

    # DatasetLoader must read the produced file and strip the label from what the
    # model sees (blind zero-shot — data isolation intact).
    scenarios = DatasetLoader(load_config(cfg_path)).load()
    assert len(scenarios) == 300
    for s in scenarios[:20]:
        assert s.held_out_label in CANON
        assert "label" not in s.feature_prompt.lower()
        assert s.held_out_label.lower() not in s.feature_prompt.lower()
