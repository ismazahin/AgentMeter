"""Phase 18 — config builder (assemble a run config from UI inputs, safely).

This turns a handful of measurement choices — which models to benchmark, the
dataset path, the SAW weights, the targets and the tier thresholds, the agent
token budgets — into a complete, VALID run config YAML, WITHOUT anyone having to
hand-edit YAML. It is the backend for the dashboard "Config builder" panel.

Hard rules (enforced here, not just in the UI):
  * Produces a CONFIG only. Nothing in this module starts a run, loads a model,
    or touches a GPU. It writes a file and returns.
  * Never overwrites the LOCKED study config (configs/run_full_l4.yaml) or the
    repo's root config.yaml. Generated configs go to configs/user/<name>.yaml and
    nowhere else — a name that would escape that directory (traversal, absolute
    path, or a resolved collision with a protected file) is refused.
  * The generated config satisfies agentmeter.config._validate: the six required
    sections, model.provider in {mock, hf}, and scoring.weights summing to 1.0.

Scope is measurement only — models, dataset, SAW weights, targets, tiers, agent
settings. Detection logic, remediation and alerting are out of scope and this
module neither reads nor writes them.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from agentmeter.config import PROJECT_ROOT

# Where generated configs live — a directory SEPARATE from the locked study
# config and the root config, so a build can never clobber a validated artifact.
USER_CONFIG_DIR = PROJECT_ROOT / "configs" / "user"

# Files the builder must NEVER write to, whatever name is requested.
LOCKED_STUDY_CONFIG = PROJECT_ROOT / "configs" / "run_full_l4.yaml"
ROOT_CONFIG = PROJECT_ROOT / "config.yaml"
_PROTECTED = {LOCKED_STUDY_CONFIG.resolve(), ROOT_CONFIG.resolve()}

# SAW dimensions — the four measured axes. Weights are over exactly these keys.
SAW_KEYS = ("accuracy", "latency", "vram", "tokens")
_PIPELINE_AGENTS = ("perceive", "reason", "decide", "act")

# The validated study uses these five models. Free-text model ids are allowed, but
# the UI surfaces this list as the known-good default.
STUDY_MODELS = [
    "mistralai/Mistral-7B-Instruct-v0.3",
    "meta-llama/Meta-Llama-3-8B-Instruct",
    "Qwen/Qwen2.5-7B-Instruct",
    "microsoft/Phi-3-mini-4k-instruct",
    "google/gemma-2-9b-it",
]

# Sensible defaults, mirroring configs/run_full_l4.yaml so a barely-specified
# build is still a realistic, runnable config.
DEFAULT_WEIGHTS = {"accuracy": 0.40, "latency": 0.25, "vram": 0.20, "tokens": 0.15}
DEFAULT_TARGETS = {"accuracy_pct": 80.0, "latency_s": 5.0,
                   "vram_mb": 16000.0, "tokens_total": 1200.0}
DEFAULT_TIERS = {"healthy_min": 80.0, "degraded_min": 60.0}
DEFAULT_CLASSES = ["Brute Force", "Volumetric DDoS", "Port Scanning",
                   "DoS Hulk", "Benign"]
DEFAULT_MITRE = {
    "Brute Force": "T1110 - Brute Force",
    "Volumetric DDoS": "T1498 - Network Denial of Service",
    "Port Scanning": "T1046 - Network Service Discovery",
    "DoS Hulk": "T1499 - Endpoint Denial of Service",
    "Benign": "N/A - No adversary technique",
}
DEFAULT_AGENT_TOKENS = {"perceive": 160, "reason": 200, "decide": 12, "act": 160}

WEIGHT_SUM_TOL = 1e-6


class ConfigBuildError(ValueError):
    """A build input was invalid; the message is safe to show the user."""


def slugify(name: str) -> str:
    """Turn a config name into a safe filename stem (no path separators, no
    traversal). Rejects anything that slugs to empty."""
    raw = (name or "").strip()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    if not slug:
        raise ConfigBuildError(
            "config name must contain at least one letter or digit")
    return slug


def _as_float(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigBuildError(f"{field} must be a number, got {value!r}")


def normalise_weights(weights: dict[str, Any] | None) -> dict[str, float]:
    """Coerce the four SAW weights to floats, requiring all four present and the
    sum to be 1.0 (the SAW requirement config._validate also enforces)."""
    if not weights:
        raise ConfigBuildError(
            "scoring weights are required (one each for: "
            + ", ".join(SAW_KEYS) + ")")
    missing = [k for k in SAW_KEYS if k not in weights]
    if missing:
        raise ConfigBuildError(
            "scoring weights missing: " + ", ".join(missing))
    extra = [k for k in weights if k not in SAW_KEYS]
    if extra:
        raise ConfigBuildError(
            "unknown scoring weight(s): " + ", ".join(sorted(extra))
            + " (allowed: " + ", ".join(SAW_KEYS) + ")")
    out = {k: _as_float(weights[k], f"weight '{k}'") for k in SAW_KEYS}
    for k, v in out.items():
        if v < 0:
            raise ConfigBuildError(f"weight '{k}' must be >= 0, got {v}")
    total = sum(out.values())
    if abs(total - 1.0) > WEIGHT_SUM_TOL:
        raise ConfigBuildError(
            f"scoring weights must sum to 1.0, got {total:.4f} "
            f"({', '.join(f'{k}={out[k]:g}' for k in SAW_KEYS)})")
    return out


def _clean_models(models: Any) -> list[str]:
    if not models:
        raise ConfigBuildError("at least one model id is required")
    if isinstance(models, str):
        models = [models]
    cleaned: list[str] = []
    for m in models:
        mid = str(m).strip()
        if mid and mid not in cleaned:
            cleaned.append(mid)
    if not cleaned:
        raise ConfigBuildError("at least one non-empty model id is required")
    return cleaned


def build_config(
    *,
    name: str,
    models: Any,
    dataset_path: str,
    weights: dict[str, Any] | None = None,
    targets: dict[str, Any] | None = None,
    tiers: dict[str, Any] | None = None,
    agent_tokens: dict[str, Any] | None = None,
    provider: str = "hf",
    classes: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble (and validate) a complete config dict from measurement inputs.

    Mirrors configs/run_full_l4.yaml's structure so the result is a realistic,
    runnable config; raises ConfigBuildError with a clear message on bad input.
    Does NOT write anything — see write_user_config / build_and_write.
    """
    if provider not in ("mock", "hf"):
        raise ConfigBuildError(
            f"provider must be 'mock' or 'hf', got {provider!r}")

    model_ids = _clean_models(models)

    ds = str(dataset_path or "").strip()
    if not ds:
        raise ConfigBuildError("dataset path is required")

    w = normalise_weights(weights or DEFAULT_WEIGHTS)

    # Targets / tiers: fill defaults, coerce provided values to float.
    tg = dict(DEFAULT_TARGETS)
    for k, v in (targets or {}).items():
        if k not in DEFAULT_TARGETS:
            raise ConfigBuildError(f"unknown target '{k}'")
        tg[k] = _as_float(v, f"target '{k}'")
    ti = dict(DEFAULT_TIERS)
    for k, v in (tiers or {}).items():
        if k not in DEFAULT_TIERS:
            raise ConfigBuildError(f"unknown tier '{k}'")
        ti[k] = _as_float(v, f"tier '{k}'")
    if ti["degraded_min"] > ti["healthy_min"]:
        raise ConfigBuildError(
            "tier 'degraded_min' must be <= 'healthy_min' "
            f"({ti['degraded_min']} > {ti['healthy_min']})")

    at = dict(DEFAULT_AGENT_TOKENS)
    for k, v in (agent_tokens or {}).items():
        if k not in DEFAULT_AGENT_TOKENS:
            raise ConfigBuildError(
                f"unknown agent '{k}' (expected one of: "
                + ", ".join(_PIPELINE_AGENTS) + ")")
        iv = int(_as_float(v, f"agent tokens '{k}'"))
        if iv <= 0:
            raise ConfigBuildError(f"agent tokens '{k}' must be > 0, got {iv}")
        at[k] = iv

    cls = list(classes) if classes else list(DEFAULT_CLASSES)
    mitre = {c: DEFAULT_MITRE.get(c, "N/A") for c in cls}

    config: dict[str, Any] = {
        "run": {
            "mode": "precompute",
            "device": "cuda" if provider == "hf" else "cpu",
            "require_gpu": provider == "hf",
            "seed": 42,
            "free_vram_tolerance_mb": 500,
            "cleanup_model_cache_after": True,
            "models": model_ids,
        },
        "model": {
            "provider": provider,
            "name": model_ids[0],
            "hf": {
                "token_env": "HF_TOKEN",
                "dtype": "float16",
                "device_map": None,
                "max_new_tokens": 200,
                "temperature": 0.0,
                "do_sample": False,
                "quantization": {
                    "load_in_4bit": True,
                    "load_in_8bit": False,
                    "bnb_4bit_quant_type": "nf4",
                    "bnb_4bit_use_double_quant": True,
                    "bnb_4bit_compute_dtype": "float16",
                },
            },
            "mock": {"latency_s": 0.0},
        },
        "dataset": {
            "path": ds,
            "label_column": "label",
            "id_column": None,
            "drop_columns": [],
            "limit": None,
            "max_feature_chars": 4000,
            "label_map": {},
            "drop_labels": [],
        },
        "pipeline": {
            "agents": list(_PIPELINE_AGENTS),
            "max_new_tokens": at,
        },
        "classes": cls,
        "mitre": mitre,
        "scoring": {
            "weights": w,
            "targets": tg,
            "tiers": ti,
        },
        "storage": {
            # A user run writes to its OWN DB, never the locked study DB.
            "sqlite_path": f"results/agentmeter_user_{slugify(name)}.db",
        },
        "output": {"results_dir": "results"},
    }
    return config


