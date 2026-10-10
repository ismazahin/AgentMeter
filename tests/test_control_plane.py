"""Phase 47: the GPU backend's side of the control plane.

* run tokens (runtoken.py): signature, expiry, audience, kinds, single use, per-request match;
* job ids carry 128 random bits; only those serve status without a token;
* every job records who started it; a free follow-up benchmark must follow the same user's
  own prepare / import (the control plane counted the wizard session once);
* registration / heartbeat / job events / session upload to a (fake) Worker, HMAC-signed;
  identification columns are stripped from the uploaded features.csv by default;
* the Worker's bundled rule file and parity fixtures are in sync with the Python reference.
"""
from __future__ import annotations

import hmac
import importlib.util
import io
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from agentmeter.config import PROJECT_ROOT
from agentmeter.server import control_plane, jobs, runtoken
from agentmeter.server.jobs import JobManager

SECRET = "test-run-token-secret-0123456789"
BACKEND_SECRET = "test-backend-secret-0123456789"
SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
MODEL = "microsoft/Phi-3-mini-4k-instruct"


def tok(kind, sub="u1", uname="alice", ttl=300, **extra):
    now = int(time.time())
    return runtoken.sign({"typ": "run", "aud": "agentmeter-backend", "jti": secrets.token_hex(16), "sub": sub,
                          "uname": uname, "kind": kind, "iat": now, "exp": now + ttl, **extra}, SECRET)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    for k in ("AGENTMETER_ALLOWED_ORIGINS", "AGENTMETER_JOBS_PER_HOUR", "AGENTMETER_MAX_QUEUED_JOBS",
              "AGENTMETER_WORKER_URL", "AGENTMETER_BACKEND_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTMETER_RUN_TOKEN_SECRET", SECRET)
    spec = importlib.util.spec_from_file_location("serve47", PROJECT_ROOT / "scripts" / "serve.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results", gpu_available=lambda: False)
    a = srv.create_app(local_results_dir=str(tmp_path / "results"), job_manager=mgr, mode="mock")
    return {"c": a.test_client(), "mgr": mgr, "app": a, "srv": srv, "tmp": tmp_path}


def prepare(c, token=None):
    return c.post("/api/prepare", data={"file": (io.BytesIO(SAMPLE_CSV.read_bytes()), SAMPLE_CSV.name), "max_flows": "5"},
                  content_type="multipart/form-data", headers=auth(token or tok("prepare")))


# ---------------------------------------------------------------------------
# run tokens
# ---------------------------------------------------------------------------
def test_token_verification():
    good = tok("read")
    assert runtoken.verify(good, SECRET)["kind"] == "read"
    with pytest.raises(runtoken.RunTokenError, match="signature"):
        runtoken.verify(good, "another-secret-0123456789")
    with pytest.raises(runtoken.RunTokenError) as e:
        runtoken.verify(tok("read", ttl=-1), SECRET)
    assert e.value.code == "run_token_expired"
    with pytest.raises(runtoken.RunTokenError, match="lifetime"):
        runtoken.verify(tok("read", ttl=3600), SECRET)                      # longer than the Worker issues
    now = int(time.time())
    for bad in ({"typ": "access"}, {"aud": "someone-else"}, {"kind": "rm"}, {"jti": "short"}, {"sub": ""}):
        p = {"typ": "run", "aud": "agentmeter-backend", "jti": secrets.token_hex(16), "sub": "u", "kind": "read",
             "iat": now, "exp": now + 60, **bad}
        with pytest.raises(runtoken.RunTokenError):
            runtoken.verify(runtoken.sign(p, SECRET), SECRET)
    for junk in ("", "v1.x", "v2.a.b", "v1.!!!.???", good.replace("v1.", "v1.e30", 1)):
        with pytest.raises(runtoken.RunTokenError):
            runtoken.verify(junk, SECRET)


def test_the_worker_and_the_backend_agree_on_the_token_format():
    """The TS signer (worker/src/crypto.ts) produces exactly runtoken.sign's bytes for the same JSON."""
    src = (PROJECT_ROOT / "worker" / "src" / "crypto.ts").read_text()
    assert 'const body = "v1." + b64url(enc.encode(JSON.stringify(payload)));' in src
    assert 'return body + "." + b64url(await hmac(secret, body));' in src
    assert "RUN_TTL_S = 300" in (PROJECT_ROOT / "worker" / "src" / "runs.ts").read_text()


