"""Phase 42: PDF benchmark report for a finished service run.

  * brief.py is a faithful mirror of dashboard/report.js recommendation() and
    optimisationHint(): checked against report.js under node (skips without node).
  * GET /api/jobs/<id>/report.pdf returns a real PDF whose text carries the input
    summary, rule-base, per-metric winners, verdict, recommendation and caveats,
    with numbers taken verbatim from session_results.json.
  * Unlabelled (PCAP) -> efficiency only, no accuracy section. Unfinished -> 409.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess

import pytest
import yaml

from agentmeter.config import PROJECT_ROOT
from agentmeter.server.jobs import JobManager
from agentmeter.session import brief

pytest.importorskip("reportlab")
pypdf = pytest.importorskip("pypdf")

from agentmeter.session.pdf_report import (_safe, build_report_pdf, fmt_int, fmt_pct,  # noqa: E402
                                           fmt_time, recommendation_lines)

SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
SAMPLE_PCAP = PROJECT_ROOT / "data" / "sample_pcaps" / "sample_small.pcap"
DATASET = PROJECT_ROOT / "data" / "cicids_full_300.csv"
DATASET_SHA256 = "ef1b13787e2c87602033f54d985355515408b4f930c9648cd1fb29cb540392b5"
REPORT_JS = PROJECT_ROOT / "dashboard" / "report.js"
NODE = shutil.which("node")


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def pdf_text(data: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(data))
    return re.sub(r"\s+", " ", " ".join(page.extract_text() for page in reader.pages))


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    pytest.importorskip("flask")
    root = tmp_path_factory.mktemp("svc")
    from agentmeter import pull_eval
    spec = importlib.util.spec_from_file_location("pull_eval_server",
                                                  PROJECT_ROOT / "scripts" / "pull_eval_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    base = root / "cfg.yaml"
    base.write_text(yaml.safe_dump({"run": {"models": ["mock/m"]}, "model": {"provider": "mock"},
                                    "dataset": {}, "pipeline": {}, "classes": [], "scoring": {},
                                    "storage": {}}))
    pmgr = pull_eval.JobManager(base_config=str(base), canonical_json=str(root / "c.json"),
                                out_dir=str(root / "pulls"))
    mgr = JobManager(jobs_dir=root / "jobs", results_root=root / "results", gpu_available=lambda: False)
    client = srv.create_app(pmgr, local_results_dir=str(root / "results"), job_manager=mgr).test_client()
    return client, mgr


def finished_job(service, path, max_flows, models):
    c, mgr = service
    data = {"file": (io.BytesIO(path.read_bytes()), path.name), "max_flows": str(max_flows)}
    run = c.post("/api/ingest", data=data, content_type="multipart/form-data").get_json()["run"]
    jid = c.post("/api/jobs", json={"run": run, "models": models, "provider": "mock"}).get_json()["job_id"]
    mgr.wait(jid, timeout=120)
    return jid, c.get(f"/api/jobs/{jid}/result").get_json()


@pytest.fixture(scope="module")
def labelled(service):
    before = {str(p): _sha(p) for p in (PROJECT_ROOT / "results").glob("*.db")}
    jid, res = finished_job(service, SAMPLE_CSV, 12,
                            ["mistralai/Mistral-7B-Instruct-v0.3", "microsoft/Phi-3-mini-4k-instruct"])
    r = service[0].get(f"/api/jobs/{jid}/report.pdf")
    return jid, res, r, before


# --- endpoint + content (labelled) ----------------------------------------------------------------
def test_labelled_report_is_a_pdf_with_every_section(labelled):
    jid, res, r, _ = labelled
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert f'agentmeter_report_{jid}.pdf' in r.headers["Content-Disposition"]
    assert r.data[:5] == b"%PDF-"
    t = pdf_text(r.data)
    for section in ("AgentMeter — Benchmark Report", "Input", "Flow selection (rule-base)",
                    "Models evaluated", "Per-metric comparison", "Verdict", "Recommendation",
                    "Accuracy by class", "Caveats"):
        assert section in t, section
    assert jid in t and res["session"]["run_id"] in t
    assert "Labelled -> accuracy + efficiency" in t
    assert "DEMO RUN — mock provider" in t and "NON-VALIDATED" in t
    assert "class_balance" in t and "fill_remaining" in t                  # the rule-base is shown


def test_numbers_match_session_results_verbatim(labelled):
    _, res, r, _ = labelled
    t = pdf_text(r.data)
    s = res["session"]
    assert f"Flows selected and benchmarked {fmt_int(s['n_flows'])}" in t
    for m in res["per_model"]:
        e = m["efficiency"]["end_to_end"]
        assert fmt_time(e["mean_latency_s"]) in t
        assert fmt_int(e["mean_tokens_per_flow"]) in t
        assert fmt_pct(m["accuracy"]["accuracy"]) in t
        assert f"{m['saw']['composite_100']} {m['saw']['tier']}" in t      # SAW composite verbatim
        assert str(m["saw_efficiency_only"]["composite_100"]) in t
    # per-metric winners are the session's own (tie / winner + how much)
    for m in res["relative_comparison"]["metrics"]:
        if m["available"] and m["winner"] == "tie":
            assert "tie" in t
    # verdict + absolute statement are the session's sentences, not re-derived
    assert _safe(res["relative_comparison"]["verdict"])[:60] in t
    assert "Absolute SAW" in t and _safe(res["comparison"]["statement"])[:40] in t
    # every caveat is carried
    for c in res["relative_comparison"]["caveats"]:
        assert _safe(c)[:50] in t, c


def test_recommendation_is_honest_about_accuracy_and_ties(labelled):
    _, res, r, _ = labelled
    t = pdf_text(r.data)
    rec = recommendation_lines(res)
    for line in rec["lines"]:
        assert _safe(line)[:50] in t
    if res["relative_comparison"]["efficiency_verdict"]["verdict"] == "tie":
        assert "equally resource-efficient" in t
        assert "best resource-efficient starting point" not in t           # no arbitrary leader
    assert "NOT an endorsement" in t or "decision support" in t
    assert "not a threat-detection product" in t


def test_unfinished_job_gets_a_clear_error(service, tmp_path):
    c, _ = service
    idle = JobManager(jobs_dir=tmp_path / "jobs", results_root=service[1].results_root, autostart=False)
    run = next(iter((service[1].results_root / "csv_runs").iterdir())).name
    j = idle.create_job(f"csv_runs/{run}", ["mock-a"], provider="mock")
    # point the app at the idle manager by using its API directly through a second client
    from agentmeter.server.jobs import JobError
    with pytest.raises(JobError) as e:
        idle.result(j["job_id"])
    assert e.value.code == "not_ready" and e.value.status == 409
    r = c.get("/api/jobs/job_20990101_000000_abcdef/report.pdf")
    assert r.status_code == 404 and r.get_json()["code"] == "not_found"


def test_report_endpoint_returns_409_while_running(service):
    import threading

    c, mgr = service
    gate = threading.Event()
    real = mgr.runner
    mgr.runner = lambda job: (gate.wait(30), real(job))[1]
    try:
        run = next(iter((mgr.results_root / "csv_runs").iterdir())).name
        jid = c.post("/api/jobs", json={"run": f"csv_runs/{run}", "models": ["mock-a"],
                                        "provider": "mock"}).get_json()["job_id"]
        r = c.get(f"/api/jobs/{jid}/report.pdf")
        assert r.status_code == 409 and r.get_json()["code"] == "not_ready"
        assert "results are available when it is done" in r.get_json()["error"]
    finally:
        gate.set()
        mgr.runner = real
    mgr.wait(jid, timeout=60)


# --- unlabelled PCAP: efficiency only --------------------------------------------------------------
def test_pcap_report_is_efficiency_only(service):
    pytest.importorskip("scapy")
    pytest.importorskip("cicflowmeter")
    jid, res = finished_job(service, SAMPLE_PCAP, 8, ["Qwen/Qwen2.5-7B-Instruct", "google/gemma-2-9b-it"])
    r = service[0].get(f"/api/jobs/{jid}/report.pdf")
    assert r.status_code == 200
    t = pdf_text(r.data)
    assert "Unlabelled -> efficiency only" in t
    assert "Accuracy by class" not in t and "accuracy on this sample" not in t
    assert "No ground-truth labels: accuracy was not measured" in t
    assert "Accuracy: not available (no ground-truth labels)" in t        # the session's own verdict
    assert "PCAP features are approximate" in t
    assert "SAW (eff.)" in t                                              # efficiency-only SAW labelled


# --- single model + robustness ----------------------------------------------------------------------
def test_single_model_and_unicode_are_handled(labelled):
    _, res, _, _ = labelled
    one = copy.deepcopy(res)
    one["per_model"] = one["per_model"][:1]
    one["relative_comparison"] = None
    one["comparison"] = None
    one["session"]["caveats"] = one["session"]["caveats"] + ["odd chars: ≥ → � ≠"]
    t = pdf_text(build_report_pdf(one))
    assert "Single-model run" in t and "odd chars: >= -> ? !=" in t


def test_locked_study_untouched(labelled):
    _, _, _, before = labelled
    assert {str(p): _sha(p) for p in (PROJECT_ROOT / "results").glob("*.db")} == before
    assert _sha(DATASET) == DATASET_SHA256


# --- parity: brief.py == report.js ---------------------------------------------------------------------
_DRIVER = r"""
const REPORT = require(process.argv[2]);
const cases = JSON.parse(require('fs').readFileSync(process.argv[3], 'utf8'));
const out = cases.map(c => ({
  rec: REPORT.recommendation(c.data, c.ranked, c.weights),
  hint: REPORT.optimisationHint(c.data, c.model),
}));
process.stdout.write(JSON.stringify(out));
"""


def _cases(res):
    unl = copy.deepcopy(res)
    unl["phase8"]["weights"] = {"accuracy": 0.0, "latency": 0.4166666666666667,
                                "vram": 0.3333333333333333, "tokens": 0.25}
    mixed = copy.deepcopy(res)
    mixed["phase8"]["saw_table"][0]["tier"] = "Healthy"
    mixed["phase8"]["targets"]["accuracy_pct"] = 75.5
    mixed["per_agent"]["dominant"][0]["vram_dominant_agent"] = "reason"
    first = res["phase8"]["saw_table"][0]["model"]
    last = res["phase8"]["saw_table"][-1]["model"]
    return [
        {"data": res, "ranked": None, "weights": None, "model": None},
        {"data": res, "ranked": [{"model": last}, {"model": first}], "weights": None, "model": last},
        {"data": res, "ranked": [{"model": None}], "weights": None, "model": None},
        {"data": unl, "ranked": None, "weights": None, "model": first},
        {"data": mixed, "ranked": None, "weights": {"tokens": 2, "vram": 1}, "model": "nobody"},
        {"data": {"phase8": {}}, "ranked": None, "weights": None, "model": None},
    ]


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_brief_py_matches_report_js(labelled, tmp_path):
    _, res, _, _ = labelled
    cases = _cases(res)
    (tmp_path / "cases.json").write_text(json.dumps(cases))
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER)
    js = json.loads(subprocess.run([NODE, str(driver), str(REPORT_JS), str(tmp_path / "cases.json")],
                                   check=True, capture_output=True, text=True).stdout)
    for c, j in zip(cases, js):
        py_rec = brief.recommendation(c["data"], c["ranked"], c["weights"])
        py_hint = brief.optimisation_hint(c["data"], c["model"])
        for k in ("headline", "caveat", "top", "allCritical"):
            assert py_rec.get(k) == j["rec"].get(k), (k, py_rec.get(k), j["rec"].get(k))
        if "accTarget" in j["rec"]:
            assert py_rec["accTarget"] == j["rec"]["accTarget"]
        assert py_hint == j["hint"]
