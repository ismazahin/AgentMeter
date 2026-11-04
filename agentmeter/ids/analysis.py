"""The IDS -> LLM pipeline's results: the IDS as detector, the LLM as triage of its alerts.

Per session (labelled input):
  ids          what the IDS caught and missed: recall, precision, false-positive rate, flagged
               share, recall per attack class, scoring time
Per model (the LLM agents triage the flagged flows only):
  alerts       how the model triaged the IDS's alerts: false alarms (benign flows the IDS flagged)
               it dismissed, true attacks it dismissed
  end_to_end   what the SOC gets: attacks missed by the IDS + attacks the model dismissed, the
               analyst queue, and the time / cost / energy per 1,000 INCOMING flows
  tau_curve    the same for every threshold at or above the session's (a higher threshold only
               removes alerts, whose triage is already known; a lower one would need new runs)
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from ..pipeline.triage import DISMISS, ESCALATE, INVESTIGATE, priority_of

BENIGN = "Benign"


def _ids_metrics(df: pd.DataFrame, tau: float) -> dict[str, Any]:
    flag = (df["score"] >= tau).to_numpy()
    att = (df["label"] != BENIGN).to_numpy()
    tp, fp = int((flag & att).sum()), int((flag & ~att).sum())
    fn, tn = int((~flag & att).sum()), int((~flag & ~att).sum())
    per = {str(c): {"n": int((df["label"] == c).sum()), "recall": float(flag[(df["label"] == c).to_numpy()].mean())}
           for c in sorted(df.loc[att, "label"].unique())}
    return {"attacks": int(att.sum()), "benign": int((~att).sum()), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "recall": tp / (tp + fn) if tp + fn else None,
            "precision": tp / (tp + fp) if tp + fp else None,
            "false_positive_rate": fp / (fp + tn) if fp + tn else None,
            "flagged_share": float(flag.mean()) if len(flag) else None,
            "recall_by_class": per}


def _per_1k(n_in: int, n_alerts: int, ids_s_per_flow: Optional[float], llm_s_per_alert: Optional[float],
            usd_per_hour: Optional[float], wh_per_alert: Optional[float]) -> dict[str, Any]:
    share = n_alerts / n_in if n_in else 0.0
    t = None if llm_s_per_alert is None else 1000.0 * ((ids_s_per_flow or 0.0) + share * llm_s_per_alert)
    return {"time_s": t,
            "cost_usd": None if (t is None or usd_per_hour is None) else t / 3600.0 * usd_per_hour,
            "energy_wh": None if wh_per_alert is None else 1000.0 * share * wh_per_alert}


def ids_block(info: dict[str, Any], ids_df: pd.DataFrame, sr: list[dict], label_of: dict[str, str],
              table: dict[str, str], *, cost: Optional[dict] = None) -> dict[str, Any]:
    tau = float(info["threshold"])
    labelled = bool(info.get("labelled"))
    n_in = int(len(ids_df))
    alerts = ids_df[ids_df["flagged"].astype(bool)]
    price = ((cost or {}).get("price") or {}).get("usd_per_hour")
    cost_of = {r["model"]: r for r in ((cost or {}).get("per_model") or [])}
    model_of = {dbm: m for m, dbm in label_of.items()}
    pred: dict[str, dict[str, str]] = {}
    for r in sr:
        if r["model"] in model_of:
            pred.setdefault(model_of[r["model"]], {})[str(r["scenario_id"])] = r.get("predicted_label")
    truth = dict(zip(ids_df["flow_id"].astype(str), ids_df["label"])) if labelled else {}
    score = dict(zip(ids_df["flow_id"].astype(str), ids_df["score"]))

    def model_view(m: str, t: float) -> dict[str, Any]:
        ids_ = [f for f in alerts["flow_id"].astype(str) if score[f] >= t]
        p = pred.get(m, {})
        prio = {f: priority_of(p.get(f), table) for f in ids_}
        queue = {k: sum(1 for v in prio.values() if v == k) for k in (ESCALATE, INVESTIGATE, DISMISS)}
        c = cost_of.get(m) or {}
        out: dict[str, Any] = {"alerts": len(ids_), "queue": queue,
                               "per_1k_incoming": _per_1k(n_in, len(ids_), info.get("scoring_s_per_flow"),
                                                          c.get("mean_latency_s"), price, c.get("wh_per_flow"))}
        if labelled:
            att_all = sum(1 for f in truth if truth[f] != BENIGN)
            ids_missed = sum(1 for f in truth if truth[f] != BENIGN and score[f] < t)
            fa = [f for f in ids_ if truth[f] == BENIGN]               # the IDS's false alarms
            ta = [f for f in ids_ if truth[f] != BENIGN]               # true attacks it flagged
            fa_dis = sum(1 for f in fa if prio[f] == DISMISS)
            ta_dis = sum(1 for f in ta if prio[f] == DISMISS)
            missed = ids_missed + ta_dis
            out.update({
                "false_alarms": len(fa), "false_alarms_dismissed": fa_dis,
                "false_alarms_dismissed_rate": fa_dis / len(fa) if fa else None,
                "attacks_flagged": len(ta), "attacks_dismissed_by_llm": ta_dis,
                "attacks_dismissed_by_llm_rate": ta_dis / len(ta) if ta else None,
                "missed_by_ids": ids_missed, "missed_total": missed,
                "missed_total_rate": missed / att_all if att_all else None,
                "analyst_queue": queue[ESCALATE] + queue[INVESTIGATE],
                "analyst_queue_share_of_incoming": (queue[ESCALATE] + queue[INVESTIGATE]) / n_in if n_in else None,
            })
        return out

    grid = sorted({round(tau, 4)} | {round(float(x), 2) for x in np.arange(0.05, 1.0, 0.05) if x > tau})
    per = []
    for m in label_of:
        v = model_view(m, tau)
        v["model"] = m
        v["tau_curve"] = [{"threshold": t, **{k: x for k, x in model_view(m, t).items() if k in
                                              ("alerts", "missed_total_rate", "per_1k_incoming", "analyst_queue")}}
                          for t in grid]
        per.append(v)
    ids_curve = [{"threshold": t, **({k: x for k, x in _ids_metrics(ids_df, t).items()
                                      if k in ("recall", "false_positive_rate", "flagged_share")}
                                     if labelled else {"flagged_share": float((ids_df["score"] >= t).mean())})}
                 for t in grid]
    return {
        "model": info.get("model"), "threshold": tau, "threshold_source": info.get("threshold_source"),
        "labelled": labelled, "n_flows_in": n_in, "n_alerts": int(len(alerts)),
        "llm_calls_saved_share": 1.0 - len(alerts) / n_in if n_in else None,
        "scoring_s_per_flow": info.get("scoring_s_per_flow"),
        "training_overlap": info.get("training_overlap"),
        "features_missing": info.get("features_missing") or [],
        "ids": _ids_metrics(ids_df, tau) if labelled else {"flagged_share": info.get("flagged_share")},
        "ids_curve": ids_curve,
        "per_model": per,
        "notes": {
            "roles": "The IDS (a Random Forest on the flow features) DETECTS; only the flows it flags reach the "
                     "LLM agents, which TRIAGE them. Cleared flows are dismissed without an LLM call.",
            "per_1k": "Per 1,000 incoming flows = the IDS's scoring time for every flow + the LLM pipeline's measured "
                      "mean time per alert x the share of flows flagged. Cost uses the GPU's hourly price for both "
                      "(the IDS runs on the same server's CPU); energy counts the GPU during the LLM work only.",
            "missed": "Missed attacks (end to end) = attacks the IDS cleared + flagged attacks the model dismissed, "
                      "over all incoming attacks.",
            "false_alarms": "False alarms = benign flows the IDS flagged; dismissing them is the triage work that "
                            "relieves the analyst.",
            "tau_curve": "Thresholds at or above the session's only: a higher threshold removes alerts whose triage "
                         "is already measured; a lower one would send new flows to the LLM (a new run).",
            "overlap": "Training overlap = selected flows whose features equal a flow the IDS was trained on; it "
                       "should be 0 (use the hold-out CSV written by scripts/train_ids.py).",
        },
    }
