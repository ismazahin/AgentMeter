#!/usr/bin/env python3
"""Train AgentMeter's IDS gate: a binary Random Forest (attack vs benign) on CICFlowMeter flows.

Steps (each one is written into the model's metadata, for the thesis):
  1. Read every CSV (any format AgentMeter accepts; labels mapped as for a session, with every
     attack kept — the 5 study classes plus "Other Attack"; "... - Attempted" rows excluded).
  2. Remove duplicate flows (identical features), and drop feature vectors that occur with both a
     benign and an attack label (ambiguous) — so no flow can be in both training and hold-out.
  3. Split, stratified by class: 70% training / 30% hold-out. Sessions must use hold-out flows
     only; this script writes them as a CSV to upload (capped; every class kept).
  4. Within the 70%: 80% fits the forest, 20% (validation) chooses the threshold tau: the LOWEST
     tau whose false-positive rate on validation benign flows is <= --max-fpr (default 0.001),
     and never above the tau that keeps validation recall >= --target-recall (default 0.99). The
     IDS is a filter: it should miss as few attacks as its false-alarm budget allows; the LLM
     triage handles the false alarms it lets through.
  5. Report recall, precision, false-positive rate and flagged share on validation and hold-out.

Usage (on the computer that has the dataset):
  python scripts/train_ids.py data/cicids2017_corrected/*.csv
  python scripts/train_ids.py data/cicids2017_corrected --out models/ids/ids_rf    (CSV files in subfolders are read too)
  python scripts/train_ids.py --demo          # a tiny demo model from the bundled 300 flows

Then copy models/ids/ids_rf.{joblib,json,hashes.npy} to the GPU server (same folder), or set
AGENTMETER_IDS_MODEL to the .joblib path. Requires scikit-learn.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentmeter.ids.model import DEFAULT_DIR, DEFAULT_STEM, feature_hashes, sha256_file  # noqa: E402
from agentmeter.ingest.csv_input import CsvValidationError, load_csv  # noqa: E402
from agentmeter.ingest.feature_map import CIC_FEATURES  # noqa: E402

BENIGN = "Benign"
DEMO_CSV = ROOT / "data" / "cicids_full_300.csv"


def read_inputs(paths: list[Path], max_rows: int | None) -> tuple[pd.DataFrame, list[dict]]:
    files: list[Path] = []
    for p in paths:
        files += sorted(p.rglob("*.csv")) if p.is_dir() else [p]      # subfolders too (unzipped archives)
    if not files:
        raise SystemExit("no CSV files given")
    parts, info = [], []
    for f in files:
        try:
            r = load_csv(f, other_attack=True, max_rows=max_rows, max_bytes=50 * 1024 ** 3)
        except CsvValidationError as e:
            raise SystemExit(f"{f}: {e}") from e
        if r.labels is None:
            raise SystemExit(f"{f}: no label column: the IDS needs labelled flows to train")
        df = r.features[CIC_FEATURES].copy()
        df["label"] = r.labels["label"].to_numpy()
        df["label_raw"] = r.labels["label_raw"].to_numpy()
        parts.append(df)
        info.append({"file": f.name, "rows_used": int(len(df)), "rows_in_file": r.report["rows_read"],
                     "excluded_attempted": r.report["label"].get("excluded_attempted", 0)})
        print(f"  {f.name}: {len(df):,} labelled flows")
    return pd.concat(parts, ignore_index=True), info


def dedupe(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    h = feature_hashes(df, CIC_FEATURES)
    df = df.assign(_h=h, _attack=(df["label"] != BENIGN))
    mixed = df.groupby("_h")["_attack"].nunique()
    ambiguous = set(mixed[mixed > 1].index)
    n0 = len(df)
    df = df[~df["_h"].isin(ambiguous)]
    n1 = len(df)
    df = df.drop_duplicates("_h")
    return df.reset_index(drop=True), {"rows": n0, "ambiguous_dropped": n0 - n1, "duplicates_dropped": n1 - len(df),
                                       "rows_after": int(len(df))}


def split(df: pd.DataFrame, frac: float, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stratified by class; a class too small to split goes wholly to the first part."""
    from sklearn.model_selection import train_test_split
    counts = df["label"].value_counts()
    small = df["label"].isin(counts[counts < 2].index)
    a, b = train_test_split(df[~small], test_size=frac, random_state=seed, stratify=df.loc[~small, "label"])
    return pd.concat([a, df[small]]), b


