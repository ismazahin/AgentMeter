"""Small per-model side measurements of a run, each written to its own JSON file beside the
session DB (never into the measured tables, never part of a score):

  model_load/<run>__<model>.json    how long loading the model took (cold start), every run
  repeat_check/<run>__<model>.json  opt-in: the first flows answered a second time, after the
                                    measured flows, to see whether the answers repeat

The repeat check uses a plain pipeline (no instrumentation hook), in the same worker with the
model still loaded, like the load-scaling step (load_test.py).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional, Sequence

REPEAT_FLOWS = 10
KINDS = ("model_load", "repeat_check")


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


def run_repeat_check(cfg, provider, scenarios: Sequence, label: str, db_path: str | Path, run_id: str,
                     n: int = REPEAT_FLOWS) -> dict[str, Any]:
    from ..pipeline import Pipeline

    pipeline = Pipeline(cfg, provider)                      # plain: no instrumentation hook
    rows = []
    for s in list(scenarios)[:n]:
        try:
            st = pipeline.run(s.scenario_id, s.feature_prompt)
            rows.append({"scenario_id": s.scenario_id, "predicted": (st.get("verdict") or {}).get("predicted_class", "Unparseable")})
        except Exception as e:  # noqa: BLE001 — recorded; the measured flows are already safe
            rows.append({"scenario_id": s.scenario_id, "predicted": None, "error": f"{type(e).__name__}: {e}"[:200]})
    data = {"model": label, "n": len(rows), "flows": rows,
            "method": f"After the measured flows, the first {n} flows answered again by the same loaded model "
                      "(plain pipeline, same prompts and settings); compared with the measured answers."}
    write(db_path, run_id, label, "repeat_check", data)
    print(f"    repeat check: {len(rows)} flows answered again")
    return data
