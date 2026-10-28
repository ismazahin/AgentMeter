"""The prepared-set hash (version 2): what the models see and are scored against.

Its own small module (json, csv, hashlib only), so the data preparation can import it
anywhere, including the browser (web/prep, Pyodide), without the analyses' dependencies.
agentmeter.session.analyses re-exports it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional


PREPARED_SET_HASH_VERSION = 2


def prepared_set_content_hash(features_csv: str, labels_csv: Optional[str],
                              feature_columns: list[str]) -> str:
    """Phase 47 follow-up — hash of WHAT THE MODELS SEE AND ARE SCORED AGAINST: the
    model-visible feature columns (input.json feature_columns), row by row in benchmark order,
    plus each row's ground-truth label (labels.csv `label`) when the set is labelled.

    Identification columns (IPs, ports, protocol, timestamp), the hidden flow id and the
    selection-audit columns are NOT part of it, so the backend's full copy and the control
    plane's stripped copy of the same prepared set hash the same. Values are hashed as the
    exact strings in the file (no float re-formatting)."""
    import csv
    import io

    rows = list(csv.reader(io.StringIO(features_csv)))
    header, body = rows[0], [r for r in rows[1:] if r]
    idx = [header.index(c) for c in feature_columns]          # KeyError-free: ValueError if missing
    labels: Optional[list[str]] = None
    if labels_csv:
        lrows = list(csv.reader(io.StringIO(labels_csv)))
        li = lrows[0].index("label")
        labels = [r[li] for r in lrows[1:] if r]
        if len(labels) != len(body):
            raise ValueError("labels.csv does not align with the features")
    h = hashlib.sha256(b"agentmeter-prepared-set-v2\0")
    h.update("\x1f".join(feature_columns).encode() + b"\n")
    for i, r in enumerate(body):
        h.update("\x1f".join(r[j] for j in idx).encode())
        h.update(b"\x1e" + (labels[i].encode() if labels is not None else b"") + b"\n")
    return h.hexdigest()


def prepared_set_sha256(run_dir: str | Path) -> Optional[str]:
    """Hash of the flows actually benchmarked — see prepared_set_content_hash (version 2).
    None when the run dir has no flows."""
    run_dir = Path(run_dir)
    sel, meta = run_dir / "selected_flows.csv", run_dir / "input.json"
    if not sel.is_file() or not meta.is_file():
        return None
    try:
        m = json.loads(meta.read_text(encoding="utf-8"))
        lab = run_dir / "labels.csv"
        return prepared_set_content_hash(sel.read_text(encoding="utf-8"),
                                         lab.read_text(encoding="utf-8") if lab.is_file() else None,
                                         list(m["feature_columns"]))
    except (OSError, ValueError, KeyError):
        return None
