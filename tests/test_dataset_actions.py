"""Sessions can be named; data sets can be renamed and deleted (Sessions and Data sets pages).

GPU backend side: the session name in PATCH /api/sessions/<id> (one-user mode), the data-set
names/deletions store (one-user mode) and POST /api/prepared/<kind>/<name>/delete, which in
control-plane mode needs a delete_set run token for that set from its owner or an admin.
The control plane's own routes are covered by controlplane/test/datasets.test.ts.
"""
from __future__ import annotations

import pytest
from test_control_plane import MODEL, app, auth, prepare, tok  # noqa: F401  (app: the control-plane-mode fixture)

from agentmeter.server.jobs import JobError
from agentmeter.server.sessions import TITLE_MAX, clean_annotations


# ---------------------------------------------------------------------------
# session names
# ---------------------------------------------------------------------------
def test_a_session_name_is_one_line_and_short():
    assert clean_annotations({"title": "  Qwen vs Llama \n  run 2 "}) == {"title": "Qwen vs Llama run 2"}
    assert clean_annotations({"title": ""}) == {"title": ""}                     # "" removes the name
    assert clean_annotations({"notes": "n"}) == {"notes": "n"}                    # untouched when absent
    with pytest.raises(JobError):
        clean_annotations({"title": "x" * (TITLE_MAX + 1)})
    with pytest.raises(JobError):
        clean_annotations({"title": 3})


def test_the_name_rides_on_the_session_and_is_searchable(tmp_path):
    from agentmeter.server.sessions import matches, session_summary
    job = {"job_id": "job_20260101_000000_" + "a" * 32, "status": "queued", "models": [MODEL], "title": "Baseline A"}
    row = session_summary(job, None)
    assert row["title"] == "Baseline A"
    assert matches(row, "baseline", "") and not matches(row, "nothing-like-it", "")
    assert session_summary({**job, "title": None}, None)["title"] == ""


# ---------------------------------------------------------------------------
# deleting a prepared set's files (control-plane mode)
# ---------------------------------------------------------------------------
def _prepared(app, sub="u1", uname="alice"):  # noqa: F811
    c, mgr = app["c"], app["mgr"]
    jid = prepare(c, tok("prepare", sub=sub, uname=uname)).get_json()["job_id"]
    return mgr.wait(jid, timeout=60)["prepared"]


def test_only_the_owner_or_an_admin_deletes_a_set(app):  # noqa: F811
    c, mgr = app["c"], app["mgr"]
    sid = _prepared(app, sub="u1")
    url = f"/api/prepared/{sid}/delete"
    assert c.post(url).status_code == 401                                                   # no token
    r = c.post(url, headers=auth(tok("files", set=sid)))
    assert r.status_code == 403 and r.get_json()["code"] == "run_token_wrong_kind"
    r = c.post(url, headers=auth(tok("delete_set", set="csv_runs/another")))
    assert r.status_code == 403 and r.get_json()["code"] == "run_token_mismatch"
    r = c.post(url, headers=auth(tok("delete_set", sub="u2", uname="bob", set=sid)))
    assert r.status_code == 403 and r.get_json()["code"] == "forbidden"                     # not the owner
    assert (mgr.results_root / sid).is_dir()
    r = c.post(url, headers=auth(tok("delete_set", sub="u1", set=sid)))
    assert r.status_code == 200 and r.get_json() == {"deleted": sid}
    assert not (mgr.results_root / sid).exists()
    r = c.post(url, headers=auth(tok("delete_set", sub="u1", set=sid)))
    assert r.status_code == 404 and r.get_json()["code"] == "not_found"                     # already gone


def test_an_admin_deletes_anyones_set(app):  # noqa: F811
    c, mgr = app["c"], app["mgr"]
    sid = _prepared(app, sub="u1")
    r = c.post(f"/api/prepared/{sid}/delete", headers=auth(tok("delete_set", sub="u9", set=sid, admin=True)))
    assert r.status_code == 200 and not (mgr.results_root / sid).exists()


def test_the_owner_the_control_plane_named_deletes_it(app):  # noqa: F811
    # the control plane decides who owns a benchmarked set (its first finished session) and says so
    # in the token: that person may delete it even if someone else prepared it
    c, mgr = app["c"], app["mgr"]
    sid = _prepared(app, sub="u1")
    r = c.post(f"/api/prepared/{sid}/delete", headers=auth(tok("delete_set", sub="u2", set=sid, owner=True)))
    assert r.status_code == 200 and not (mgr.results_root / sid).exists()


