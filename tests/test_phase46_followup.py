"""Phase 46 follow-up: total peak device memory, idle-net energy, session rate limits,
and the "estimate" label on agentic overhead. No score, rank, SAW value or verdict changes
(test_scored_keys_golden.py)."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import shutil
import sys
import types
from pathlib import Path

import pytest

from agentmeter.config import PROJECT_ROOT, load_config
from agentmeter.db.storage import Storage
from agentmeter.pipeline.instrument import GpuProbe
from agentmeter.server.jobs import JobManager
from agentmeter.session import analyses, constraints
from agentmeter.session.benchmark import prepare_session
from agentmeter.session.scoring import score_session

FIXTURE = Path(__file__).parent / "fixtures" / "golden_session"
MODELS = ["Qwen/Qwen2.5-7B-Instruct", "microsoft/Phi-3-mini-4k-instruct"]
SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
WEB = (PROJECT_ROOT / "web" / "index.html").read_text()


@pytest.fixture()
def golden(tmp_path):
    run = tmp_path / "golden"
    shutil.copytree(FIXTURE, run)
    g = json.loads((run / "golden_session_results.json").read_text())
    plan = prepare_session(run, MODELS, provider="mock")
    p = score_session(plan, g["session"]["run_id"])
    p["environment"] = {**g["environment"], "provider": "real", "gpu_name": "NVIDIA L4"}
    labels = {pm["model"]: pm["db_label"] for pm in p["per_model"]}
    return {"run": run, "plan": plan, "run_id": g["session"]["run_id"], "payload": p, "labels": labels,
            "cfg": load_config(plan["config_path"]).data}


def analysed(golden, **kw):
    p = copy.deepcopy(golden["payload"])
    analyses.add_analyses(p, db_path=golden["plan"]["db_path"], run_id=golden["run_id"],
                          run_dir=golden["run"], config_data=golden["cfg"], **kw)
    return p


def windows(golden):
    sr, _ = analyses.read_rows(golden["plan"]["db_path"], golden["run_id"])
    return analyses.model_windows(sr, golden["labels"])


# ---------------------------------------------------------------------------
# 1. total peak device memory
# ---------------------------------------------------------------------------
class FakeCuda:
    def __init__(self):
        self.alloc, self.peak, self.reserved, self.max_reserved = 0, 0, 0, 0

    def is_available(self): return True
    def current_device(self): return 0
    def synchronize(self, d=None): pass
    def memory_allocated(self, d=None): return self.alloc
    def max_memory_allocated(self, d=None): return self.peak
    def memory_reserved(self, d=None): return self.reserved
    def max_memory_reserved(self, d=None): return self.max_reserved

    def reset_peak_memory_stats(self, d=None):
        self.peak, self.max_reserved = self.alloc, self.reserved


def test_gpu_probe_tracks_absolute_peaks_across_agent_calls():
    cuda = FakeCuda()
    probe = GpuProbe()
    probe.available, probe._torch, probe.device = True, types.SimpleNamespace(cuda=cuda), 0
    MB = 1024 * 1024
    cuda.alloc = cuda.reserved = 5000 * MB                       # weights resident
    assert probe.total_used_mb() == 5000 and probe.reserved_mb() == 5000
    for work_mb, res_mb in ((800, 6200), (1500, 6800), (300, 6000)):
        probe.reset()                                              # per-call reset (the existing hook)
        cuda.peak, cuda.max_reserved = (5000 + work_mb) * MB, res_mb * MB
        assert probe.read_peak_delta_mb() == work_mb                # working VRAM (excl. weights), unchanged
    assert probe.max_allocated_abs_mb == 6500 and probe.max_reserved_abs_mb == 6800   # survives the resets


def test_model_memory_table_keeps_the_larger_peak_on_resume(tmp_path):
    st = Storage(tmp_path / "s.db")
    w = {"allocated_mb": 5000.0, "reserved_mb": 5100.0, "nvml_used_mb": 5600.0, "nvml_before_load_mb": 400.0}
    st.persist_model_memory("r1", "m", weights=w, peak_allocated_mb=6500.0, peak_reserved_mb=6800.0)
    st.persist_model_memory("r1", "m", weights=w, peak_allocated_mb=6100.0, peak_reserved_mb=7000.0)
    row = dict(st.conn.execute("SELECT * FROM model_memory").fetchone())
    assert (row["peak_allocated_mb"], row["peak_reserved_mb"], row["weights_allocated_mb"]) == (6500.0, 7000.0, 5000.0)
    tables = {r[0] for r in st.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    st.close()
    assert "model_memory" in tables
    fresh = Storage(tmp_path / "other.db")                         # not part of the shared SCHEMA
    assert "model_memory" not in {r[0] for r in fresh.conn.execute("SELECT name FROM sqlite_master")}
    fresh.close()


def _write_memory(golden, reserved=(6800.0, 4200.0)):
    st = Storage(golden["plan"]["db_path"])
    for (m, lab), res in zip(golden["labels"].items(), reserved):
        st.persist_model_memory(golden["run_id"], lab, weights={"allocated_mb": res - 1500, "reserved_mb": res - 1400,
                                                                "nvml_used_mb": res - 1000, "nvml_before_load_mb": 400.0},
                                peak_allocated_mb=res - 300, peak_reserved_mb=res)
    st.close()


def test_memory_block_and_total_peak_in_the_decision_helper(golden):
    _write_memory(golden)
    p = analysed(golden)
    m0, m1 = p["memory"]["per_model"]
    assert m0["total_peak_mb"] == 6800.0 and m0["weights_mb"] == 5300.0 and m0["status"] == "measured"
    assert m0["total_basis"].startswith("torch peak reserved")
    assert m1["total_peak_mb"] == 4200.0
    d = constraints.evaluate_constraints(p, {"max_peak_vram_mb": 5000})
    r0, r1 = (next(r for r in m["rows"] if r["rule"] == "peak_vram") for m in d["per_model"])
    assert (r0["result"], r1["result"]) == ("fails", "meets")
    assert "6,800 MB (weights + working)" in r0["note"]
    # an NVML device-used peak (includes the CUDA context) above torch's reserved wins
    w = windows(golden)
    t0, t1 = min(v[0] for v in w.values()) - 2, max(v[1] for v in w.values()) + 2
    mem = [(t0 + i * 0.1, 7300.0) for i in range(int((t1 - t0) / 0.1))]
    p2 = analysed(golden, energy={"samples": [], "mem_samples": mem})
    top = p2["memory"]["per_model"][0]
    assert top["device_peak_nvml_mb"] == 7300.0 and top["total_peak_mb"] == 7300.0 and "NVML" in top["total_basis"]


def test_mock_session_memory_is_not_measured_and_the_vram_limit_is_no_data(golden):
    p = copy.deepcopy(golden["payload"])
    p["environment"]["provider"] = "mock"
    analyses.add_analyses(p, db_path=golden["plan"]["db_path"], run_id=golden["run_id"],
                          run_dir=golden["run"], config_data=golden["cfg"])
    assert all(m["status"] == "not measured" for m in p["memory"]["per_model"])
    d = constraints.evaluate_constraints(p, {"max_peak_vram_mb": 1e9})
    assert all(m["result"] == "no_data" for m in d["per_model"])


def test_constraint_rule_is_on_total_peak():
    rb = constraints.load_rules()
    assert rb["inputs"]["max_peak_vram_mb"]["label"] == "Max total peak VRAM"
    assert "Total peak" in next(r for r in rb["rules"] if r["id"] == "peak_vram")["description"]


# ---------------------------------------------------------------------------
# 2. energy: whole board and net of idle
# ---------------------------------------------------------------------------
def _separate(monkeypatch, golden):
    """The mock models ran ~2 s apart; give them well-separated windows so each idle sample
    sits only before its own model's run."""
    sep = {m: (10_000.0 + 1000 * i, 10_020.0 + 1000 * i) for i, m in enumerate(golden["labels"])}
    monkeypatch.setattr(analyses, "model_windows", lambda sr, label_of: dict(sep))
    return sep