def test_single_use_kind_and_target_checks(app):
    c, mgr = app["c"], app["mgr"]
    t = tok("prepare")
    assert prepare(c, t).status_code == 202
    r = prepare(c, t)
    assert r.status_code == 409 and r.get_json()["code"] == "run_token_used"            # replay refused
    r = prepare(c, tok("benchmark", run="csv_runs/x"))
    assert r.status_code == 403 and r.get_json()["code"] == "run_token_wrong_kind"
    assert c.get("/api/sessions", headers=auth(tok("prepare"))).status_code == 403      # a write token is not a read token
    jid = prepare(c).get_json()["job_id"]
    sid = mgr.wait(jid, timeout=60)["prepared"]
    # a refused request does not burn its token: fix the request, reuse the token
    bt = tok("benchmark", run=sid)
    assert c.post("/api/jobs", json={"run": sid, "models": [MODEL, MODEL, MODEL]}, headers=auth(bt)).status_code == 400
    assert c.post("/api/jobs", json={"run": sid, "models": [MODEL]}, headers=auth(bt)).status_code == 202
    # the body must be what the token was issued for
    r = c.post("/api/jobs", json={"run": sid, "models": [MODEL]}, headers=auth(tok("benchmark", run="csv_runs/other")))
    assert r.status_code == 403 and r.get_json()["code"] == "run_token_mismatch"
    r = c.post(f"/api/jobs/{jid}/resume", headers=auth(tok("resume", job_id="job_20260101_000000_" + "0" * 32)))
    assert r.status_code == 403
    r = c.get(f"/api/prepared/{sid}/features.csv", headers=auth(tok("files", set="csv_runs/another")))
    assert r.status_code == 403


def test_jobs_record_who_started_them(app):
    c, mgr = app["c"], app["mgr"]
    t = tok("prepare", sub="u42", uname="bob")
    jid = prepare(c, t).get_json()["job_id"]
    job = mgr._load(jid)
    assert job["user"] == {"sub": "u42", "uname": "bob"}
    assert job["grant_jti"] == runtoken.verify(t, SECRET)["jti"]
    assert set(job["user"]) == {"sub", "uname"}                                         # nothing else about the user


def test_a_free_follow_up_benchmark_must_follow_the_users_own_preparation(app):
    c, mgr = app["c"], app["mgr"]
    pt = tok("prepare", sub="u1")
    pjti = runtoken.verify(pt, SECRET)["jti"]
    prep = prepare(c, pt).get_json()["job_id"]
    # someone else's prepare_jti, or another user's token naming my prepare: refused
    r = c.post("/api/jobs", json={"after_prepare": prep, "models": [MODEL]},
               headers=auth(tok("benchmark", sub="u1", after_prepare=prep, prepare_jti=secrets.token_hex(16))))
    assert r.status_code == 403 and r.get_json()["code"] == "run_token_mismatch"
    r = c.post("/api/jobs", json={"after_prepare": prep, "models": [MODEL]},
               headers=auth(tok("benchmark", sub="u2", after_prepare=prep, prepare_jti=pjti)))
    assert r.status_code == 403
    r = c.post("/api/jobs", json={"after_prepare": prep, "models": [MODEL]},
               headers=auth(tok("benchmark", sub="u1", after_prepare=prep, prepare_jti=pjti)))
    assert r.status_code == 202, r.get_json()
    # an imported set: the import's grant is recorded and checked the same way
    sid = mgr.wait(prep, timeout=60)["prepared"]
    run_dir = mgr.results_root / sid
    files = {"manifest": (io.BytesIO((run_dir / "manifest.json").read_bytes()), "manifest.json"),
             "features": (io.BytesIO((run_dir / "features.csv").read_bytes()), "features.csv")}
    if (run_dir / "labels.csv").exists():
        files["labels"] = (io.BytesIO((run_dir / "labels.csv").read_bytes()), "labels.csv")
    it = tok("import", sub="u1")
    imp = c.post("/api/prepared/import", data=files, content_type="multipart/form-data", headers=auth(it))
    assert imp.status_code == 201, imp.get_json()
    new = imp.get_json()["prepared_set"]
    assert mgr.set_grant(new)["grant_jti"] == runtoken.verify(it, SECRET)["jti"]
    r = c.post("/api/jobs", json={"run": new, "models": [MODEL]},
               headers=auth(tok("benchmark", sub="u1", run=new, prepare_jti=runtoken.verify(it, SECRET)["jti"])))
    assert r.status_code == 202, r.get_json()


