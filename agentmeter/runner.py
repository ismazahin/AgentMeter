"""Phase 6 — full-run orchestrator with checkpoint/resume.

Iterates models x scenarios STRICTLY SEQUENTIALLY (one model resident at a
time; fully unload + free CUDA before the next — same isolation as
pilot.run_pilot_models). Never parallel. Persists after EACH scenario via the
SQLite Storage layer, so an interrupted run can be resumed.

This module only orchestrates + persists around the EXISTING instrumentation
(MetricsCollector / AgentMetrics / the pipeline node_hook). It adds no detection
logic and computes no accuracy/SAW aggregates — it stores the raw rows Phase
7/8 will consume.

Checkpoint/resume contract:
  - A run is identified by a config_fingerprint (a hash of every input that
    affects the comparison: model list, effective N, dataset, pipeline agents +
    token budgets, classes, quant, seed, provider).
  - On start (no --fresh): the most recent 'running' run is the resume
    candidate. Same fingerprint -> resume, skipping every (model, scenario_id)
    already marked complete. Different fingerprint -> STOP loudly (resuming
    across a changed config would corrupt the comparison).
  - --fresh: abandon any incomplete runs and start a new one.
  - A scenario is 'complete' only after its agent rows + scenario_result are
    written in one atomic transaction.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Config, load_config
from .dataset import DatasetLoader
from .storage import Storage


class ConfigMismatchError(RuntimeError):
    """Raised when an incomplete run's fingerprint differs from current config."""


class WorkerError(RuntimeError):
    """Raised when a per-model worker subprocess exits non-zero."""


def _spawn_model_worker(
    config_path: str,
    model: str,
    run_id: str,
    n: Optional[int],
    baseline_vram_mb: Optional[float] = None,
    vram_out: Optional[str] = None,
) -> int:
    """Spawn ONE per-model worker (sqlite mode) and BLOCK until it exits.

    Sequential by construction: the parent waits here, so two workers never run
    at once. `baseline_vram_mb` is the fresh-context reference the worker checks
    its own start against; `vram_out` is a sidecar the worker writes its
    before-load reading to, so the parent can establish that baseline from the
    first worker. Returns the worker's exit code (0 = success, its scenarios
    persisted atomically; non-zero = crash, model left incomplete for resume).
    """
    cmd = [
        sys.executable, "-m", "agentmeter.worker",
        "--mode", "sqlite",
        "--model", model,
        "--run-id", run_id,
        "--config", config_path,
    ]
    if n is not None:
        cmd += ["--n", str(n)]
    if baseline_vram_mb is not None:
        cmd += ["--baseline-vram-mb", repr(float(baseline_vram_mb))]
    if vram_out is not None:
        cmd += ["--vram-out", vram_out]
    return subprocess.run(cmd).returncode


def _read_worker_before_load(sidecar_path: str) -> Optional[float]:
    """Read a worker's fresh-context before-load VRAM from its sidecar (or None)."""
    try:
        import json as _json
        from pathlib import Path as _Path

        data = _json.loads(_Path(sidecar_path).read_text())
        val = data.get("device_vram_before_load_mb")
        return float(val) if val is not None else None
    except Exception:
        return None


@dataclass
class RunFullResult:
    run_id: str
    resumed: bool
    models: list[str]
    n_scenarios: int
    scenarios_run: int      # newly executed this invocation
    scenarios_skipped: int  # skipped because already complete (resume)
    report: str


# --- configuration-derived identity ------------------------------------

def resolve_models(cfg: Config) -> list[str]:
    """Models to benchmark. `run.models` (list) or fall back to `model.name`."""
    models = cfg.get("run.models") or []
    models = [str(m) for m in models if m]
    if not models:
        one = cfg.get("model.name")
        if one:
            models = [str(one)]
    if not models:
        raise ValueError("No models to run: set run.models (list) or model.name in config.")
    return models


def quant_setting(cfg: Config) -> str:
    if cfg.get("model.provider") != "hf":
        return "none"
    quant = cfg.get("model.hf.quantization", {}) or {}
    if quant.get("load_in_4bit"):
        return "4bit-" + str(quant.get("bnb_4bit_quant_type", "nf4"))
    if quant.get("load_in_8bit"):
        return "8bit"
    return "none"


def hardware_label() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.get_device_name(torch.cuda.current_device())
    except Exception:
        pass
    return "cpu"


def model_label(cfg: Config) -> str:
    """Label persisted per row. Reflects the CONFIGURED model name for both
    providers, so distinct mock 'models' get distinct labels."""
    name = cfg.get("model.name")
    return name if cfg.get("model.provider") == "hf" else f"mock:{name}"


