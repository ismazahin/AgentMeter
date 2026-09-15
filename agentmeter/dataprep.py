"""Dataset preparation for the AgentMeter full run (data-prep ONLY).

Builds ONE balanced N-per-class CSV from the four official CIC-IDS2017 CSVs, in
RAW feature values (never scaled/normalized). It does not touch agents, the
pipeline, instrumentation, the SQLite schema, or any detection logic — it only
produces the input CSV the DatasetLoader later reads.

Everything tunable lives in config.yaml under `data_prep:` (input dir, file list,
output path, label_map, seed, n_per_class) — nothing is hard-coded here, so the
mapping and sampling are declared in config and reproducible.

Pipeline:
  1. Read ONLY the label column of each file first (cheap) to confirm raw-label
     counts, handling CIC-IDS's leading-space column names (' Label').
  2. Read each file fully, normalize column names (strip surrounding spaces),
     concatenate, apply the config label_map, and drop unmapped rows.
  3. Inf/NaN policy (DECLARED): CIC-IDS has some non-finite values in flow
     features (e.g. 'Flow Bytes/s' = Infinity). We DROP any row with an Inf or
     NaN in ANY feature column BEFORE sampling, so the N/class we keep are all
     valid. We do NOT sample first and then drop (that would unbalance the set).
     Otherwise values are kept RAW — no scaling, no imputation.
  4. Sample EXACTLY n_per_class rows per canonical class with a fixed seed. BENIGN
     is pooled across all four files (we sample from the concatenated pool, so it
     is not taken from a single day). If any class has < n_per_class valid rows,
     FAIL LOUDLY naming the class and its count — never silently unbalance.
  5. Write the 78 original feature columns + a canonical 'label' column, and print
     the final class distribution + total as a sanity check.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from .config import PROJECT_ROOT, load_config


def _resolve(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def _normalize_columns(cols) -> list[str]:
    """Strip surrounding whitespace from column names (CIC-IDS uses ' Label')."""
    stripped = [str(c).strip() for c in cols]
    if len(set(stripped)) != len(stripped):
        dupes = sorted({c for c in stripped if stripped.count(c) > 1})
        raise ValueError(f"Column names collide after stripping whitespace: {dupes}")
    return stripped


def _find_label_column(raw_cols, label_column: str) -> str:
    """Return the RAW column name whose stripped form equals label_column."""
    for raw in raw_cols:
        if str(raw).strip() == label_column:
            return raw
    raise ValueError(
        f"Label column {label_column!r} not found (columns: {list(raw_cols)})"
    )


def _read_labels_only(path: Path, label_column: str) -> pd.Series:
    """Read ONLY the label column (cheap first pass), stripped."""
    header = pd.read_csv(path, nrows=0)
    raw_label = _find_label_column(header.columns, label_column)
    s = pd.read_csv(path, usecols=[raw_label]).iloc[:, 0]
    return s.astype(str).str.strip()


def build_dataset(
    config_path: Optional[str] = None,
    input_dir: Optional[str] = None,
    output_path: Optional[str] = None,
    seed: Optional[int] = None,
    n_per_class: Optional[int] = None,
) -> dict[str, Any]:
    cfg = load_config(config_path)
    dp = cfg.get("data_prep", {}) or {}

    input_dir = _resolve(input_dir or dp.get("input_dir", "data/cicids_raw"))
    output_path = _resolve(output_path or dp.get("output_path", "data/cicids_full_300.csv"))
    label_column = dp.get("label_column", "Label")
    out_label = dp.get("output_label_column", "label")
    seed = int(seed if seed is not None else dp.get("seed", 42))
    n_per_class = int(n_per_class if n_per_class is not None else dp.get("n_per_class", 60))
    label_map = dict(dp.get("label_map", {}) or {})
    files = list(dp.get("files", []) or [])

    if not label_map:
        raise ValueError("data_prep.label_map is empty — declare the raw->canonical mapping in config.")

    # Canonical classes are exactly the distinct label_map targets (declared).
    canonical = sorted(set(label_map.values()))

    # Source files: use the explicit config list when given, otherwise read EVERY
    # .csv in input_dir (sorted for a deterministic, reproducible order). The
    # label_map drops any rows whose label isn't mapped, so extra CSVs are safe.
    if files:
        paths = [input_dir / f for f in files]
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            raise FileNotFoundError(
                "Missing CIC-IDS input file(s):\n  " + "\n  ".join(missing)
                + f"\nPlace the official CSVs in {input_dir} (see config.data_prep.files), "
                "or clear data_prep.files to auto-read every .csv in that folder."
            )
    else:
        if not input_dir.is_dir():
            raise FileNotFoundError(f"Input dir not found: {input_dir}")
        paths = sorted(input_dir.glob("*.csv"))
        if not paths:
            raise FileNotFoundError(f"No .csv files found in {input_dir}")
        print(f"(data_prep.files empty — auto-reading {len(paths)} .csv file(s) from {input_dir})")

    print("=" * 74)
    print("  AgentMeter — build-dataset (RAW CIC-IDS -> balanced, config-driven)")
    print("=" * 74)
    print(f"Input dir     : {input_dir}")
    print(f"Output        : {output_path}")
    print(f"Seed          : {seed}   n_per_class: {n_per_class}   classes: {canonical}")
    print("")

    # --- pass 1: read ONLY labels to confirm raw counts -------------------
    print("Pass 1 — raw label counts (label column read alone):")
    raw_totals: dict[str, int] = {}
    for p in paths:
        s = _read_labels_only(p, label_column)
        vc = s.value_counts()
        for lbl, n in vc.items():
            raw_totals[lbl] = raw_totals.get(lbl, 0) + int(n)
        top = ", ".join(f"{k}={v}" for k, v in vc.head(6).items())
        print(f"  {p.name:<52} {len(s):>8} rows | {top}")
    mapped_totals = {k: v for k, v in sorted(raw_totals.items()) if k in label_map}
    print("  raw labels that map to a canonical class:")
    for lbl, n in mapped_totals.items():
        print(f"    {lbl:<22} -> {label_map[lbl]:<16} {n}")
    print("")

    # --- pass 2: read fully, normalize, map, concat -----------------------
    print("Pass 2 — reading full files, normalizing columns, mapping labels...")
    frames: list[pd.DataFrame] = []
    feature_cols_ref: Optional[list[str]] = None
    for p in paths:
        df = pd.read_csv(p, low_memory=False)
        df.columns = _normalize_columns(df.columns)
        if label_column not in df.columns:
            raise ValueError(f"{p.name}: no {label_column!r} column after normalization.")
        df[label_column] = df[label_column].astype(str).str.strip()
        # Map to canonical; keep only mapped rows (drop everything else).
        df[out_label] = df[label_column].map(label_map)
        df = df[df[out_label].notna()].copy()

        feats = [c for c in df.columns if c not in (label_column, out_label)]
        if feature_cols_ref is None:
            feature_cols_ref = feats
        elif feats != feature_cols_ref:
            raise ValueError(
                f"{p.name}: feature schema differs from the first file "
                f"(expected {len(feature_cols_ref)} cols, got {len(feats)})."
            )
        frames.append(df[feats + [out_label]])

    combined = pd.concat(frames, ignore_index=True)
    feature_cols = feature_cols_ref or []
    print(f"  mapped rows (all files pooled): {len(combined)}  | feature columns: {len(feature_cols)}")

    # --- Inf/NaN policy: drop rows with any non-finite feature BEFORE sampling ---
    numeric = combined[feature_cols].apply(pd.to_numeric, errors="coerce")
    bad_mask = numeric.replace([np.inf, -np.inf], np.nan).isna().any(axis=1)
    n_bad = int(bad_mask.sum())
    clean = combined[~bad_mask].copy()
    print(f"  Inf/NaN policy: dropped {n_bad} row(s) with a non-finite feature "
          f"BEFORE sampling; {len(clean)} valid rows remain (values kept RAW).")
    print("")

    # --- balance check + sampling ----------------------------------------
    avail = clean[out_label].value_counts().to_dict()
    short = {c: int(avail.get(c, 0)) for c in canonical if int(avail.get(c, 0)) < n_per_class}
    if short:
        detail = ", ".join(f"{c}={n}" for c, n in short.items())
        raise ValueError(
            f"FAIL: class(es) with < {n_per_class} valid rows after cleaning: {detail}. "
            f"Refusing to produce an unbalanced dataset. (Available: {avail})"
        )

    parts = []
    for c in canonical:  # deterministic order + fixed seed -> reproducible file
        pool = clean[clean[out_label] == c]
        parts.append(pool.sample(n=n_per_class, random_state=seed))
    out = pd.concat(parts, ignore_index=True)[feature_cols + [out_label]]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(output_path, index=False)

    # --- sanity summary ---------------------------------------------------
    dist = out[out_label].value_counts().reindex(canonical).to_dict()
    print("Final class distribution:")
    for c in canonical:
        print(f"    {c:<18} {int(dist[c])}")
    print(f"    {'TOTAL':<18} {len(out)}")
    print(f"\nWrote {len(out)} rows x {out.shape[1]} cols -> {output_path}")
    print("=" * 74)

    return {
        "output_path": str(output_path),
        "n_rows": int(len(out)),
        "n_feature_cols": len(feature_cols),
        "class_distribution": {c: int(dist[c]) for c in canonical},
        "dropped_non_finite": n_bad,
        "seed": seed,
        "n_per_class": n_per_class,
    }
