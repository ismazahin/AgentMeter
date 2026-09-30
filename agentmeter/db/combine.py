"""Phase 26 — merge the legacy two-file databases into one unified file.

Historically AgentMeter used two SQLite files: the study results DB
(runs / scenario_results / agent_metrics) and the app-metadata DB (session /
weight_preset / note / tag / session_tag / hf_metadata_cache). Phase 26 unifies
them into ONE file. This module builds that unified file from the two existing
ones WITHOUT modifying either source (they stay as backups).

It preserves every primary-key id exactly, so all foreign keys survive the move.
The only value it may change: a `session.source_run_id` that does not match any
`runs.run_id` is set to NULL, because it is now a real foreign key (the old file
allowed such dangling weak references; the unified schema does not).
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import appdb, storage

# App tables to copy, in dependency order (parents before children).
_APP_TABLES = ("weight_preset", "tag", "session", "note", "session_tag", "hf_metadata_cache")


def _table_columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    # attached-DB form is `PRAGMA app.table_info(t)`, not `PRAGMA table_info(app.t)`
    return [r["name"] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def combine_databases(study_path: str | Path, app_path: str | Path,
                      out_path: str | Path, overwrite: bool = False) -> dict[str, Any]:
    """Build a unified DB at out_path from study_path + app_path.

    Returns a summary dict {tables, rows_copied, source_run_id_nulled}. Never
    writes to the two source files.
    """
    study_path, app_path, out_path = Path(study_path), Path(app_path), Path(out_path)
    if not study_path.exists():
        raise FileNotFoundError(f"study DB not found: {study_path}")
    if not app_path.exists():
        raise FileNotFoundError(f"app DB not found: {app_path}")
    if out_path.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {out_path} (use --overwrite to replace)")
    if out_path.resolve() in (study_path.resolve(), app_path.resolve()):
        raise ValueError("output path must differ from both source files")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Start the unified file as a copy of the study DB (keeps its data + indexes),
    # then add the app schema and copy the app rows in.
    if out_path.exists():
        out_path.unlink()
    shutil.copyfile(study_path, out_path)

    conn = sqlite3.connect(str(out_path))
    conn.row_factory = sqlite3.Row
    summary: dict[str, Any] = {"tables": {}, "rows_copied": 0, "source_run_id_nulled": 0}
    try:
        conn.execute("PRAGMA foreign_keys = OFF;")
        # Ensure ALL tables exist in the unified file (study tables already present).
        conn.executescript(storage.SCHEMA)
        conn.executescript(appdb._SCHEMA)

        conn.execute("ATTACH DATABASE ? AS app;", (str(app_path),))
        for table in _APP_TABLES:
            # only tables that exist in the source app DB
            exists = conn.execute(
                "SELECT 1 FROM app.sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if not exists:
                continue
            dest_cols = set(_table_columns(conn, table))
            src_cols = [c for c in _table_columns(conn, table, schema="app") if c in dest_cols]
            if not src_cols:
                continue
            select_cols = list(src_cols)
            if table == "session" and "source_run_id" in src_cols:
                # dangling weak ref -> NULL (source_run_id is now a real FK to runs)
                idx = select_cols.index("source_run_id")
                select_cols[idx] = (
                    "CASE WHEN source_run_id IN (SELECT run_id FROM main.runs) "
                    "THEN source_run_id ELSE NULL END")
                summary["source_run_id_nulled"] = conn.execute(
                    "SELECT COUNT(*) FROM app.session WHERE source_run_id IS NOT NULL "
                    "AND source_run_id NOT IN (SELECT run_id FROM main.runs)").fetchone()[0]
            conn.execute(
                f"INSERT INTO main.{table} ({', '.join(src_cols)}) "
                f"SELECT {', '.join(select_cols)} FROM app.{table}")
            n = conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
            summary["tables"][table] = n
            summary["rows_copied"] += n
        conn.commit()                       # close the write txn before DETACH
        conn.execute("DETACH DATABASE app;")

        conn.execute("PRAGMA foreign_keys = ON;")
        violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
        if violations:
            raise RuntimeError(f"unified DB failed foreign-key check: {violations[:5]}")
    finally:
        conn.close()

    # Open once through AppStore to backfill hf promoted columns and confirm the
    # builtins are present (no duplication — they were copied with their ids), then
    # report FINAL row counts for every table (builtins seeded here are included).
    store = appdb.AppStore(out_path)
    for t in ("runs", "scenario_results", "agent_metrics", *_APP_TABLES):
        summary["tables"][t] = store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    store.close()
    summary["rows_copied"] = sum(summary["tables"][t] for t in _APP_TABLES)
    return summary
