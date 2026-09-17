"""measure-vram tests (CPU, no GPU). Verifies the refusal-without-CUDA guard, the
weight-footprint math, and the orchestrator aggregation (spawn mocked)."""
from __future__ import annotations

import json

import pytest

from agentmeter import measure, worker

CFG = "configs/run_full_l4.yaml"
MODELS = [
    "mistralai/Mistral-7B-Instruct-v0.3",
    "meta-llama/Meta-Llama-3-8B-Instruct",
    "Qwen/Qwen2.5-7B-Instruct",
    "microsoft/Phi-3-mini-4k-instruct",
    "google/gemma-2-9b-it",
]


# --- refuses without CUDA (no fabricated readings) ---------------------

def test_orchestrator_refuses_without_cuda():
    # require_gpu is true in the L4 config and there is no CUDA here.
    with pytest.raises(RuntimeError, match="require_gpu|CUDA|GPU"):
        measure.run_measure_vram(config_path=CFG, out_path="/tmp/should_not_write.json")


def test_worker_measure_mode_refuses_without_cuda(tmp_path):
    out = tmp_path / "w.json"
    with pytest.raises(RuntimeError, match="require_gpu|CUDA|GPU"):
        worker.main(["--mode", "measure", "--model", MODELS[0], "--config", CFG, "--out", str(out)])
    assert not out.exists()  # nothing fabricated/written


# --- weight-footprint math ---------------------------------------------

def test_vram_record_footprint_math():
    r = worker._vram_record("m", "NVIDIA L4", "4bit-nf4", 664.9, 5200.0)
    assert abs(r["weight_footprint_mb"] - 4535.1) < 1e-9
    assert r["hardware_label"] == "NVIDIA L4" and r["quant"] == "4bit-nf4"
    # None-safety when a reading is missing
    assert worker._vram_record("m", "L4", "4bit-nf4", None, 5200.0)["weight_footprint_mb"] is None


# --- orchestrator aggregation (spawn + CUDA guard mocked) ---------------

def _install_fake_spawn(monkeypatch, footprints, hardware):
    monkeypatch.setattr(measure, "_require_cuda", lambda cfg: None)

    def fake_spawn(config_path, model, out_json):
        before = 664.9
        rec = worker._vram_record(model, hardware[model], "4bit-nf4", before, before + footprints[model])
        with open(out_json, "w") as fh:
            json.dump(rec, fh)
        return 0

    monkeypatch.setattr(measure, "_spawn_measure_worker", fake_spawn)


def test_aggregation_consistent_hardware(tmp_path, monkeypatch):
    fp = {m: 4000.0 + 100 * i for i, m in enumerate(MODELS)}
    hw = {m: "NVIDIA L4" for m in MODELS}
    _install_fake_spawn(monkeypatch, fp, hw)

    out = tmp_path / "model_vram.json"
    payload = measure.run_measure_vram(config_path=CFG, out_path=str(out))

    assert payload["consistent_hardware"] is True
    assert payload["hardware_labels"] == ["NVIDIA L4"]
    assert {r["model"] for r in payload["models"]} == set(MODELS)
    for r in payload["models"]:
        assert abs(r["weight_footprint_mb"] - fp[r["model"]]) < 1e-9
        assert r["started_clean"] is True          # all began at the same baseline
    # written to disk
    on_disk = json.loads(out.read_text())
    assert len(on_disk["models"]) == len(MODELS)
    assert on_disk["quant"] == "4bit-nf4"


def test_aggregation_flags_mixed_hardware(tmp_path, monkeypatch):
    fp = {m: 4000.0 for m in MODELS}
    hw = {m: "NVIDIA L4" for m in MODELS}
    hw[MODELS[-1]] = "Tesla T4"                     # one model measured on the wrong GPU
    _install_fake_spawn(monkeypatch, fp, hw)

    payload = measure.run_measure_vram(config_path=CFG, out_path=str(tmp_path / "mv.json"))
    assert payload["consistent_hardware"] is False
    assert set(payload["hardware_labels"]) == {"NVIDIA L4", "Tesla T4"}
