"""Configuration loader.

All tunable values (model name, dataset path, scoring weights, run mode,
thresholds) live in config.yaml. This module loads it and exposes it as a
plain nested dict plus a few typed accessors. Nothing is hard-coded here.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# Project root = parent of the agentmeter/ package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class Config:
    """Thin wrapper over the parsed config.yaml dict."""

    def __init__(self, data: dict[str, Any], path: Path):
        self._data = data
        self.path = path

    # --- generic access -------------------------------------------------
    def get(self, dotted_key: str, default: Any = None) -> Any:
        """Fetch a nested value with a dotted path, e.g. 'model.provider'."""
        node: Any = self._data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    # --- convenience accessors used across phases -----------------------
    def resolve_path(self, dotted_key: str, default: str | None = None) -> Path:
        """Resolve a configured (possibly relative) path against the project root."""
        raw = self.get(dotted_key, default)
        if raw is None:
            raise KeyError(f"No path configured at '{dotted_key}'")
        p = Path(raw)
        return p if p.is_absolute() else (PROJECT_ROOT / p)


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load and lightly validate config.yaml."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    _validate(data, cfg_path)
    return Config(data, cfg_path)


def _validate(data: dict[str, Any], cfg_path: Path) -> None:
    """Fail fast on obvious misconfiguration."""
    required_sections = ["run", "model", "dataset", "pipeline", "classes", "scoring"]
    missing = [s for s in required_sections if s not in data]
    if missing:
        raise ValueError(
            f"config.yaml ({cfg_path}) missing required section(s): {', '.join(missing)}"
        )

    provider = data.get("model", {}).get("provider")
    if provider not in ("mock", "hf"):
        raise ValueError(f"model.provider must be 'mock' or 'hf', got: {provider!r}")

    # Scoring weights must be present and sum to ~1.0 (SAW requirement).
    weights = data.get("scoring", {}).get("weights", {})
    if not weights:
        raise ValueError("scoring.weights is required (SAW weights, must not be hard-coded).")
    total = sum(float(v) for v in weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(
            f"scoring.weights must sum to 1.0, got {total:.4f} ({weights})"
        )
