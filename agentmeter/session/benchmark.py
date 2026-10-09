"""Run a prepared user input run (CSV or PCAP) through the EXISTING pipeline.

Nothing here re-implements execution or measurement. It prepares two files in
the user run's directory, then calls the existing Phase 6 runner:

  session_scenarios.csv  one row per selected flow: `flow_id` (scenario id, hidden),
                         the model-visible feature columns (input.json
                         `feature_columns`), and `label` (held out). The
                         identification columns (IPs, ports, timestamps) are not
                         written, so they cannot reach a prompt.
  session_config.yaml    the base config (e.g. configs/run_full_l4.yaml for
                         Colab: hf + 4-bit NF4 + require_gpu) with per-session
                         overrides: run.models (<=2), dataset -> the CSV above,
                         storage -> <run_dir>/session.db, and classes -> the
                         run's class set.

`runner.run_full(session_config)` then does what it always does: one
`agentmeter.run.worker` subprocess per model, SEQUENTIALLY (the OS reclaims VRAM
when each exits); each worker loads scenarios with DatasetLoader (label
isolation), runs Pipeline + make_instrumented_hook (MetricsCollector) via
get_provider, and persists every scenario atomically — resumable on a crash.

6-class runs: the Decide agent and normalize_class read `classes` from the config,
so setting classes to the 5 study classes + "Other Attack" is all that is needed
for Decide to offer and accept it. 5-class runs get the base config's classes
unchanged, so they behave exactly as before.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml

from ..config import PROJECT_ROOT, load_config
from ..ingest.csv_input import OTHER_ATTACK
from ..ingest.unified import load_input_run

MAX_MODELS = 2
SESSION_DB = "session.db"
SCENARIOS_CSV = "session_scenarios.csv"
SESSION_CONFIG = "session_config.yaml"
RESULTS_JSON = "session_results.json"
UNLABELLED = "UNLABELLED"   # placeholder held-out label for efficiency-only runs (never scored)
OTHER_ATTACK_MITRE = "N/A - outside the 5-class study taxonomy"
CANONICAL_MODELS_CONFIG = PROJECT_ROOT / "configs" / "run_full_l4.yaml"


class SessionError(ValueError):
    """The session request is invalid (model count, run dir, locked DB …)."""


def canonical_models() -> list[str]:
    """The fixed 5-model set of the locked study (read, never modified)."""
    try:
        return list(load_config(CANONICAL_MODELS_CONFIG).get("run.models") or [])
    except FileNotFoundError:
        return []


def validate_models(models: list[str]) -> list[str]:
    models = [str(m).strip() for m in models if str(m).strip()]
    if not models:
        raise SessionError("choose 1 or 2 models to benchmark")
    if len(models) > MAX_MODELS:
        raise SessionError(
            f"at most {MAX_MODELS} models per benchmark session (got {len(models)}: "
            f"{', '.join(models)}). Pick two — e.g. from the 5-model set or a pulled model.")
    if len(set(models)) != len(models):
        raise SessionError(f"duplicate model in {models}")
    return models


def _locked_dbs() -> set[Path]:
    from ..analysis.analyze import _validated_study_dbs
    return _validated_study_dbs()


def prepare_session(run_dir: str | Path, models: list[str], *,
                    base_config: Optional[str] = None,
                    provider: Optional[str] = None) -> dict[str, Any]:
    """Write session_scenarios.csv + session_config.yaml into run_dir and return
    what the session will do. Raises SessionError / InputContractError."""
    run_dir = Path(run_dir).resolve()
    models = validate_models(models)
    run = load_input_run(run_dir)               # contract check (input.json + selected_flows)
    meta = run.metadata
    db_path = run_dir / SESSION_DB
    if db_path.resolve() in _locked_dbs():
        raise SessionError(f"refusing to write a session into the locked study DB ({db_path})")

    base = load_config(base_config)
    data = copy.deepcopy(base.data)

    # --- class set: 5 study classes, or the run's 6-class scheme ---------------------
    base_classes = list(base.get("classes") or [])
    scheme = meta.get("class_scheme")
    six = bool(scheme and scheme.get("name") == "6-class")
    classes = list(scheme["classes"]) if six else base_classes
    if six:
        if classes[:-1] != base_classes or classes[-1] != OTHER_ATTACK:
            raise SessionError(f"unexpected 6-class scheme {classes} (base classes {base_classes})")
        data.setdefault("mitre", {})[OTHER_ATTACK] = OTHER_ATTACK_MITRE
    data["classes"] = classes

    # --- scenarios: hidden id + model-visible features + held-out label ---------------
    feats = list(meta["feature_columns"])
    scen = run.selected[["flow_id"] + feats].copy()
    if run.labels is not None:
        scen["label"] = run.labels["label"].to_numpy()
    else:
        scen["label"] = UNLABELLED
    scen.to_csv(run_dir / SCENARIOS_CSV, index=False)

    data.setdefault("run", {})
    data["run"]["models"] = models
    data["run"]["auto_analyze"] = False          # session scoring replaces analyze
    data.setdefault("model", {})["name"] = models[0]
    if provider:
        data["model"]["provider"] = provider
    data["dataset"] = {
        "path": str(run_dir / SCENARIOS_CSV),
        "label_column": "label",
        "id_column": "flow_id",                  # scenario_id; hidden from the model
        "drop_columns": [],
        "limit": None,
        "max_feature_chars": base.get("dataset.max_feature_chars", 4000),
        "label_map": {},                         # labels are already canonical
        "drop_labels": [],
    }
    data["storage"] = {"sqlite_path": str(db_path)}
    data["session"] = {                          # informational; not read by the runner
        "source_run": str(run_dir), "base_config": str(base.path),
        "class_scheme": "6-class" if six else "5-class",
        "evaluation_mode": meta["evaluation_mode"], "non_validated": True,
    }
    cfg_path = run_dir / SESSION_CONFIG
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    canon = set(canonical_models())
    return {
        "run_dir": str(run_dir), "config_path": str(cfg_path), "db_path": str(db_path),
        "models": [{"model": m, "canonical": m in canon} for m in models],
        "classes": classes, "six_class": six, "n_flows": int(len(scen)),
        "provider": data["model"].get("provider"), "metadata": meta,
    }


def run_session(run_dir: str | Path, models: list[str], *, base_config: Optional[str] = None,
                provider: Optional[str] = None, fresh: bool = False,
                environment: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Prepare, execute (existing sequential runner), score, and write
    <run_dir>/session_results.json. Returns the results payload."""
    from ..run.runner import run_full
    from .scoring import score_session, write_results

    plan = prepare_session(run_dir, models, base_config=base_config, provider=provider)
    result = run_full(config_path=plan["config_path"], fresh=fresh, auto_analyze=False)
    payload = score_session(plan, run_id=result.run_id)
    if environment is None:                     # CLI: describe this machine the same way
        from ..server.runtime import environment as _env
        environment = _env("real" if plan["provider"] == "hf" else "mock")
    # Phase E: provider, GPU, driver/CUDA and library versions — traceability only.
    payload["environment"] = environment
    write_results(Path(plan["run_dir"]) / RESULTS_JSON, payload)
    return payload


def read_scenarios(run_dir: str | Path) -> pd.DataFrame:
    return pd.read_csv(Path(run_dir) / SCENARIOS_CSV)