def test_queue_cap_follows_the_control_plane_limits(app):
    c, mgr, a = app["c"], app["mgr"], app["app"]
    idle = JobManager(jobs_dir=app["tmp"] / "jobs2", results_root=app["tmp"] / "results", gpu_available=lambda: False,
                      autostart=False)
    a2 = app["srv"].create_app(local_results_dir=str(app["tmp"] / "results"), job_manager=idle, mode="mock")
    app["srv"].apply_control_plane_limits(a2, None, {"max_queued_sessions": 1, "max_upload_mb": 5})
    c2 = a2.test_client()
    assert prepare(c2).status_code == 202
    r = prepare(c2)
    assert r.status_code == 429 and r.get_json()["code"] == "queue_full"
    assert a2.config["SERVICE_LIMITS"]["max_upload_mb"] == 5
    assert a2.config["MAX_CONTENT_LENGTH"] <= 6 * 1024 ** 2


# ---------------------------------------------------------------------------
# job ids: 128 random bits
# ---------------------------------------------------------------------------
def test_new_job_ids_have_128_random_bits():
    ids = {jobs._new_job_id() for _ in range(200)}
    assert len(ids) == 200
    for j in ids:
        m = re.fullmatch(r"job_\d{8}_\d{6}_([0-9a-f]{32})", j)
        assert m and jobs.strong_job_id(j)
    assert 4 * 32 >= 128
    assert not jobs.strong_job_id("job_20260101_000000_abcdef")                  # pre-Phase-47 id: 24 bits
    assert jobs._JOB_ID.match("job_20260101_000000_abcdef")                       # still readable


def test_legacy_job_ids_need_a_token_for_status(app):
    c, mgr = app["c"], app["mgr"]
    legacy = "job_20260101_000000_abcdef"
    mgr.jobs_dir.mkdir(parents=True, exist_ok=True)
    (mgr.jobs_dir / f"{legacy}.json").write_text(json.dumps({
        "job_id": legacy, "kind": "prepare", "status": "failed", "created_at": "2026-01-01T00:00:00+00:00",
        "created_ns": 1, "models": [], "message": "old"}))
    assert c.get(f"/api/jobs/{legacy}").status_code == 401
    assert c.get(f"/api/jobs/{legacy}", headers=auth(tok("read"))).status_code == 200
    jid = prepare(c).get_json()["job_id"]
    assert jobs.strong_job_id(jid) and c.get(f"/api/jobs/{jid}").status_code == 200
    assert c.get(f"/api/jobs/{jid}/result").status_code == 401                   # results always need a token


def test_the_passcode_is_gone():
    for p in ("agentmeter/server/access.py", "agentmeter/server/runtime.py", "scripts/serve.py", "scripts/vast_up.sh"):
        src = (PROJECT_ROOT / p).read_text()
        assert "X-AgentMeter-Passcode" not in src and 'environ.get("AGENTMETER_PASSCODE")' not in src, p
        assert "AGENTMETER_PASSCODE:-" not in src, p


# ---------------------------------------------------------------------------
# identification columns
# ---------------------------------------------------------------------------
def test_identification_columns_are_stripped_from_the_uploaded_copy():
    src = "flow_id,src_ip,src_port,dst_ip,protocol,protocol_name,timestamp,Destination Port,Flow Duration\n" \
          "f1,10.0.0.1,5555,10.0.0.2,6,TCP,2017-07-05 10:00,80,123\n"
    out = control_plane.strip_identification(src.encode()).decode()
    assert out.splitlines() == ["flow_id,Destination Port,Flow Duration", "f1,80,123"]
    assert "10.0.0.1" not in out and "5555" not in out
    from agentmeter.ingest.feature_map import META_COLUMNS
    assert set(control_plane.IDENTIFICATION_COLUMNS) == set(META_COLUMNS) - {"flow_id"}


