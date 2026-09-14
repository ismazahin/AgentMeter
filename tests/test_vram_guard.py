"""Phase 6 / pilot — startup-isolation guard tests (no GPU; readings simulated).

In subprocess-per-model isolation the meaningful signal is whether a model's
worker STARTED clean: its device VRAM before loading its own weights should be
within tolerance of the fresh-context baseline (the first worker's before-load).
The per-worker POST-UNLOAD residual is NOT a failure — the process exits and the
OS reclaims VRAM — so it must not drive exceeded.

These tests simulate the readings (the exact numbers from the real T4 pilot) and
run on CPU with no GPU, no pynvml, no cost.
"""
from __future__ import annotations

import json

import yaml

from agentmeter.pilot import run_pilot, startup_isolation_guard


def test_clean_start_is_not_flagged(capsys):
    # Confirmed on the real T4 run: both models started at 552 MB (the fresh
    # context). before_load == baseline -> clean, exceeded:false.
    g = startup_isolation_guard(baseline_mb=552.0, before_load_mb=552.0,
                                tolerance_mb=500.0, model_label="Llama")
    assert g["checked"] is True
    assert g["exceeded"] is False
    assert g["startup_excess_mb"] == 0.0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "WARNING" not in out


def test_small_startup_delta_within_tolerance_is_clean():
    # A few hundred MB of context jitter is still clean (348 <= 500).
    g = startup_isolation_guard(552.0, 900.0, 500.0, "Mistral")
    assert g["exceeded"] is False
    assert abs(g["startup_excess_mb"] - 348.0) < 1e-6


def test_contaminated_start_is_flagged(capsys):
    # Simulated leak: a worker starts at 4424 MB vs the 552 MB baseline (a prior
    # process failed to release ~3872 MB). THAT must set exceeded:true and warn.
    g = startup_isolation_guard(baseline_mb=552.0, before_load_mb=4424.0,
                                tolerance_mb=500.0, model_label="Llama")
    assert g["checked"] is True
    assert g["exceeded"] is True
    assert abs(g["startup_excess_mb"] - 3872.0) < 1e-6
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "STARTED CONTAMINATED" in out


def test_post_unload_residual_does_not_drive_the_guard():
    # The old false alarm: a large in-process post-unload residual must NOT make
    # the startup guard trip. The guard only sees before_load vs baseline, so a
    # clean start stays exceeded:false regardless of any residual.
    g = startup_isolation_guard(baseline_mb=552.0, before_load_mb=552.0,
                                tolerance_mb=500.0, model_label="Mistral")
    assert g["exceeded"] is False


def test_missing_reading_skips_without_fabricating(capsys):
    # pynvml/CUDA unavailable (mock/CPU) -> skip, do not fabricate, exceeded:false.
    g = startup_isolation_guard(None, None, 500.0, "X")
    assert g["checked"] is False
    assert g["exceeded"] is False
    assert g["startup_excess_mb"] is None
    out = capsys.readouterr().out
    assert "skipped" in out
    assert "not fabricating" in out


def _mock_config(tmp_path):
    csv = tmp_path / "flows.csv"
    csv.write_text("Destination Port,Flow Duration,Label\n80,100,Benign\n")
    cfg = {
        "run": {"mode": "pilot", "device": "cpu", "require_gpu": False, "seed": 42},
        "model": {"provider": "mock", "name": "mock/m", "mock": {"latency_s": 0.0}},
        "dataset": {"path": str(csv), "label_column": "Label", "limit": 1,
                    "max_feature_chars": 4000},
        "pipeline": {"agents": ["perceive", "reason", "decide", "act"]},
        "classes": ["Benign", "Brute Force", "Volumetric DDoS", "Port Scanning", "DoS Hulk"],
        "mitre": {"Benign": "N/A"},
        "scoring": {"weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15}},
        "storage": {"sqlite_path": str(tmp_path / "x.db")},
    }
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return str(p)


def test_run_pilot_payload_uses_startup_guard_not_residual(tmp_path):
    """A mock/CPU pilot run: vram_guard is the startup-isolation dict (not the old
    post-unload residual), the in-process residual is recorded separately with a
    note, and nothing is falsely flagged."""
    cfg_path = _mock_config(tmp_path)
    out_json = tmp_path / "pilot_m.json"
    res = run_pilot(config_path=cfg_path, json_path=str(out_json))
    payload = json.loads(out_json.read_text())

    guard = payload["vram_guard"]
    assert guard["kind"] == "startup_isolation"
    assert guard["exceeded"] is False          # never a false alarm on a clean run
    assert guard["checked"] is False           # CPU: no reading, skipped honestly
    # The in-process residual is a separate, clearly-labelled diagnostic.
    assert "in_process_residual_mb" in payload
    assert "in_process_residual_note" in payload
    assert "harmless" in payload["in_process_residual_note"]
    # exceeded is not driven by the residual.
    assert res.payload["vram_guard"]["exceeded"] is False
