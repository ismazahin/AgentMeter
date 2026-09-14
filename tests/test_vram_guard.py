"""Phase 6 / pilot — VRAM residual guard tests (no GPU; readings simulated).

The guard compares post-unload device VRAM against the pre-first-load baseline
and warns when the residual exceeds tolerance. These tests simulate the readings
(the exact numbers from the 2-model pilot leak) so they run on CPU with no GPU,
no pynvml, no cost.
"""
from __future__ import annotations

from agentmeter.pilot import check_vram_residual, vram_guard_report


def test_guard_triggers_when_residual_exceeds_tolerance(capsys):
    # From the bug report: Llama's before-load was 4424 MB vs a 552 MB baseline —
    # a 3872 MB residual (~Mistral's weights) that should trip the guard.
    rep = vram_guard_report(
        baseline_mb=552.0, used_mb=4424.0, tolerance_mb=500.0, model_label="Llama"
    )
    assert rep is not None
    assert rep["exceeded"] is True
    assert abs(rep["residual_mb"] - 3872.0) < 1e-6
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "did NOT return to baseline" in out


def test_guard_ok_when_within_tolerance(capsys):
    # A clean unload: device settles back near baseline (residual 348 <= 500).
    rep = vram_guard_report(
        baseline_mb=552.0, used_mb=900.0, tolerance_mb=500.0, model_label="Mistral"
    )
    assert rep is not None
    assert rep["exceeded"] is False
    assert abs(rep["residual_mb"] - 348.0) < 1e-6
    out = capsys.readouterr().out
    assert "WARNING" not in out
    assert "OK" in out


def test_guard_skips_without_fabricating_when_reading_missing(capsys):
    # pynvml unavailable -> used_mb is None. The guard must SKIP, never fabricate.
    assert vram_guard_report(552.0, None, 500.0, "X") is None
    out = capsys.readouterr().out
    assert "skipped" in out
    assert "not fabricating" in out
    # Missing baseline is likewise skipped.
    assert vram_guard_report(None, 4424.0, 500.0, "X") is None


def test_check_vram_residual_skips_without_cuda():
    # No CUDA device in CI -> GpuProbe.available is False -> guard skipped (None),
    # not a fabricated reading.
    assert check_vram_residual(baseline_mb=100.0, tolerance_mb=500.0, model_label="m") is None