# ---------------------------------------------------------------------------
# protocol against a fake Worker (signature checked exactly like worker/src/backend.ts)
# ---------------------------------------------------------------------------
class FakeWorker:
    def __init__(self, fail_first: int = 0):
        self.calls: list[tuple[str, str, bytes]] = []
        self.store: dict[str, bytes] = {}
        self.fail_first = fail_first
        self.limits = {"sessions_per_hour": 7, "max_queued_sessions": 3, "max_upload_mb": 40, "idle_minutes": 25,
                       "keep_identification_columns_in_r2": False}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: ANN002
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                ts = self.headers.get("X-AM-Timestamp", "")
                want = control_plane.sign(BACKEND_SECRET, self.command, self.path, ts, body)
                if not hmac.compare_digest(want, self.headers.get("X-AM-Signature", "")) or abs(time.time() - int(ts)) > 120:
                    return self._send(401, {"code": "bad_signature"})
                if outer.fail_first > 0:
                    outer.fail_first -= 1
                    return self._send(500, {"code": "internal"})
                outer.calls.append((self.command, self.path, body))
                if self.command == "PUT":
                    outer.store[self.path] = body
                if self.path in ("/api/backend/register", "/api/backend/heartbeat"):
                    return self._send(200, {"ok": True, "limits": outer.limits})
                if self.command == "GET":
                    return self._get()
                m = re.fullmatch(r"/api/backend/sessions/([^/]+)/identity", self.path)
                if m:                              # like worker/src/sessions.ts backendSetIdentity
                    key = f"/api/backend/sessions/{m.group(1)}/summary"
                    doc = json.loads(outer.store[key])
                    upd = json.loads(body)
                    doc["summary"]["identity"].update(upd)
                    doc["summary"]["prepared_set_sha256"] = upd["prepared_set_sha256"]
                    outer.store[key] = json.dumps(doc).encode()
                return self._send(200, {"ok": True})

            def _get(self):
                if self.path == "/api/backend/sessions":
                    out = []
                    for k, v in outer.store.items():
                        m = re.fullmatch(r"/api/backend/sessions/([^/]+)/summary", k)
                        if m:
                            ident = json.loads(v)["summary"].get("identity") or {}
                            pre = f"/api/backend/sessions/{m.group(1)}/files/"
                            out.append({"id": m.group(1), "prepared_set_sha256": ident.get("prepared_set_sha256"),
                                        "prepared_set_hash_version": ident.get("prepared_set_hash_version", 1),
                                        "files": [x[len(pre):] for x in outer.store if x.startswith(pre)]})
                    return self._send(200, {"sessions": out})
                if self.path in outer.store:
                    b = outer.store[self.path]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    return self.wfile.write(b)
                return self._send(404, {"code": "not_found"})

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            do_POST = do_PUT = do_GET = _do

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def paths(self):
        return [(m, p) for m, p, _ in self.calls]


