"""Phase 43b §1: the two-step service — Prepare (async job) -> prepared set -> Benchmark.

Prepare runs on the Phase 40 job layer; the upload request only saves the file
and queues a job. A prepared set is downloadable (features.csv, labels.csv,
manifest.json) and can be benchmarked by id or re-uploaded. CPU / mock only.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import threading

import pandas as pd
import pytest
import yaml

from agentmeter.config import PROJECT_ROOT
from agentmeter.ingest.feature_map import CIC_FEATURES
from agentmeter.server.jobs import JobManager

SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
SAMPLE_PCAP = PROJECT_ROOT / "data" / "sample_pcaps" / "sample_small.pcap"
DATASET = PROJECT_ROOT / "data" / "cicids_full_300.csv"
DATASET_SHA256 = "ef1b13787e2c87602033f54d985355515408b4f930c9648cd1fb29cb540392b5"


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _app(tmp_path, monkeypatch, mgr=None):
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
    mgr = mgr or JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results",
                            gpu_available=lambda: False)
    return srv.create_app(local_results_dir=str(tmp_path / "results"), job_manager=mgr).test_client(), mgr


@pytest.fixture
def env(tmp_path, monkeypatch):
    c, mgr = _app(tmp_path, monkeypatch)
    return c, mgr, tmp_path


def prepare(c, mgr, path, **form):
    r = c.post("/api/prepare", data={"file": (io.BytesIO(path.read_bytes()), path.name), **form},
               content_type="multipart/form-data")
    assert r.status_code == 202, r.get_json()
    job = mgr.wait(r.get_json()["job_id"], timeout=120)
    assert job["status"] == "done", job
    return job


# ---------------------------------------------------------------------------
# the upload request only saves + queues
# ---------------------------------------------------------------------------
def test_upload_request_only_saves_the_file_and_returns_a_job(tmp_path, monkeypatch):
    idle = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results",
                      gpu_available=lambda: False, autostart=False)
    c, _ = _app(tmp_path, monkeypatch, mgr=idle)
    r = c.post("/api/prepare", data={"file": (io.BytesIO(SAMPLE_CSV.read_bytes()), SAMPLE_CSV.name),
                                     "max_flows": "12"}, content_type="multipart/form-data")
    j = r.get_json()
    assert r.status_code == 202 and j["kind"] == "prepare" and j["status"] == "queued"
    assert j["status_url"] == f"/api/jobs/{j['job_id']}" and "path" not in j["source"]
    assert j["source"]["sha256"] == _sha(SAMPLE_CSV) and j["source"]["size_bytes"] == SAMPLE_CSV.stat().st_size
    assert (tmp_path / "results" / "uploads").exists()                      # file saved ...
    assert not (tmp_path / "results" / "csv_runs").exists()                 # ... nothing ingested yet
    assert c.get(f"/api/jobs/{j['job_id']}").get_json()["progress"]["phase"] == "queued"


@pytest.mark.parametrize("form, code", [
    ({}, "invalid_file"),
    ({"url": "http://example.org/x.pcap"}, "bad_url"),
    ({"url": "ftp://example.org/x.pcap"}, "bad_url"),
    ({"url": "https://example.org/x.pcap", "max_flows": "0"}, "bad_request"),
])
def test_prepare_request_validation(env, form, code):
    c, _, _ = env
    r = c.post("/api/prepare", data=form, content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["code"] == code


def test_bad_file_fails_the_job_with_a_reason_and_is_resumable(env):
    c, mgr, _ = env
    r = c.post("/api/prepare", data={"file": (io.BytesIO(b"a,b\n1,2\n"), "bad.csv")},
               content_type="multipart/form-data")
    job = mgr.wait(r.get_json()["job_id"], timeout=60)
    assert job["status"] == "failed" and "none of the 78" in job["error"]
    assert job["error_code"] == "invalid_file" and "bad.csv" in job["error"]
    again = c.post(f"/api/jobs/{job['job_id']}/resume")
    assert again.status_code == 202 and again.get_json()["status"] == "queued"


# ---------------------------------------------------------------------------
# the prepared set
# ---------------------------------------------------------------------------
def test_labelled_csv_prepared_set_files_and_manifest(env):
    c, mgr, tmp = env
    job = prepare(c, mgr, SAMPLE_CSV, max_flows="12", other_attack="1")
    sid = job["prepared"]
    assert sid.startswith("csv_runs/") and job["progress"]["phase"] == "done"
    res = c.get(f"/api/jobs/{job['job_id']}/result").get_json()
    assert res["prepared"] == sid and res["summary"]["rows_selected"] == 12
    s = c.get(f"/api/prepared/{sid}").get_json()
    assert s["prepared_set"] == sid and s["downloads"] == ["features.csv", "labels.csv", "manifest.json"]

    feats = c.get(f"/api/prepared/{sid}/features.csv")
    assert feats.status_code == 200 and feats.mimetype == "text/csv"
    fdf = pd.read_csv(io.BytesIO(feats.data))
    assert len(fdf) == 12 and all(col in fdf.columns for col in CIC_FEATURES)
    assert not any(col.lower() == "label" for col in fdf.columns)           # label isolation
    ldf = pd.read_csv(io.BytesIO(c.get(f"/api/prepared/{sid}/labels.csv").data))
    assert list(ldf.columns) == ["flow_id", "label_raw", "label"]
    assert list(ldf["flow_id"]) == list(fdf["flow_id"])

    m = json.loads(c.get(f"/api/prepared/{sid}/manifest.json").data)["prepared_set"]
    assert m["id"] == sid and m["schema"] == 1 and "not an analysis" in m["framing"]
    assert m["source"] == {"kind": "upload", "filename": SAMPLE_CSV.name,
                           "size_bytes": SAMPLE_CSV.stat().st_size, "sha256": _sha(SAMPLE_CSV)}
    assert m["input_type"] == "csv" and m["evaluation_mode"] == "accuracy_available"
    assert m["limits"]["max_csv_rows"] and m["sampling"]["method"] == "full_file"
    sel = m["selection"]
    assert sel["selected"] == 12 and "class_balance" in sel["rules_fired"]
    assert {r["id"] for r in sel["rules"]} >= {"class_balance"} and all("admitted" in r for r in sel["rules"])
    cc = m["class_counts"]
    assert cc["class_set"][-1] == "Other Attack" and sum(cc["selected"].values()) == 12
    assert cc["in_pool"]["Other Attack"] == 2 and cc["lost_in_selection"] == []
    assert m["files"]["features.csv"]["sha256"] == hashlib.sha256(feats.data).hexdigest()
    assert m["started_at"] and m["finished_at"] and m["tool_versions"]["python"] and m["tool_versions"]["pandas"]
    assert m["input_metadata"]["rows_selected"] == 12
    for bad in ("../input.json", "selected_flows.csv", "session.db"):
        assert c.get(f"/api/prepared/{sid}/{bad}").status_code in (400, 404)


def test_pcap_prepared_set_has_no_labels(env):
    pytest.importorskip("scapy")
    pytest.importorskip("cicflowmeter")
    c, mgr, _ = env
    sid = prepare(c, mgr, SAMPLE_PCAP, max_flows="8")["prepared"]
    s = c.get(f"/api/prepared/{sid}").get_json()
    assert sid.startswith("pcap_runs/") and s["downloads"] == ["features.csv", "manifest.json"]
    assert s["accuracy_available"] is False and s["rows_selected"] == 8
    m = json.loads(c.get(f"/api/prepared/{sid}/manifest.json").data)["prepared_set"]
    assert m["class_counts"] == {"available": False, "reason": "no ground-truth labels in this input"}
    assert m["sampling"]["method"] == "full_capture" and m["sampling"]["packets_in_capture"] == 325


def test_benchmark_by_id_then_pdf(env):
    before = {str(p): _sha(p) for p in (PROJECT_ROOT / "results").glob("*.db")}
    c, mgr, _ = env
    sid = prepare(c, mgr, SAMPLE_CSV, max_flows="6")["prepared"]
    cfg = c.get("/api/service/config").get_json()
    j = c.post("/api/jobs", json={"run": sid, "models": cfg["canonical_models"][:2], "provider": "mock"})
    assert j.status_code == 202
    jid = j.get_json()["job_id"]
    assert mgr.wait(jid, timeout=120)["status"] == "done"
    assert c.get(f"/api/jobs/{jid}/report.pdf").data[:5] == b"%PDF-"
    kinds = {x["job_id"]: x["kind"] for x in c.get("/api/jobs").get_json()["jobs"]}
    assert kinds[jid] == "benchmark" and "prepare" in kinds.values()
    assert {str(p): _sha(p) for p in (PROJECT_ROOT / "results").glob("*.db")} == before
    assert _sha(DATASET) == DATASET_SHA256


# ---------------------------------------------------------------------------
# re-upload of a prepared set
# ---------------------------------------------------------------------------
def _download(c, sid):
    return {f: c.get(f"/api/prepared/{sid}/{f}").data for f in ("manifest.json", "features.csv", "labels.csv")}


def _import(c, files):
    data = {}
    for field, fname in (("manifest", "manifest.json"), ("features", "features.csv"), ("labels", "labels.csv")):
        if files.get(fname) is not None:
            data[field] = (io.BytesIO(files[fname]), fname)
    return c.post("/api/prepared/import", data=data, content_type="multipart/form-data")


def test_reuploaded_prepared_set_benchmarks_like_the_original(env):
    c, mgr, _ = env
    sid = prepare(c, mgr, SAMPLE_CSV, max_flows="6")["prepared"]
    r = _import(c, _download(c, sid))
    assert r.status_code == 201, r.get_json()
    s = r.get_json()
    new = s["prepared_set"]
    assert new != sid and new.startswith("csv_runs/") and s["imported"]["from_prepared_set"] == sid
    assert s["rows_selected"] == 6 and s["accuracy_available"] is True
    j = c.post("/api/jobs", json={"run": new, "models": ["mistralai/Mistral-7B-Instruct-v0.3"], "provider": "mock"})
    assert j.status_code == 202 and mgr.wait(j.get_json()["job_id"], timeout=120)["status"] == "done"


def _tamper(files, **changes):
    out = dict(files)
    out.update(changes)
    return out


@pytest.mark.parametrize("case, msg", [
    ("no_manifest", "missing manifest.json"),
    ("no_features", "missing features.csv"),
    ("no_labels", "upload its labels.csv too"),
    ("not_json", "not valid JSON"),
    ("wrong_schema", "not an AgentMeter prepared-set manifest"),
    ("edited_features", "sha256 differs"),
    ("label_in_features", "contains a label column"),
    ("wrong_columns", "does not have the prepared-set columns"),
    ("labels_misaligned", "sha256 differs"),
])
def test_malformed_prepared_set_is_rejected(env, case, msg):
    c, mgr, _ = env
    sid = prepare(c, mgr, SAMPLE_CSV, max_flows="6")["prepared"]
    f = _download(c, sid)
    man = json.loads(f["manifest.json"])
    df = pd.read_csv(io.BytesIO(f["features.csv"]))

    def unhashed(m):                   # a manifest without file hashes, to reach the deeper checks
        m = json.loads(json.dumps(m))
        m["prepared_set"]["files"] = {}
        return json.dumps(m).encode()
    variants = {
        "no_manifest": _tamper(f, **{"manifest.json": None}),
        "no_features": _tamper(f, **{"features.csv": None}),
        "no_labels": _tamper(f, **{"labels.csv": None}),
        "not_json": _tamper(f, **{"manifest.json": b"{not json"}),
        "wrong_schema": _tamper(f, **{"manifest.json": json.dumps({"prepared_set": {"schema": 99}}).encode()}),
        "edited_features": _tamper(f, **{"features.csv": f["features.csv"].replace(b"row", b"rox", 1)}),
        "label_in_features": _tamper(f, **{"manifest.json": unhashed(man),
                                           "features.csv": df.assign(Label="x").to_csv(index=False).encode()}),
        "wrong_columns": _tamper(f, **{"manifest.json": unhashed(man),
                                       "features.csv": df.drop(columns=[CIC_FEATURES[0]]).to_csv(index=False).encode()}),
        "labels_misaligned": _tamper(f, **{"labels.csv": f["labels.csv"].replace(b"row", b"rox", 1)}),
    }
    r = _import(c, variants[case])
    assert r.status_code == 400 and r.get_json()["code"] == "invalid_prepared_set"
    assert msg in r.get_json()["error"]


def test_import_contract_check_catches_misaligned_labels_without_hashes(env):
    c, mgr, _ = env
    sid = prepare(c, mgr, SAMPLE_CSV, max_flows="6")["prepared"]
    f = _download(c, sid)
    man = json.loads(f["manifest.json"])
    man["prepared_set"]["files"] = {}
    lab = pd.read_csv(io.BytesIO(f["labels.csv"])).iloc[::-1]
    r = _import(c, {**f, "manifest.json": json.dumps(man).encode(), "labels.csv": lab.to_csv(index=False).encode()})
    assert r.status_code == 400 and "align" in r.get_json()["error"]


def test_benchmark_rejects_an_unknown_prepared_set(env):
    c, _, _ = env
    r = c.post("/api/jobs", json={"run": "csv_runs/nope", "models": ["mistralai/Mistral-7B-Instruct-v0.3"]})
    assert r.status_code == 404 and r.get_json()["code"] == "run_missing"
    assert c.get("/api/prepared/csv_runs/..").status_code in (400, 404)
    assert c.get("/api/prepared/etc/passwd").status_code == 400


# ---------------------------------------------------------------------------
# single-job lock: prepare and benchmark share the one worker
# ---------------------------------------------------------------------------
def test_prepare_waits_behind_a_running_benchmark(tmp_path, monkeypatch):
    gate, started = threading.Event(), threading.Event()

    def slow_benchmark(job):
        started.set()
        gate.wait(30)
        raise RuntimeError("stopped by the test")
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results",
                     runner=slow_benchmark, gpu_available=lambda: False)
    c, _ = _app(tmp_path, monkeypatch, mgr=mgr)
    first = prepare(c, mgr, SAMPLE_CSV, max_flows="5")["prepared"]
    b = c.post("/api/jobs", json={"run": first, "models": ["mistralai/Mistral-7B-Instruct-v0.3"], "provider": "mock"})
    assert started.wait(10)
    r = c.post("/api/prepare", data={"file": (io.BytesIO(SAMPLE_CSV.read_bytes()), SAMPLE_CSV.name)},
               content_type="multipart/form-data")
    pj = r.get_json()["job_id"]
    st = c.get(f"/api/jobs/{pj}").get_json()
    assert st["status"] == "queued" and st["queue_position"] == 1       # behind the benchmark
    gate.set()
    assert mgr.wait(pj, timeout=60)["status"] == "done"
    assert mgr.wait(b.get_json()["job_id"], timeout=10)["status"] == "failed"


def test_entry_urls_redirect_to_the_steps(env):
    c, _, _ = env
    # Phase 46: one app — the old step URLs open the New-benchmark wizard
    assert c.get("/service/prepare").headers["Location"].endswith("/#/new")
    assert c.get("/service/benchmark").headers["Location"].endswith("/#/new")
    html = c.get("/service").get_data(as_text=True)
    assert html == c.get("/").get_data(as_text=True)
    for hook in ("view-new", "wiz-step-data", "wiz-step-models", "wiz-step-run", "tab-src-url", "svc-url",
                 "tab-src-reuse", "svc-import-form", "svc-bench-id", "svc-downloads", "New benchmark",
                 "after_prepare"):
        assert hook in html, hook
