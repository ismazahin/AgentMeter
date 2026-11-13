"""Small per-model side measurements of a run, each written to its own JSON file beside the
session DB (never into the measured tables, never part of a score):

  model_load/<run>__<model>.json    how long loading the model took (cold start), every run
  flow_times/<run>__<model>.json    when each flow started (unix seconds) and each agent's wall time,
                                    recorded between flows (outside every timed call) so the GPU
                                    timeline can mark each agent's turn

Sessions recorded before the repeat check was removed may also hold repeat_check/ files. They
are kept on disk but no longer read.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

KINDS = ("model_load", "flow_times")


def out_path(db_path: str | Path, run_id: str, label: str, kind: str) -> Path:
    assert kind in KINDS, kind
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", label)
    return Path(db_path).parent / kind / f"{run_id}__{slug}.json"


def write(db_path: str | Path, run_id: str, label: str, kind: str, data: dict[str, Any]) -> Path:
    p = out_path(db_path, run_id, label, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def read_all(db_path: str | Path, run_id: str, labels: dict[str, str], kind: str) -> dict[str, Optional[dict]]:
    """{model: data or None} for the session's models (labels: model -> DB label)."""
    out: dict[str, Optional[dict]] = {}
    for model, label in labels.items():
        p = out_path(db_path, run_id, label, kind)
        try:
            out[model] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except (OSError, ValueError):
            out[model] = None
    return out
