"""Phase 12 — CRUD verify suite (CPU only, no GPU).

Round-trips for sessions / weight presets / notes against a temp metadata DB;
builtin presets are read-only; a session PATCH cannot alter the stored metrics;
weight validation rejects negatives/non-numbers; and the CRUD layer NEVER opens
the locked study DB for writing.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentmeter import appdb

REPO_ROOT = Path(__file__).resolve().parents[1]

ANALYSIS = {
    "run_ids": ["run_locked"],
    "phase8": {
        "weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15},
        "tiers": {"healthy_min": 80, "degraded_min": 60},
        "normalised": [
            {"model": "A", "accuracy": 0.33, "latency": 0.39, "vram": 1.0, "tokens": 0.39},
            {"model": "B", "accuracy": 0.42, "latency": 0.24, "vram": 1.0, "tokens": 0.24},
        ],
        "saw_table": [
            {"model": "A", "composite": 0.489, "rank": 1, "tier": "Critical"},
            {"model": "B", "composite": 0.462, "rank": 2, "tier": "Critical"},
        ],
    },
    "statistics": {"scenario_total_time_s": {"H": 42.0, "p_value": 1e-8}},
}


@pytest.fixture
def store(tmp_path):
    s = appdb.AppStore(tmp_path / "app.db")
    yield s
    s.close()


# --- sessions ----------------------------------------------------------

def test_session_crud_roundtrip(store):
    s = store.create_session("Run 1", ANALYSIS, source_filename="analysis.json")
    sid = s["id"]
    assert s["name"] == "Run 1"
    assert s["summary"]["model_count"] == 2
    assert s["summary"]["top_model"] == "A"          # rank 1, read (not recomputed)
    assert s["summary"]["tiers"] == {"A": "Critical", "B": "Critical"}
    assert s["analysis"]["phase8"]["saw_table"][0]["model"] == "A"

    # read + list
    assert store.get_session(sid)["name"] == "Run 1"
    assert [x["id"] for x in store.list_sessions()] == [sid]
    # list is metadata only (no heavy analysis blob)
    assert "analysis" not in store.list_sessions()[0]

    # update name only
    up = store.update_session_name(sid, "Renamed")
    assert up["name"] == "Renamed" and up["updated_at"] >= s["created_at"]

    # delete
    store.delete_session(sid)
    with pytest.raises(appdb.NotFound):
        store.get_session(sid)
    assert store.list_sessions() == []


def test_session_patch_cannot_alter_metrics(store):
    s = store.create_session("S", ANALYSIS)
    before = store.get_session(s["id"])
    store.update_session_name(s["id"], "New name")
    after = store.get_session(s["id"])
    # metrics/summary/analysis are byte-for-byte unchanged; only name/updated_at move
    assert after["summary"] == before["summary"]
    assert after["analysis"] == before["analysis"]
    assert after["name"] == "New name"


def test_session_name_required(store):
    with pytest.raises(ValueError):
        store.create_session("   ", ANALYSIS)
    with pytest.raises(ValueError):
        store.create_session("ok", {"not": "an analysis"})   # missing phase8


def test_delete_session_cascades_notes(store):
    s = store.create_session("S", ANALYSIS)
    n = store.create_note(s["id"], "a note")
    store.delete_session(s["id"])
    with pytest.raises(appdb.NotFound):
        store.get_note(n["id"])


# --- weight presets ----------------------------------------------------

def test_builtins_seeded_and_readonly(store):
    presets = store.list_presets()
    builtins = {p["name"] for p in presets if p["is_builtin"]}
    assert builtins == {"default", "equal", "accuracy_heavy", "efficiency_heavy"}
    default = next(p for p in presets if p["name"] == "default")
    assert (default["w_accuracy"], default["w_latency"], default["w_vram"],
            default["w_tokens"]) == (0.40, 0.25, 0.20, 0.15)
    # builtin cannot be edited or deleted
    with pytest.raises(appdb.Forbidden):
        store.update_preset(default["id"], name="hacked")
    with pytest.raises(appdb.Forbidden):
        store.delete_preset(default["id"])


def test_preset_crud_roundtrip(store):
    p = store.create_preset("mine", {"w_accuracy": 0.5, "w_latency": 0.2,
                                     "w_vram": 0.2, "w_tokens": 0.1})
    pid = p["id"]
    assert p["is_builtin"] is False
    assert store.get_preset(pid)["w_accuracy"] == 0.5

    up = store.update_preset(pid, name="mine2",
                             weights={"w_accuracy": 1, "w_latency": 0,
                                      "w_vram": 0, "w_tokens": 0})
    assert up["name"] == "mine2" and up["w_accuracy"] == 1.0 and up["w_latency"] == 0.0

    store.delete_preset(pid)
    with pytest.raises(appdb.NotFound):
        store.get_preset(pid)


def test_preset_weight_validation(store):
    bad_sets = [
        {"w_accuracy": -0.1, "w_latency": 0.2, "w_vram": 0.2, "w_tokens": 0.1},  # negative
        {"w_accuracy": "x", "w_latency": 0.2, "w_vram": 0.2, "w_tokens": 0.1},   # non-number
        {"w_accuracy": True, "w_latency": 0.2, "w_vram": 0.2, "w_tokens": 0.1},  # bool
        {"w_accuracy": float("nan"), "w_latency": 0.2, "w_vram": 0.2, "w_tokens": 0.1},  # NaN
        {"w_accuracy": 0.2, "w_latency": 0.2, "w_vram": 0.2},                    # missing key
    ]
    for w in bad_sets:
        with pytest.raises(ValueError):
            store.create_preset("bad", w)
    with pytest.raises(ValueError):
        store.create_preset("  ", {"w_accuracy": 0.25, "w_latency": 0.25,
                                   "w_vram": 0.25, "w_tokens": 0.25})   # blank name


# --- notes -------------------------------------------------------------

def test_note_crud_roundtrip(store):
    s = store.create_session("S", ANALYSIS)
    n = store.create_note(s["id"], "first note")
    nid = n["id"]
    assert n["body"] == "first note" and n["session_id"] == s["id"]
    assert [x["id"] for x in store.list_notes(s["id"])] == [nid]

    up = store.update_note(nid, "edited")
    assert up["body"] == "edited"

    store.delete_note(nid)
    assert store.list_notes(s["id"]) == []
    with pytest.raises(appdb.NotFound):
        store.get_note(nid)


def test_note_requires_existing_session_and_body(store):
    with pytest.raises(appdb.NotFound):
        store.create_note(9999, "orphan")
    s = store.create_session("S", ANALYSIS)
    with pytest.raises(ValueError):
        store.create_note(s["id"], "   ")


# --- the locked study DB is never touched ------------------------------

def test_appstore_refuses_locked_db():
    with pytest.raises(ValueError, match="locked study DB"):
        appdb.AppStore(appdb.LOCKED_STUDY_DB)


def test_crud_never_opens_locked_db(tmp_path, monkeypatch):
    """A full CRUD flow must never open the locked study DB (any mode)."""
    opened = []
    real_connect = sqlite3.connect

    def spy_connect(target, *a, **k):
        opened.append(str(target))
        if "agentmeter_full_l4.db" in str(target):
            raise AssertionError(f"CRUD opened the locked study DB: {target}")
        return real_connect(target, *a, **k)

    monkeypatch.setattr(appdb.sqlite3, "connect", spy_connect)

    s = appdb.AppStore(tmp_path / "app.db")
    try:
        sess = s.create_session("S", ANALYSIS)
        s.update_session_name(sess["id"], "R")
        p = s.create_preset("p", {"w_accuracy": 1, "w_latency": 0, "w_vram": 0, "w_tokens": 0})
        s.update_preset(p["id"], weights={"w_accuracy": 0.5, "w_latency": 0.5,
                                          "w_vram": 0, "w_tokens": 0})
        n = s.create_note(sess["id"], "note")
        s.update_note(n["id"], "note2")
        s.delete_note(n["id"])
        s.delete_preset(p["id"])
        s.delete_session(sess["id"])
    finally:
        s.close()

    assert opened  # connections did happen (to the temp DB)
    assert all("agentmeter_full_l4.db" not in p for p in opened)  # never the locked DB


# --- HTTP layer (same-origin CRUD routes) ------------------------------

def _crud_client(tmp_path):
    pytest.importorskip("flask")
    from flask import Flask
    from agentmeter import crud_api
    app = Flask(__name__)
    store = appdb.AppStore(tmp_path / "app.db")
    crud_api.register_crud(app, lambda: store)
    return app.test_client(), store


def test_http_session_and_note_crud(tmp_path):
    client, _ = _crud_client(tmp_path)

    # create session (201) with an imported analysis
    r = client.post("/sessions", json={"name": "HTTP run", "analysis": ANALYSIS,
                                       "source_filename": "a.json"})
    assert r.status_code == 201
    sid = r.get_json()["id"]

    assert len(client.get("/sessions").get_json()["sessions"]) == 1
    assert client.get(f"/sessions/{sid}").get_json()["analysis"]["phase8"]["saw_table"]

    # rename via PATCH
    assert client.patch(f"/sessions/{sid}", json={"name": "R2"}).get_json()["name"] == "R2"

    # notes
    n = client.post("/notes", json={"session_id": sid, "body": "hi"})
    assert n.status_code == 201
    nid = n.get_json()["id"]
    assert len(client.get(f"/sessions/{sid}/notes").get_json()["notes"]) == 1
    assert client.patch(f"/notes/{nid}", json={"body": "bye"}).get_json()["body"] == "bye"
    assert client.delete(f"/notes/{nid}").status_code == 200

    assert client.delete(f"/sessions/{sid}").status_code == 200
    assert client.get(f"/sessions/{sid}").status_code == 404


def test_http_preset_rules(tmp_path):
    client, _ = _crud_client(tmp_path)
    presets = client.get("/presets").get_json()["presets"]
    builtin = next(p for p in presets if p["is_builtin"])
    # builtin PATCH/DELETE rejected with 403
    assert client.patch(f"/presets/{builtin['id']}", json={"name": "x"}).status_code == 403
    assert client.delete(f"/presets/{builtin['id']}").status_code == 403

    # create + negative weight rejected with 400
    ok = client.post("/presets", json={"name": "p", "w_accuracy": 0.5, "w_latency": 0.5,
                                       "w_vram": 0, "w_tokens": 0})
    assert ok.status_code == 201
    bad = client.post("/presets", json={"name": "p2", "w_accuracy": -1, "w_latency": 0,
                                        "w_vram": 0, "w_tokens": 0})
    assert bad.status_code == 400
