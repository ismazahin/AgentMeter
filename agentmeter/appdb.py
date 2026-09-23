"""Phase 12 — application metadata DB (CRUD for sessions, weight presets, notes).

A SEPARATE SQLite database (results/agentmeter_app.db by default) that lets the
system manage its own records. It is a VIEW/MANAGEMENT layer only and NEVER
touches the locked study:
  * the locked study DB (results/agentmeter_full_l4.db) is never opened here;
  * imported analysis.json data is stored READ-ONLY (its summary is extracted
    once at import and never recomputed or edited);
  * there is no CRUD on datasets or the 5-model set — out of scope by design.

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

from .config import PROJECT_ROOT

DEFAULT_APP_DB = PROJECT_ROOT / "results" / "agentmeter_app.db"
# The locked study DB — this module must NEVER open it (guard below).
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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    source_filename TEXT,
    summary         TEXT,            -- small JSON blob (read-only, from import)
    analysis_ref    TEXT,            -- original path/name of the imported analysis.json
    analysis_json   TEXT NOT NULL    -- stored copy of the imported analysis (read-only)
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
"""


class AppStore:
    """CRUD over the metadata DB. Thread-safe (single connection + lock), so it can
    back a threaded Flask server."""

    def __init__(self, db_path: str | Path = DEFAULT_APP_DB):
        self.path = Path(db_path)
        # Integrity guard: refuse to ever be pointed at the locked study DB.
        if self.path.resolve() == LOCKED_STUDY_DB:
            raise ValueError(
                f"AppStore must not use the locked study DB ({LOCKED_STUDY_DB}). "
                "CRUD uses a separate metadata DB.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.execute("PRAGMA busy_timeout = 5000;")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._lock = threading.Lock()
        self._seed_builtins()

    def close(self) -> None:
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
                        analysis_ref: Optional[str] = None) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("session name is required and must be non-empty")
        if not isinstance(analysis, dict) or "phase8" not in analysis:
            raise ValueError("analysis must be an object containing a 'phase8' block "
                             "(an AgentMeter analysis.json)")
        summary = summarize_analysis(analysis)
        now = _now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO session (name, created_at, updated_at, source_filename, "
                "summary, analysis_ref, analysis_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, now, now, source_filename, json.dumps(summary),
                 analysis_ref or source_filename, json.dumps(analysis)))
            sid = cur.lastrowid
        return self.get_session(sid, include_analysis=True)

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, name, created_at, updated_at, source_filename, summary, "
            "analysis_ref FROM session ORDER BY datetime(created_at) DESC, id DESC"
        ).fetchall()
        return [self._session_meta(r) for r in rows]

    def get_session(self, session_id: int, include_analysis: bool = True) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM session WHERE id = ?",
                                (session_id,)).fetchone()
        if row is None:
            raise NotFound(f"session {session_id} not found")
        out = self._session_meta(row)
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
            # explicit note cleanup (independent of PRAGMA cascade support)
            self.conn.execute("DELETE FROM note WHERE session_id = ?", (session_id,))
            cur = self.conn.execute("DELETE FROM session WHERE id = ?", (session_id,))
            if cur.rowcount == 0:
                raise NotFound(f"session {session_id} not found")

    def _session_meta(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "source_filename": row["source_filename"],
            "summary": json.loads(row["summary"]) if row["summary"] else None,
            "analysis_ref": row["analysis_ref"],
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
