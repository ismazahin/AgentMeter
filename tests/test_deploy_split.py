"""Phase E: front-end/back-end split, access control, and real-vs-mock honesty.

* web/ is a static front-end (no functions); it finds the backend via config.json.
* CORS is limited to AGENTMETER_ALLOWED_ORIGINS; a real-GPU server never sends "*".
* Phase 47: run tokens from the control plane guard every write, listing and prepared-set file;
  a job's status by its 128-bit id stays open (tests/test_control_plane.py has the details).
* Job creation is rate-limited and the queue is capped (single-job lock unchanged).
* The provider is decided once: real mode refuses to start without GPU/models/
  control-plane secrets, a real server never runs mock jobs, and every result names the GPU.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys

import pytest
import yaml

from agentmeter.config import PROJECT_ROOT
from agentmeter.server import runtime
from agentmeter.server.jobs import JobManager

SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
WEB = PROJECT_ROOT / "web"
ENVS = ("AGENTMETER_ALLOWED_ORIGINS", "AGENTMETER_RUN_TOKEN_SECRET", "AGENTMETER_WORKER_URL",
        "AGENTMETER_BACKEND_SECRET", "AGENTMETER_CONTROL_PLANE_URL", "AGENTMETER_JOBS_PER_HOUR",
        "AGENTMETER_MAX_QUEUED_JOBS", "AGENTMETER_SERVICE_PROVIDER", "AGENTMETER_AUTH_PASS")


@pytest.fixture
def make(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    for k in ENVS:
        monkeypatch.delenv(k, raising=False)

    def build(mode=None, mgr=None, **env):
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        spec = importlib.util.spec_from_file_location("serve",
                                                      PROJECT_ROOT / "scripts" / "serve.py")
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        base = tmp_path / "cfg.yaml"
        base.write_text(yaml.safe_dump({"run": {"models": ["mock/m"]}, "model": {"provider": "mock"},
                                        "dataset": {}, "pipeline": {}, "classes": [], "scoring": {},
                                        "storage": {}}))
        mgr = mgr or JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results",
                                gpu_available=lambda: False)
        app = srv.create_app(local_results_dir=str(tmp_path / "results"), job_manager=mgr, mode=mode)
        return app.test_client(), mgr, srv
    return build


SECRET = "test-run-token-secret-0123456789"


def bearer(kind, **extra):
    import secrets
    import time

    from agentmeter.server import runtoken
    now = int(time.time())
    tok = runtoken.sign({"typ": "run", "aud": "agentmeter-backend", "jti": secrets.token_hex(16), "sub": "u1",
                         "uname": "alice", "kind": kind, "iat": now, "exp": now + 300, **extra}, SECRET)
    return {"Authorization": f"Bearer {tok}"}


def _preflight_msg():
    with pytest.raises(runtime.RealModeError) as e:
        runtime.preflight()
    return str(e.value)


def upload(c, headers=None, **form):
    return c.post("/api/prepare", data={"file": (io.BytesIO(SAMPLE_CSV.read_bytes()), SAMPLE_CSV.name),
                                        "max_flows": "5", **form},
                  content_type="multipart/form-data", headers=headers or {})


# ---------------------------------------------------------------------------
# the static front-end
# ---------------------------------------------------------------------------
def test_web_is_static_and_report_js_is_in_sync():
    assert (WEB / "report.js").read_bytes() == (PROJECT_ROOT / "dashboard" / "report.js").read_bytes(), \
        "web/report.js must be a copy of dashboard/report.js: cp dashboard/report.js web/report.js"
    cfg = json.loads((WEB / "config.json").read_text())
    assert cfg["api_base"] == ""
    v = json.loads((WEB / "vercel.json").read_text())
    assert not {"functions", "rewrites", "routes", "redirects"} & set(v)        # static only, no proxying
    assert not (WEB / "api").exists() and not (WEB / "functions").exists()


def test_front_end_only_talks_to_the_configured_backend():
    html = (WEB / "index.html").read_text()
    assert 'fetch("config.json"' in html and "?api=" in html.replace("api=<url>", "?api=")
    import re
    assert not re.search(r"""fetch\(\s*["']/""", html)             # no same-origin absolute fetches
    assert not re.search(r"""href=["']/api/""", html)               # downloads go through apiUrl()
    assert "fetch(API + path" in html and "fetch(API + \"/health\"" in html
    for hook in ("view-offline", "GPU backend offline", "svc-backend-badge", "Real GPU: ", "DEMO (mock)",
                 "svc-run-mode", "not a threat-detection", "view-login", "/api/runs/authorize", "data-auth-dl",
                 "GPU offline", "agentmeter.refresh"):
        assert hook in html, hook
    for gone in ("svc-pass-dialog", "X-AgentMeter-Passcode", "agentmeter.passcode"):
        assert gone not in html, gone


def test_backend_serves_the_same_page_same_origin(make):
    c, _, _ = make()
    assert c.get("/config.json").get_json() == {"api_base": "", "control_plane": None}
    page = c.get("/service").get_data(as_text=True)
    assert page == (WEB / "index.html").read_text()
    assert c.get("/report.js").status_code == 200


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
def test_health_reports_provider_queue_and_auth(make):
    c, _, _ = make(AGENTMETER_RUN_TOKEN_SECRET=SECRET)
    h = c.get("/health").get_json()
    assert h["ok"] is True and h["provider"] == "mock" and h["gpu"] is None
    assert h["queue"] == {"running": False, "queued": 0} and h["auth_required"] is True
    assert "not a threat-detection product" in h["service"]


def test_health_in_real_mode_names_the_gpu(make, monkeypatch):
    monkeypatch.setattr(runtime, "gpu_info", lambda: {"cuda_available": True, "name": "NVIDIA L4",
                                                      "vram_total_mb": 23034, "driver_version": "550.54",
                                                      "cuda_runtime_version": "12.1", "count": 1})
    monkeypatch.setattr(runtime, "models_local", lambda models=None: {m: True for m in runtime.study_models()})
    mgr = JobManager(jobs_dir=PROJECT_ROOT / "nonexistent-jobs-dir-for-test", gpu_available=lambda: True,
                     autostart=False)
    c, _, _ = make(mode="real", mgr=mgr, AGENTMETER_RUN_TOKEN_SECRET=SECRET)
    h = c.get("/health").get_json()
    assert h["provider"] == "real" and h["gpu"]["name"] == "NVIDIA L4" and h["gpu"]["vram_total_mb"] == 23034
    assert sum(h["models_local"].values()) == 5


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
def test_cors_is_limited_to_the_configured_origins(make):
    c, _, _ = make(AGENTMETER_ALLOWED_ORIGINS="https://agentmeter.vercel.app, https://am.pages.dev")
    ok = c.get("/health", headers={"Origin": "https://agentmeter.vercel.app"})
    assert ok.headers["Access-Control-Allow-Origin"] == "https://agentmeter.vercel.app"
    assert "Authorization" in ok.headers["Access-Control-Allow-Headers"]
    assert ok.headers["Vary"] == "Origin"
    bad = c.get("/health", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in bad.headers
    pre = c.open("/api/prepare", method="OPTIONS", headers={
        "Origin": "https://am.pages.dev", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization"})
    assert pre.status_code == 204 and pre.headers["Access-Control-Allow-Origin"] == "https://am.pages.dev"
    assert "Content-Disposition" in pre.headers["Access-Control-Expose-Headers"]


def test_real_server_never_sends_a_wildcard(make, monkeypatch):
    monkeypatch.setattr(runtime, "models_local", lambda models=None: {})
    mgr = JobManager(jobs_dir=PROJECT_ROOT / "nonexistent-jobs-dir-for-test", gpu_available=lambda: True,
                     autostart=False)
    c, _, _ = make(mode="real", mgr=mgr)
    for origin in ("https://evil.example", "null", "http://localhost:3000"):
        r = c.get("/health", headers={"Origin": origin})
        assert "Access-Control-Allow-Origin" not in r.headers


def test_mock_dev_server_without_origins_stays_permissive(make):
    c, _, _ = make()
    assert c.get("/health", headers={"Origin": "null"}).headers["Access-Control-Allow-Origin"] == "*"


# ---------------------------------------------------------------------------
# run tokens (Phase 47: the passcode is gone)
# ---------------------------------------------------------------------------
def test_writes_need_a_run_token_status_by_strong_id_does_not(make):
    c, mgr, _ = make(AGENTMETER_RUN_TOKEN_SECRET=SECRET)
    r = upload(c)
    assert r.status_code == 401 and r.get_json()["code"] == "run_token_required"
    r = upload(c, headers={"X-AgentMeter-Passcode": "correct horse battery staple"})   # the old header: ignored
    assert r.status_code == 401
    r = upload(c, headers=bearer("prepare"))
    assert r.status_code == 202
    jid = r.get_json()["job_id"]
    job = mgr.wait(jid, timeout=60)
    assert c.get("/api/jobs").status_code == 401                              # the job list is guarded
    assert c.get("/api/jobs", headers=bearer("read")).status_code == 200
    assert c.get(f"/api/jobs/{jid}").status_code == 200                       # status by 128-bit id: open
    assert c.get(f"/api/prepared/{job['prepared']}").status_code == 401
    assert c.get(f"/api/prepared/{job['prepared']}", headers=bearer("read")).status_code == 200
    assert c.get(f"/api/prepared/{job['prepared']}/features.csv", headers=bearer("read")).status_code == 403
    assert c.get(f"/api/prepared/{job['prepared']}/features.csv",
                 headers=bearer("files", set=job["prepared"])).status_code == 200
    assert c.get("/health").status_code == 200 and c.get("/service").status_code == 200
    for path, body in (("/api/jobs", {"run": job["prepared"], "models": ["x"]}),
                       ("/pull-eval", {"model_id": "org/m"}), ("/api/build-config", {}),
                       ("/api/notify-test", {})):
        assert c.post(path, json=body).status_code in (401, 403), path
    assert c.post(f"/api/jobs/{jid}/resume").status_code == 401


# ---------------------------------------------------------------------------
# rate limit + queue cap
# ---------------------------------------------------------------------------
def test_job_creation_is_rate_limited_per_client(make):
    c, mgr, _ = make(AGENTMETER_JOBS_PER_HOUR=2, AGENTMETER_MAX_QUEUED_JOBS=50)
    a, b = upload(c), upload(c)
    assert a.status_code == b.status_code == 202
    third = upload(c)
    assert third.status_code == 429 and third.get_json()["code"] == "rate_limited"
    assert int(third.headers["Retry-After"]) > 0
    other = upload(c, headers={"CF-Connecting-IP": "203.0.113.9"})          # another client, via the tunnel
    assert other.status_code == 202


def test_queue_is_capped(make, tmp_path):
    idle = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results",
                      gpu_available=lambda: False, autostart=False)
    c, _, _ = make(mgr=idle, AGENTMETER_MAX_QUEUED_JOBS=2)
    assert upload(c).status_code == 202 and upload(c).status_code == 202
    r = upload(c)
    assert r.status_code == 429 and r.get_json()["code"] == "queue_full"


# ---------------------------------------------------------------------------
# provider: decided by the server, recorded everywhere, no silent fallback
# ---------------------------------------------------------------------------
def test_real_server_forces_real_jobs(make, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "gpu_info", lambda: {"cuda_available": True, "name": "NVIDIA RTX A6000",
                                                      "vram_total_mb": 49140, "driver_version": "550.1",
                                                      "cuda_runtime_version": "12.1"})
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results",
                     gpu_available=lambda: True, autostart=False)
    c, _, _ = make(mode="real", mgr=mgr, AGENTMETER_RUN_TOKEN_SECRET=SECRET)
    from agentmeter.server.prepare import prepare_file
    from agentmeter.server.service_api import save_upload, service_limits
    up = save_upload(io.BytesIO(SAMPLE_CSV.read_bytes()), SAMPLE_CSV.name, mgr.results_root / "uploads",
                     service_limits())
    src = dict(up["source"])
    path = src.pop("path")
    sid = prepare_file(path, source=src, results_root=mgr.results_root, name=up["name"], max_flows=5,
                       other_attack=False, limits=service_limits())
    s = {"prepared_set": sid}
    run = s["prepared_set"]
    hdr = bearer("benchmark", run=run)
    r = c.post("/api/jobs", json={"run": run, "models": ["Qwen/Qwen2.5-7B-Instruct"], "provider": "mock"},
               headers=hdr)
    assert r.status_code == 400 and r.get_json()["code"] == "provider_mismatch"
    r = c.post("/api/jobs", json={"run": run, "models": ["Qwen/Qwen2.5-7B-Instruct"],
                                  "base_config": "run_full_mock.yaml"}, headers=hdr)
    assert r.status_code == 202
    job = r.get_json()
    assert job["effective_provider"] == "hf" and job["base_config"].endswith("run_full_l4.yaml")
    assert job["environment"]["provider"] == "real" and job["environment"]["gpu_name"] == "NVIDIA RTX A6000"
    assert c.get("/api/service/config").get_json()["provider"] == "hf"


def test_mock_jobs_record_the_mock_environment_in_results_and_pdf(make):
    c, mgr, _ = make()
    r = upload(c)
    sid = mgr.wait(r.get_json()["job_id"], timeout=60)["prepared"]
    man = json.loads(c.get(f"/api/prepared/{sid}/manifest.json").data)["prepared_set"]
    assert man["backend"]["provider"] == "mock"
    j = c.post("/api/jobs", json={"run": sid, "models": ["mistralai/Mistral-7B-Instruct-v0.3"]})
    jid = j.get_json()["job_id"]
    assert mgr.wait(jid, timeout=120)["status"] == "done"
    res = c.get(f"/api/jobs/{jid}/result").get_json()
    assert res["environment"]["provider"] == "mock" and res["environment"]["gpu_name"] is None
    from pypdf import PdfReader
    t = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(c.get(f"/api/jobs/{jid}/report.pdf").data)).pages)
    assert "Measured on: no GPU — provider mock (demo)" in t