def test_energy_net_of_idle(golden, monkeypatch):
    w = _separate(monkeypatch, golden)
    labels = golden["labels"]
    samples, idle = [], {}
    for m, (a, b) in w.items():                                    # 30 W idle just before, 80 W during
        idle[labels[m]] = {"t0": a - 6, "t1": a - 1}
        samples += [(a - 6 + i * 0.5, 30.0) for i in range(11)]
        samples += [(a - 0.5 + i * 0.05, 80.0) for i in range(int((b - a + 1) / 0.05) + 1)]
    p = analysed(golden, price={"usd_per_hour": 1.0, "source": "config"},
                 energy={"method": "nvml", "interval_s": 0.5, "samples": sorted(samples), "idle": idle})
    for r in p["cost_energy"]["per_model"]:
        assert r["energy_status"] == "measured" and r["idle_power_w"] == pytest.approx(30.0)
        assert r["wh_per_flow"] == pytest.approx(80.0 * r["window_s"] / 3600 / r["n_flows"], rel=1e-6)
        assert r["wh_per_flow_net_idle"] == pytest.approx(50.0 * r["window_s"] / 3600 / r["n_flows"], rel=1e-6)
        assert r["wh_per_1k_flows_net_idle"] == pytest.approx(r["wh_per_flow_net_idle"] * 1000)
    assert p["cost_energy"]["energy_sampling"]["idle_windows"] == 2
    assert "idle" in p["cost_energy"]["notes"]["energy_net"]
    # no idle window -> whole-board figure only, with the reason
    p2 = analysed(golden, energy={"method": "nvml", "interval_s": 0.5, "samples": sorted(samples)})
    r = p2["cost_energy"]["per_model"][0]
    assert r["wh_per_flow"] is not None and r["wh_per_flow_net_idle"] is None and "idle" in r["idle_reason"]


