"""CSV input formats: rename CICFlowMeter columns that use other names, find the label column.

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product. Many flow
datasets carry the same CICFlowMeter features as CIC-IDS2017 under other column names
(CSE-CIC-IDS2018 "Tot Fwd Pkts", the Python port's "tot_fwd_pkts", CICFlowMeter-V4 "Total Fwd
Packet"). configs/input_formats.yaml lists those aliases; this module maps a file's header onto
the CIC-IDS2017 names the pipeline uses and picks the label column. It only renames: no value
is computed or guessed, and every rename is reported. User runs only — the locked study's
dataset never goes through here.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from .feature_map import CIC_FEATURES, FEATURE_MAP

FORMATS_PATH = Path(__file__).resolve().parents[2] / "configs" / "input_formats.yaml"
_BINARY = {"0", "1", "0.0", "1.0", "true", "false"}
_MISSING = {"", "nan", "none", "null", "na", "n/a"}


def norm(name: str) -> str:
    """Case-, space- and punctuation-insensitive key: 'Tot Fwd Pkts' == 'tot_fwd_pkts'."""
    return re.sub(r"[^a-z0-9]", "", str(name).casefold())


def load_formats(path: Optional[str | Path] = None) -> dict[str, Any]:
    p = Path(path or FORMATS_PATH)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    formats: dict[str, dict[str, Any]] = {}
    for key, f in (data.get("formats") or {}).items():
        cols = {str(a): str(c) for a, c in (f.get("columns") or {}).items()}
        if f.get("from_feature_map"):
            for cic, port_key, status, _ in FEATURE_MAP:
                if status != "duplicate":
                    cols.setdefault(port_key, cic)
        formats[str(key)] = {"name": str(f.get("name") or key), "columns": cols}
    labels = data.get("labels") or {}
    return {"source": str(p), "formats": formats,
            "duplicates": {str(k): str(v) for k, v in (data.get("duplicates") or {}).items()},
            "label_columns": [str(c) for c in (labels.get("columns") or ["Label"])],
            "label_values": {str(k): str(v) for k, v in (labels.get("values") or {}).items()}}


def resolve_columns(cols: Iterable[str], keep: Iterable[str], fmt: Optional[dict] = None) -> dict[str, Any]:
    """Map a header onto CIC-IDS2017 names.

    keep: names that are already right (the 78 features, identification columns); a column
    with such a name is never renamed, and an alias never overrides it. Returns
    {"rename": {file column: CIC name}, "format": key | None, "format_name", "conflicts"}."""
    fmt = fmt or load_formats()
    cols = [str(c) for c in cols]
    keep = list(keep)
    exact = set(cols) & set(keep)
    by_norm = {norm(k): k for k in keep}                       # case / punctuation variants
    alias: dict[str, tuple[str, Optional[str]]] = {}
    for key, f in fmt["formats"].items():
        for a, c in f["columns"].items():
            alias.setdefault(norm(a), (c, key))
    per_format = {key: {norm(a) for a in f["columns"]} for key, f in fmt["formats"].items()}
    rename: dict[str, str] = {}
    hits: dict[str, int] = {}
    conflicts: list[str] = []
    taken = set(exact)
    for c in cols:
        if c in exact:
            continue
        target, key = (by_norm[norm(c)], None) if norm(c) in by_norm else alias.get(norm(c), (None, None))
        if target is None:
            continue
        if target in taken:
            conflicts.append(f"{c!r} also maps to {target!r}; the first column is kept")
            continue
        rename[c] = target
        taken.add(target)
        if key:                                   # credit every format that knows this name
            for k, names in per_format.items():
                if norm(c) in names:
                    hits[k] = hits.get(k, 0) + 1
    best = max(hits, key=hits.get) if hits else None   # the format that explains the most columns
    return {"rename": rename, "format": best,
            "format_name": fmt["formats"][best]["name"] if best else None,
            "conflicts": conflicts}


def pick_label_column(sample, cols: list[str], fmt: Optional[dict] = None) -> tuple[Optional[str], list[str]]:
    """The label column (or None) and notes. Candidates come from input_formats.yaml in
    priority order, matched case-insensitively; a 0/1-only column is skipped (an attack yes/no
    flag cannot be mapped to the classes)."""
    fmt = fmt or load_formats()
    by_norm = {norm(c): c for c in cols}
    notes: list[str] = []
    for cand in fmt["label_columns"]:
        col = by_norm.get(norm(cand))
        if col is None:
            continue
        vals = {str(v).strip().casefold() for v in sample[col].dropna().unique()} - _MISSING
        if vals and vals <= _BINARY:
            notes.append(f"column {col!r} only says attack yes/no (0/1), which cannot be mapped to the "
                         "classes — it is not used as the label")
            continue
        if norm(col) != "label":                    # say so when it is not the usual "Label"
            notes.append(f"label column {col!r} detected")
        return col, notes
    return None, notes
