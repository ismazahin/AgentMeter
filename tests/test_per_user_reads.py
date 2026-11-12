"""Each user sees only their own work on the GPU server; an admin sees everyone's
(design: docs/superpowers/specs/2026-11-12-per-user-separation-design.md). Control-plane mode:
a read token names its user (sub) and whether they are an admin."""
from __future__ import annotations

from test_control_plane import MODEL, app, auth, prepare, tok  # noqa: F401  (app: the control-plane-mode fixture)


def _alice_benchmark(app):  # noqa: F811
    c, mgr = app["c"], app["mgr"]
    sid = mgr.wait(prepare(c, tok("prepare", sub="u1")).get_json()["job_id"], timeout=60)["prepared"]
    r = c.post("/api/jobs", json={"run": sid, "models": [MODEL]}, headers=auth(tok("benchmark", sub="u1", run=sid)))
    assert r.status_code == 202, r.get_json()
    jid = r.get_json()["job_id"]
    mgr.wait(jid, timeout=120)
    return sid, jid


def test_another_user_cannot_read_a_session_or_its_set(app):  # noqa: F811
    c = app["c"]
    sid, jid = _alice_benchmark(app)
    alice, bob, admin = tok("read", sub="u1"), tok("read", sub="u2", uname="bob"), tok("read", sub="u9", admin=True)
    for path in (f"/api/jobs/{jid}/result", f"/api/jobs/{jid}/flows", f"/api/jobs/{jid}/explanation",
                 f"/api/jobs/{jid}/telemetry", f"/api/jobs/{jid}/constraints", f"/api/sessions/{jid}",
                 f"/api/prepared/{sid}"):
        assert c.get(path, headers=auth(bob)).status_code == 404, path
        assert c.get(path, headers=auth(admin)).status_code != 404 or path.endswith("/explanation"), path
        assert c.get(path, headers=auth(alice)).status_code != 404 or path.endswith("/explanation"), path
    r = c.get(f"/api/prepared/{sid}/manifest.json", headers=auth(tok("files", sub="u2", set=sid)))
    assert r.status_code == 404
    assert c.get(f"/api/prepared/{sid}/manifest.json", headers=auth(tok("files", sub="u1", set=sid))).status_code == 200


def test_lists_and_rankings_hold_only_the_callers_own_work(app):  # noqa: F811
    c = app["c"]
    _, jid = _alice_benchmark(app)
    ids = lambda t: [j["job_id"] for j in c.get("/api/jobs?limit=50", headers=auth(t)).get_json()["jobs"]]
    assert jid in ids(tok("read", sub="u1")) and jid in ids(tok("read", sub="u9", admin=True))
    assert ids(tok("read", sub="u2")) == []
    sessions = lambda t: [s["session_id"] for s in c.get("/api/sessions", headers=auth(t)).get_json()["sessions"]]
    assert sessions(tok("read", sub="u2")) == [] and sessions(tok("read", sub="u1")) == [jid]
    lb = c.get("/api/leaderboard", headers=auth(tok("read", sub="u2"))).get_json()
    assert lb["groups"] == []


def test_gpu_and_queue_shows_another_users_run_without_its_details(app):  # noqa: F811
    c, mgr = app["c"], app["mgr"]
    _, jid = _alice_benchmark(app)
    mgr._update(jid, status="running")                        # as if it were still on the GPU
    bob = c.get("/api/gpu", headers=auth(tok("read", sub="u2"))).get_json()
    assert bob["running"] == [{**bob["running"][0], "job_id": None, "other": True, "models": None, "owner": None, "run_name": None}]
    alice = c.get("/api/gpu", headers=auth(tok("read", sub="u1"))).get_json()
    assert alice["running"][0]["job_id"] == jid and alice["running"][0]["owner"] == "alice"
