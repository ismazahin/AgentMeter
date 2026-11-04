"""The IDS stage of a session: score every selected flow, keep only the flagged ones for the LLM.

Runs in prepare_session, before any model is loaded. Writes into the run folder:
  ids_results.csv   flow_id, score, flagged, label   (every selected flow)
  ids.json          the model's identity, the threshold, timing, counts, training-overlap check
The session's scenarios (what the LLM agents triage) are the flagged flows only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .model import IdsError, load_model

IDS_RESULTS_CSV = "ids_results.csv"
IDS_JSON = "ids.json"


def normalize_ids_option(opt: Any) -> Optional[dict[str, Any]]:
    """None/False = no IDS; True = the model's own threshold; {"threshold": t} = threshold t."""
    if opt in (None, False):
        return None
    if opt is True:
        return {"threshold": None}
    if not isinstance(opt, dict):
        raise IdsError("'ids' must be true, false or {\"threshold\": 0..1}")
    t = opt.get("threshold")
    if t is not None and not (isinstance(t, (int, float)) and not isinstance(t, bool) and 0 < t <= 1):
        raise IdsError(f"IDS threshold must be a number in (0, 1], got {t!r}")
    return {"threshold": None if t is None else float(t)}


def apply_ids(run_dir: Path, scen: pd.DataFrame, features: list[str], opt: dict[str, Any],
              labelled: bool, model_path: Optional[Path] = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Score `scen` (flow_id + features + label); return (flagged rows, summary)."""
    model = load_model(model_path)
    tau = model.threshold if opt.get("threshold") is None else float(opt["threshold"])
    missing = [f for f in model.features if f not in features]
    scores, dt = model.score(scen)
    flagged = scores >= tau
    out = pd.DataFrame({"flow_id": scen["flow_id"].to_numpy(), "score": scores, "flagged": flagged,
                        "label": scen["label"].to_numpy() if labelled else None})
    out.to_csv(run_dir / IDS_RESULTS_CSV, index=False)
    n = int(len(scen))
    info = {
        "model": model.identity,
        "model_metrics_holdout": (model.meta.get("metrics") or {}).get("holdout"),
        "threshold": tau,
        "threshold_source": "model (" + str(model.meta.get("threshold_rule")) + ")" if opt.get("threshold") is None
        else "set for this session",
        "n_flows_in": n, "n_flagged": int(flagged.sum()), "n_cleared": int(n - flagged.sum()),
        "flagged_share": float(flagged.mean()) if n else None,
        "scoring_s_total": dt, "scoring_s_per_flow": dt / n if n else None,
        "training_overlap": model.overlap(scen),
        "features_missing": missing,
        "labelled": labelled,
    }
    (run_dir / IDS_JSON).write_text(json.dumps(info, indent=2), encoding="utf-8")
    return scen[flagged].reset_index(drop=True), info


def read_ids(run_dir: Path) -> tuple[Optional[dict[str, Any]], Optional[pd.DataFrame]]:
    j, c = Path(run_dir) / IDS_JSON, Path(run_dir) / IDS_RESULTS_CSV
    if not j.exists() or not c.exists():
        return None, None
    return json.loads(j.read_text(encoding="utf-8")), pd.read_csv(c, dtype={"flow_id": str})
