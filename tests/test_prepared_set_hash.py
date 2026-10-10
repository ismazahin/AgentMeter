"""Phase 47 follow-up: a prepared set is reusable across Vast rentals.

The prepared-set hash (Compare / Leaderboard group by it) covers only what the models see and are
scored against — the model-visible feature columns and the labels — so the backend's full copy and
the control plane's copy with the identification columns stripped hash the SAME. That stripped copy
can be imported on a later rental, and a session run on it compares like-for-like with the first.
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil

import pytest

from agentmeter.config import PROJECT_ROOT
from agentmeter.server import control_plane
from agentmeter.server.jobs import JobManager
from agentmeter.server.sessions import compare_s, leaderboard_s
from agentmeter.session.analyses import PREPARED_SET_HASH_VERSION, prepared_set_content_hash, prepared_set_sha256

from test_control_plane import BACKEND_SECRET, MODEL, SECRET, FakeWorker, auth, prepare, tok

SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"


@pytest.fixture
def rentals(tmp_path, monkeypatch):
    """make(name) -> a fresh backend (its own disk), like a new Vast rental."""
    pytest.importorskip("flask")
    for k in ("AGENTMETER_ALLOWED_ORIGINS", "AGENTMETER_JOBS_PER_HOUR", "AGENTMETER_MAX_QUEUED_JOBS",
              "AGENTMETER_WORKER_URL", "AGENTMETER_BACKEND_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTMETER_RUN_TOKEN_SECRET", SECRET)
    spec = importlib.util.spec_from_file_location("serve47h", PROJECT_ROOT / "scripts" / "serve.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)

    def make(name):
        root = tmp_path / name
        mgr = JobManager(jobs_dir=root / "jobs", results_root=root / "results", gpu_available=lambda: False)
        a = srv.create_app(local_results_dir=str(root / "results"), job_manager=mgr, mode="mock")
        return a.test_client(), mgr, root
    return make


def _prepared(c, mgr):
    return mgr.wait(prepare(c).get_json()["job_id"], timeout=60)["prepared"]


def _bench(c, mgr, run):
    j = c.post("/api/jobs", json={"run": run, "models": [MODEL]}, headers=auth(tok("benchmark", run=run)))
    assert j.status_code == 202, j.get_json()
    jid = j.get_json()["job_id"]
    assert mgr.wait(jid, timeout=120)["status"] == "done"
    return jid


def _cp(fw, mgr):
    return control_plane.ControlPlane(control_plane.Client(fw.url, BACKEND_SECRET), get_manager=lambda: mgr, mode="mock",
                                      environment=lambda: {}, url_fn=lambda: "https://x.trycloudflare.com")


def test_full_and_stripped_copies_hash_the_same(rentals):
    c, mgr, _ = rentals("r1")
    run_dir = mgr.results_root / _prepared(c, mgr)
    meta = json.loads((run_dir / "input.json").read_text())
    full, labels = (run_dir / "features.csv").read_text(), (run_dir / "labels.csv").read_text()
    stripped = control_plane.strip_identification(full.encode()).decode()
    h = prepared_set_sha256(run_dir)
    assert h and h == prepared_set_content_hash(full, labels, meta["feature_columns"]) \
        == prepared_set_content_hash(stripped, labels, meta["feature_columns"])
    man = json.loads((run_dir / "manifest.json").read_text())["prepared_set"]
    assert man["content_sha256"] == h and man["content_hash_version"] == PREPARED_SET_HASH_VERSION
    # what the hash covers: a model-visible value or a label changes it; identification columns do not
    rows = full.splitlines()
    hdr = rows[0].split(",")
    i = hdr.index("Flow Duration")
    r1 = rows[1].split(",")
    r1[i] = str(int(float(r1[i])) + 1)
    assert prepared_set_content_hash("\n".join([rows[0], ",".join(r1)] + rows[2:]), labels, meta["feature_columns"]) != h
    r1 = rows[1].split(",")
    r1[hdr.index("src_ip")] = "203.0.113.77"
    assert prepared_set_content_hash("\n".join([rows[0], ",".join(r1)] + rows[2:]), labels, meta["feature_columns"]) == h
    lab = labels.splitlines()
    lab[1] = lab[1].rsplit(",", 1)[0] + ",BENIGN"
    if lab[1] != labels.splitlines()[1]:
        assert prepared_set_content_hash(full, "\n".join(lab), meta["feature_columns"]) != h


def test_session_reused_on_a_new_rental_compares_like_for_like(rentals):
    fw = FakeWorker()
    # rental 1: prepare + benchmark, uploaded to the control plane
    c1, m1, root1 = rentals("rental1")
    set_a = _prepared(c1, m1)
    job_a = _bench(c1, m1, set_a)
    assert _cp(fw, m1).upload(job_a)
    base = f"/api/backend/sessions/{job_a}"
    sum_a = json.loads(fw.store[f"{base}/summary"])["summary"]
    stored = {n: fw.store[f"{base}/files/{n}"] for n in ("manifest.json", "features.csv", "labels.csv")}
    assert "src_ip" not in stored["features.csv"].decode().splitlines()[0]          # the R2 copy is stripped
    assert json.loads(stored["manifest.json"])["prepared_set"]["content_sha256"] == sum_a["identity"]["prepared_set_sha256"]

    # the rental is destroyed: its disk is gone
    shutil.rmtree(root1)

    # rental 2: a fresh backend; the wizard re-imports the stored (stripped) copy
    c2, m2, _ = rentals("rental2")
    imp = c2.post("/api/prepared/import", content_type="multipart/form-data", headers=auth(tok("import")), data={
        "manifest": (io.BytesIO(stored["manifest.json"]), "manifest.json"),
        "features": (io.BytesIO(stored["features.csv"]), "features.csv"),
        "labels": (io.BytesIO(stored["labels.csv"]), "labels.csv")})
    assert imp.status_code == 201, imp.get_json()
    set_b = imp.get_json()["prepared_set"]
    restored = (m2.results_root / set_b / "selected_flows.csv").read_text().splitlines()
    assert restored[0].split(",")[:7] == ["flow_id", "src_ip", "src_port", "dst_ip", "protocol", "protocol_name", "timestamp"]
    assert restored[1].split(",")[1:7] == [""] * 6                                     # restored as empty, never invented
    job_b = _bench(c2, m2, set_b)
    assert _cp(fw, m2).upload(job_b)
    sum_b = json.loads(fw.store[f"/api/backend/sessions/{job_b}/summary"])["summary"]

    assert sum_a["identity"]["prepared_set_sha256"] == sum_b["identity"]["prepared_set_sha256"]
    cmp = compare_s(sum_a, sum_b)                       # the Worker runs a parity-tested port of this
    assert cmp["validity"]["like_for_like"] is True, cmp["validity"]
    lb = leaderboard_s([sum_b, sum_a])
    assert len(lb["groups"]) == 1 and lb["groups"][0]["n_sessions"] == 2


def test_a_stripped_copy_must_match_its_content_hash(rentals):
    c, mgr, _ = rentals("r1")
    run_dir = mgr.results_root / _prepared(c, mgr)
    man = json.loads((run_dir / "manifest.json").read_text())
    feats = control_plane.strip_identification((run_dir / "features.csv").read_bytes())
    labels = (run_dir / "labels.csv").read_bytes()

    def imp(m, f):
        return c.post("/api/prepared/import", content_type="multipart/form-data", headers=auth(tok("import")), data={
            "manifest": (io.BytesIO(json.dumps(m).encode()), "manifest.json"), "features": (io.BytesIO(f), "features.csv"),
            "labels": (io.BytesIO(labels), "labels.csv")})
    lines = feats.decode().splitlines()
    i = lines[0].split(",").index("Flow Duration")
    row = lines[1].split(",")
    row[i] = "999999999"
    edited = "\n".join([lines[0], ",".join(row)] + lines[2:]).encode()
    r = imp(man, edited)
    assert r.status_code == 400 and "content hash differs" in r.get_json()["error"]
    old = json.loads(json.dumps(man))
    del old["prepared_set"]["content_sha256"]
    r = imp(old, feats)
    assert r.status_code == 400 and "no content hash" in r.get_json()["error"]
    assert imp(man, feats).status_code == 201


def test_sessions_from_before_hash_v2_are_recomputed_from_their_flows(rentals):
    from agentmeter.server.sessions import cp_summary
    c, mgr, _ = rentals("r1")
    run = _prepared(c, mgr)
    jid = _bench(c, mgr, run)
    job, res = mgr._load(jid), mgr.result(jid)
    v2 = res["session_identity"]["prepared_set_sha256"]
    old = json.loads(json.dumps(res))
    old["session_identity"]["prepared_set_sha256"] = "0" * 64                          # a v1 hash, no version marker
    del old["session_identity"]["prepared_set_hash_version"]
    assert cp_summary(job, old)["identity"]["prepared_set_sha256"] == v2
    gone = dict(job, run_dir=str(mgr.results_root / "nowhere"))
    ident = cp_summary(gone, old)["identity"]
    assert ident["prepared_set_sha256"] == "0" * 64 and ident["prepared_set_hash_version"] == 1   # kept, marked as old


def test_migration_recomputes_stored_hashes_from_the_stored_copy(rentals, capsys):
    """Sessions stored before hash v2 are migrated from their R2 copy (the backend may be gone)."""
    fw = FakeWorker()
    c1, m1, root1 = rentals("rental1")
    set_a = _prepared(c1, m1)
    job_a = _bench(c1, m1, set_a)
    assert _cp(fw, m1).upload(job_a)
    key = f"/api/backend/sessions/{job_a}/summary"
    v2 = json.loads(fw.store[key])["summary"]["identity"]["prepared_set_sha256"]
    # make it look like a session stored by the first Phase 47 release (hash v1, manifest without content hash)
    doc = json.loads(fw.store[key])
    doc["summary"]["identity"]["prepared_set_sha256"] = "9" * 64
    doc["summary"]["identity"].pop("prepared_set_hash_version")
    doc["summary"]["prepared_set_sha256"] = "9" * 64
    fw.store[key] = json.dumps(doc).encode()
    mk = f"/api/backend/sessions/{job_a}/files/manifest.json"
    man = json.loads(fw.store[mk])
    man["prepared_set"].pop("content_sha256")
    man["prepared_set"].pop("content_hash_version")
    fw.store[mk] = json.dumps(man).encode()
    shutil.rmtree(root1)                                         # the backend is gone: only the stored copy is left

    spec = importlib.util.spec_from_file_location("mig", PROJECT_ROOT / "scripts" / "migrate_prepared_set_hash.py")
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    client = control_plane.Client(fw.url, BACKEND_SECRET)
    assert mig.migrate(client, dry_run=True) == {"migrated": 1, "already": 0, "skipped": 0}
    assert json.loads(fw.store[key])["summary"]["identity"]["prepared_set_sha256"] == "9" * 64     # dry run: unchanged
    assert mig.migrate(client) == {"migrated": 1, "already": 0, "skipped": 0}
    s = json.loads(fw.store[key])["summary"]
    assert s["identity"]["prepared_set_sha256"] == v2 == s["prepared_set_sha256"]
    assert s["identity"]["prepared_set_hash_version"] == PREPARED_SET_HASH_VERSION
    assert json.loads(fw.store[mk])["prepared_set"]["content_sha256"] == v2      # the copy is importable again
    assert mig.migrate(client) == {"migrated": 0, "already": 1, "skipped": 0}    # idempotent
    # and a new rental can import the migrated copy and compare like-for-like
    c2, m2, _ = rentals("rental2")
    stored = {n: fw.store[f"/api/backend/sessions/{job_a}/files/{n}"] for n in ("manifest.json", "features.csv", "labels.csv")}
    imp = c2.post("/api/prepared/import", content_type="multipart/form-data", headers=auth(tok("import")), data={
        "manifest": (io.BytesIO(stored["manifest.json"]), "manifest.json"),
        "features": (io.BytesIO(stored["features.csv"]), "features.csv"),
        "labels": (io.BytesIO(stored["labels.csv"]), "labels.csv")})
    assert imp.status_code == 201, imp.get_json()
    job_b = _bench(c2, m2, imp.get_json()["prepared_set"])
    assert _cp(fw, m2).upload(job_b)
    sb = json.loads(fw.store[f"/api/backend/sessions/{job_b}/summary"])["summary"]
    assert compare_s(s, sb)["validity"]["like_for_like"] is True


def test_docs_state_the_hash_and_the_replay_window():
    d = (PROJECT_ROOT / "docs" / "DEPLOY.md").read_text()
    assert "Known limitation — replay window after a backend restart" in d and "5 minutes" in d
    assert "migrate_prepared_set_hash.py" in d and "model-visible" in d
