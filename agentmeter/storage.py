"""Phase 6 — SQLite persistence layer.

Persists the RAW rows the existing instrumentation already collects (per-agent
metrics, per-scenario verdicts, and one row per run). It deliberately does NOT
compute anything derived — no accuracy aggregates, no SAW Composite Health
Score. Those are Phase 7/8 and consume these raw rows later.

Schema (normalized, reproducible):
  runs(run_id, config_fingerprint, quant_setting, hardware_label,
       started_at, finished_at, status, notes)
  scenario_results(run_id, model, scenario_id, predicted_label,
       held_out_label, correct, scenario_total_time_s, scenario_peak_vram_mb,
       status, completed_at)      -- PK (run_id, model, scenario_id)
  agent_metrics(run_id, model, scenario_id, agent_name, wall_time_s, ttft_s,
       vram_delta_mb, input_tokens, output_tokens)  -- PK (.., agent_name)

Atomicity guarantee (checkpoint/resume): a scenario's four agent rows AND its
scenario_result row are written in ONE transaction. A crash mid-scenario rolls
back — it never leaves a half-written scenario that resume would skip.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .instrument import AgentMetrics


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    config_fingerprint TEXT NOT NULL,
    quant_setting      TEXT,
    hardware_label     TEXT,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    status             TEXT NOT NULL,          -- running | complete | abandoned
    notes              TEXT
);

CREATE TABLE IF NOT EXISTS scenario_results (
    run_id                 TEXT NOT NULL,
    model                  TEXT NOT NULL,
    scenario_id            TEXT NOT NULL,
    predicted_label        TEXT,
    held_out_label         TEXT,
    correct                INTEGER,            -- 0/1, stored for Phase 7 accuracy
    scenario_total_time_s  REAL,
    scenario_peak_vram_mb  REAL,               -- peak/max across agents, not sum
    status                 TEXT NOT NULL,      -- complete
    completed_at           TEXT,
    PRIMARY KEY (run_id, model, scenario_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS agent_metrics (
    run_id         TEXT NOT NULL,
    model          TEXT NOT NULL,
    scenario_id    TEXT NOT NULL,
    agent_name     TEXT NOT NULL,
    wall_time_s    REAL,
    ttft_s         REAL,                       -- NULL on mock/CPU (never fabricated)
    vram_delta_mb  REAL,                       -- NULL on CPU
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    PRIMARY KEY (run_id, model, scenario_id, agent_name),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
"""


class Storage:
    """Thin SQLite wrapper. One connection; writes are transactional."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        # The run-full parent and its per-model worker each hold a connection to
        # this file; wait rather than fail if the other briefly holds a lock.
        self.conn.execute("PRAGMA busy_timeout = 5000;")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- run lifecycle --------------------------------------------------
    def create_run(
        self,
        run_id: str,
        config_fingerprint: str,
        quant_setting: str,
        hardware_label: str,
        notes: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs (run_id, config_fingerprint, quant_setting, "
                "hardware_label, started_at, finished_at, status, notes) "
                "VALUES (?, ?, ?, ?, ?, NULL, 'running', ?)",
                (run_id, config_fingerprint, quant_setting, hardware_label, _now(), notes),
            )

    def find_incomplete_run(self) -> Optional[dict[str, Any]]:
        """Most recent run still marked 'running' (a candidate to resume)."""
        row = self.conn.execute(
            "SELECT * FROM runs WHERE status = 'running' "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def finish_run(self, run_id: str, status: str = "complete") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, _now(), run_id),
            )

    def abandon_incomplete_runs(self) -> int:
        """Mark every 'running' run as 'abandoned' (used by --fresh). Returns count."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE runs SET status = 'abandoned', finished_at = ? "
                "WHERE status = 'running'",
                (_now(),),
            )
            return cur.rowcount

    # --- checkpoint / resume -------------------------------------------
    def completed_pairs(self, run_id: str) -> set[tuple[str, str]]:
        """(model, scenario_id) pairs already persisted complete for this run."""
        rows = self.conn.execute(
            "SELECT model, scenario_id FROM scenario_results "
            "WHERE run_id = ? AND status = 'complete'",
            (run_id,),
        ).fetchall()
        return {(r["model"], r["scenario_id"]) for r in rows}

    def persist_scenario(
        self,
        run_id: str,
        model: str,
        scenario_id: str,
        predicted_label: str,
        held_out_label: str,
        correct: bool,
        scenario_total_time_s: float,
        scenario_peak_vram_mb: Optional[float],
        agent_rows: Iterable[AgentMetrics],
    ) -> None:
        """Write all agent rows + the scenario_result in ONE atomic transaction.

        Idempotent: any prior rows for this (run_id, model, scenario_id) are
        deleted first inside the same transaction, so a re-run after a crash
        never duplicates rows.
        """
        agent_params = [
            (
                run_id,
                model,
                scenario_id,
                m.agent_name,
                m.wall_time_s,
                m.ttft_s,
                m.vram_peak_mb,
                m.input_tokens,
                m.output_tokens,
            )
            for m in agent_rows
        ]
        with self.conn:  # BEGIN ... COMMIT (or ROLLBACK on any exception)
            self.conn.execute(
                "DELETE FROM agent_metrics WHERE run_id=? AND model=? AND scenario_id=?",
                (run_id, model, scenario_id),
            )
            self.conn.execute(
                "DELETE FROM scenario_results WHERE run_id=? AND model=? AND scenario_id=?",
                (run_id, model, scenario_id),
            )
            self.conn.executemany(
                "INSERT INTO agent_metrics (run_id, model, scenario_id, agent_name, "
                "wall_time_s, ttft_s, vram_delta_mb, input_tokens, output_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                agent_params,
            )
            self.conn.execute(
                "INSERT INTO scenario_results (run_id, model, scenario_id, "
                "predicted_label, held_out_label, correct, scenario_total_time_s, "
                "scenario_peak_vram_mb, status, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?)",
                (
                    run_id,
                    model,
                    scenario_id,
                    predicted_label,
                    held_out_label,
                    int(bool(correct)),
                    scenario_total_time_s,
                    scenario_peak_vram_mb,
                    _now(),
                ),
            )

    # --- read-back helpers (for the verify step / CLI summaries) --------
    def table_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in ("runs", "scenario_results", "agent_metrics"):
            out[t] = self.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        return out

    def scenario_row(self, run_id: str, model: str, scenario_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM scenario_results WHERE run_id=? AND model=? AND scenario_id=?",
            (run_id, model, scenario_id),
        ).fetchone()
        return dict(row) if row else None

    def agent_rows(self, run_id: str, model: str, scenario_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM agent_metrics WHERE run_id=? AND model=? AND scenario_id=? "
            "ORDER BY rowid",
            (run_id, model, scenario_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def mean_scenario_wall_s(self, run_id: str) -> float:
        row = self.conn.execute(
            "SELECT AVG(scenario_total_time_s) AS m FROM scenario_results WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return float(row["m"]) if row and row["m"] is not None else 0.0