def choose_threshold(scores: np.ndarray, attack: np.ndarray, target_recall: float) -> float:
    att = np.sort(scores[attack])
    if not len(att):
        return 0.5
    k = int(np.floor((1.0 - target_recall) * len(att)))     # attacks allowed below tau
    return float(att[min(k, len(att) - 1)])


def choose_threshold_fpr(scores: np.ndarray, attack: np.ndarray, max_fpr: float) -> float:
    """The lowest tau at which at most max_fpr of the benign flows score >= tau."""
    ben = np.sort(scores[~attack])[::-1]                       # highest first
    if not len(ben):
        return 0.0
    k = int(np.floor(max_fpr * len(ben)))                      # benign flows allowed at or above tau
    if k >= len(ben):
        return 0.0
    return float(np.nextafter(ben[k], np.inf))                 # just above the (k+1)-th highest benign score


def pick_threshold(scores: np.ndarray, attack: np.ndarray, target_recall: float, max_fpr: float) -> float:
    """min(recall rule, false-alarm-budget rule): lower tau catches more attacks; the budget bounds
    how many false alarms that may cost, and the recall rule bounds how high tau may go."""
    return min(choose_threshold(scores, attack, target_recall), choose_threshold_fpr(scores, attack, max_fpr))


def metrics(scores: np.ndarray, attack: np.ndarray, labels: np.ndarray, tau: float) -> dict:
    flag = scores >= tau
    tp, fp = int((flag & attack).sum()), int((flag & ~attack).sum())
    fn, tn = int((~flag & attack).sum()), int((~flag & ~attack).sum())
    per = {}
    for c in sorted(set(labels[attack])):
        m = labels == c
        per[c] = {"n": int(m.sum()), "recall": float(flag[m].mean())}
    return {"n": int(len(scores)), "attacks": int(attack.sum()), "benign": int((~attack).sum()),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "recall": tp / (tp + fn) if tp + fn else None,
            "precision": tp / (tp + fp) if tp + fp else None,
            "false_positive_rate": fp / (fp + tn) if fp + tn else None,
            "flagged_share": float(flag.mean()) if len(flag) else None,
            "recall_by_class": per}


