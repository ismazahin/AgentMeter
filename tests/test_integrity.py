"""Phase 25 — schema hardening + cross-database integrity checker (CPU-only).

Covers exactly the safe fixes added in Phase 25:
  (a) study DB: per-model secondary indexes exist on new run DBs, and the
      DEFERRABLE composite FK agent_metrics -> scenario_results is enforced at
      COMMIT (an orphan agent row is rejected) while the normal atomic write
      still succeeds;
  (b) app DB: hf_metadata_cache promotes payload fields into typed columns on
      write, hf_cache_get still reads from the payload, and an older app DB with
      no promoted columns is migrated and backfilled from its stored payloads;
  (c) integrity checker: a resolvable vs a dangling source_run_id, a dangling
      preset_id reported as a hard error, per-model HF coverage, and graceful
      handling of a missing database file.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from agentmeter.db.storage import Storage
from agentmeter.db import appdb
from agentmeter.pipeline.instrument import AgentMetrics
from agentmeter.analysis.integrity import check_integrity


AGENTS = ("perceive", "reason", "decide", "act")


def _agent_rows(model, scenario_id):
    return [AgentMetrics(scenario_id, model, a, 0.1, None, None, 5, 3) for a in AGENTS]


def _seed_study(path, run_id="runX", models=("m/A", "m/B")):
    st = Storage(path)
    st.create_run(run_id, "fp", "4bit", "L4")
    for m in models:
        st.persist_scenario(run_id, m, "s1", "BENIGN", "BENIGN", True, 0.4, None,
                            _agent_rows(m, "s1"))
    return st


# ---------------- (a) study DB schema hardening ----------------

def test_new_study_db_has_model_indexes(tmp_path):
    st = _seed_study(tmp_path / "study.db")
    idx = {r[0] for r in st.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")}
    assert {"idx_sr_model", "idx_am_model"} <= idx
    st.close()


def test_composite_fk_is_enforced_at_commit(tmp_path):
    st = _seed_study(tmp_path / "study.db")
    # normal write already succeeded for two models
    assert st.table_counts() == {"runs": 1, "scenario_results": 2, "agent_metrics": 8}
    # an agent row with no matching scenario_results parent is rejected at COMMIT
    with pytest.raises(sqlite3.IntegrityError):
        with st.conn:
            st.conn.execute(
                "INSERT INTO agent_metrics (run_id, model, scenario_id, agent_name, "
                "wall_time_s, ttft_s, vram_delta_mb, input_tokens, output_tokens) "
                "VALUES ('runX', 'm/A', 'GHOST', 'perceive', 0.1, NULL, NULL, 1, 1)")
    # the failed transaction left nothing behind
    assert st.conn.execute(
        "SELECT COUNT(*) FROM agent_metrics WHERE scenario_id='GHOST'").fetchone()[0] == 0
    st.close()


def test_persist_scenario_still_atomic_with_deferred_fk(tmp_path):
    # writing agent rows first + scenario_results last (as persist_scenario does)
    # must succeed under the deferred composite FK.
    st = Storage(tmp_path / "study.db")
    st.create_run("r", "fp", "4bit", "L4")
    st.persist_scenario("r", "m/A", "s1", "BENIGN", "BENIGN", True, 0.4, None,
                        _agent_rows("m/A", "s1"))
    assert st.scenario_row("r", "m/A", "s1") is not None
    assert len(st.agent_rows("r", "m/A", "s1")) == 4
    st.close()


# ---------------- (b) app DB hybrid HF columns ----------------

def test_hf_cache_promotes_payload_into_columns(tmp_path):
    store = appdb.AppStore(tmp_path / "app.db")
    store.hf_cache_set("meta/Model-8B", {
        "fetched_at": time.time(), "status": "ok",
        "data": {"model": "meta/Model-8B", "params_b": 8.03, "downloads": 1234,
                 "likes": 56, "license": "apache-2.0", "pipeline_tag": "text-generation"}})
    row = store.conn.execute(
        "SELECT params_b, downloads, likes, license, pipeline_tag "
        "FROM hf_metadata_cache WHERE model_id = ?", ("meta/Model-8B",)).fetchone()
    assert dict(row) == {"params_b": 8.03, "downloads": 1234, "likes": 56,
                         "license": "apache-2.0", "pipeline_tag": "text-generation"}
    # payload remains the source of truth and hf_cache_get is unchanged
    assert store.hf_cache_get("meta/Model-8B")["data"]["params_b"] == 8.03
    store.close()


def test_hf_cache_unavailable_entry_leaves_columns_null(tmp_path):
    store = appdb.AppStore(tmp_path / "app.db")
    store.hf_cache_set("gated/x", {"fetched_at": 1.0, "status": "unavailable",
                                   "data": {"model": "gated/x"}})
    row = store.conn.execute(
        "SELECT downloads, params_b FROM hf_metadata_cache WHERE model_id='gated/x'").fetchone()
    assert row["downloads"] is None and row["params_b"] is None
    store.close()


def test_old_app_db_hf_columns_migrated_and_backfilled(tmp_path):
    # build an OLD-shape hf_metadata_cache with no promoted columns + one row
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript(
        "CREATE TABLE hf_metadata_cache (model_id TEXT PRIMARY KEY, fetched_at REAL "
        "NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);")
    c.execute("INSERT INTO hf_metadata_cache VALUES (?,?,?,?)",
              ("mistral/7B", 1.0, "ok", json.dumps(
                  {"params_b": 7.24, "downloads": 999, "likes": 10,
                   "license": "apache-2.0", "pipeline_tag": "text-generation"})))
    c.commit(); c.close()

    store = appdb.AppStore(p)  # __init__ -> _migrate() adds columns + backfills
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(hf_metadata_cache)")}
    assert {"params_b", "downloads", "likes", "license", "pipeline_tag"} <= cols
    row = store.conn.execute(
        "SELECT params_b, downloads, license FROM hf_metadata_cache "
        "WHERE model_id='mistral/7B'").fetchone()
    assert row["params_b"] == 7.24 and row["downloads"] == 999 and row["license"] == "apache-2.0"
    store.close()


def test_app_db_migration_is_idempotent(tmp_path):
    p = tmp_path / "app.db"
    appdb.AppStore(p).close()
    appdb.AppStore(p).close()   # second open must not raise (columns already present)
    store = appdb.AppStore(p)
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(hf_metadata_cache)")}
    assert "downloads" in cols
    store.close()


# ---------------- (c) integrity checker ----------------

def _seed_app(path):
    store = appdb.AppStore(path)
    preset = store.create_preset(
        "mine", {"w_accuracy": 0.4, "w_latency": 0.3, "w_vram": 0.2, "w_tokens": 0.1})
    store.create_session("ok", {"phase8": {"saw_table": [{"model": "x", "rank": 1}]}},
                         preset_id=preset["id"])
    store.create_session("bad", {"phase8": {}})
    # Simulate a legacy/migrated app file whose source_run_id points at runs held in
    # a SEPARATE study file (create_session would otherwise NULL an unresolved ref).
    # This is exactly the cross-file state check-integrity is meant to verify.
    store.conn.execute("PRAGMA foreign_keys = OFF")
    store.conn.execute("UPDATE session SET source_run_id = 'runX' WHERE name = 'ok'")
    store.conn.execute("UPDATE session SET source_run_id = 'ghost-run' WHERE name = 'bad'")
    store.conn.execute("PRAGMA foreign_keys = ON")
    store.conn.commit()
    store.hf_cache_set("m/A", {"fetched_at": time.time(), "status": "ok",
                               "data": {"params_b": 8.0, "downloads": 1, "likes": 1}})
    return store, preset


def test_integrity_reports_weak_dangling_but_stays_ok(tmp_path):
    sp, ap = tmp_path / "study.db", tmp_path / "app.db"
    _seed_study(sp).close()
    _seed_app(ap)[0].close()

    rep = check_integrity(sp, ap)
    assert rep["ok"] is True                 # weak danglings never fail the check
    src = rep["checks"]["source_run_id"]
    assert src["referencing_sessions"] == 2
    assert [d["source_run_id"] for d in src["unresolved"]] == ["ghost-run"]
    assert len(rep["warnings"]) == 1


def test_integrity_model_coverage(tmp_path):
    sp, ap = tmp_path / "study.db", tmp_path / "app.db"
    _seed_study(sp, models=("m/A", "m/B")).close()
    _seed_app(ap)[0].close()

    mc = check_integrity(sp, ap)["checks"]["model_coverage"]
    assert mc["study_models"] == ["m/A", "m/B"]
    assert mc["cached_with_metadata"] == ["m/A"]
    assert mc["uncached_study_models"] == ["m/B"]


def test_integrity_flags_dangling_preset_as_error(tmp_path):
    sp, ap = tmp_path / "study.db", tmp_path / "app.db"
    _seed_study(sp).close()
    store, preset = _seed_app(ap)
    store.close()
    # Simulate a MIGRATED old DB, where preset_id is a soft reference with no real
    # FK: write a dangling value through a raw connection with FK enforcement off
    # (the live AppStore correctly refuses this) — exactly what the checker guards.
    raw = sqlite3.connect(ap)
    raw.execute("PRAGMA foreign_keys = OFF")
    raw.execute("UPDATE session SET preset_id = 999 WHERE name = 'ok'")
    raw.commit(); raw.close()

    rep = check_integrity(sp, ap)
    assert rep["ok"] is False
    assert rep["checks"]["preset_id"]["unresolved"][0]["preset_id"] == 999
    assert any("preset_id 999" in e for e in rep["errors"])


def test_default_study_db_falls_back_to_locked_db(tmp_path, monkeypatch):
    from agentmeter.analysis import integrity

    class _Cfg:  # configured path points at a file that does not exist
        def resolve_path(self, key, default):
            return tmp_path / "not_here.db"

    # no locked DB present -> keep the configured (missing) path, so the report can
    # show exactly what it looked for
    monkeypatch.setattr(integrity.appdb, "LOCKED_STUDY_DB", tmp_path / "locked_absent.db")
    assert integrity._default_study_db(_Cfg()) == str(tmp_path / "not_here.db")

    # locked DB present -> prefer it over the generic default filename
    locked = tmp_path / "agentmeter_full_l4.db"
    _seed_study(locked).close()
    monkeypatch.setattr(integrity.appdb, "LOCKED_STUDY_DB", locked)
    assert integrity._default_study_db(_Cfg()) == str(locked)


def test_integrity_handles_missing_study_db(tmp_path):
    ap = tmp_path / "app.db"
    _seed_app(ap)[0].close()
    rep = check_integrity(tmp_path / "does_not_exist.db", ap)
    assert rep["study_db_present"] is False
    assert rep["app_db_present"] is True
    # with no study DB, referenced run ids cannot resolve -> reported as warnings, not errors
    assert rep["ok"] is True
    assert rep["checks"]["model_coverage"]["study_models"] == []
