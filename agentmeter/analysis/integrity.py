"""Phase 25 — cross-database integrity checker (READ-ONLY).

The study results DB (agentmeter_full_l4.db) and the app metadata DB
(agentmeter_app.db) are deliberately separate files, so the few references that
cross between them cannot be enforced by a SQL foreign key. This module makes
those references *checkable on demand* instead: it opens both databases
READ-ONLY and reports

  1. session.source_run_id values that do not resolve to a runs.run_id
     (a WEAK, one-way cross-DB reference — dangling here is allowed, reported
     as a warning, never an error);
  2. session.preset_id values that do not resolve to a weight_preset.id
     (an app-internal FK — dangling here IS an integrity error);
  3. model coverage: the distinct model names measured in the study DB, and for
     each whether Hugging Face metadata is cached in the app DB — the practical
     stand-in for a `model` lookup table, computed rather than stored.

It NEVER writes to either database and NEVER opens the study DB through Storage
(so no schema/DDL is ever applied to the locked file): both connections use a
raw `mode=ro` URI, exactly like analyze.py.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from ..config import load_config
from ..db import appdb


def _connect_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    """Read-only connection, or None if the file is absent/unopenable."""
    if not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError:
        return None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def check_integrity(study_db_path: str | Path, app_db_path: str | Path) -> dict[str, Any]:
    """Run every cross-DB / lookup check and return a structured report.

    `ok` is True when no HARD integrity error was found. Weak cross-DB dangling
    references and uncached models are reported but do not make `ok` False.
    """
    report: dict[str, Any] = {
        "study_db": str(study_db_path),
        "app_db": str(app_db_path),
        "study_db_present": False,
        "app_db_present": False,
        "errors": [],        # hard integrity failures (app-internal FKs)
        "warnings": [],       # weak cross-DB danglings (allowed by design)
        "checks": {},         # per-check detail
    }

    study = _connect_ro(study_db_path)
    app = _connect_ro(app_db_path)
    report["study_db_present"] = study is not None
    report["app_db_present"] = app is not None

    try:
        study_run_ids: set[str] = set()
        study_models: set[str] = set()
        if study is not None:
            if _table_exists(study, "runs"):
                study_run_ids = {r[0] for r in study.execute("SELECT run_id FROM runs")}
            if _table_exists(study, "scenario_results"):
                study_models = {
                    r[0] for r in study.execute(
                        "SELECT DISTINCT model FROM scenario_results") if r[0] is not None}

        # ---- 1 & 2: session references -------------------------------------
        if app is not None and _table_exists(app, "session"):
            scols = _columns(app, "session")

            # 1. WEAK cross-DB reference: session.source_run_id -> runs.run_id
            if "source_run_id" in scols:
                rows = app.execute(
                    "SELECT id, name, source_run_id FROM session "
                    "WHERE source_run_id IS NOT NULL").fetchall()
                dangling = [
                    {"session_id": r["id"], "name": r["name"], "source_run_id": r["source_run_id"]}
                    for r in rows
                    if study is None or r["source_run_id"] not in study_run_ids
                ]
                report["checks"]["source_run_id"] = {
                    "referencing_sessions": len(rows),
                    "unresolved": dangling,
                    "resolvable": study is not None,
                }
                for d in dangling:
                    report["warnings"].append(
                        f"session {d['session_id']} ('{d['name']}') source_run_id "
                        f"'{d['source_run_id']}' has no matching run"
                        + ("" if study is not None else " (study DB not present to check)"))

            # 2. app-internal FK: session.preset_id -> weight_preset.id
            if "preset_id" in scols and _table_exists(app, "weight_preset"):
                preset_ids = {r[0] for r in app.execute("SELECT id FROM weight_preset")}
                rows = app.execute(
                    "SELECT id, name, preset_id FROM session "
                    "WHERE preset_id IS NOT NULL").fetchall()
                bad = [
                    {"session_id": r["id"], "name": r["name"], "preset_id": r["preset_id"]}
                    for r in rows if r["preset_id"] not in preset_ids
                ]
                report["checks"]["preset_id"] = {
                    "referencing_sessions": len(rows), "unresolved": bad}
                for b in bad:
                    report["errors"].append(
                        f"session {b['session_id']} ('{b['name']}') preset_id "
                        f"{b['preset_id']} does not exist in weight_preset")

        # ---- 3: model coverage (the computed `model` lookup) ---------------
        cached_models: set[str] = set()
        if app is not None and _table_exists(app, "hf_metadata_cache"):
            cached_models = {
                r[0] for r in app.execute(
                    "SELECT model_id FROM hf_metadata_cache WHERE status = 'ok'")
                if r[0] is not None}
        report["checks"]["model_coverage"] = {
            "study_models": sorted(study_models),
            "cached_with_metadata": sorted(study_models & cached_models),
            "uncached_study_models": sorted(study_models - cached_models),
            "cached_not_in_study": sorted(cached_models - study_models),
        }

        report["ok"] = not report["errors"]
        return report
    finally:
        if study is not None:
            study.close()
        if app is not None:
            app.close()


def format_report(report: dict[str, Any]) -> str:
    """Human-readable summary for the CLI."""
    L: list[str] = []
    L.append("=" * 64)
    L.append("  AgentMeter — cross-database integrity check (read-only)")
    L.append("=" * 64)
    L.append(f"Study DB : {report['study_db']}"
             f"  [{'found' if report['study_db_present'] else 'MISSING'}]")
    L.append(f"App DB   : {report['app_db']}"
             f"  [{'found' if report['app_db_present'] else 'MISSING'}]")
    L.append("")

    src = report["checks"].get("source_run_id")
    if src is not None:
        n = src["referencing_sessions"]
        bad = len(src["unresolved"])
        L.append(f"[weak] session.source_run_id -> runs.run_id : "
                 f"{n - bad}/{n} resolve"
                 + ("" if src["resolvable"] else "  (study DB absent — not checked)"))
        for d in src["unresolved"]:
            L.append(f"    ! session {d['session_id']} ('{d['name']}') -> "
                     f"'{d['source_run_id']}' (no such run)")
    else:
        L.append("[weak] session.source_run_id : no sessions reference a run")

    pre = report["checks"].get("preset_id")
    if pre is not None:
        n = pre["referencing_sessions"]
        bad = len(pre["unresolved"])
        L.append(f"[FK]   session.preset_id -> weight_preset.id : {n - bad}/{n} resolve")
        for b in pre["unresolved"]:
            L.append(f"    X session {b['session_id']} ('{b['name']}') -> "
                     f"preset {b['preset_id']} (missing)")

    mc = report["checks"]["model_coverage"]
    L.append("")
    L.append(f"[lookup] model coverage — {len(mc['study_models'])} model(s) in the study DB:")
    for m in mc["study_models"]:
        tick = "metadata cached" if m in mc["cached_with_metadata"] else "no HF metadata"
        L.append(f"    - {m:<45} {tick}")
    if mc["cached_not_in_study"]:
        L.append(f"    (cached but not in this study: {', '.join(mc['cached_not_in_study'])})")

    L.append("")
    if report["errors"]:
        L.append(f"RESULT: {len(report['errors'])} integrity ERROR(s), "
                 f"{len(report['warnings'])} warning(s).")
    elif report["warnings"]:
        L.append(f"RESULT: OK (no integrity errors); {len(report['warnings'])} "
                 "weak-reference warning(s) — expected by design.")
    else:
        L.append("RESULT: OK — all references resolve.")
    return "\n".join(L)


def run_integrity_check(config_path: Optional[str] = None,
                        study_db: Optional[str] = None,
                        app_db: Optional[str] = None) -> dict[str, Any]:
    """Resolve both DB paths (CLI overrides > config > defaults) and run the check."""
    cfg = load_config(config_path)
    study_path = study_db or str(cfg.resolve_path("storage.sqlite_path", "results/agentmeter.db"))
    app_path = app_db or str(appdb.DEFAULT_APP_DB)
    return check_integrity(study_path, app_path)