def _resolve_target(name: str, base_dir: Path | None = None) -> Path:
    """Resolve the target path for a user config and REFUSE anything outside the
    user config dir or that collides with a protected (locked/root) file."""
    base = (base_dir or USER_CONFIG_DIR)
    slug = slugify(name)
    target = (base / f"{slug}.yaml")
    resolved = target.resolve()

    # Never write a protected file, even if a base_dir override pointed here.
    if resolved in _PROTECTED:
        raise ConfigBuildError(
            f"refusing to write protected config: {resolved.name} is locked "
            "and cannot be overwritten by the builder")

    # Must stay inside the intended user config directory (no traversal).
    base_resolved = base.resolve()
    if base_resolved not in resolved.parents:
        raise ConfigBuildError(
            "refusing to write outside the user config directory")
    return target


def write_user_config(name: str, config: dict[str, Any],
                      base_dir: Path | None = None,
                      overwrite: bool = True) -> Path:
    """Write an already-built config dict to configs/user/<name>.yaml.

    Refuses any path resolving to the locked study config or the root config, or
    escaping the user config directory. Returns the written path.
    """
    target = _resolve_target(name, base_dir)
    if target.exists() and not overwrite:
        raise ConfigBuildError(
            f"config '{target.name}' already exists (set overwrite to replace)")
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(to_yaml(config))
    return target


def to_yaml(config: dict[str, Any]) -> str:
    """Render a config dict to YAML text (stable key order, block style)."""
    header = ("# Generated by the AgentMeter config builder (Phase 18).\n"
              "# This is a USER config — edit or regenerate freely. It does NOT\n"
              "# affect the locked study config (configs/run_full_l4.yaml).\n\n")
    body = yaml.safe_dump(config, sort_keys=False, default_flow_style=False,
                          allow_unicode=True)
    return header + body


def build_and_write(base_dir: Path | None = None, overwrite: bool = True,
                    **inputs: Any) -> tuple[Path, dict[str, Any]]:
    """Build, validate and write in one step. Returns (path, config)."""
    name = inputs.get("name", "")
    config = build_config(**inputs)
    path = write_user_config(name, config, base_dir=base_dir, overwrite=overwrite)
    return path, config
