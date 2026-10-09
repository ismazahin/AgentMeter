"""Phase E: front-end/back-end split, access control, and real-vs-mock honesty.

* web/ is a static front-end (no functions); it finds the backend via config.json.
* CORS is limited to AGENTMETER_ALLOWED_ORIGINS; a real-GPU server never sends "*".
* AGENTMETER_PASSCODE guards every write and the job list; reads by id stay open.
* Job creation is rate-limited and the queue is capped (single-job lock unchanged).
* The provider is decided once: real mode refuses to start without GPU/models/
  passcode, a real server never runs mock jobs, and every result names the GPU.
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
ENVS = ("AGENTMETER_ALLOWED_ORIGINS", "AGENTMETER_PASSCODE", "AGENTMETER_JOBS_PER_HOUR",
        "AGENTMETER_MAX_QUEUED_JOBS", "AGENTMETER_SERVICE_PROVIDER", "AGENTMETER_AUTH_PASS")


@pytest.fixture
def make(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    for k in ENVS:
        monkeypatch.delenv(k, raising=False)

    def build(mode=None, mgr=None, **env):
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        from agentmeter import pull_eval
        spec = importlib.util.spec_from_file_location("pull_eval_server",
                                                      PROJECT_ROOT / "scripts" / "pull_eval_server.py")
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        base = tmp_path / "cfg.yaml"
        base.write_text(yaml.safe_dump({"run": {"models": ["mock/m"]}, "model": {"provider": "mock"},
                                        "dataset": {}, "pipeline": {}, "classes": [], "scoring": {},
                                        "storage": {}}))
        pmgr = pull_eval.JobManager(base_config=str(base), canonical_json=str(tmp_path / "c.json"),
                                    out_dir=str(tmp_path / "pulls"))
        mgr = mgr or JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results",
                                gpu_available=lambda: False)
        app = srv.create_app(pmgr, local_results_dir=str(tmp_path / "results"), job_manager=mgr, mode=mode)
        return app.test_client(), mgr, srv
    return build


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
                 "svc-pass-dialog", "X-AgentMeter-Passcode", "svc-run-mode", "not a threat-detection"):
        assert hook in html, hook


def test_backend_serves_the_same_page_same_origin(make):
    c, _, _ = make()
    assert c.get("/config.json").get_json() == {"api_base": ""}
    page = c.get("/service").get_data(as_text=True)
    assert page == (WEB / "index.html").read_text()
    assert c.get("/report.js").status_code == 200


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
def test_health_reports_provider_queue_and_auth(make):
    c, _, _ = make(AGENTMETER_PASSCODE="a-long-test-passcode")
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
    c, _, _ = make(mode="real", mgr=mgr, AGENTMETER_PASSCODE="a-long-test-passcode")
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
    assert "X-AgentMeter-Passcode" in ok.headers["Access-Control-Allow-Headers"]
    assert ok.headers["Vary"] == "Origin"
    bad = c.get("/health", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in bad.headers
    pre = c.open("/api/prepare", method="OPTIONS", headers={
        "Origin": "https://am.pages.dev", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-agentmeter-passcode"})
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
# passcode
# ---------------------------------------------------------------------------
PASS = "correct horse battery staple"


def test_writes_need_the_passcode_reads_by_id_do_not(make):
    c, mgr, _ = make(AGENTMETER_PASSCODE=PASS)
    r = upload(c)
    assert r.status_code == 401 and r.get_json()["code"] == "passcode_required"
    r = upload(c, headers={"X-AgentMeter-Passcode": "nope"})
    assert r.status_code == 401 and r.get_json()["code"] == "passcode_invalid"
    r = upload(c, headers={"X-AgentMeter-Passcode": PASS})
    assert r.status_code == 202
    jid = r.get_json()["job_id"]
    job = mgr.wait(jid, timeout=60)
    assert c.get("/api/jobs").status_code == 401                              # the job list is guarded
    assert c.get("/api/jobs", headers={"X-AgentMeter-Passcode": PASS}).status_code == 200
    assert c.get(f"/api/jobs/{jid}").status_code == 200                       # status by id: open
    assert c.get(f"/api/prepared/{job['prepared']}").status_code == 200       # prepared set by id: open
    assert c.get(f"/api/prepared/{job['prepared']}/features.csv").status_code == 200
    assert c.get("/health").status_code == 200 and c.get("/service").status_code == 200
    for path, body in (("/api/jobs", {"run": job["prepared"], "models": ["x"]}),
                       ("/pull-eval", {"model_id": "org/m"}), ("/api/build-config", {})):
        assert c.post(path, json=body).status_code == 401, path
    assert c.post(f"/api/jobs/{jid}/resume").status_code == 401


def test_wrong_passcodes_are_rate_limited(make):
    c, _, _ = make(AGENTMETER_PASSCODE=PASS)
    codes = [upload(c, headers={"X-AgentMeter-Passcode": f"guess{i}"}).status_code for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


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
    c, _, _ = make(mode="real", mgr=mgr, AGENTMETER_PASSCODE=PASS)
    hdr = {"X-AgentMeter-Passcode": PASS}
    from agentmeter.server.service_api import ingest_upload, service_limits
    s = ingest_upload(io.BytesIO(SAMPLE_CSV.read_bytes()), SAMPLE_CSV.name, mgr.results_root,
                      mgr.results_root / "uploads", 5, False, service_limits())
    run = s["prepared_set"]
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
           "HF_HOME": str(tmp_path / "hf"), "AGENTMETER_PASSCODE": PASS}
    p = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "pull_eval_server.py"),
                        "--provider", "real", "--port", "8791", "--results-dir", str(tmp_path)],
                       env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 2, p.stdout + p.stderr
    assert "refusing to start in REAL mode (no fallback to mock)" in p.stderr
    assert "no CUDA GPU is visible" in p.stderr and "study models not on local disk" in p.stderr


def test_preflight_lists_every_problem_and_passes_when_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "gpu_info", lambda: {"cuda_available": False})
    monkeypatch.delenv("AGENTMETER_PASSCODE", raising=False)
    monkeypatch.delenv("AGENTMETER_INSECURE_NO_PASSCODE", raising=False)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    with pytest.raises(runtime.RealModeError) as e:
        runtime.preflight()
    msg = str(e.value)
    assert "no CUDA GPU" in msg and "AGENTMETER_PASSCODE is not set" in msg and "Phi-3" in msg
    # now make everything present
    for m in runtime.study_models():
        snap = tmp_path / "hub" / ("models--" + m.replace("/", "--")) / "snapshots" / "abc"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        (snap / "model.safetensors").write_bytes(b"x")
    monkeypatch.setattr(runtime, "gpu_info", lambda: {"cuda_available": True, "name": "NVIDIA L4",
                                                      "vram_total_mb": 23034})
    monkeypatch.setattr(runtime, "_version", lambda d: "1.0")
    monkeypatch.setenv("AGENTMETER_PASSCODE", PASS)
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
    guard = srv.CostGuard(manager=None, auto_destroy=True, idle_timeout_min=0.001, instance_id="1",
                          destroy=lambda instance_id=None: calls.append(instance_id) or True)
    from agentmeter import pull_eval
    pm = pull_eval.JobManager(base_config=str(PROJECT_ROOT / "configs" / "run_full_mock.yaml"),
                              canonical_json=str(mgr.results_root / "c.json"), out_dir=str(mgr.results_root / "p"))
    app = srv.create_app(pm, guard=guard, local_results_dir=str(mgr.results_root), job_manager=mgr)
    guard.manager = pm
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

    class Idle:
        def snapshot(self):
            return type("S", (), {"is_active": lambda s: False})()
    guard = srv.CostGuard(manager=Idle(), auto_destroy=True, idle_timeout_min=0.0005, instance_id="1",
                          destroy=lambda instance_id=None: fired.append(1) or True)
    guard.job_manager = idle
    stop = threading.Event()
    t = threading.Thread(target=guard.watchdog, args=(stop,), kwargs={"poll": 0.01}, daemon=True)
    t.start()
    t.join(0.3)
    assert fired == []                                   # a queued job counts as busy
    stop.set()
