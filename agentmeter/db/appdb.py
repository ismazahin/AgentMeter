"""Phase 12 — application metadata (CRUD for sessions, weight presets, notes).

Phase 26: SINGLE-DATABASE model. The app-metadata tables (session, weight_preset,
note, tag, session_tag, hf_metadata_cache) live in the SAME SQLite file as the
study results tables (runs, scenario_results, agent_metrics), so the whole schema
is one connected database — one file to open, back up and diagram. The earlier
two-file split (and the guard that blocked this module from the study DB) is gone.

Consequences of the unified file:
  * AppStore ensures BOTH schemas exist in the file, so session.source_run_id can
    be a REAL foreign key to runs.run_id;
  * imported analysis.json data is still stored READ-ONLY (its summary is extracted
    once at import and never recomputed or edited);
  * there is still no CRUD on datasets or the model set — out of scope by design.

This module has no Flask dependency so it is fully testable on CPU. The HTTP
layer (agentmeter/crud_api.py) is a thin wrapper over these methods.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..config import PROJECT_ROOT

# Phase 26: one unified database file holds both the study and the app tables.
DEFAULT_APP_DB = PROJECT_ROOT / "results" / "agentmeter.db"
# Historical study-DB path, kept only as a reference for the integrity checker's
# fallback and for provenance in analyze.py. It is no longer a forbidden target —
# the app and the study now share one file.
LOCKED_STUDY_DB = (PROJECT_ROOT / "results" / "agentmeter_full_l4.db").resolve()

WEIGHT_KEYS = ("w_accuracy", "w_latency", "w_vram", "w_tokens")

# Builtin presets mirror configs/run_full_l4.yaml (scoring.weights +
# sensitivity_weight_sets). Builtins are read-only: load-only, never edited/deleted.
BUILTIN_PRESETS = [
    {"name": "default", "w_accuracy": 0.40, "w_latency": 0.25, "w_vram": 0.20, "w_tokens": 0.15},
    {"name": "equal", "w_accuracy": 0.25, "w_latency": 0.25, "w_vram": 0.25, "w_tokens": 0.25},
    {"name": "accuracy_heavy", "w_accuracy": 0.55, "w_latency": 0.20, "w_vram": 0.15, "w_tokens": 0.10},
    {"name": "efficiency_heavy", "w_accuracy": 0.25, "w_latency": 0.30, "w_vram": 0.30, "w_tokens": 0.15},
]


class NotFound(Exception):
    """Raised when a row id does not exist."""


class Forbidden(Exception):
    """Raised when an operation is not allowed (e.g. editing a builtin preset)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_weights(data: dict[str, Any]) -> dict[str, float]:
    """Every weight must be a finite number >= 0. Rejects bools, NaN/inf, negatives,
    non-numbers, and missing keys. Returns the coerced float dict."""
    out: dict[str, float] = {}
    for k in WEIGHT_KEYS:
        if k not in data or data[k] is None:
            raise ValueError(f"missing weight '{k}'")
        v = data[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"weight '{k}' must be a number")
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            raise ValueError(f"weight '{k}' must be a finite number")
        if f < 0:
            raise ValueError(f"weight '{k}' must be >= 0")
        out[k] = f
    return out


def summarize_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """Extract a SMALL read-only summary from an analysis.json — reading existing
    fields only, never recomputing the study numbers."""
    p8 = (analysis or {}).get("phase8", {}) or {}
    saw = p8.get("saw_table") or []
    norm = p8.get("normalised") or []
    models = [r.get("model") for r in (saw or norm) if r.get("model")]
    top_model = None
    if saw:
        ranked = sorted(saw, key=lambda r: (r.get("rank") if r.get("rank") is not None else 1e9))
        top_model = ranked[0].get("model") if ranked else None
    tiers = {r.get("model"): r.get("tier") for r in saw if r.get("model") and r.get("tier")}
    return {"model_count": len(models), "top_model": top_model, "tiers": tiers}