def config_fingerprint(cfg: Config, models: list[str], effective_n: Optional[int]) -> str:
    """Stable hash over every input that affects the comparison's validity."""
    material = {
        "provider": cfg.get("model.provider"),
        "models": list(models),
        "effective_n": effective_n,
        "seed": cfg.get("run.seed"),
        "classes": cfg.get("classes"),
        "dataset": {
            "path": cfg.get("dataset.path"),
            "label_column": cfg.get("dataset.label_column"),
            "id_column": cfg.get("dataset.id_column"),
            "drop_columns": cfg.get("dataset.drop_columns"),
            "limit": cfg.get("dataset.limit"),
            "max_feature_chars": cfg.get("dataset.max_feature_chars"),
            "label_map": cfg.get("dataset.label_map"),
            "drop_labels": cfg.get("dataset.drop_labels"),
        },
        "pipeline": {
            "agents": cfg.get("pipeline.agents"),
            "max_new_tokens": cfg.get("pipeline.max_new_tokens"),
        },
        "hf": {
            "dtype": cfg.get("model.hf.dtype"),
            "max_new_tokens": cfg.get("model.hf.max_new_tokens"),
            "temperature": cfg.get("model.hf.temperature"),
            "do_sample": cfg.get("model.hf.do_sample"),
            "quantization": cfg.get("model.hf.quantization"),
        },
    }
    blob = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _gpu_guard(cfg: Config) -> None:
    """No silent CPU fallback for real (HF) runs."""
    if not bool(cfg.get("run.require_gpu", False)):
        return
    try:
        import torch

        cuda = torch.cuda.is_available()
    except Exception:
        cuda = False
    if not cuda:
        raise RuntimeError(
            "STOP: run.require_gpu is true but torch.cuda is not available. "
            "VRAM measurement is a core requirement — refusing to run on CPU."
        )


# --- orchestrator ------------------------------------------------------

def run_full(
    config_path: Optional[str] = None,
    n: Optional[int] = None,
    fresh: bool = False,
    proj_scenarios: int = 1000,
    proj_models: Optional[int] = None,
) -> RunFullResult:
    cfg = load_config(config_path)
    _gpu_guard(cfg)

    models = resolve_models(cfg)
    proj_models = proj_models if proj_models is not None else len(models)

    # Scenarios are deterministic from the dataset (stable scenario_ids), so the
    # same set is reproduced on every invocation — safe to resume against.
    scenarios = DatasetLoader(cfg).load()
    if n is not None:
        scenarios = scenarios[:n]
    n_scn = len(scenarios)
    effective_n = n_scn

    fingerprint = config_fingerprint(cfg, models, effective_n)
    db_path = cfg.resolve_path("storage.sqlite_path", "results/agentmeter.db")
    store = Storage(db_path)

    try:
        run_id, resumed = _select_run(
            store, fingerprint, fresh,
            quant=quant_setting(cfg), hardware=hardware_label(),
        )
        done = store.completed_pairs(run_id) if resumed else set()

        print("=" * 78)
        print("  AgentMeter — Phase 6: Full Run (sequential, persisted)")
        print("=" * 78)
        print(f"DB               : {db_path}")
        print(f"Run              : {run_id}  ({'RESUMING' if resumed else 'fresh'})")
        print(f"Config fingerprint: {fingerprint}")
        print(f"Provider         : {cfg.get('model.provider')}  (quant={quant_setting(cfg)})")
        print(f"Hardware         : {hardware_label()}")
        print(f"Models ({len(models)})       : {', '.join(models)}")
        print(f"Scenarios/model  : {n_scn}")
        if resumed and done:
            print(f"Already complete : {len(done)} (model, scenario) pair(s) — will skip")
        print("")

        initial_done = len(done)
        wall_start = time.perf_counter()

        # The exact config file the workers must load (so parent and workers agree
        # on models, dataset, DB path, quant — everything the fingerprint covers).
        worker_config = str(cfg.path)

        # The fresh-context baseline is established by the first worker that runs
        # and passed to later workers so each worker's startup-isolation guard is
        # judged against a known-clean reference (before_load ~ baseline -> clean).
        baseline_vram_mb: Optional[float] = None
        vram_sidecar = str(db_path) + ".worker_vram.json"

        # SEQUENTIAL subprocess-per-model: each model runs in its own fresh process
        # so the OS reclaims all GPU memory on exit — clean VRAM by construction.
        # Never two workers at once.
        for mi, model_name in enumerate(models):
            cfg.data.setdefault("model", {})["name"] = model_name
            label = model_label(cfg)

            remaining = [s for s in scenarios if (label, s.scenario_id) not in done]
            if not remaining:
                print(f"[{mi+1}/{len(models)}] {label}: all {n_scn} scenarios already complete — skipping model")
                continue

            print(f"[{mi+1}/{len(models)}] {label}: spawning worker (running {len(remaining)}/{n_scn} scenario(s))")
            rc = _spawn_model_worker(
                worker_config, model_name, run_id, n,
                baseline_vram_mb=baseline_vram_mb, vram_out=vram_sidecar,
            )
            if rc != 0:
                # A crashed/killed worker leaves this model incomplete; the run row
                # stays 'running' so a later resume re-runs just this model. Its
                # already-persisted scenarios are safe (each was atomic).
                raise WorkerError(
                    f"worker for model {label!r} exited with code {rc}. Run {run_id} left "
                    f"incomplete — re-run `run-full` (no --fresh) to resume just this model."
                )
            # First worker establishes the fresh-context baseline for the rest.
            if baseline_vram_mb is None:
                baseline_vram_mb = _read_worker_before_load(vram_sidecar)
            # The worker persisted atomically; refresh completion from the DB.
            done = store.completed_pairs(run_id)

        store.finish_run(run_id, status="complete")
        final_done = len(done)
        ran = final_done - initial_done
        skipped = initial_done
        wall_elapsed = time.perf_counter() - wall_start

        report = _format_report(
            store, run_id, models, n_scn, ran, skipped, wall_elapsed,
            proj_scenarios, proj_models,
        )
        return RunFullResult(
            run_id=run_id,
            resumed=resumed,
            models=models,
            n_scenarios=n_scn,
            scenarios_run=ran,
            scenarios_skipped=skipped,
            report=report,
        )
    finally:
        store.close()