def test_a_set_from_another_rental_is_not_found_for_anyone(app):  # noqa: F811
    # not on this server: 404 before any owner check (no prepare record here to name an owner)
    r = app["c"].post("/api/prepared/csv_runs/elsewhere_1/delete", headers=auth(tok("delete_set", sub="u2", set="csv_runs/elsewhere_1")))
    assert r.status_code == 404 and r.get_json()["code"] == "not_found"


def test_a_set_in_use_is_not_deleted(app, monkeypatch):  # noqa: F811
    c, mgr = app["c"], app["mgr"]
    sid = _prepared(app, sub="u1")
    real = mgr._all
    queued = {"job_id": "job_20260101_000000_" + "b" * 32, "kind": "benchmark", "status": "queued", "run_name": sid}
    monkeypatch.setattr(mgr, "_all", lambda: [*real(), queued])     # a benchmark waiting on the set (no worker race)
    r = c.post(f"/api/prepared/{sid}/delete", headers=auth(tok("delete_set", sub="u1", set=sid)))
    assert r.status_code == 409 and r.get_json()["code"] == "set_in_use"
    assert (mgr.results_root / sid).is_dir()


def test_writes_to_the_names_store_are_refused_with_a_control_plane(app):  # noqa: F811
    c = app["c"]
    r = c.post("/api/datasets/rename", json={"key": "name:csv_runs/x", "label": "y"}, headers=auth(tok("read")))
    assert r.status_code == 403 and r.get_json()["code"] == "control_plane_only"


# ---------------------------------------------------------------------------
# one-user mode: names and deletions kept by this server
# ---------------------------------------------------------------------------
@pytest.fixture
def solo(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    import importlib.util

    from agentmeter.config import PROJECT_ROOT
    from agentmeter.server.jobs import JobManager
    monkeypatch.delenv("AGENTMETER_RUN_TOKEN_SECRET", raising=False)
    spec = importlib.util.spec_from_file_location("serve_solo", PROJECT_ROOT / "scripts" / "serve.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results", gpu_available=lambda: False)
    return srv.create_app(local_results_dir=str(tmp_path / "results"), job_manager=mgr, mode="mock").test_client(), mgr


def test_names_and_deletions_in_one_user_mode(solo):
    c, _ = solo
    assert c.get("/api/datasets").get_json() == {"datasets": []}
    key = "a" * 64
    r = c.post("/api/datasets/rename", json={"key": key, "set": "csv_runs/s1", "label": "  Monday   traffic "})
    assert r.status_code == 200 and r.get_json()["label"] == "Monday traffic"
    assert c.post("/api/datasets/rename", json={"key": "../etc", "label": "x"}).status_code == 400
    assert c.post("/api/datasets/rename", json={"key": key, "label": "x" * 81}).status_code == 400
    assert c.post("/api/datasets/delete", json={"key": key, "set": "csv_runs/s1"}).status_code == 200
    row = c.get("/api/datasets").get_json()["datasets"][0]
    assert row["key"] == key and row["set"] == "csv_runs/s1" and row["label"] == "Monday traffic" and row["deleted_at"]


def test_delete_needs_no_token_in_one_user_mode(solo):
    c, mgr = solo
    from svc import prepare as prep
    import io

    from test_control_plane import SAMPLE_CSV
    r = prep(c, {"file": (io.BytesIO(SAMPLE_CSV.read_bytes()), SAMPLE_CSV.name), "max_flows": "5"})
    assert r.status_code == 201, r.get_json()
    sid = next(j["prepared"] for j in mgr._all() if j.get("kind") == "prepare")
    assert c.post(f"/api/prepared/{sid}/delete").status_code == 200
    assert not (mgr.results_root / sid).exists()
    assert c.post("/api/prepared/csv_runs/..%2F..%2Fetc/delete").status_code in (400, 404, 405)   # never routed
    assert c.post("/api/prepared/csv_runs/.hidden/delete").status_code == 400
    assert c.post("/api/prepared/other_runs/x/delete").status_code == 400