def source_run_id_of(analysis: dict[str, Any]) -> Optional[str]:
    """The study run this imported analysis came from.

    Reads the analysis' `run_ids` (the field analyze.py writes) and returns the first
    one as a plain string, or None if absent. Phase 26: this maps to session.source_run_id,
    a real FK to runs.run_id; create_session stores NULL if the run is not present.
    """
    if not isinstance(analysis, dict):
        return None
    run_ids = analysis.get("run_ids")
    if isinstance(run_ids, (list, tuple)) and run_ids:
        first = run_ids[0]
        return str(first) if first is not None else None
    return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    source_filename TEXT,
    summary         TEXT,            -- small JSON blob (read-only, from import)
    analysis_ref    TEXT,            -- original path/name of the imported analysis.json
    analysis_json   TEXT NOT NULL,   -- stored copy of the imported analysis (read-only)
    source_run_id   TEXT,            -- which study run this imported analysis came from
                                     -- (from the analysis' run_ids). Phase 26: a REAL FK to
                                     -- runs.run_id now that both live in one file; nullable.
    preset_id       INTEGER,         -- Phase 24: which weight_preset this session was saved with
                                     -- (nullable; real FK).
    FOREIGN KEY (preset_id) REFERENCES weight_preset(id) ON DELETE SET NULL,
    FOREIGN KEY (source_run_id) REFERENCES runs(run_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS weight_preset (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    w_accuracy  REAL NOT NULL,
    w_latency   REAL NOT NULL,
    w_vram      REAL NOT NULL,
    w_tokens    REAL NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    is_builtin  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS note (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
);

-- Phase 19 — tags for organising sessions. Names are unique case-insensitively.
CREATE TABLE IF NOT EXISTS tag (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_name_nocase ON tag (name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS session_tag (
    session_id  INTEGER NOT NULL,
    tag_id      INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (session_id, tag_id),
    FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)     REFERENCES tag(id)     ON DELETE CASCADE
);

-- Phase 22 — cache for Hugging Face Hub model metadata (external CONTEXT only,
-- never study data). Lives in the unified DB alongside the study tables.
CREATE TABLE IF NOT EXISTS hf_metadata_cache (
    model_id    TEXT PRIMARY KEY,
    fetched_at  REAL NOT NULL,
    status      TEXT NOT NULL,
    payload     TEXT NOT NULL,     -- JSON of the cleaned metadata dict (source of truth)
    -- Phase 25: fields PROMOTED from payload into typed columns so they are
    -- queryable/sortable in SQL (hybrid relational + document). payload stays the
    -- source of truth; these are a projection, refreshed on every hf_cache_set.
    -- Nullable: an 'unavailable' fetch caches a status with no metadata fields.
    params_b     REAL,             -- model size in billions of parameters
    downloads    INTEGER,
    likes        INTEGER,
    license      TEXT,
    pipeline_tag TEXT
);
"""

# Payload fields promoted into hf_metadata_cache columns (name -> column is 1:1).
_HF_PROMOTED = ("params_b", "downloads", "likes", "license", "pipeline_tag")
_HF_INT_COLS = ("downloads", "likes")
_HF_REAL_COLS = ("params_b",)


def _hf_promoted_value(data: dict[str, Any], col: str) -> Any:
    """Coerce one promoted field out of a cached HF payload to its column type,
    returning None when absent or not coercible (so a bad/partial payload never
    breaks the write — the JSON payload remains the source of truth)."""
    v = (data or {}).get(col)
    if v is None:
        return None
    try:
        if col in _HF_INT_COLS:
            return int(v)
        if col in _HF_REAL_COLS:
            f = float(v)
            return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None
    return str(v)


class AppStore:
    """CRUD over the metadata DB. Thread-safe (single connection + lock), so it can
    back a threaded Flask server."""

    def __init__(self, db_path: str | Path = DEFAULT_APP_DB):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.execute("PRAGMA busy_timeout = 5000;")
        # Phase 26: the app and study tables share one file. Ensure the STUDY
        # tables exist first (imported lazily to avoid an import cycle) so that
        # session.source_run_id can be a real FK to runs.run_id, then the app tables.
        from .storage import SCHEMA as _STUDY_SCHEMA
        self.conn.execute("PRAGMA journal_mode = WAL;")  # one file, two writers -> WAL
        self.conn.executescript(_STUDY_SCHEMA)
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()
        self._lock = threading.Lock()
        self._seed_builtins()

    def _migrate(self) -> None:
        """Idempotent, additive migrations for an app DB created by an older version.
        Only ever touches this app DB (self.conn) — never the locked study DB.

        Phase 23: add session.source_run_id if it is missing.
        Phase 24: add session.preset_id if it is missing. Safe to run repeatedly.
        (ALTER TABLE cannot re-declare the FK on an existing table; on migrated DBs
        preset_id is a validated soft reference — create/set_session_preset check the
        preset exists, and delete_preset nulls any sessions pointing at it.)
        Phase 25: add the promoted hf_metadata_cache columns if missing, then
        backfill them from each row's existing JSON payload.
        """
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(session)")}
        if "source_run_id" not in cols:
            self.conn.execute("ALTER TABLE session ADD COLUMN source_run_id TEXT")
        if "preset_id" not in cols:
            self.conn.execute("ALTER TABLE session ADD COLUMN preset_id INTEGER")

        hf_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(hf_metadata_cache)")}
        col_types = {"params_b": "REAL", "downloads": "INTEGER", "likes": "INTEGER",
                     "license": "TEXT", "pipeline_tag": "TEXT"}
        added = [c for c in _HF_PROMOTED if c not in hf_cols]
        for c in added:
            self.conn.execute(f"ALTER TABLE hf_metadata_cache ADD COLUMN {c} {col_types[c]}")
        if added:  # backfill the new columns from each row's stored JSON payload
            for row in self.conn.execute(
                    "SELECT model_id, payload FROM hf_metadata_cache").fetchall():
                try:
                    data = json.loads(row["payload"])
                except (ValueError, TypeError):
                    continue
                self.conn.execute(
                    "UPDATE hf_metadata_cache SET "
                    + ", ".join(f"{c} = ?" for c in _HF_PROMOTED)
                    + " WHERE model_id = ?",
                    [_hf_promoted_value(data, c) for c in _HF_PROMOTED] + [row["model_id"]])

    def close(self) -> None:
        # Flush the WAL back into the main file so a later read-only opener
        # (analysis, the integrity checker) sees a self-contained database.
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except sqlite3.OperationalError:
            pass
        self.conn.close()

    # --- builtin presets (seeded once; read-only) ----------------------
    def _seed_builtins(self) -> None:
        with self._lock, self.conn:
            have = {r["name"] for r in self.conn.execute(
                "SELECT name FROM weight_preset WHERE is_builtin = 1")}
            for p in BUILTIN_PRESETS:
                if p["name"] in have:
                    continue
                now = _now()
                self.conn.execute(
                    "INSERT INTO weight_preset (name, w_accuracy, w_latency, w_vram, "
                    "w_tokens, created_at, updated_at, is_builtin) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    (p["name"], p["w_accuracy"], p["w_latency"], p["w_vram"],
                     p["w_tokens"], now, now))

    # ================= sessions =================
    def create_session(self, name: str, analysis: dict[str, Any],
                        source_filename: Optional[str] = None,
                        analysis_ref: Optional[str] = None,
                        preset_id: Optional[int] = None) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("session name is required and must be non-empty")
        if not isinstance(analysis, dict) or "phase8" not in analysis:
            raise ValueError("analysis must be an object containing a 'phase8' block "
                             "(an AgentMeter analysis.json)")
        if preset_id is not None:
            self._require_preset(preset_id)          # real FK: must reference an existing preset
        summary = summarize_analysis(analysis)
        source_run_id = source_run_id_of(analysis)   # the run this analysis came from
        # source_run_id is now a real FK to runs.run_id (unified DB). If the run is
        # not present locally (analysis imported from elsewhere), degrade to NULL
        # rather than reject the import.
        if source_run_id is not None and not self._run_exists(source_run_id):
            source_run_id = None
        now = _now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO session (name, created_at, updated_at, source_filename, "
                "summary, analysis_ref, analysis_json, source_run_id, preset_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, now, now, source_filename, json.dumps(summary),
                 analysis_ref or source_filename, json.dumps(analysis), source_run_id,
                 preset_id))
            sid = cur.lastrowid
        return self.get_session(sid, include_analysis=True)

    def _run_exists(self, run_id: str) -> bool:
        """Whether a study run is present in the (now unified) DB. Tolerates a DB
        with no runs table yet (returns False rather than raising)."""
        try:
            return self.conn.execute("SELECT 1 FROM runs WHERE run_id = ?",
                                     (run_id,)).fetchone() is not None
        except sqlite3.OperationalError:
            return False

    def _require_preset(self, preset_id: int) -> None:
        """Validate that a weight_preset id exists (soft-FK check, works on migrated
        DBs too). Raises NotFound otherwise."""
        if self.conn.execute("SELECT 1 FROM weight_preset WHERE id = ?",
                             (preset_id,)).fetchone() is None:
            raise NotFound(f"weight preset {preset_id} not found")

    def set_session_preset(self, session_id: int, preset_id: Optional[int]) -> dict[str, Any]:
        """Set (or clear, with None) which weight_preset a session was saved with."""
        if self.conn.execute("SELECT 1 FROM session WHERE id = ?",
                             (session_id,)).fetchone() is None:
            raise NotFound(f"session {session_id} not found")
        if preset_id is not None:
            self._require_preset(preset_id)
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE session SET preset_id = ?, updated_at = ? WHERE id = ?",
                (preset_id, _now(), session_id))
        return self.get_session(session_id, include_analysis=False)

    def list_sessions(self, tag: Optional[str | int] = None) -> list[dict[str, Any]]:
        """List session metadata (newest first), each with its `tags`. If `tag`
        (a tag id or name) is given, only sessions carrying that tag are returned."""
        if tag is not None:
            trow = self._find_tag(tag)
            if trow is None:
                return []                      # unknown tag -> no sessions match
            rows = self.conn.execute(
                "SELECT s.id, s.name, s.created_at, s.updated_at, s.source_filename, "
                "s.summary, s.analysis_ref, s.source_run_id, s.preset_id FROM session s "
                "JOIN session_tag st ON st.session_id = s.id WHERE st.tag_id = ? "
                "ORDER BY datetime(s.created_at) DESC, s.id DESC", (trow["id"],)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, name, created_at, updated_at, source_filename, summary, "
                "analysis_ref, source_run_id, preset_id FROM session ORDER BY datetime(created_at) DESC, id DESC"
            ).fetchall()
        tags_by_session = self._all_session_tags()
        out = []
        for r in rows:
            meta = self._session_meta(r)
            meta["tags"] = tags_by_session.get(r["id"], [])
            out.append(meta)
        return out

    def get_session(self, session_id: int, include_analysis: bool = True) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM session WHERE id = ?",
                                (session_id,)).fetchone()
        if row is None:
            raise NotFound(f"session {session_id} not found")
        out = self._session_meta(row)
        out["tags"] = self.list_session_tags(session_id)
        if include_analysis:
            out["analysis"] = json.loads(row["analysis_json"])
        return out

    def update_session_name(self, session_id: int, name: str) -> dict[str, Any]:
        """Rename ONLY. The imported metrics/summary/analysis are never altered."""
        name = (name or "").strip()
        if not name:
            raise ValueError("session name is required and must be non-empty")
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE session SET name = ?, updated_at = ? WHERE id = ?",
                (name, _now(), session_id))
            if cur.rowcount == 0:
                raise NotFound(f"session {session_id} not found")
        return self.get_session(session_id, include_analysis=False)

    def delete_session(self, session_id: int) -> None:
        with self._lock, self.conn:
            # explicit child cleanup (independent of PRAGMA cascade support)
            self.conn.execute("DELETE FROM note WHERE session_id = ?", (session_id,))
            self.conn.execute("DELETE FROM session_tag WHERE session_id = ?", (session_id,))
            cur = self.conn.execute("DELETE FROM session WHERE id = ?", (session_id,))
            if cur.rowcount == 0:
                raise NotFound(f"session {session_id} not found")

    def _session_meta(self, row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        return {
            "id": row["id"], "name": row["name"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "source_filename": row["source_filename"],
            "summary": json.loads(row["summary"]) if row["summary"] else None,
            "analysis_ref": row["analysis_ref"],
            "source_run_id": (row["source_run_id"] if "source_run_id" in keys else None),
            "preset_id": (row["preset_id"] if "preset_id" in keys else None),
        }

    # ================= weight presets =================
    def create_preset(self, name: str, weights: dict[str, Any]) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("preset name is required and must be non-empty")
        w = validate_weights(weights)
        now = _now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO weight_preset (name, w_accuracy, w_latency, w_vram, "
                "w_tokens, created_at, updated_at, is_builtin) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (name, w["w_accuracy"], w["w_latency"], w["w_vram"], w["w_tokens"], now, now))
            pid = cur.lastrowid
        return self.get_preset(pid)

    def list_presets(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM weight_preset ORDER BY is_builtin DESC, datetime(created_at), id"
        ).fetchall()
        return [self._preset_row(r) for r in rows]

    def get_preset(self, preset_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM weight_preset WHERE id = ?",
                                (preset_id,)).fetchone()
        if row is None:
            raise NotFound(f"preset {preset_id} not found")
        return self._preset_row(row)

    def update_preset(self, preset_id: int, name: Optional[str] = None,
                      weights: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM weight_preset WHERE id = ?",
                                (preset_id,)).fetchone()
        if row is None:
            raise NotFound(f"preset {preset_id} not found")
        if row["is_builtin"]:
            raise Forbidden(f"preset '{row['name']}' is a builtin and is read-only "
                            "(load-only; cannot be edited)")
        sets, params = [], []
        if name is not None:
            n = name.strip()
            if not n:
                raise ValueError("preset name must be non-empty")
            sets.append("name = ?"); params.append(n)
        if weights is not None:
            w = validate_weights(weights)
            for k in WEIGHT_KEYS:
                sets.append(f"{k} = ?"); params.append(w[k])
        if not sets:
            return self._preset_row(row)
        sets.append("updated_at = ?"); params.append(_now())
        params.append(preset_id)
        with self._lock, self.conn:
            self.conn.execute(
                f"UPDATE weight_preset SET {', '.join(sets)} WHERE id = ?", params)
        return self.get_preset(preset_id)

    def delete_preset(self, preset_id: int) -> None:
        row = self.conn.execute("SELECT is_builtin, name FROM weight_preset WHERE id = ?",
                                (preset_id,)).fetchone()
        if row is None:
            raise NotFound(f"preset {preset_id} not found")
        if row["is_builtin"]:
            raise Forbidden(f"preset '{row['name']}' is a builtin and cannot be deleted")
        with self._lock, self.conn:
            # detach any sessions that referenced it (keeps migrated DBs consistent even
            # without the DB-level ON DELETE SET NULL); the sessions themselves survive.
            self.conn.execute("UPDATE session SET preset_id = NULL WHERE preset_id = ?", (preset_id,))
            self.conn.execute("DELETE FROM weight_preset WHERE id = ?", (preset_id,))

    def _preset_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"],
            "w_accuracy": row["w_accuracy"], "w_latency": row["w_latency"],
            "w_vram": row["w_vram"], "w_tokens": row["w_tokens"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "is_builtin": bool(row["is_builtin"]),
        }

    # ================= notes =================
    def create_note(self, session_id: int, body: str) -> dict[str, Any]:
        if self.conn.execute("SELECT 1 FROM session WHERE id = ?",
                             (session_id,)).fetchone() is None:
            raise NotFound(f"session {session_id} not found")
        body = (body or "").strip()
        if not body:
            raise ValueError("note body is required and must be non-empty")
        now = _now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO note (session_id, body, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)", (session_id, body, now, now))
            nid = cur.lastrowid
        return self.get_note(nid)

    def list_notes(self, session_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM note WHERE session_id = ? ORDER BY datetime(created_at), id",
            (session_id,)).fetchall()
        return [self._note_row(r) for r in rows]

    def get_note(self, note_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM note WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            raise NotFound(f"note {note_id} not found")
        return self._note_row(row)

    def update_note(self, note_id: int, body: str) -> dict[str, Any]:
        body = (body or "").strip()
        if not body:
            raise ValueError("note body is required and must be non-empty")
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE note SET body = ?, updated_at = ? WHERE id = ?",
                (body, _now(), note_id))
            if cur.rowcount == 0:
                raise NotFound(f"note {note_id} not found")
        return self.get_note(note_id)

    def delete_note(self, note_id: int) -> None:
        with self._lock, self.conn:
            cur = self.conn.execute("DELETE FROM note WHERE id = ?", (note_id,))
            if cur.rowcount == 0:
                raise NotFound(f"note {note_id} not found")

    def _note_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "session_id": row["session_id"], "body": row["body"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    # ================= tags (Phase 19) =================
    @staticmethod
    def _clean_tag_name(name: str) -> str:
        n = (name or "").strip()
        if not n:
            raise ValueError("tag name is required and must be non-empty")
        if len(n) > 64:
            raise ValueError("tag name must be 64 characters or fewer")
        return n

    def _tag_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "created_at": row["created_at"]}

    def _find_tag(self, ref: str | int) -> Optional[sqlite3.Row]:
        """Look up a tag by id (int / digit-string) or by name (case-insensitive)."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            return self.conn.execute("SELECT * FROM tag WHERE id = ?", (int(ref),)).fetchone()
        return self.conn.execute(
            "SELECT * FROM tag WHERE name = ? COLLATE NOCASE", (str(ref).strip(),)).fetchone()

    def get_or_create_tag(self, name: str) -> dict[str, Any]:
        """Return the tag with this name, creating it if new (case-insensitive)."""
        n = self._clean_tag_name(name)
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM tag WHERE name = ? COLLATE NOCASE", (n,)).fetchone()
            if row is not None:
                return self._tag_row(row)
            cur = self.conn.execute(
                "INSERT INTO tag (name, created_at) VALUES (?, ?)", (n, _now()))
            tid = cur.lastrowid
        return self.get_tag(tid)

    def get_tag(self, tag_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM tag WHERE id = ?", (tag_id,)).fetchone()
        if row is None:
            raise NotFound(f"tag {tag_id} not found")
        return self._tag_row(row)

    def list_tags(self) -> list[dict[str, Any]]:
        """All tags with how many sessions carry each (for filter menus)."""
        rows = self.conn.execute(
            "SELECT t.id, t.name, t.created_at, "
            "  (SELECT COUNT(*) FROM session_tag st WHERE st.tag_id = t.id) AS session_count "
            "FROM tag t ORDER BY t.name COLLATE NOCASE").fetchall()
        return [{"id": r["id"], "name": r["name"], "created_at": r["created_at"],
                 "session_count": r["session_count"]} for r in rows]

    def delete_tag(self, tag_id: int) -> None:
        """Delete a tag and remove it from every session (never touches analyses)."""
        with self._lock, self.conn:
            if self.conn.execute("SELECT 1 FROM tag WHERE id = ?", (tag_id,)).fetchone() is None:
                raise NotFound(f"tag {tag_id} not found")
            self.conn.execute("DELETE FROM session_tag WHERE tag_id = ?", (tag_id,))
            self.conn.execute("DELETE FROM tag WHERE id = ?", (tag_id,))

    def list_session_tags(self, session_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT t.id, t.name, t.created_at FROM tag t "
            "JOIN session_tag st ON st.tag_id = t.id WHERE st.session_id = ? "
            "ORDER BY t.name COLLATE NOCASE", (session_id,)).fetchall()
        return [self._tag_row(r) for r in rows]

    def _all_session_tags(self) -> dict[int, list[dict[str, Any]]]:
        rows = self.conn.execute(
            "SELECT st.session_id AS sid, t.id, t.name, t.created_at FROM session_tag st "
            "JOIN tag t ON t.id = st.tag_id ORDER BY t.name COLLATE NOCASE").fetchall()
        out: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["sid"], []).append(
                {"id": r["id"], "name": r["name"], "created_at": r["created_at"]})
        return out

    def add_session_tag(self, session_id: int, name: str) -> dict[str, Any]:
        """Attach a tag (by name, created on demand) to a session. Idempotent.
        Returns the session's full tag list."""
        if self.conn.execute("SELECT 1 FROM session WHERE id = ?",
                             (session_id,)).fetchone() is None:
            raise NotFound(f"session {session_id} not found")
        tag = self.get_or_create_tag(name)
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO session_tag (session_id, tag_id, created_at) "
                "VALUES (?, ?, ?)", (session_id, tag["id"], _now()))
        return {"session_id": session_id, "tags": self.list_session_tags(session_id)}

    def remove_session_tag(self, session_id: int, tag_id: int) -> dict[str, Any]:
        """Detach a tag from a session (the tag itself is left intact)."""
        if self.conn.execute("SELECT 1 FROM session WHERE id = ?",
                             (session_id,)).fetchone() is None:
            raise NotFound(f"session {session_id} not found")
        with self._lock, self.conn:
            cur = self.conn.execute(
                "DELETE FROM session_tag WHERE session_id = ? AND tag_id = ?",
                (session_id, tag_id))
            if cur.rowcount == 0:
                raise NotFound(f"tag {tag_id} is not on session {session_id}")
        return {"session_id": session_id, "tags": self.list_session_tags(session_id)}

    # ================= HF metadata cache (Phase 22) =================
    def hf_cache_get(self, model_id: str) -> Optional[dict[str, Any]]:
        """Return {'fetched_at','status','data'} for a model, or None if absent."""
        row = self.conn.execute(
            "SELECT fetched_at, status, payload FROM hf_metadata_cache WHERE model_id = ?",
            (model_id,)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["payload"])
        except (ValueError, TypeError):
            return None
        return {"fetched_at": row["fetched_at"], "status": row["status"], "data": data}

    def hf_cache_set(self, model_id: str, entry: dict[str, Any]) -> None:
        """Upsert a cache entry (entry = {'fetched_at','status','data'}).

        Writes the full JSON payload AND the promoted queryable columns in one
        statement, so the columns always match the payload they were derived from.
        """
        data = entry.get("data", {}) or {}
        promoted = [_hf_promoted_value(data, c) for c in _HF_PROMOTED]
        cols = ", ".join(_HF_PROMOTED)
        placeholders = ", ".join("?" for _ in _HF_PROMOTED)
        set_promoted = ", ".join(f"{c} = excluded.{c}" for c in _HF_PROMOTED)
        with self._lock, self.conn:
            self.conn.execute(
                f"INSERT INTO hf_metadata_cache (model_id, fetched_at, status, payload, {cols}) "
                f"VALUES (?, ?, ?, ?, {placeholders}) ON CONFLICT(model_id) DO UPDATE SET "
                "fetched_at = excluded.fetched_at, status = excluded.status, "
                f"payload = excluded.payload, {set_promoted}",
                [model_id, float(entry.get("fetched_at", 0.0)),
                 str(entry.get("status", "ok")), json.dumps(data)] + promoted)
