"""Phase 1 — dataset loader, data isolation, and feature-only prompt builder.

Contract (from the build plan, sec 4.1):
  - Input: a network-flow CSV with a Label column.
  - On load, STRIP the Label column from what the model sees; keep labels in
    memory only, as the validation reference (blind zero-shot).
  - Produce a compact, structured feature-only text prompt per row.
  - Output: a list of Scenario(scenario_id, feature_prompt, held_out_label).

Data isolation is the important guarantee here: the label (and any configured
id / drop columns) must never appear in feature_prompt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .config import Config


@dataclass
class Scenario:
    """One CSV row turned into a blind zero-shot test case."""

    scenario_id: str
    feature_prompt: str          # feature-only text the model sees (NO label)
    held_out_label: str          # ground-truth class (after mapping), memory only
    raw_features: dict[str, Any] = field(default_factory=dict)  # for storage/debug
    raw_label: str = ""          # original CSV label before any mapping


def _format_value(value: Any) -> str:
    """Render a feature value compactly and deterministically."""
    if isinstance(value, float):
        # Trim floats: integers as ints, otherwise up to 4 sig figs, no sci-notation noise.
        if value.is_integer():
            return str(int(value))
        return f"{value:.4g}"
    return str(value)


def build_feature_prompt(features: dict[str, Any], max_chars: int | None = None) -> str:
    """Build a compact, structured, feature-only description of one flow.

    Ordering follows the dict insertion order (i.e. CSV column order), so prompts
    are stable and reproducible across runs.
    """
    lines = ["Network flow features:"]
    for name, value in features.items():
        lines.append(f"- {name}: {_format_value(value)}")
    prompt = "\n".join(lines)
    if max_chars is not None and len(prompt) > max_chars:
        prompt = prompt[:max_chars].rstrip() + "\n- [truncated]"
    return prompt


class DatasetLoader:
    """Loads a network-flow CSV and produces isolated, blind test scenarios."""

    def __init__(self, config: Config):
        self.config = config
        self.path: Path = config.resolve_path("dataset.path")
        self.label_column: str = config.get("dataset.label_column", "Label")
        self.id_column: str | None = config.get("dataset.id_column")
        self.drop_columns: list[str] = list(config.get("dataset.drop_columns", []) or [])
        self.limit: int | None = config.get("dataset.limit")
        self.max_feature_chars: int | None = config.get("dataset.max_feature_chars")
        self.known_classes: list[str] = list(config.get("classes", []) or [])
        # Ground-truth normalization for real datasets (memory only; never seen
        # by the model). Applied to the held-out label, not the features.
        self.label_map: dict[str, str] = dict(config.get("dataset.label_map", {}) or {})
        self.drop_labels: set[str] = set(config.get("dataset.drop_labels", []) or [])

    def load(self) -> list[Scenario]:
        if not self.path.exists():
            raise FileNotFoundError(f"Dataset not found: {self.path}")

        df = pd.read_csv(self.path)

        if self.label_column not in df.columns:
            raise ValueError(
                f"Label column '{self.label_column}' not found in {self.path.name}. "
                f"Columns: {list(df.columns)}"
            )

        # Drop rows whose raw label is excluded (e.g. classes out of taxonomy),
        # BEFORE applying the pilot row limit.
        if self.drop_labels:
            df = df[~df[self.label_column].astype(str).isin(self.drop_labels)]

        df = df.reset_index(drop=True)
        if self.limit is not None:
            df = df.head(int(self.limit))

        # --- DATA ISOLATION -------------------------------------------------
        # Labels are pulled out FIRST and never re-attached to the model view.
        raw_labels = df[self.label_column].astype(str).tolist()
        # Map raw dataset labels onto the canonical taxonomy (memory only).
        labels = [self.label_map.get(lbl, lbl) for lbl in raw_labels]

        id_values = None
        if self.id_column and self.id_column in df.columns:
            id_values = df[self.id_column].astype(str).tolist()

        # Columns hidden from the model: the label, the id column, and any extras.
        hidden = {self.label_column}
        if self.id_column:
            hidden.add(self.id_column)
        hidden.update(self.drop_columns)

        feature_df = df.drop(columns=[c for c in hidden if c in df.columns])

        # Hard guarantee: the label must not survive into the feature view.
        assert self.label_column not in feature_df.columns, "label leaked into features"

        scenarios: list[Scenario] = []
        for i, (_, row) in enumerate(feature_df.iterrows()):
            features = row.to_dict()
            scenario_id = id_values[i] if id_values is not None else f"row_{i:04d}"
            prompt = build_feature_prompt(features, self.max_feature_chars)
            scenarios.append(
                Scenario(
                    scenario_id=scenario_id,
                    feature_prompt=prompt,
                    held_out_label=labels[i],
                    raw_features=features,
                    raw_label=raw_labels[i],
                )
            )
        return scenarios

    def summarize(self, scenarios: list[Scenario]) -> dict[str, Any]:
        """Small summary for the CLI: counts, label distribution, unknown labels."""
        label_counts: dict[str, int] = {}
        raw_counts: dict[str, int] = {}
        for s in scenarios:
            label_counts[s.held_out_label] = label_counts.get(s.held_out_label, 0) + 1
            raw_counts[s.raw_label] = raw_counts.get(s.raw_label, 0) + 1
        # After mapping, which effective labels are still outside the taxonomy?
        unknown = sorted(
            {lbl for lbl in label_counts if self.known_classes and lbl not in self.known_classes}
        )
        return {
            "n_scenarios": len(scenarios),
            "label_distribution": dict(sorted(label_counts.items())),
            "raw_label_distribution": dict(sorted(raw_counts.items())),
            "labels_not_in_config_classes": unknown,
            "label_map_applied": dict(self.label_map),
            "dataset_path": str(self.path),
        }
