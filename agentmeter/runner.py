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
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Config, load_config
from .dataset import DatasetLoader
from .instrument import GpuProbe, MetricsCollector, make_instrumented_hook
from .pilot import free_cuda
from .pipeline import Pipeline
from .providers import get_provider
from .storage import Storage


class ConfigMismatchError(RuntimeError):
    """Raised when an incomplete run's fingerprint differs from current config."""


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

        ran = 0
        skipped = 0
        wall_start = time.perf_counter()

        for mi, model_name in enumerate(models):
            # Isolation: guarantee the previous model is gone before this loads.
            free_cuda()
            cfg.data.setdefault("model", {})["name"] = model_name
            label = model_label(cfg)

            # Skip loading the model entirely if every scenario is already done.
            remaining = [s for s in scenarios if (label, s.scenario_id) not in done]
            if not remaining:
                print(f"[{mi+1}/{len(models)}] {label}: all {n_scn} scenarios already complete — skipping model")
                skipped += n_scn
                continue

            print(f"[{mi+1}/{len(models)}] {label}: loading (running {len(remaining)}/{n_scn} scenario(s))")
            provider = get_provider(cfg)
            provider.load()
            gpu = GpuProbe()
            collector = MetricsCollector()
            hook = make_instrumented_hook(collector, label, gpu)
            pipeline = Pipeline(cfg, provider, node_hook=hook)

            try:
                for s in scenarios:
                    if (label, s.scenario_id) in done:
                        skipped += 1
                        continue

                    offset = len(collector.rows)
                    state = pipeline.run(s.scenario_id, s.feature_prompt)
                    new_rows = collector.rows[offset:]

                    verdict = state.get("verdict", {}) or {}
                    pred = verdict.get("predicted_class", "Unparseable")
                    ok = pred == s.held_out_label

                    total_time = sum(r.wall_time_s for r in new_rows)
                    vram_vals = [r.vram_peak_mb for r in new_rows if r.vram_peak_mb is not None]
                    peak_vram = max(vram_vals) if vram_vals else None  # peak, not sum

                    store.persist_scenario(
                        run_id=run_id,
                        model=label,
                        scenario_id=s.scenario_id,
                        predicted_label=pred,
                        held_out_label=s.held_out_label,
                        correct=ok,
                        scenario_total_time_s=total_time,
                        scenario_peak_vram_mb=peak_vram,
                        agent_rows=new_rows,
                    )
                    done.add((label, s.scenario_id))
                    ran += 1
                    print(f"    {s.scenario_id:<10} -> {pred:<16} "
                          f"(gt={s.held_out_label}, {total_time:.4f}s) [persisted]")
            finally:
                provider.unload()
                free_cuda()  # release VRAM before the next model loads

        store.finish_run(run_id, status="complete")
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