def _select_run(
    store: Storage, fingerprint: str, fresh: bool, quant: str, hardware: str
) -> tuple[str, bool]:
    """Decide whether to resume an existing run or create a new one.

    Returns (run_id, resumed). Raises ConfigMismatchError if a --fresh was NOT
    requested but the incomplete run's fingerprint differs from the current one.
    """
    if fresh:
        abandoned = store.abandon_incomplete_runs()
        if abandoned:
            print(f"--fresh: abandoned {abandoned} incomplete run(s).")
        run_id = _new_run_id()
        store.create_run(run_id, fingerprint, quant, hardware, notes="fresh")
        return run_id, False

    incomplete = store.find_incomplete_run()
    if incomplete is None:
        run_id = _new_run_id()
        store.create_run(run_id, fingerprint, quant, hardware, notes="")
        return run_id, False

    if incomplete["config_fingerprint"] != fingerprint:
        raise ConfigMismatchError(
            "REFUSING TO RESUME: an incomplete run exists with a DIFFERENT config "
            f"fingerprint.\n  incomplete run : {incomplete['run_id']} "
            f"(fingerprint {incomplete['config_fingerprint']})\n"
            f"  current config : fingerprint {fingerprint}\n"
            "Resuming across a changed config would corrupt the comparison. "
            "Re-run with --fresh to abandon it and start a new run (or restore the "
            "original config to resume)."
        )

    # Hardware guard: VRAM and latency are hardware-specific, so resuming a run
    # on a different host than it started on would blend readings across hardware
    # and corrupt the apples-to-apples comparison (a real risk on the Colab free
    # tier, which can hand out a different GPU on reconnect). Refuse, don't blend.
    stored_hw = incomplete["hardware_label"]
    if stored_hw != hardware:
        raise ConfigMismatchError(
            "REFUSING TO RESUME: an incomplete run exists that started on DIFFERENT "
            f"hardware.\n  incomplete run : {incomplete['run_id']} (hardware {stored_hw!r})\n"
            f"  current host   : hardware {hardware!r}\n"
            "VRAM and latency are hardware-specific — resuming across a hardware change "
            "would blend readings from two machines and corrupt the apples-to-apples "
            "comparison. Re-run with --fresh to start a new run on this hardware (or "
            "resume on the original hardware)."
        )

    return incomplete["run_id"], True


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"run_{stamp}_{uuid.uuid4().hex[:6]}"


def _format_report(
    store: Storage,
    run_id: str,
    models: list[str],
    n_scn: int,
    ran: int,
    skipped: int,
    wall_elapsed: float,
    proj_scenarios: int,
    proj_models: int,
) -> str:
    counts = store.table_counts()
    mean_scn = store.mean_scenario_wall_s(run_id)
    proj_total = mean_scn * proj_scenarios * proj_models

    lines: list[str] = []
    lines.append("")
    lines.append("-" * 78)
    lines.append(f"Run {run_id} complete.")
    lines.append(f"  Scenarios executed this invocation : {ran}")
    lines.append(f"  Scenarios skipped (already done)    : {skipped}")
    lines.append(f"  Wall time this invocation           : {wall_elapsed:.2f} s")
    lines.append("")
    lines.append("SQLite row counts:")
    lines.append(f"  runs             : {counts['runs']}")
    lines.append(f"  scenario_results : {counts['scenario_results']}")
    lines.append(f"  agent_metrics    : {counts['agent_metrics']}")
    lines.append("")
    lines.append(f"Mean per-scenario wall (this run): {mean_scn:.4f} s")
    lines.append(f"Projection : {proj_scenarios} scenarios x {proj_models} models")
    lines.append(f"   ~ {proj_total:.0f} s = {proj_total/60:.1f} min = {proj_total/3600:.2f} h "
                 f"(from THIS run's per-scenario timings)")
    lines.append("-" * 78)
    return "\n".join(lines)