def holdout_sample(df: pd.DataFrame, cap: int, seed: int) -> pd.DataFrame:
    if len(df) <= cap:
        return df
    out = []
    for c, g in df.groupby("label"):
        k = min(len(g), max(2000, round(cap * len(g) / len(df))))
        out.append(g.sample(n=k, random_state=seed))
    return pd.concat(out).sample(frac=1.0, random_state=seed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="*", type=Path, help="CSV files or folders of CSV files")
    ap.add_argument("--out", type=Path, default=DEFAULT_DIR / DEFAULT_STEM, help="output stem (no extension)")
    ap.add_argument("--demo", action="store_true", help="tiny demo model from the bundled 300 flows")
    ap.add_argument("--holdout", type=float, default=0.30)
    ap.add_argument("--target-recall", type=float, default=0.99, help="tau never above the one giving this recall")
    ap.add_argument("--max-fpr", type=float, default=0.001, help="false-alarm budget: lowest tau with validation FPR <= this")
    ap.add_argument("--trees", type=int, default=100)
    ap.add_argument("--max-depth", type=int, default=20)
    ap.add_argument("--min-leaf", type=int, default=2)
    ap.add_argument("--max-rows-per-file", type=int, default=400_000, help="cap per CSV (drawn across the file)")
    ap.add_argument("--train-cap", type=int, default=500_000, help="cap on rows used to fit the forest")
    ap.add_argument("--holdout-max", type=int, default=100_000, help="cap on rows written to the hold-out CSV")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args(argv)
    try:
        import joblib
        import sklearn
        from sklearn.ensemble import RandomForestClassifier
    except ImportError:
        raise SystemExit("scikit-learn is required: pip install scikit-learn")

    inputs = [DEMO_CSV] if a.demo else a.inputs
    if a.demo:
        a.trees, a.holdout_max = min(a.trees, 50), 0
        if a.out == DEFAULT_DIR / DEFAULT_STEM:
            a.out = DEFAULT_DIR / "demo_ids"
    if not inputs:
        ap.error("give the dataset CSV files or folders (or --demo)")

    print("1. reading")
    df, files = read_inputs(inputs, a.max_rows_per_file)
    print("2. removing duplicate and ambiguous flows")
    df, dd = dedupe(df)
    print(f"   {dd['rows']:,} -> {dd['rows_after']:,} ({dd['duplicates_dropped']:,} duplicates, "
          f"{dd['ambiguous_dropped']:,} ambiguous)")
    print(f"3. splitting {1 - a.holdout:.0%} training / {a.holdout:.0%} hold-out (stratified by class)")
    train, hold = split(df, a.holdout, a.seed)
    fit, val = split(train, 0.20, a.seed)
    if len(fit) > a.train_cap:
        fit = holdout_sample(fit, a.train_cap, a.seed)
    print(f"   fit {len(fit):,} · validation {len(val):,} · hold-out {len(hold):,}")

    params = dict(n_estimators=a.trees, max_depth=a.max_depth, min_samples_leaf=a.min_leaf,
                  class_weight="balanced_subsample", n_jobs=-1, random_state=a.seed)
    print("4. fitting the Random Forest", params)
    clf = RandomForestClassifier(**params)
    x = lambda d: d[CIC_FEATURES].replace([np.inf, -np.inf], np.nan).to_numpy(dtype=np.float64)  # noqa: E731
    att = lambda d: (d["label"] != BENIGN).to_numpy()  # noqa: E731
    clf.fit(x(fit), att(fit).astype(int))
    pos = list(clf.classes_).index(1)
    sv = clf.predict_proba(x(val))[:, pos]
    tau = pick_threshold(sv, att(val), a.target_recall, a.max_fpr)
    sh = clf.predict_proba(x(hold))[:, pos]
    m_val = metrics(sv, att(val), val["label"].to_numpy(), tau)
    m_hold = metrics(sh, att(hold), hold["label"].to_numpy(), tau)
    rule = (f"lowest tau with validation false-positive rate <= {a.max_fpr}, "
            f"never above the tau giving validation recall >= {a.target_recall}")
    print(f"5. threshold tau = {tau:.6f} ({rule})")
    for name, m in (("validation", m_val), ("hold-out", m_hold)):
        print(f"   {name}: recall {m['recall']:.4f} ({m['fn']:,} of {m['attacks']:,} attacks missed), "
              f"precision {m['precision'] or 0:.4f}, false-positive rate {m['false_positive_rate'] or 0:.5f} "
              f"({m['fp']:,} of {m['benign']:,} benign flagged), flagged {m['flagged_share']:.2%}")
    print("   hold-out recall by attack class:")
    for c, r in sorted(m_hold["recall_by_class"].items(), key=lambda kv: kv[1]["recall"]):
        print(f"     {c:<20} {r['recall']:.4f}  ({r['n']:,} flows)")

    out = a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    jl = out.with_suffix(".joblib")
    joblib.dump(clf, jl, compress=3)
    np.save(out.with_suffix(".hashes.npy"), np.sort(train["_h"].to_numpy(dtype=np.uint64)))
    hold_csv = None
    if a.holdout_max:
        hs = holdout_sample(hold, a.holdout_max, a.seed)
        hold_csv = out.parent / f"{out.name}_holdout.csv"
        hs[CIC_FEATURES].assign(Label=hs["label_raw"].to_numpy()).to_csv(hold_csv, index=False)
        print(f"   hold-out flows for sessions: {hold_csv} ({len(hs):,} rows)")
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {
        "name": out.name, "version": f"{out.name}-{created[:10]}", "algorithm": "RandomForestClassifier (binary: attack vs benign)",
        "demo": bool(a.demo), "created_at": created, "sklearn": sklearn.__version__, "params": params, "seed": a.seed,
        "features": CIC_FEATURES, "threshold": tau,
        "threshold_rule": rule, "target_recall": a.target_recall, "max_fpr": a.max_fpr,
        "data": {"files": files, "dedupe": dd, "holdout_share": a.holdout,
                 "classes": {k: {"fit": int((fit['label'] == k).sum()), "validation": int((val['label'] == k).sum()),
                                 "holdout": int((hold['label'] == k).sum())} for k in sorted(df["label"].unique())},
                 "holdout_csv": hold_csv.name if hold_csv else None,
                 "holdout_csv_sha256": sha256_file(hold_csv) if hold_csv else None},
        "metrics": {"validation": m_val, "holdout": m_hold},
        "sha256": sha256_file(jl),
    }
    out.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"wrote {jl} (+ .json, .hashes.npy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
