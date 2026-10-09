"""CSV input path: a CIC-IDS2017-format flow CSV (optionally LABELLED).

Role vs the PCAP path:
  * CSV (CIC-IDS2017 format) already carries the 78 reference features computed
    by the Java CICFlowMeter, so features match exactly, and usually a label
    column -> resource efficiency AND accuracy.
  * PCAP (raw) -> resource efficiency only (22-feature gap, no labels).

Validation (rejects rather than silently proceeding):
  1. Not a CSV at all: a packet capture (pcap magic), binary content, or an
     empty / header-less file.
  2. Schema: column names are whitespace-stripped (official CIC files use
     ' Label', ' Flow Duration' …). All 78 CIC-IDS2017 feature columns must be
     present. With allow_partial=True a subset is accepted, but only if it still
     contains every column the rule-base needs; the run is then marked
     feature_match "partial".
  3. Values: feature columns are coerced to numbers. Rows with NaN/Inf in any
     present feature are DROPPED and counted, the same declared policy as
     data/dataprep.py. If no valid rows remain the file is rejected.

Labels (the accuracy enabler) are detected case-insensitively ('Label'/'label'),
normalised (case-insensitively) through config.yaml `data_prep.label_map` (raw CIC
label -> canonical class) or accepted when already canonical (`classes`).
A named label outside the 5 study classes is, by default, EXCLUDED and counted
(mirroring dataprep). With other_attack=True (opt-in, user runs only) it becomes
the 6th class "Other Attack" instead, and the run's class_scheme says "6-class" so
accuracy/confusion is computed over the right set. Empty/NaN labels are never an
attack: those rows are excluded as missing labels. The locked baseline (its
dataset, DB and 5 classes) is never touched by any of this.

The label is then split off: it never enters the feature table the model sees —
it is kept as a separate held-out table keyed by flow_id, like
DatasetLoader.held_out_label. (The CSV path's optional class_balance rule may
read it for SELECTION only; see rules.py.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..config import load_config
from ..data.dataprep import _normalize_columns
from .feature_map import CIC_FEATURES, META_COLUMNS
from .pcap import _MAGIC

DEFAULT_MAX_BYTES = 2 * 1024 ** 3
DEFAULT_SEED = 42                 # config.yaml run.seed: the pool draw is reproducible
POOL_CHUNK_ROWS = 100_000

# Identification columns of the full "TrafficLabelling" CIC-IDS2017 CSVs, mapped
# onto the shared META_COLUMNS. They are not features (MachineLearningCVE CSVs
# omit them) and are hidden from the model like the PCAP path's META columns.
CSV_META_SOURCES = {"Source IP": "src_ip", "Source Port": "src_port",
                    "Destination IP": "dst_ip", "Protocol": "protocol", "Timestamp": "timestamp"}
_IGNORED = {"Flow ID"}
_PROTO_NAMES = {6: "TCP", 17: "UDP", 0: "HOPOPT"}


# 6th class for NEW USER CSV runs only (opt-in): named attack labels outside the 5
# study classes (DoS GoldenEye, Heartbleed, Web Attack …, Infiltration, Bot …).
OTHER_ATTACK = "Other Attack"
_MISSING_LABELS = {"", "nan", "none", "null", "na", "n/a"}


class CsvValidationError(ValueError):
    """The file is not a usable CIC-IDS2017-format CSV."""


@dataclass
class CsvInput:
    features: pd.DataFrame               # META_COLUMNS + 78 CIC features (missing ones NaN)
    labels: Optional[pd.DataFrame]       # flow_id, label_raw, label — held out, never a feature
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def labelled(self) -> bool:
        return self.labels is not None


def _sniff(path: Path) -> None:
    if not path.is_file():
        raise CsvValidationError(f"not a file: {path}")
    with path.open("rb") as fh:
        head = fh.read(4096)
    if not head.strip():
        raise CsvValidationError(f"{path.name}: empty file")
    if head[:4] in _MAGIC:
        raise CsvValidationError(f"{path.name}: this is a packet capture, not a CSV — use the PCAP input")
    if b"\x00" in head:
        raise CsvValidationError(f"{path.name}: binary content, not a CSV")


def _label_maps(config_path: Optional[str]) -> tuple[dict[str, str], list[str]]:
    cfg = load_config(config_path)
    label_map = {str(k).strip(): str(v) for k, v in (cfg.get("data_prep.label_map", {}) or {}).items()}
    classes = [str(c) for c in (cfg.get("classes", []) or [])]
    return label_map, classes


def required_columns(rule_fields: set[str]) -> set[str]:
    """CIC feature columns the rule-base reads (META fields are filled for CSV)."""
    return {f for f in rule_fields if f in CIC_FEATURES}


def _allocate(counts: dict[str, int], total: int) -> dict[str, int]:
    """Split `total` pool slots across strata: rare strata keep every row, the
    rest share what is left equally (water-filling)."""
    out: dict[str, int] = {}
    left, todo = total, sorted(counts, key=lambda k: counts[k])
    for i, k in enumerate(todo):
        share = left // (len(todo) - i)
        out[k] = min(counts[k], share)
        left -= out[k]
    return out


def read_pool(p: Path, *, max_rows: Optional[int], label_col: Optional[str], read_kw: dict,
              seed: int = DEFAULT_SEED, progress=None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """One streaming pass over the WHOLE file -> the candidate pool (<= max_rows).

    A file with <= max_rows data rows is returned whole, in file order (identical
    to reading it at once). A larger file gets a stratified random sample: every
    row draws a uniform random key; per stratum (raw label, or one stratum when
    unlabelled) the rows with the smallest keys are kept, up to a water-filled
    share of max_rows. That is a uniform sample within each stratum, drawn from
    the whole file in one pass with bounded memory, and no label present in the
    file is lost. Rows keep their file position (index), so flow ids are the
    original data-row numbers."""
    rng = np.random.default_rng(seed)
    pool: Optional[pd.DataFrame] = None
    seen: dict[str, int] = {}
    n = 0
    trimmed = False
    for chunk in pd.read_csv(p, chunksize=POOL_CHUNK_ROWS, **read_kw):
        chunk.columns = _normalize_columns(chunk.columns)
        chunk.index = pd.RangeIndex(n, n + len(chunk))
        n += len(chunk)
        strat = (chunk[label_col].fillna("").astype(str).str.strip() if label_col
                 else pd.Series("(all rows)", index=chunk.index))
        for k, v in strat.value_counts().items():
            seen[str(k)] = seen.get(str(k), 0) + int(v)
        if max_rows is not None:
            chunk["__stratum"] = strat.to_numpy()
            chunk["__key"] = rng.random(len(chunk))
        pool = chunk if pool is None else pd.concat([pool, chunk])
        if max_rows is not None and len(pool) > max_rows:
            cap = _allocate(seen, max_rows)
            rank = pool.groupby("__stratum")["__key"].rank(method="first")
            pool = pool[rank.to_numpy() <= pool["__stratum"].map(cap).to_numpy()]
            trimmed = True
        if progress:
            progress(phase="sampling", rows_scanned=n,
                     message=f"scanning rows across the file ({n:,} so far)")
    if pool is None:
        pool = pd.read_csv(p, nrows=0, **read_kw)
        pool.columns = _normalize_columns(pool.columns)
    pool = pool.drop(columns=["__stratum", "__key"], errors="ignore").sort_index()
    in_pool = ((pool[label_col].fillna("").astype(str).str.strip() if label_col
                else pd.Series("(all rows)", index=pool.index)).value_counts())
    return pool, {
        "method": "stratified_reservoir" if trimmed else "full_file",
        "description": (f"stratified random sample of {len(pool):,} rows drawn from all {n:,} rows "
                        f"of the file (per-label reservoir, seed {seed})" if trimmed else
                        f"all {n:,} rows of the file (within the {max_rows:,}-row pool cap)"
                        if max_rows is not None else f"all {n:,} rows of the file"),
        "rows_in_file": n, "pool_rows": int(len(pool)), "max_pool_rows": max_rows,
        "stratified_by": (label_col if label_col else None), "seed": seed if trimmed else None,
        "strata_in_file": dict(sorted(seen.items())),
        "strata_in_pool": {str(k): int(v) for k, v in sorted(in_pool.items())},
    }


def _canonical_counts(raw: dict[str, int], by_map: dict, by_class: dict, other_attack: bool) -> dict[str, int]:
    """Raw-label counts (whole file) -> counts per canonical class."""
    out: dict[str, int] = {}
    for v, n in raw.items():
        hit = by_map.get(v.casefold()) or by_class.get(v.casefold())
        if hit is None and other_attack and v.casefold() not in _MISSING_LABELS:
            hit = OTHER_ATTACK
        if hit is not None:
            out[hit] = out.get(hit, 0) + int(n)
    return dict(sorted(out.items()))


def load_csv(path: str | Path, *, rule_fields: Optional[set[str]] = None,
             allow_partial: bool = False, other_attack: bool = False, max_rows: Optional[int] = None,
             max_bytes: int = DEFAULT_MAX_BYTES, config_path: Optional[str] = None,
             seed: int = DEFAULT_SEED, progress=None) -> CsvInput:
    """Validate and load a CIC-IDS2017-format CSV, or raise CsvValidationError.

    max_rows caps the CANDIDATE POOL, drawn from across the whole file (read_pool);
    a file within the cap is loaded in full, exactly as before."""
    p = Path(path)
    _sniff(p)
    size = p.stat().st_size
    if size > max_bytes:
        raise CsvValidationError(f"{p.name}: {size:,} bytes exceeds the {max_bytes:,}-byte limit")

    # Official CIC files contain a mis-encoded byte in 'Web Attack – …' labels.
    read_kw = dict(low_memory=False, encoding_errors="replace", skipinitialspace=True)
    try:
        # A short sample surfaces a malformed file before any long scan.
        cols = list(_normalize_columns(pd.read_csv(p, nrows=1000, **read_kw).columns))
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as e:
        raise CsvValidationError(f"{p.name}: not a readable CSV ({e})") from e
    except ValueError as e:
        raise CsvValidationError(f"{p.name}: {e}") from e

    # Schema checks on the header + sample, before the whole file is scanned.
    present = [c for c in CIC_FEATURES if c in cols]
    missing = [c for c in CIC_FEATURES if c not in cols]
    label_col = next((c for c in cols if c.lower() == "label"), None)
    extra = [c for c in cols if c not in CIC_FEATURES and c != label_col
             and c not in CSV_META_SOURCES and c not in _IGNORED]

    if not present:
        raise CsvValidationError(
            f"{p.name}: not CIC-IDS2017 format — none of the 78 feature columns found "
            f"(first columns: {cols[:6]})")
    if missing and not allow_partial:
        raise CsvValidationError(
            f"{p.name}: {len(missing)} of 78 CIC-IDS2017 feature columns missing "
            f"({', '.join(missing[:8])}{' …' if len(missing) > 8 else ''}); "
            f"re-export with all columns or pass allow_partial")
    need_missing = sorted(required_columns(rule_fields or set()) - set(present))
    if need_missing:
        raise CsvValidationError(
            f"{p.name}: columns required by the flow-selection rules are missing: {need_missing}")

    try:
        df, sampling = read_pool(p, max_rows=max_rows, label_col=label_col, read_kw=read_kw,
                                 seed=seed, progress=progress)
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as e:
        raise CsvValidationError(f"{p.name}: not a readable CSV ({e})") from e
    except ValueError as e:
        raise CsvValidationError(f"{p.name}: {e}") from e
    if len(df) == 0:
        raise CsvValidationError(f"{p.name}: no data rows")

    # --- values: numeric features, declared NaN/Inf drop policy --------------------
    feats = df[present].apply(pd.to_numeric, errors="coerce").astype(float)
    non_numeric = {c: int(feats[c].isna().sum() - df[c].isna().sum())
                   for c in present if feats[c].isna().sum() > df[c].isna().sum()}
    bad = ~np.isfinite(feats.to_numpy()).all(axis=1)
    n_bad = int(bad.sum())

    # --- labels -------------------------------------------------------------------
    label_map, classes = _label_maps(config_path)
    by_map = {k.casefold(): v for k, v in label_map.items()}
    by_class = {c.casefold(): c for c in classes}
    excluded_labels: dict[str, int] = {}
    other_sources: dict[str, int] = {}
    raw_dist: dict[str, int] = {}
    n_missing_label = 0
    canon = None
    if label_col is not None:
        # fillna first: pandas 3's str dtype keeps NaN as a float, not "nan".
        raw = df[label_col].fillna("").astype(str).str.strip()
        raw_dist = {str(k): int(v) for k, v in raw.value_counts().items()}
        missing_label = raw.str.casefold().isin(_MISSING_LABELS)

        def to_class(v: str) -> Optional[str]:
            hit = by_map.get(v.casefold()) or by_class.get(v.casefold())
            if hit is None and other_attack and v.casefold() not in _MISSING_LABELS:
                return OTHER_ATTACK            # a named label outside the 5 study classes
            return hit

        canon = raw.map(to_class)
        n_missing_label = int((missing_label & ~bad).sum())
        out_of_tax = canon.isna() & ~bad & ~missing_label
        excluded_labels = {str(k): int(v) for k, v in raw[out_of_tax].value_counts().items()}
        is_other = (canon == OTHER_ATTACK) & ~bad
        other_sources = {str(k): int(v) for k, v in raw[is_other].value_counts().items()}
        keep = ~bad & canon.notna()
    else:
        keep = ~bad

    if not keep.any():
        raise CsvValidationError(
            f"{p.name}: no usable rows after dropping {n_bad} rows with NaN/Inf/non-numeric "
            f"features{' and out-of-taxonomy labels' if excluded_labels else ''}")

    # --- normalised feature table (unified shape) ------------------------------------
    kept_idx = df.index[keep]
    out = pd.DataFrame(index=kept_idx)
    out["flow_id"] = [f"row{int(i):07d}" for i in kept_idx]   # 0-based data-row number in the file
    for src, dst in CSV_META_SOURCES.items():
        out[dst] = df.loc[kept_idx, src].astype(str).str.strip() if src in cols else pd.NA
    out["src_port"] = pd.to_numeric(out["src_port"], errors="coerce").astype("Int64")
    out["protocol"] = pd.to_numeric(out["protocol"], errors="coerce").astype("Int64")
    out["protocol_name"] = [_PROTO_NAMES.get(int(v), str(int(v))) if pd.notna(v) else "unknown"
                            for v in out["protocol"]]
    for c in CIC_FEATURES:
        out[c] = feats.loc[kept_idx, c] if c in present else np.nan
    out = out[META_COLUMNS + CIC_FEATURES].reset_index(drop=True)

    labels = None
    if label_col is not None:
        labels = pd.DataFrame({"flow_id": out["flow_id"].to_numpy(),
                               "label_raw": raw.loc[kept_idx].to_numpy(),
                               "label": canon.loc[kept_idx].to_numpy()})
        # Hard guarantee, as in DatasetLoader: the label never survives into features.
        assert label_col not in out.columns and "label" not in out.columns, "label leaked into features"

    report = {
        "file": p.name, "file_size_bytes": size,
        "rows_read": int(sampling["rows_in_file"]), "rows_in_pool": int(len(df)),
        "rows_usable": int(len(out)),
        "rows_dropped_nan_inf": n_bad,
        "rows_excluded_out_of_taxonomy": int(sum(excluded_labels.values())),
        "rows_excluded_missing_label": n_missing_label,
        "sampling": {**sampling, "class_counts_in_file": _canonical_counts(
            sampling.get("strata_in_file") or {}, by_map, by_class, other_attack) if label_col else {}},
        "columns": {"expected": len(CIC_FEATURES), "present": len(present),
                    "missing": missing, "extra_ignored": extra,
                    "meta_found": [c for c in CSV_META_SOURCES if c in cols]},
        "non_numeric_values": non_numeric,
        "notes": ([] if "Protocol" in cols else
                  ["no Protocol column (MachineLearningCVE format): protocol_name is 'unknown', "
                   "so protocol-coverage rules see a single group"]),
        "label": {
            "column": label_col, "found": label_col is not None,
            "raw_distribution": raw_dist,
            "excluded_out_of_taxonomy": excluded_labels,
            "distribution": ({str(k): int(v) for k, v in labels["label"].value_counts().items()}
                             if labels is not None else {}),
            "mapping": "config.yaml data_prep.label_map (raw CIC label -> canonical class)",
            "classes": classes,
            "other_attack": {"enabled": other_attack, "rows": int(sum(other_sources.values())),
                             "sources": other_sources},
        },
        "class_scheme": class_scheme(labels, classes, other_attack),
    }
    if excluded_labels and not other_attack:
        report["notes"].append("rows with attack labels outside the 5 study classes were excluded; "
                               "enable other_attack (--other-attack) to keep them as 'Other Attack'")
    return CsvInput(features=out, labels=labels, report=report)


def class_scheme(labels: Optional[pd.DataFrame], classes: list[str],
                 other_attack: bool) -> Optional[dict[str, Any]]:
    """The class set downstream accuracy/confusion must use for THIS run.

    5-class unless Other Attack was enabled AND at least one row fell into it.
    Applies to user runs only: the locked baseline's 5 classes are never changed."""
    if labels is None:
        return None
    six = other_attack and bool((labels["label"] == OTHER_ATTACK).any())
    return {"name": "6-class" if six else "5-class",
            "classes": classes + ([OTHER_ATTACK] if six else []),
            "other_attack_enabled": other_attack,
            "note": ("5 study classes + 'Other Attack' (user-run only; baseline stays 5-class)"
                     if six else "the 5 study classes")}