def test_pdf_names_the_real_gpu(make):
    c, mgr, _ = make()
    sid = mgr.wait(upload(c).get_json()["job_id"], timeout=60)["prepared"]
    jid = c.post("/api/jobs", json={"run": sid, "models": ["microsoft/Phi-3-mini-4k-instruct"]}).get_json()["job_id"]
    assert mgr.wait(jid, timeout=120)["status"] == "done"
    res = c.get(f"/api/jobs/{jid}/result").get_json()
    res["environment"] = {"provider": "real", "gpu_name": "NVIDIA L40S", "gpu_vram_total_mb": 46068,
                          "driver_version": "550.54.15", "cuda_runtime_version": "12.1", "torch": "2.4.0",
                          "transformers": "4.44.2", "bitsandbytes": "0.43.3", "host": "vast.ai instance 123"}
    from agentmeter.session.pdf_report import build_report_pdf
    from pypdf import PdfReader
    t = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(build_report_pdf(res))).pages)
    t = " ".join(t.split())
    assert "REAL GPU NVIDIA L40S, 45 GB" in t and "driver 550.54.15" in t and "CUDA 12.1" in t
    assert "vast.ai instance 123" in t


def test_real_mode_refuses_to_start_without_a_gpu(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONPATH": str(PROJECT_ROOT),
           "HF_HOME": str(tmp_path / "hf"), "AGENTMETER_RUN_TOKEN_SECRET": SECRET,
           "AGENTMETER_WORKER_URL": "https://cp.example.workers.dev", "AGENTMETER_BACKEND_SECRET": SECRET}
    p = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "serve.py"),
                        "--provider", "real", "--port", "8791", "--results-dir", str(tmp_path)],
                       env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 2, p.stdout + p.stderr
    assert "refusing to start in REAL mode (no fallback to mock)" in p.stderr
    assert "no CUDA GPU is visible" in p.stderr and "study models not on local disk" in p.stderr


