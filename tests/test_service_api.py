"""Phase 41: the service web flow's backend — upload/ingest, run summary, service
config, and the /service page — driven through the Flask test client (mock, CPU).

The full browser walk-through lives in tests/e2e/service_flow.js (Playwright,
run against a live server); these tests cover the same API contract in CI.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import time

import pytest

from svc import prepare  # noqa: E402
import yaml

from agentmeter.config import PROJECT_ROOT
from agentmeter.server.jobs import JobManager

SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
SAMPLE_PCAP = PROJECT_ROOT / "data" / "sample_pcaps" / "sample_small.pcap"
DATASET = PROJECT_ROOT / "data" / "cicids_full_300.csv"
DATASET_SHA256 = "ef1b13787e2c87602033f54d985355515408b4f930c9648cd1fb29cb540392b5"


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    monkeypatch.delenv("AGENTMETER_SERVICE_PROVIDER", raising=False)
    spec = importlib.util.spec_from_file_location("serve",
                                                  PROJECT_ROOT / "scripts" / "serve.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    base = tmp_path / "cfg.yaml"
    base.write_text(yaml.safe_dump({"run": {"models": ["mock/m"]}, "model": {"provider": "mock"},
                                    "dataset": {}, "pipeline": {}, "classes": [], "scoring": {},
                                    "storage": {}}))
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results",
                     gpu_available=lambda: False)
    client = srv.create_app(local_results_dir=str(tmp_path / "results"),
                            job_manager=mgr).test_client()
    return client, mgr, tmp_path


def upload(client, path, name=None, **form):
    data = {"file": (io.BytesIO(path.read_bytes()), name or path.name), **form}
    return prepare(client, data)


def test_service_page_and_config(env):
    c, _, _ = env
    page = c.get("/service")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    for hook in ("svc-upload-form", "svc-file", "svc-models", "svc-run", "svc-progress",
                 "view-session", "sessions-table", 'src="report.js"'):
        assert hook in html
    cfg = c.get("/api/service/config").get_json()
    assert cfg["max_models"] == 2 and len(cfg["canonical_models"]) == 5
    assert cfg["gpu_available"] is False and cfg["demo_mode"] is True and cfg["provider"] == "mock"
    assert "MOCK" in cfg["note"]
    assert c.get("/").status_code == 200                     # the app (Phase 46)
    assert c.get("/baseline").status_code == 200             # the locked study page: direct URL only


def test_csv_upload_gives_the_validation_summary(env):
    c, mgr, tmp = env
    r = upload(c, SAMPLE_CSV, max_flows="12")
    assert r.status_code == 201
    s = r.get_json()
    assert s["run"].startswith("csv_runs/cicids2017_sample_")
    assert (s["source_type"], s["evaluation_mode"], s["accuracy_available"]) == ("csv", "accuracy_available", True)
    assert (s["rows_read"], s["rows_usable"], s["rows_dropped"], s["rows_excluded_out_of_taxonomy"]) == (43, 40, 1, 2)
    assert s["class_distribution"] == {c_: 8 for c_ in ("Benign", "Brute Force", "DoS Hulk",
                                                        "Port Scanning", "Volumetric DDoS")}
    assert s["rows_selected"] == 12 and sum(s["label_distribution_selected"].values()) == 12
    assert s["feature_match"]["status"] == "exact" and s["columns_present"] == 78
    assert "class_balance" in s["rules_fired"] and {r_["id"] for r_ in s["rules"]} >= {"class_balance"}
    assert s["class_scheme"]["name"] == "5-class"
    # stored under the manager's results root + the upload kept for traceability
    name = s["run"].split("/")[1]
    assert (tmp / "results" / "csv_runs" / name / "input.json").exists()
    assert (tmp / "results" / "uploads" / f"{name}.csv").exists()
    # the same summary is re-readable after a reload
    again = c.get(f"/api/prepared/{s['run']}").get_json()
    assert again["rows_selected"] == 12 and again["run"] == s["run"]


def test_other_attack_option_makes_a_6_class_run(env):
    c, _, _ = env
    s = upload(c, SAMPLE_CSV, max_flows="12", other_attack="1").get_json()
    assert s["class_scheme"]["name"] == "6-class" and s["other_attack"]["rows"] == 2


def test_same_file_twice_gets_two_runs(env):
    c, _, _ = env
    a = upload(c, SAMPLE_CSV, max_flows="5").get_json()["run"]
    time.sleep(0.01)
    b = upload(c, SAMPLE_CSV, max_flows="5").get_json()["run"]
    assert a != b


@pytest.mark.parametrize("content, name, form, status, code, msg", [
    (b"a,b\n1,2\n", "bad.csv", {}, 400, "invalid_file", "none of the 78"),
    (b"key: value\n", "config.yaml", {}, 400, "invalid_file", "unsupported file type"),
    (b"", "empty.csv", {}, 400, "invalid_file", "empty"),
    (b"\xd4\xc3\xb2\xa1" + b"\x00\xff" * 30, "broken.pcap", {}, 400, "invalid_file", "corrupt"),
    (b"a,b\n1,2\n", "x.csv", {"max_flows": "0"}, 400, "bad_request", "between 1 and 500"),
    (b"a,b\n1,2\n", "x.csv", {"max_flows": "lots"}, 400, "bad_request", "whole number"),
])
def test_bad_uploads_are_rejected_with_a_reason(env, content, name, form, status, code, msg):
    pytest.importorskip("scapy") if name.endswith(".pcap") else None
    c, _, _ = env
    r = prepare(c, {"file": (io.BytesIO(content), name), **form})
    body = r.get_json()
    assert r.status_code == status and body["code"] == code and msg in body["error"]


def test_missing_file_and_bad_run_names(env):
    c, _, _ = env
    assert c.post("/api/prepare", data={}, content_type="multipart/form-data").get_json()["code"] == "invalid_file"
    assert c.get("/api/prepared/csv_runs/nope").status_code == 404
    assert c.get("/api/prepared/etc/passwd").status_code == 400
    assert c.get("/api/prepared/csv_runs/..%2F..").status_code in (400, 404)
    assert c.post("/api/ingest", data={}).status_code in (404, 405)       # legacy route removed
    assert c.get("/api/runs/csv_runs/nope").status_code == 404


def test_pcap_upload_is_efficiency_only(env):
    pytest.importorskip("scapy")
    pytest.importorskip("cicflowmeter")
    c, _, _ = env
    s = upload(c, SAMPLE_PCAP, max_flows="8").get_json()
    assert s["source_type"] == "pcap" and s["accuracy_available"] is False
    assert s["evaluation_mode"] == "efficiency_only" and s["class_distribution"] == {}
    assert s["flows_extracted"] == 41 and s["rows_selected"] == 8 and s["packets"] == 325
    assert s["feature_match"]["status"] == "approximate"
    assert any("no ground-truth labels" in x for x in s["accuracy_unavailable_reasons"])


def test_upload_then_run_through_the_job_api_end_to_end(env):
    before = {str(p): _sha(p) for p in (PROJECT_ROOT / "results").glob("*.db")}
    c, mgr, _ = env
    run = upload(c, SAMPLE_CSV, max_flows="6").get_json()["run"]
    cfg = c.get("/api/service/config").get_json()
    j = c.post("/api/jobs", json={"run": run, "models": cfg["canonical_models"][:2],
                                  "provider": cfg["provider"], "base_config": cfg["base_config"]})
    assert j.status_code == 202
    jid = j.get_json()["job_id"]
    mgr.wait(jid, timeout=120)
    st = c.get(f"/api/jobs/{jid}").get_json()
    assert st["status"] == "done" and st["result_ready"]
    res = c.get(f"/api/jobs/{jid}/result").get_json()
    assert res["session"]["accuracy_available"] is True
    assert res["relative_comparison"]["models"] == cfg["canonical_models"][:2]
    assert {str(p): _sha(p) for p in (PROJECT_ROOT / "results").glob("*.db")} == before
    assert _sha(DATASET) == DATASET_SHA256


def test_three_models_rejected_by_the_api_too(env):
    c, _, _ = env
    run = upload(c, SAMPLE_CSV, max_flows="5").get_json()["run"]
    r = c.post("/api/jobs", json={"run": run, "models": ["a", "b", "c"], "provider": "mock"})
    assert r.status_code == 400 and r.get_json()["code"] == "too_many_models"
