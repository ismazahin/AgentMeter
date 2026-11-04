"""Loading and running a trained IDS model (scripts/train_ids.py writes it).

An IDS model is three files with one stem, e.g. models/ids/ids_rf.*:
  ids_rf.joblib      the scikit-learn RandomForestClassifier (binary: 1 = attack)
  ids_rf.json        metadata: version, feature order, threshold, validation and hold-out metrics,
                     the training data, and the sha256 of the .joblib (checked on load)
  ids_rf.hashes.npy  hashes of every training row's features, to detect a session that would
                     reuse training flows (the IDS would have seen the answer)

The .joblib is a pickle: load only a model you trained yourself. Its sha256 must match the
metadata, so a swapped or corrupted file is refused.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..config import PROJECT_ROOT

DEFAULT_DIR = PROJECT_ROOT / "models" / "ids"
DEFAULT_STEM = "ids_rf"
HASH_DECIMALS = 6          # features are rounded before hashing (float noise must not hide a match)


class IdsError(Exception):
    pass


DEMO_STEM = "demo_ids"       # trained on the bundled 300 flows (scripts/train_ids.py --demo): demos only


def default_model_path() -> Path:
    """AGENTMETER_IDS_MODEL (a .joblib path), else models/ids/ids_rf.joblib (your trained model),
    else the bundled demo model (its metadata says demo: true and every report says so)."""
    env = (os.environ.get("AGENTMETER_IDS_MODEL") or "").strip()
    if env:
        return Path(env)
    own = DEFAULT_DIR / f"{DEFAULT_STEM}.joblib"
    return own if own.exists() else DEFAULT_DIR / f"{DEMO_STEM}.joblib"


def model_available(path: Optional[Path] = None) -> bool:
    p = Path(path or default_model_path())
    return p.exists() and p.with_suffix(".json").exists()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def feature_hashes(df: pd.DataFrame, features: list[str]) -> np.ndarray:
    """One 64-bit hash per row of the given feature columns (rounded; NaN kept as NaN)."""
    x = df.reindex(columns=features).apply(pd.to_numeric, errors="coerce").round(HASH_DECIMALS)
    return pd.util.hash_pandas_object(x, index=False).to_numpy(dtype=np.uint64)


class IdsModel:
    def __init__(self, path: Path, clf: Any, meta: dict[str, Any], train_hashes: Optional[np.ndarray]):
        self.path, self.clf, self.meta, self.train_hashes = path, clf, meta, train_hashes

    @property
    def features(self) -> list[str]:
        return list(self.meta["features"])

    @property
    def threshold(self) -> float:
        return float(self.meta["threshold"])

    @property
    def identity(self) -> dict[str, Any]:
        """What identifies this IDS in a session's settings (fingerprint) and reports."""
        return {k: self.meta.get(k) for k in ("name", "version", "sha256", "algorithm", "demo")}

    def score(self, df: pd.DataFrame) -> tuple[np.ndarray, float]:
        """P(attack) per row, and the wall time of scoring all rows (seconds)."""
        x = df.reindex(columns=self.features).apply(pd.to_numeric, errors="coerce")
        x = x.replace([np.inf, -np.inf], np.nan).to_numpy(dtype=np.float64)
        t0 = time.perf_counter()
        proba = self.clf.predict_proba(x)
        dt = time.perf_counter() - t0
        pos = list(self.clf.classes_).index(1)
        return proba[:, pos], dt

    def overlap(self, df: pd.DataFrame) -> int:
        """How many of these rows have exactly the features of a training row."""
        if self.train_hashes is None or not len(df):
            return 0
        return int(np.isin(feature_hashes(df, self.features), self.train_hashes).sum())


def load_model(path: Optional[Path] = None) -> IdsModel:
    p = Path(path or default_model_path())
    meta_p = p.with_suffix(".json")
    if not p.exists() or not meta_p.exists():
        raise IdsError(f"no IDS model at {p} (train one with scripts/train_ids.py, or set AGENTMETER_IDS_MODEL)")
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    if meta.get("sha256") != sha256_file(p):
        raise IdsError(f"{p.name} does not match its metadata (sha256): refusing to load it")
    try:
        import joblib
    except ImportError as e:                       # pragma: no cover - scikit-learn ships joblib
        raise IdsError("the IDS needs scikit-learn (pip install scikit-learn)") from e
    import sklearn
    if meta.get("sklearn") and meta["sklearn"] != sklearn.__version__:
        raise IdsError(f"{p.name} was trained with scikit-learn {meta['sklearn']} but {sklearn.__version__} is "
                       f"installed: install scikit-learn=={meta['sklearn']} (requirements.txt) or retrain")
    clf = joblib.load(p)
    hp = p.with_suffix(".hashes.npy")
    hashes = np.load(hp) if hp.exists() else None
    return IdsModel(p, clf, meta, hashes)