def test_register_heartbeat_events_and_upload(app, tmp_path, monkeypatch):
    c, mgr, a, srv = app["c"], app["mgr"], app["app"], app["srv"]
    fw = FakeWorker()
    url_file = tmp_path / "public_url"
    monkeypatch.setenv("AGENTMETER_PUBLIC_URL_FILE", str(url_file))
    cp = control_plane.ControlPlane(control_plane.Client(fw.url, BACKEND_SECRET), get_manager=lambda: mgr, mode="mock",
                                    environment=lambda: {"provider": "mock"}, version="test",
                                    apply_limits=lambda lim: srv.apply_control_plane_limits(a, None, lim),
                                    creds_fn=lambda: {"hf_token": True, "vast_api_key": False})
    with pytest.raises(control_plane.ControlPlaneError):                       # no tunnel URL yet
        cp.register()
    url_file.write_text("https://fluffy-cat.trycloudflare.com\n")
    cp.heartbeat()                                                             # registers first
    reg = json.loads(fw.calls[0][2])
    assert fw.paths()[0] == ("POST", "/api/backend/register")
    assert reg["url"] == "https://fluffy-cat.trycloudflare.com" and reg["creds"] == {"hf_token": True, "vast_api_key": False}
    assert "test-run-token" not in fw.calls[0][2].decode()                      # never a secret value
    assert a.config["ACCESS"]["max_queued"] == 3 and a.config["SERVICE_LIMITS"]["max_upload_mb"] == 40
    cp.heartbeat()
    assert fw.paths()[-1] == ("POST", "/api/backend/heartbeat")
    url_file.write_text("https://agentmeter.example.org\n")                     # quick -> named tunnel: same call
    cp.heartbeat()
    assert json.loads(fw.calls[-1][2])["url"] == "https://agentmeter.example.org"

    # a full session through the app, with events delivered synchronously
    mgr.on_event = lambda job, created: cp._deliver("created" if created else "status", job)
    jid = prepare(c, tok("prepare", uname="alice")).get_json()["job_id"]
    sid = mgr.wait(jid, timeout=60)["prepared"]
    bt = tok("benchmark", uname="alice", run=sid)
    bj = c.post("/api/jobs", json={"run": sid, "models": [MODEL]}, headers=auth(bt)).get_json()["job_id"]
    assert mgr.wait(bj, timeout=120)["status"] == "done"
    for _ in range(100):
        if mgr._load(bj).get("cp_uploaded"):
            break
        time.sleep(0.05)
    created = [json.loads(b) for m, p, b in fw.calls if p == "/api/backend/jobs"]
    assert {x["kind"] for x in created} == {"prepare", "benchmark"}
    bench = next(x for x in created if x["kind"] == "benchmark")
    assert bench["jti"] == runtoken.verify(bt, SECRET)["jti"] and bench["owner_username"] == "alice"
    assert ("POST", f"/api/backend/jobs/{bj}/status") in fw.paths()
    base = f"/api/backend/sessions/{bj}"
    job = mgr._load(bj)
    assert job["cp_uploaded"] is True and job["cp_identification_stripped"] is True
    assert fw.store[f"{base}/results"] == Path(job["result_path"]).read_bytes()          # raw, byte for byte
    assert fw.store[f"{base}/files/report.pdf"][:4] == b"%PDF"
    feats = fw.store[f"{base}/files/features.csv"].decode()
    assert "src_ip" not in feats.splitlines()[0] and "flow_id" in feats.splitlines()[0]
    assert "src_ip" in (Path(job["run_dir"]) / "features.csv").read_text().splitlines()[0]   # the backend copy keeps them
    summary = json.loads(fw.store[f"{base}/summary"])["summary"]
    assert summary["session_id"] == bj and summary["owner_username"] == "alice"
    assert summary["weights_used"]["note"].startswith("the weights the stored SAW scores")
    assert isinstance(summary["facts"], list) and summary["metrics"]
    assert fw.paths()[-1] == ("PUT", f"{base}/summary")                         # summary last: marks it Done
    n = len(fw.calls)
    assert cp.upload(bj) is True and len(fw.calls) == n                         # once only


def test_a_failed_upload_is_retried_on_the_next_heartbeat(app, monkeypatch):
    c, mgr = app["c"], app["mgr"]
    sid = mgr.wait(prepare(c).get_json()["job_id"], timeout=60)["prepared"]
    bj = c.post("/api/jobs", json={"run": sid, "models": [MODEL]}, headers=auth(tok("benchmark", run=sid))).get_json()["job_id"]
    assert mgr.wait(bj, timeout=120)["status"] == "done"
    fw = FakeWorker(fail_first=1)
    cp = control_plane.ControlPlane(control_plane.Client(fw.url, BACKEND_SECRET), get_manager=lambda: mgr, mode="mock",
                                    environment=lambda: {}, url_fn=lambda: "https://x.trycloudflare.com")
    assert cp.upload_pending() == 0 and not mgr._load(bj).get("cp_uploaded")    # Worker returned 500
    assert cp.upload_pending() == 1 and mgr._load(bj)["cp_uploaded"] is True


def test_backfill_cli(app, monkeypatch, capsys):
    c, mgr = app["c"], app["mgr"]
    sid = mgr.wait(prepare(c).get_json()["job_id"], timeout=60)["prepared"]
    bj = c.post("/api/jobs", json={"run": sid, "models": [MODEL]}, headers=auth(tok("benchmark", run=sid))).get_json()["job_id"]
    assert mgr.wait(bj, timeout=120)["status"] == "done"
    j = mgr._load(bj)
    j.pop("user")                                                              # a session from before accounts
    mgr._save(j)
    fw = FakeWorker()
    spec = importlib.util.spec_from_file_location("backfill", PROJECT_ROOT / "scripts" / "backfill_sessions.py")
    bf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bf)
    monkeypatch.setenv("AGENTMETER_BACKEND_SECRET", BACKEND_SECRET)
    args = ["--owner", "carol", "--jobs-dir", str(mgr.jobs_dir), "--results-root", str(mgr.results_root), "--worker", fw.url]
    assert bf.main(args + ["--dry-run"]) == 0 and bj in capsys.readouterr().out and not fw.calls
    assert bf.main(args) == 0
    summary = json.loads(fw.store[f"/api/backend/sessions/{bj}/summary"])["summary"]
    assert summary["owner_username"] == "carol"
    assert "src_ip" not in fw.store[f"/api/backend/sessions/{bj}/files/features.csv"].decode().splitlines()[0]
    n = len(fw.calls)
    assert bf.main(args) == 0 and len(fw.calls) == n                           # idempotent: nothing left