def test_power_sampler_idle_window_and_memory(monkeypatch):
    from agentmeter.session.power import PowerSampler
    fake = types.SimpleNamespace(nvmlInit=lambda: None, nvmlDeviceGetHandleByIndex=lambda i: "h",
                                 nvmlDeviceGetPowerUsage=lambda h: 31000,
                                 nvmlDeviceGetMemoryInfo=lambda h: types.SimpleNamespace(used=512 * 1024 * 1024))
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    s = PowerSampler(interval_s=0.05)
    assert s.start()
    s.idle_window("mock:m", 0.3)
    r = s.stop()
    assert set(r["idle"]) == {"mock:m"} and r["idle"]["mock:m"]["t1"] - r["idle"]["mock:m"]["t0"] >= 0.29
    assert r["mem_samples"] and {mb for _, mb in r["mem_samples"]} == {512.0}
    idle = [w for t, w in r["samples"] if r["idle"]["mock:m"]["t0"] <= t <= r["idle"]["mock:m"]["t1"]]
    assert len(idle) >= 3


def test_run_full_calls_before_model_once_per_model_while_idle(tmp_path):
    from agentmeter.ingest.run import process_csv
    from agentmeter.run.runner import run_full
    process_csv(SAMPLE_CSV, name="c", out_root=tmp_path, max_flows=3, max_rows=500_000)
    plan = prepare_session(tmp_path / "c", MODELS, provider="mock")
    calls = []
    run_full(config_path=plan["config_path"], auto_analyze=False, before_model=calls.append)
    assert calls == [f"mock:{m}" for m in MODELS]


def test_ops_settings_idle(monkeypatch):
    from agentmeter.session.benchmark import ops_settings
    monkeypatch.delenv("AGENTMETER_IDLE_SAMPLE_S", raising=False)
    assert ops_settings()["idle_sample_s"] == 5.0
    monkeypatch.setenv("AGENTMETER_IDLE_SAMPLE_S", "0")
    assert ops_settings()["idle_sample_s"] == 0.0


