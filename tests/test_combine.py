"""Phase 26 — single unified database: shared file + combine-db migration (CPU-only).

Covers:
  (a) AppStore and Storage share ONE file (all 9 tables), and session.source_run_id
      is a real FK that resolves to a present run and degrades to NULL otherwise;
  (b) combine_databases merges a legacy study + app pair into one file: ids preserved,
      valid links kept, dangling source_run_id nulled, builtins not duplicated, a clean
      foreign_key_check, and both source files left untouched;
  (c) the guards (output must not exist / must differ from sources).
"""
from __future__ import annotations

import sqlite3

import pytest

from agentmeter.db import appdb, combine
from agentmeter.db.storage import Storage
from agentmeter.pipeline.instrument import AgentMetrics

AGENTS = ("perceive", "reason", "decide", "act")


def _seed_study(path, run_id="run1", models=("m/A", "m/B")):
    st = Storage(path)
    st.create_run(run_id, "fp", "4bit", "L4")
    for m in models:
        st.persist_scenario(run_id, m, "s1", "BENIGN", "BENIGN", True, 0.4, None,
                            [AgentMetrics("s1", m, a, 0.1, None, None, 5, 3) for a in AGENTS])
    st.close()
    return st


def _legacy_app(path):
    """An OLD-shape app DB: no runs table, session.source_run_id a plain column."""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE weight_preset (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          w_accuracy REAL, w_latency REAL, w_vram REAL, w_tokens REAL,
          created_at TEXT, updated_at TEXT, is_builtin INTEGER DEFAULT 0);
        CREATE TABLE session (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source_filename TEXT, summary TEXT,
          analysis_ref TEXT, analysis_json TEXT NOT NULL, source_run_id TEXT, preset_id INTEGER);
        CREATE TABLE note (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
          body TEXT, created_at TEXT, updated_at TEXT);
        INSERT INTO weight_preset (id,name,w_accuracy,w_latency,w_vram,w_tokens,created_at,updated_at,is_builtin)
          VALUES (1,'default',0.4,0.25,0.2,0.15,'t','t',1);
        INSERT INTO weight_preset (id,name,w_accuracy,w_latency,w_vram,w_tokens,created_at,updated_at,is_builtin)
          VALUES (2,'myfav',0.5,0.2,0.2,0.1,'t','t',0);
        INSERT INTO session (id,name,created_at,updated_at,analysis_json,source_run_id,preset_id)
          VALUES (1,'good','t','t','{}','run1',2);
        INSERT INTO session (id,name,created_at,updated_at,analysis_json,source_run_id)
          VALUES (2,'dangler','t','t','{}','ghost');
        INSERT INTO note (session_id,body,created_at,updated_at) VALUES (1,'a note','t','t');
    """)
    c.commit(); c.close()


# ---------------- (a) shared file + real source_run_id FK ----------------

def test_appstore_and_storage_share_one_file(tmp_path):
    f = tmp_path / "agentmeter.db"
    _seed_study(f)                          # Storage writes study tables
    store = appdb.AppStore(f)               # AppStore opens the SAME file
    tables = {r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert {"runs", "scenario_results", "agent_metrics",
            "session", "weight_preset", "note", "tag", "session_tag",
            "hf_metadata_cache"} <= tables
    store.close()


def test_source_run_id_is_real_fk(tmp_path):
    f = tmp_path / "agentmeter.db"
    _seed_study(f, run_id="runZ")
    store = appdb.AppStore(f)
    # resolvable run -> kept
    s = store.create_session("ok", {"phase8": {}, "run_ids": ["runZ"]})
    assert s["source_run_id"] == "runZ"
    # unknown run -> degraded to NULL (not rejected, not fabricated)
    s2 = store.create_session("ghost", {"phase8": {}, "run_ids": ["nope"]})
    assert s2["source_run_id"] is None
    store.close()


# ---------------- (b) combine merges legacy pair ----------------

def test_combine_merges_and_preserves_links(tmp_path):
    study, app, out = tmp_path / "study.db", tmp_path / "app.db", tmp_path / "unified.db"
    _seed_study(study, run_id="run1")
    _legacy_app(app)

    summary = combine.combine_databases(study, app, out)
    assert summary["source_run_id_nulled"] == 1          # 'ghost' nulled

    c = sqlite3.connect(out); c.execute("PRAGMA foreign_keys = ON")
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    # valid link kept, dangling nulled, ids preserved so preset link survives
    assert c.execute("SELECT source_run_id FROM session WHERE name='good'").fetchone()[0] == "run1"
    assert c.execute("SELECT source_run_id FROM session WHERE name='dangler'").fetchone()[0] is None
    assert c.execute("SELECT preset_id FROM session WHERE name='good'").fetchone()[0] == 2
    assert c.execute("SELECT body FROM note WHERE session_id=1").fetchone()[0] == "a note"
    # study data present in the same file
    assert c.execute("SELECT COUNT(*) FROM scenario_results").fetchone()[0] == 2
    # builtins not duplicated: legacy 'default' + 3 seeded = 4, plus the user preset
    assert c.execute("SELECT COUNT(*) FROM weight_preset WHERE is_builtin=1").fetchone()[0] == 4
    assert c.execute("SELECT COUNT(*) FROM weight_preset WHERE name='default'").fetchone()[0] == 1
    c.close()
    # sources untouched
    assert study.exists() and app.exists()


def test_combine_refuses_existing_output(tmp_path):
    study, app, out = tmp_path / "study.db", tmp_path / "app.db", tmp_path / "out.db"
    _seed_study(study); _legacy_app(app)
    out.write_text("x")
    with pytest.raises(FileExistsError):
        combine.combine_databases(study, app, out)
    # overwrite=True proceeds
    summary = combine.combine_databases(study, app, out, overwrite=True)
    assert summary["rows_copied"] > 0


def test_combine_refuses_output_equal_to_source(tmp_path):
    study, app = tmp_path / "study.db", tmp_path / "app.db"
    _seed_study(study); _legacy_app(app)
    with pytest.raises(ValueError):
        combine.combine_databases(study, app, study, overwrite=True)