# ---------------------------------------------------------------------------
# the Worker's copies of Python artefacts are in sync
# ---------------------------------------------------------------------------
def test_worker_rule_file_matches_the_python_rule_base():
    spec = importlib.util.spec_from_file_location("exp", PROJECT_ROOT / "scripts" / "export_constraint_rules.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert (PROJECT_ROOT / "worker" / "src" / "constraint_rules.json").read_text() == m.exported(), \
        "run: python scripts/export_constraint_rules.py"


def test_worker_parity_fixtures_are_current():
    spec = importlib.util.spec_from_file_location("par", PROJECT_ROOT / "scripts" / "export_parity_fixtures.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert json.loads((PROJECT_ROOT / "worker" / "test" / "fixtures" / "parity.json").read_text()) == json.loads(m.exported()), \
        "run: python scripts/export_parity_fixtures.py (then the Worker's parity test)"


def test_control_plane_settings_cannot_hide_the_demo_banner_or_caveats():
    src = (PROJECT_ROOT / "worker" / "src" / "settings.ts").read_text()
    keys = set(re.findall(r"^\s{2}(\w+):", src.split("export const DEFAULT_SETTINGS")[1].split("};")[0], re.M))
    assert keys == {"theme", "telegram_chat_id", "notify_on", "default_models", "default_flows",
                    "default_other_attack", "weight_presets", "timezone"}
    assert not any(re.search(r"demo|banner|caveat|endorse|disclaim", k) for k in keys)


def test_pbkdf2_iterations_are_documented():
    toml = (PROJECT_ROOT / "worker" / "wrangler.toml").read_text()
    assert 'PBKDF2_ITERATIONS = "40000"' in toml
    assert "40,000" in (PROJECT_ROOT / "docs" / "DEPLOY.md").read_text()


# ---------------------------------------------------------------------------
# the front-end on Cloudflare Pages
# ---------------------------------------------------------------------------
def test_pages_ships_the_analysis_page_and_its_scripts():
    for a, b in (("web/analysis.html", "dashboard/index.html"), ("web/saw.js", "dashboard/saw.js"),
                 ("web/report.js", "dashboard/report.js")):
        assert (PROJECT_ROOT / a).read_bytes() == (PROJECT_ROOT / b).read_bytes(), f"cp {b} {a}"
    cfg = json.loads((PROJECT_ROOT / "web" / "config.json").read_text())
    assert cfg["control_plane"] == "" and cfg["api_base"] == ""


def test_pages_csp_is_strict_and_current():
    spec = importlib.util.spec_from_file_location("csp", PROJECT_ROOT / "scripts" / "csp_headers.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    hdr = (PROJECT_ROOT / "web" / "_headers").read_text()
    assert hdr == m.headers(), "run: python scripts/csp_headers.py"
    csp = next(l for l in hdr.splitlines() if "Content-Security-Policy" in l)
    script_src = csp.split("script-src")[1].split(";")[0]
    assert "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src and "*" not in script_src
    assert "frame-ancestors 'self'" in csp and "object-src 'none'" in csp and "base-uri 'none'" in csp
    for page in ("index.html", "analysis.html"):
        html = (PROJECT_ROOT / "web" / page).read_text()
        assert not re.search(r'\son[a-z]+="', html), f"{page}: inline event handler (blocked by the CSP)"
        assert not re.search(r"javascript:", html), page


def test_front_end_never_shows_identification_columns():
    html = (PROJECT_ROOT / "web" / "index.html").read_text()
    for col in control_plane.IDENTIFICATION_COLUMNS:            # as a field / key (prose naming them is fine)
        assert not re.search(rf"""(["'.\[]){col}(["'\]])""", html) and not re.search(rf"\.{col}\b", html), col
    assert "src_ip" not in html and "dst_ip" not in html