# ---------------------------------------------------------------------------
# 3. session rate limits
# ---------------------------------------------------------------------------
def test_limits_come_from_config_with_env_override(monkeypatch):
    from agentmeter.server import access
    monkeypatch.delenv("AGENTMETER_JOBS_PER_HOUR", raising=False)
    monkeypatch.delenv("AGENTMETER_MAX_QUEUED_JOBS", raising=False)
    lim = access.limits()
    assert (lim["sessions_per_hour"], lim["max_queued_sessions"]) == (12, 4)
    assert lim["source"] == {"sessions_per_hour": "config service.sessions_per_hour",
                             "max_queued_sessions": "config service.max_queued_sessions"}
    monkeypatch.setenv("AGENTMETER_JOBS_PER_HOUR", "30")
    assert access.limits()["sessions_per_hour"] == 30 and access.limits()["source"]["sessions_per_hour"].startswith("env")


def _app(tmp_path, monkeypatch, autostart=True, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("serve_rl", PROJECT_ROOT / "scripts" / "serve.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results", gpu_available=lambda: False,
                     autostart=autostart)
    return srv.create_app(local_results_dir=str(tmp_path / "results"), job_manager=mgr).test_client(), mgr


def _prepare(c):
    return c.post("/api/prepare", data={"file": (io.BytesIO(SAMPLE_CSV.read_bytes()), "s.csv"), "max_flows": "3"},
                  content_type="multipart/form-data")


def test_a_wizard_session_counts_once(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    c, mgr = _app(tmp_path, monkeypatch, autostart=False, AGENTMETER_JOBS_PER_HOUR="2", AGENTMETER_MAX_QUEUED_JOBS="10")
    for _ in range(2):                                             # 2 sessions = 2 x (prepare + benchmark)
        p = _prepare(c)
        assert p.status_code == 202
        b = c.post("/api/jobs", json={"after_prepare": p.get_json()["job_id"], "models": MODELS[:1]})
        assert b.status_code == 202, b.get_json()
    last = p.get_json()["job_id"]
    assert _prepare(c).status_code == 429                           # a 3rd session is over the limit
    again = c.post("/api/jobs", json={"after_prepare": last, "models": MODELS[:1]})
    assert again.status_code == 429 and again.get_json()["code"] == "rate_limited"   # 2nd benchmark on one prepare counts


def test_queue_cap_counts_sessions(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    c, mgr = _app(tmp_path, monkeypatch, autostart=False, AGENTMETER_MAX_QUEUED_JOBS="1", AGENTMETER_JOBS_PER_HOUR="50")
    p = _prepare(c)
    b = c.post("/api/jobs", json={"after_prepare": p.get_json()["job_id"], "models": MODELS[:1]})
    assert b.status_code == 202                                     # same session: not blocked by the cap
    r = _prepare(c)
    assert r.status_code == 429 and r.get_json()["code"] == "queue_full"


# ---------------------------------------------------------------------------
# UI + PDF labels
# ---------------------------------------------------------------------------
def test_ui_labels():
    for hook in ("excludes model weights", "Total peak VRAM", "svc-memory-card", 'value="total_vram"',
                 "Agentic overhead (estimate)", "an estimate, not a tokenizer count", "net of idle",
                 "svc-energy-method", "body.after_prepare = WIZ.prep"):
        assert hook in WEB, hook
    assert "Peak VRAM</th>" not in WEB


def test_pdf_labels(golden, monkeypatch):
    pypdf = pytest.importorskip("pypdf")
    from agentmeter.session.pdf_report import build_report_pdf
    _write_memory(golden)
    w = _separate(monkeypatch, golden)
    labels = golden["labels"]
    samples, idle = [], {}
    for m, (a, b) in w.items():
        idle[labels[m]] = {"t0": a - 6, "t1": a - 1}
        samples += [(a - 6 + i * 0.5, 30.0) for i in range(11)] + [(a - 0.5 + i * 0.05, 80.0) for i in range(int((b - a + 1) / 0.05) + 1)]
    p = analysed(golden, energy={"method": "nvml", "interval_s": 0.5, "samples": sorted(samples), "idle": idle})
    text = " ".join(" ".join(pg.extract_text() for pg in pypdf.PdfReader(io.BytesIO(build_report_pdf(p))).pages).split())
    for needle in ("Working VRAM (excl. weights)", "Total peak VRAM", "GPU memory", "6,800 MB", "net of idle",
                   "agentic overhead (estimate)", "Agentic overhead is an estimate", "Method."):
        assert needle in text, needle
