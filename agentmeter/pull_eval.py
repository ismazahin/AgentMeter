"""Phase 11 — admin model-pull + on-demand evaluation (core logic).

Lets an ADMIN pull ONE additional Hugging Face model and run it through the
EXISTING harness under conditions IDENTICAL to the locked 5-model study, for a
side-by-side comparison — WITHOUT contaminating that study.

Hard integrity rules enforced here (see the phase spec):
  * The pulled model runs via the SAME code path as the locked 5: it reuses
    runner.run_full (subprocess-per-model VRAM isolation, sequential, resume),
    which in turn reuses the worker / pipeline / instrumentation / HFProvider
    (4-bit NF4). Nothing is reimplemented.
  * NEVER writes the locked DB. Each pull writes a SEPARATE DB under
    results/pulls/agentmeter_pull_<slug>.db. build_pull_config refuses if the
    derived path resolves to the locked DB.
  * The canonical 5-model results, weights, tiers and STATISTICS (Kruskal-Wallis
    + Dunn) are READ-ONLY. merged_analysis deep-copies the canonical analysis and
    only APPENDS the pulled model as an `exploratory` block — it never recomputes
    or mutates the canonical statistics, so the locked numbers stay byte-for-byte.
  * The pulled model is flagged exploratory:true / validated:false and kept in a
    separate list; its composite is computed with the SAME SAW weights/targets as
    the study, but it is explicitly EXCLUDED from the validated statistics.
  * Size guard: a model whose parameter count exceeds MAX_PARAMS (8B) — or that is
    not a causal-LM text model — is rejected from HF metadata BEFORE any weights
    are downloaded.
  * Single-job lock: only one pull/eval runs at a time.

This module is import/seam-friendly so it can be verified on CPU with a mock
provider and an injected model-info fetcher (no GPU, no network, no server).
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
import yaml

from . import analyze, runner
from .config import PROJECT_ROOT, load_config

# --- constants ---------------------------------------------------------

MAX_PARAMS = 8_000_000_000  # reject anything larger BEFORE downloading weights
PULLS_DIR = PROJECT_ROOT / "results" / "pulls"
DEFAULT_BASE_CONFIG = PROJECT_ROOT / "configs" / "run_full_l4.yaml"
# The locked, statistically-validated study output (read-only for merging).
DEFAULT_CANONICAL_JSON = PROJECT_ROOT / "results" / "analysis" / "analysis.json"

# Pipeline tags that are unambiguously NOT causal-LM text generation.
_NON_TEXT_TAGS = {
    "text-classification", "token-classification", "image-classification",
    "object-detection", "image-segmentation", "audio-classification",
    "automatic-speech-recognition", "text-to-image", "image-to-text",
    "feature-extraction", "sentence-similarity", "fill-mask",
    "question-answering", "translation", "summarization",
    "zero-shot-classification", "tabular-classification",
}


# --- model-size + type validation (BEFORE any download) ----------------

def fetch_model_info(model_id: str) -> Any:
    """Fetch HF model metadata (no weights). Lazy import so CPU/test paths that
    inject their own fetcher never need huggingface_hub installed."""
    from huggingface_hub import HfApi

    return HfApi().model_info(model_id, files_metadata=False)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Attribute-or-key access, tolerant of both real ModelInfo objects and the
    simple dict/namespace stubs the tests inject."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _param_count(info: Any) -> int:
    """Best-effort parameter count from HF metadata (safetensors index), WITHOUT
    downloading weights. Raises if it cannot be determined — we refuse to pull a
    model whose size we cannot verify against the 8B cap."""
    st = _get(info, "safetensors")
    if st is not None:
        total = _get(st, "total")
        if total is not None:
            return int(total)
        params = _get(st, "parameters")
        if isinstance(params, dict) and params:
            return int(sum(int(v) for v in params.values()))
    # Some metadata exposes a flat count.
    for k in ("num_parameters", "n_parameters"):
        v = _get(info, k)
        if v is not None:
            return int(v)
    raise ValueError(
        "cannot determine parameter count from HF metadata (no safetensors index) "
        "— refusing to pull a model whose size cannot be verified against the "
        f"{MAX_PARAMS/1e9:.0f}B cap."
    )


def _architectures(info: Any) -> list[str]:
    cfg = _get(info, "config") or {}
    arch = _get(cfg, "architectures") or _get(info, "architectures") or []
    return [str(a) for a in arch]


def _is_causal_lm(info: Any) -> bool:
    """True only with POSITIVE evidence that this is a causal-LM text model.

    Accept when the pipeline tag is text(2text)-generation OR an architecture ends
    in *ForCausalLM / *LMHeadModel. Reject a clearly non-text tag. Absent any
    positive signal we refuse (the spec says abort if not a causal-LM text model).
    """
    tag = _get(info, "pipeline_tag")
    arch = _architectures(info)
    causal_arch = any(
        a.endswith("ForCausalLM") or a.endswith("LMHeadModel") or "CausalLM" in a
        for a in arch
    )
    if tag in ("text-generation", "text2text-generation"):
        return True
    if causal_arch:
        return True
    if tag in _NON_TEXT_TAGS:
        return False
    return False


def validate_model(
    model_id: str,
    max_params: int = MAX_PARAMS,
    info_fetcher: Optional[Callable[[str], Any]] = None,
) -> dict[str, Any]:
    """Validate a model id from HF metadata BEFORE downloading any weights.

    Raises ValueError if the model is larger than `max_params` or is not a
    causal-LM text-generation model. Returns a small metadata summary on success.
    """
    if not model_id or "/" not in str(model_id).strip("/"):
        raise ValueError(f"invalid model id: {model_id!r} (expected 'org/name').")
    fetch = info_fetcher or fetch_model_info
    info = fetch(model_id)

    params = _param_count(info)
    if params > max_params:
        raise ValueError(
            f"{model_id} has ~{params/1e9:.2f}B parameters, above the "
            f"{max_params/1e9:.0f}B limit — refusing to pull (checked from HF "
            "metadata before any download)."
        )
    if not _is_causal_lm(info):
        raise ValueError(
            f"{model_id} does not look like a causal-LM text-generation model "
            f"(pipeline_tag={_get(info, 'pipeline_tag')!r}, "
            f"architectures={_architectures(info)}) — refusing to pull."
        )
    return {
        "model_id": model_id,
        "params": params,
        "params_b": round(params / 1e9, 3),
        "pipeline_tag": _get(info, "pipeline_tag"),
        "architectures": _architectures(info),
    }


# --- per-pull config derivation (SEPARATE DB, never the locked one) ----

def model_slug(model_id: str) -> str:
    """Filesystem-safe slug for a model id, e.g. org/Name-1.0 -> org__Name-1.0."""
    slug = str(model_id).replace("/", "__")
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", slug)
    return slug.strip("_") or "model"


def pull_db_path(model_id: str) -> Path:
    return PULLS_DIR / f"agentmeter_pull_{model_slug(model_id)}.db"


def _locked_db_path(base_cfg) -> Path:
    return base_cfg.resolve_path("storage.sqlite_path", "results/agentmeter.db").resolve()


def build_pull_config(
    base_config_path: str | Path,
    model_id: str,
    out_dir: str | Path = PULLS_DIR,
) -> tuple[str, str]:
    """Derive a per-pull config from the base study config.

    Identical to the locked study in every way that affects the measurement
    (provider, quant, dataset, pipeline, scoring, require_gpu) — only run.models,
    model.name and storage.sqlite_path change. Refuses if the derived DB path
    resolves to the locked study DB.

    Returns (config_path, db_path) as strings.
    """
    base_cfg = load_config(str(base_config_path))
    data = copy.deepcopy(base_cfg.data)

    # Run ONLY the pulled model (one worker), through the same harness.
    data.setdefault("run", {})["models"] = [model_id]
    data.setdefault("model", {})["name"] = model_id

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    db = out / f"agentmeter_pull_{model_slug(model_id)}.db"
    data.setdefault("storage", {})["sqlite_path"] = str(db)
    db.parent.mkdir(parents=True, exist_ok=True)

    # Integrity guard: NEVER point at the locked study DB.
    locked = _locked_db_path(base_cfg)
    if db.resolve() == locked:
        raise ValueError(
            f"REFUSING: derived pull DB {db} resolves to the locked study DB {locked}. "
            "A pull must never write the validated study database."
        )

    cfg_path = out / f"config_{model_slug(model_id)}.yaml"
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False)
    return str(cfg_path), str(db)


# --- run the pulled model through the EXISTING orchestrator ------------

def run_pull(
    config_path: str,
    n: Optional[int] = None,
    fresh: bool = False,
    run_full: Optional[Callable[..., Any]] = None,
) -> Any:
    """Run the pulled model via runner.run_full (checkpoint/resume, subprocess
    isolation, 4-bit NF4, require_gpu — all inherited from the config). `run_full`
    is injectable purely so the CPU verify suite can stub the orchestrator."""
    rf = run_full or runner.run_full
    return rf(config_path=config_path, n=n, fresh=fresh)


# --- merged analysis: canonical (read-only) + exploratory row ----------

def _pull_model_row(
    db_path: str | Path, model_id: str, classes: list[str], scoring: dict[str, Any]
) -> dict[str, Any]:
    """Compute the pulled model's SAW row from its OWN DB, reusing the exact
    analyze helpers (same criteria math, same normalisation, same composite/tier
    as the locked study). VRAM uses marginal working memory (no measured weight
    footprint is captured for a pull) — recorded honestly as best-effort."""
    db = Path(db_path)
    conn = analyze._connect_ro(db)
    try:
        run_ids = analyze._complete_run_ids(conn)
        if not run_ids:
            raise ValueError(f"no complete run in pull DB {db} — nothing to analyze.")
        qmarks = ",".join("?" * len(run_ids))
        sr = pd.read_sql_query(
            f"SELECT * FROM scenario_results WHERE status='complete' AND run_id IN ({qmarks})",
            conn, params=run_ids)
        am = pd.read_sql_query(
            f"SELECT * FROM agent_metrics WHERE run_id IN ({qmarks})", conn, params=run_ids)
    finally:
        conn.close()

    if sr.empty:
        raise ValueError(f"pull DB {db} has no complete scenario rows.")

    # A pull DB holds exactly ONE model; its DB label may differ from the display
    # id (mock provider labels as "mock:<name>"), so look up by the DB label and
    # only relabel for display.
    db_models = sorted(sr["model"].unique())
    if len(db_models) != 1:
        raise ValueError(f"pull DB {db} unexpectedly holds {len(db_models)} models: {db_models}.")
    db_model = db_models[0]

    keep = set(zip(sr["model"], sr["scenario_id"]))
    am = am[[(m, s) in keep for m, s in zip(am["model"], am["scenario_id"])]].copy()

    p7 = analyze.phase7(sr, classes)
    acc_by_model = dict(zip(p7["per_model"]["model"], p7["per_model"]["accuracy"]))
    raw = analyze._criteria(sr, am, acc_by_model, footprint=None)

    weights = {k: float(v) for k, v in scoring["weights"].items()}
    targets = {k: float(v) for k, v in scoring["targets"].items()}
    tiers = {k: float(v) for k, v in scoring["tiers"].items()}

    norm = analyze._normalise(raw, targets)
    comp = analyze._composite(norm, weights)

    row_raw = {k: _jsonable(v) for k, v in raw.iloc[0].to_dict().items()}
    row_norm = {k: _jsonable(v) for k, v in norm.iloc[0].to_dict().items()}
    row_raw["model"] = model_id      # relabel for display (was the DB label)
    row_norm["model"] = model_id
    composite = float(comp.iloc[0])
    tier = analyze._tier(composite * 100.0, tiers)

    saw_row = dict(row_raw)
    for k in ("accuracy", "latency", "vram", "tokens"):
        saw_row[k] = row_norm[k]
    saw_row["composite"] = composite
    saw_row["tier"] = tier
    saw_row["exploratory"] = True
    saw_row["validated"] = False
    saw_row["model"] = model_id

    return {
        "n_scenarios": int(len(sr)),
        "raw_criteria": row_raw,
        "normalised": {"model": model_id, **{k: row_norm[k] for k in
                       ("accuracy", "latency", "vram", "tokens")}},
        "saw_row": saw_row,
        "accuracy": float(acc_by_model.get(db_model, float("nan"))),
    }


def _jsonable(v: Any) -> Any:
    """Coerce numpy/pandas scalars to plain JSON types (NaN -> None)."""
    import math

    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return None if math.isnan(f) else f


EXPLORATORY_NOTE = (
    "EXPLORATORY (validated:false) — pulled on demand and run through the same "
    "harness (identical dataset, 4-bit NF4, sequential subprocess-per-model VRAM "
    "isolation) as the locked 5-model study, so timing is best-effort comparable. "
    "VRAM here is marginal working memory (no measured weight footprint is captured "
    "for a pull), so its total device VRAM is not directly comparable to the "
    "study's. Its composite uses the SAME SAW weights/targets, but it is EXCLUDED "
    "from the validated statistics (Kruskal-Wallis + Dunn), which are unchanged."
)


def merged_analysis(
    canonical_json_path: str | Path,
    pull_db_path: str | Path,
    config_path: str | Path,
    model_id: str,
) -> dict[str, Any]:
    """Load the locked canonical analysis (READ-ONLY) and return a deep copy with
    the pulled model APPENDED as an `exploratory` block. The canonical phase8
    tables and the statistics are left byte-for-byte unchanged."""
    cfg = load_config(str(config_path))
    classes = list(cfg.get("classes", []) or [])
    scoring = cfg.get("scoring", {}) or {}

    canonical = json.loads(Path(canonical_json_path).read_text())
    merged = copy.deepcopy(canonical)  # canonical statistics untouched

    row = _pull_model_row(pull_db_path, model_id, classes, scoring)

    merged["exploratory"] = {
        "validated": False,
        "exploratory": True,
        "model": model_id,
        "n_scenarios": row["n_scenarios"],
        "accuracy": row["accuracy"],
        "note": EXPLORATORY_NOTE,
        "weights": {k: float(v) for k, v in scoring.get("weights", {}).items()},
        "targets": {k: float(v) for k, v in scoring.get("targets", {}).items()},
        "tiers": {k: float(v) for k, v in scoring.get("tiers", {}).items()},
        "raw_criteria": row["raw_criteria"],
        "normalised": row["normalised"],
        "saw_row": row["saw_row"],
    }
    return merged


# --- progress (read the pull DB; never touch the runner) ----------------

def _flush_json(path: str | Path, payload: dict) -> None:
    """Write JSON and fsync it to disk, so the file is durable before any
    subsequent action (e.g. auto-destroy) can run."""
    import os

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
        fh.flush()
        os.fsync(fh.fileno())


def count_completed(db_path: str | Path) -> int:
    """Complete scenario_results rows in a pull DB (0 if the DB is absent yet)."""
    db = Path(db_path)
    if not db.exists():
        return 0
    try:
        conn = analyze._connect_ro(db)
    except Exception:
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM scenario_results WHERE status='complete'"
        ).fetchone()
        return int(row["n"]) if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


# --- single-job state machine ------------------------------------------

_ACTIVE_STATES = {"validating", "pulling", "running", "analyzing"}


@dataclass
class JobState:
    state: str = "idle"        # idle|validating|pulling|running|analyzing|done|error
    model_id: Optional[str] = None
    db_path: Optional[str] = None
    analysis_path: Optional[str] = None   # where the merged analysis JSON was flushed
    total: int = 0
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    analysis: Optional[dict] = field(default=None, repr=False)

    def is_active(self) -> bool:
        return self.state in _ACTIVE_STATES


class JobManager:
    """In-process single-job lock + state machine. Only ONE pull/eval at a time."""

    def __init__(
        self,
        base_config: str | Path = DEFAULT_BASE_CONFIG,
        canonical_json: str | Path = DEFAULT_CANONICAL_JSON,
        max_params: int = MAX_PARAMS,
        info_fetcher: Optional[Callable[[str], Any]] = None,
        run_full: Optional[Callable[..., Any]] = None,
        n: Optional[int] = None,
        out_dir: str | Path = PULLS_DIR,
        on_complete: Optional[Callable[["JobState"], None]] = None,
    ):
        self.base_config = str(base_config)
        self.canonical_json = str(canonical_json)
        self.max_params = max_params
        self.info_fetcher = info_fetcher
        self.run_full = run_full
        self.n = n
        self.out_dir = str(out_dir)
        # Fired ONCE after a job reaches "done" and the merged analysis JSON has
        # been flushed to disk — the server uses it for auto-destroy (last step).
        self.on_complete = on_complete
        self._lock = threading.Lock()
        self._state = JobState()
        self._thread: Optional[threading.Thread] = None

    # --- state access (thread-safe) ------------------------------------
    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self._state, k, v)

    def snapshot(self) -> JobState:
        with self._lock:
            return copy.copy(self._state)

    def status(self) -> dict[str, Any]:
        s = self.snapshot()
        done = count_completed(s.db_path) if s.db_path else 0
        total = s.total or 0
        progress = (done / total) if total else 0.0
        return {
            "state": s.state,
            "model_id": s.model_id,
            "progress": round(progress, 4),
            "scenario": {"completed": done, "total": total},
            "error": s.error,
        }

    def analysis_payload(self) -> Optional[dict]:
        return self.snapshot().analysis

    # --- lifecycle ------------------------------------------------------
    def start(self, model_id: str) -> dict[str, Any]:
        """Acquire the single-job lock and launch the pull/eval in a thread.

        Raises RuntimeError if a job is already active (single-job lock)."""
        with self._lock:
            if self._state.is_active():
                raise RuntimeError(
                    f"a pull/eval is already running for {self._state.model_id!r} "
                    f"(state={self._state.state}); only one runs at a time."
                )
            self._state = JobState(
                state="validating", model_id=model_id, started_at=time.time()
            )
        self._thread = threading.Thread(
            target=self._run, args=(model_id,), daemon=True
        )
        self._thread.start()
        return self.status()

    def _run(self, model_id: str) -> None:
        try:
            validate_model(model_id, max_params=self.max_params,
                            info_fetcher=self.info_fetcher)

            cfg_path, db_path = build_pull_config(self.base_config, model_id,
                                                  out_dir=self.out_dir)
            total = self._resolve_total(cfg_path)
            self._set(state="pulling", db_path=db_path, total=total)

            stop = threading.Event()
            mon = threading.Thread(target=self._monitor, args=(db_path, stop),
                                   daemon=True)
            mon.start()
            try:
                run_pull(cfg_path, n=self.n, run_full=self.run_full)
            finally:
                stop.set()

            self._set(state="analyzing")
            merged = merged_analysis(self.canonical_json, db_path, cfg_path, model_id)
            # Flush the merged analysis to disk BEFORE marking done, so results are
            # durably persisted before any auto-destroy can run.
            analysis_path = str(Path(db_path).with_name(
                f"analysis_{model_slug(model_id)}.json"))
            _flush_json(analysis_path, merged)
            self._set(state="done", analysis=merged, analysis_path=analysis_path,
                      finished_at=time.time())
        except Exception as e:  # noqa: BLE001 — surface any failure via /status
            self._set(state="error", error=str(e), finished_at=time.time())
            return
        # Completion hook runs LAST, only on success, after the JSON is on disk.
        if self.on_complete is not None:
            try:
                self.on_complete(self.snapshot())
            except Exception:  # noqa: BLE001 — a hook must never corrupt state
                import logging
                logging.getLogger("agentmeter.pull_eval").exception(
                    "on_complete hook raised (ignored).")

    def _monitor(self, db_path: str, stop: threading.Event) -> None:
        """Flip pulling -> running once the first scenario is persisted."""
        while not stop.wait(2.0):
            if count_completed(db_path) > 0:
                with self._lock:
                    if self._state.state == "pulling":
                        self._state.state = "running"
                return

    def _resolve_total(self, cfg_path: str) -> int:
        """How many scenarios this run will cover (dataset size, capped by n)."""
        try:
            from .dataset import DatasetLoader
            cfg = load_config(cfg_path)
            scenarios = DatasetLoader(cfg).load()
            total = len(scenarios)
            if self.n is not None:
                total = min(total, self.n)
            return int(total)
        except Exception:
            return int(self.n or 0)