def test_preflight_lists_every_problem_and_passes_when_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "gpu_info", lambda: {"cuda_available": False})
    for k in runtime.CONTROL_PLANE_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    with pytest.raises(runtime.RealModeError) as e:
        runtime.preflight()
    msg = str(e.value)
    assert "no CUDA GPU" in msg and "AGENTMETER_RUN_TOKEN_SECRET is not set" in msg and "Phi-3" in msg
    assert "AGENTMETER_WORKER_URL is not set" in msg and "AGENTMETER_BACKEND_SECRET is not set" in msg
    monkeypatch.setenv("AGENTMETER_WORKER_URL", "http://insecure.example")
    assert "must be an https:// URL" in _preflight_msg()
    # now make everything present
    for m in runtime.study_models():
        snap = tmp_path / "hub" / ("models--" + m.replace("/", "--")) / "snapshots" / "abc"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        (snap / "model.safetensors").write_bytes(b"x")
    monkeypatch.setattr(runtime, "gpu_info", lambda: {"cuda_available": True, "name": "NVIDIA L4",
                                                      "vram_total_mb": 23034})
    monkeypatch.setattr(runtime, "_version", lambda d: "1.0")
    monkeypatch.setenv("AGENTMETER_WORKER_URL", "https://cp.example.workers.dev")
    monkeypatch.setenv("AGENTMETER_RUN_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("AGENTMETER_BACKEND_SECRET", SECRET)
    env = runtime.preflight()
    assert env["provider"] == "real" and env["gpu_name"] == "NVIDIA L4"
    assert runtime.resolve_mode("mock") == "mock" and runtime.resolve_mode("hf") == "real"
    with pytest.raises(runtime.RealModeError):
        runtime.resolve_mode("gpu-please")


# ---------------------------------------------------------------------------
# cost guard: polling /health is not "activity"; service jobs keep the box alive
# ---------------------------------------------------------------------------
def test_health_polling_does_not_keep_the_instance_alive(make):
    c, mgr, srv = make()
    calls = []
    guard = srv.CostGuard(auto_destroy=True, idle_timeout_min=0.001, instance_id="1",
                          destroy=lambda instance_id=None: calls.append(instance_id) or True, job_manager=mgr)
    app = srv.create_app(guard=guard, local_results_dir=str(mgr.results_root), job_manager=mgr)
    t0 = guard.last_activity
    app.test_client().get("/health")
    app.test_client().get("/config.json")
    assert guard.last_activity == t0
    app.test_client().get("/api/service/config")
    assert guard.last_activity > t0


def test_watchdog_waits_while_a_service_job_is_queued(make, tmp_path):
    import threading
    _, _, srv = make()
    idle = JobManager(jobs_dir=tmp_path / "j2", results_root=tmp_path / "r2", autostart=False,
                      gpu_available=lambda: False)
    from agentmeter.server.service_api import save_upload, service_limits
    up = save_upload(io.BytesIO(SAMPLE_CSV.read_bytes()), SAMPLE_CSV.name, tmp_path / "r2" / "uploads",
                     service_limits())
    idle.create_prepare_job(up["source"], name=up["name"], max_flows=5, other_attack=False,
                            limits=service_limits())
    fired = []
    guard = srv.CostGuard(auto_destroy=True, idle_timeout_min=0.0005, instance_id="1",
                          destroy=lambda instance_id=None: fired.append(1) or True, job_manager=idle)
    stop = threading.Event()
    t = threading.Thread(target=guard.watchdog, args=(stop,), kwargs={"poll": 0.01}, daemon=True)
    t.start()
    t.join(0.3)
    assert fired == []                                   # a queued job counts as busy
    stop.set()
