"""Unified input contract: what BOTH input paths hand to the later LLM phase.

Whatever the source (CSV or PCAP), a run directory contains:

    input.json           the metadata record below (schema_version 1)
    selected_flows.csv   META_COLUMNS + the 78 CIC-IDS2017 features + selection columns
    labels.csv           ONLY for labelled CSV: flow_id, label_raw, label (held out)

`input.json` tells the pipeline phase what it may compute:

    source_type        "csv" | "pcap"
    input_role         "labelled_csv" | "unlabelled_csv" | "raw_pcap"
    evaluation_mode    "accuracy_available" | "efficiency_only"
    capabilities       {"resource_efficiency": true, "accuracy": bool}
    class_scheme       {"name": "5-class" | "6-class", "classes": [...]} or null
    selection_mode     "label_aware_balanced" | "label_blind_statistical"
    accuracy_unavailable_reasons   why not, when accuracy is false
    feature_match      {"status": "exact" | "partial" | "approximate", ...}
    feature_columns    feature columns that carry real values (model input)
    hidden_columns     META_COLUMNS (+ label): never shown to the model

Label isolation: labels live only in labels.csv / InputRun.labels, keyed by
flow_id. selected_flows.csv never contains a label column (asserted on write and
on load).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .feature_map import CIC_FEATURES, META_COLUMNS

SCHEMA_VERSION = 1
SELECTION_COLUMNS = ["selection_rule", "selection_reason", "matched_rules"]
SELECTED_COLUMNS = META_COLUMNS + CIC_FEATURES + SELECTION_COLUMNS


class InputContractError(ValueError):
    """A run directory does not satisfy the unified input contract."""


@dataclass
class InputRun:
    metadata: dict[str, Any]
    selected: pd.DataFrame
    labels: Optional[pd.DataFrame] = None

    @property
    def accuracy_available(self) -> bool:
        return bool(self.metadata["capabilities"]["accuracy"])


def build_metadata(*, source_type: str, source_file: str, run_name: str, labelled: bool,
                   feature_match: dict[str, Any], feature_columns: list[str],
                   rows_total: int, rows_selected: int, rules_fired: list[str],
                   label_distribution_selected: Optional[dict[str, int]] = None,
                   class_scheme: Optional[dict[str, Any]] = None, label_aware_selection: bool = False,
                   extra_reasons: Optional[list[str]] = None) -> dict[str, Any]:
    if source_type not in ("csv", "pcap"):
        raise ValueError(f"unknown source_type {source_type!r}")
    accuracy = labelled and source_type == "csv"
    reasons: list[str] = []
    if not labelled:
        reasons.append("no ground-truth label column" if source_type == "csv"
                       else "raw PCAP has no ground-truth labels")
    reasons += extra_reasons or []
    return {
        "schema_version": SCHEMA_VERSION,
        "run_name": run_name,
        "source_type": source_type,
        "source_file": source_file,
        "input_role": ("labelled_csv" if accuracy else "unlabelled_csv") if source_type == "csv" else "raw_pcap",
        "evaluation_mode": "accuracy_available" if accuracy else "efficiency_only",
        "capabilities": {"resource_efficiency": True, "accuracy": accuracy},
        "accuracy_unavailable_reasons": [] if accuracy else reasons,
        "labelled": labelled,
        "feature_match": feature_match,
        "feature_columns": feature_columns,
        "hidden_columns": META_COLUMNS + (["label"] if labelled else []),
        "rows_total": rows_total,
        "rows_selected": rows_selected,
        "rules_fired": rules_fired,
        "label_distribution_selected": label_distribution_selected or {},
        # Class set accuracy/confusion must use for THIS run (None when unlabelled).
        # "6-class" = the 5 study classes + "Other Attack" — user runs only.
        "class_scheme": class_scheme,
        # label_aware_balanced: class_balance read held-out labels to pick rows
        # (selection only); label_blind_statistical: rules saw no labels at all.
        "selection_mode": "label_aware_balanced" if label_aware_selection else "label_blind_statistical",
        "framing": ("Input for MEASURING LLM resource efficiency (and accuracy where labels "
                    "exist). Not a threat-detection product; no threat decision is made here."),
    }


def _check_selected(df: pd.DataFrame) -> None:
    if list(df.columns) != SELECTED_COLUMNS:
        extra = [c for c in df.columns if c not in SELECTED_COLUMNS]
        missing = [c for c in SELECTED_COLUMNS if c not in df.columns]
        raise InputContractError(f"selected_flows columns differ (missing {missing}, extra {extra})")
    if any(c.lower() == "label" for c in df.columns):
        raise InputContractError("label column leaked into selected_flows")


def write_input_run(out_dir: Path, run: InputRun) -> None:
    _check_selected(run.selected)
    out_dir.mkdir(parents=True, exist_ok=True)
    run.selected.to_csv(out_dir / "selected_flows.csv", index=False)
    if run.labels is not None:
        if list(run.labels["flow_id"]) != list(run.selected["flow_id"]):
            raise InputContractError("labels.csv must align 1:1 with selected_flows.csv")
        run.labels.to_csv(out_dir / "labels.csv", index=False)
    (out_dir / "input.json").write_text(json.dumps(run.metadata, indent=2, default=float),
                                        encoding="utf-8")


def load_input_run(run_dir: str | Path) -> InputRun:
    """Load any run (CSV or PCAP) through the one contract the LLM phase uses."""
    d = Path(run_dir)
    meta = json.loads((d / "input.json").read_text(encoding="utf-8"))
    if meta.get("schema_version") != SCHEMA_VERSION:
        raise InputContractError(f"unsupported schema_version {meta.get('schema_version')!r}")
    sel = pd.read_csv(d / "selected_flows.csv", dtype={"flow_id": str, "src_ip": str, "dst_ip": str,
                                                       "timestamp": str, "protocol_name": str})
    _check_selected(sel)
    labels = None
    if meta["capabilities"]["accuracy"]:
        labels = pd.read_csv(d / "labels.csv", dtype=str)
        if list(labels["flow_id"]) != list(sel["flow_id"]):
            raise InputContractError("labels.csv does not align with selected_flows.csv")
    return InputRun(metadata=meta, selected=sel, labels=labels)
