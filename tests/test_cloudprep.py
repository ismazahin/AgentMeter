"""Cloud data preparation (cloudprep/app.py): only signed jobs run; a job reads its input from a
single-use URL, prepares it with the server's own code (byte-identical data files), PUTs the
prepared set to single-use upload URLs and reports every step to the control plane, signed."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agentmeter.config import PROJECT_ROOT
from agentmeter.server import prepare as P

spec = importlib.util.spec_from_file_location("cloudprep_app", PROJECT_ROOT / "cloudprep" / "app.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)

SECRET = "test-cloud-prep-secret-0123456789"
SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
SAMPLE_PCAP = PROJECT_ROOT / "data" / "sample_pcaps" / "sample_small.pcap"
LIMITS = dict(max_csv_rows=500_000, max_pcap_packets=100_000, max_pcap_flows=20_000, pcap_windows=10)


class Stub:
    """Stands in for Supabase Storage (single-use GET/PUT URLs) and the control plane's report route."""

    def __init__(self):
        self.inputs: dict[str, bytes] = {}
        self.outputs: dict[str, bytes] = {}
        self.reports: list[dict] = []
        self.bad_reports = 0
        self.done = threading.Event()
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body=b"{}"):
                self.send_response(code)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                name = self.path.removeprefix("/in/")
                data = stub.inputs.get(name)
                self._reply(200, data) if data is not None else self._reply(404)

            def do_PUT(self):  # noqa: N802
                stub.outputs[self.path.removeprefix("/out/")] = self.rfile.read(int(self.headers["content-length"]))
                self._reply(200)

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["content-length"]))
                if not C.verify(SECRET, "POST", "/api/prep/jobs/j1/events", self.headers, body):
                    stub.bad_reports += 1
                    return self._reply(401)
                msg = json.loads(body)
                stub.reports.append(msg)
                if "done" in msg or "failed" in msg:
                    stub.done.set()
                self._reply(200)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def job(self, filename: str, **kw) -> dict:
        j = {"job_id": "j1", "filename": filename, "source": {"kind": "file", "url": f"{self.base}/in/{filename}"},
             "max_flows": 20, "other_attack": False, "limits": LIMITS, "max_bytes": 50 * 2 ** 20,
             "outputs": {n: f"{self.base}/out/{n}" for n in C.FILES},
             "report_url": f"{self.base}/api/prep/jobs/j1/events", "report_path": "/api/prep/jobs/j1/events"}
        j.update(kw)
        return j


@pytest.fixture
def service(tmp_path):
    svc = C.Service(SECRET, tmp_path / "work")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), C.make_handler(svc))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield svc, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def signed_post(base: str, body: bytes, secret: str = SECRET, ts: int | None = None) -> tuple[int, dict]:
    ts = str(int(time.time()) if ts is None else ts)
    req = urllib.request.Request(base + "/prepare", data=body, method="POST", headers={
        "content-type": "application/json", "X-AM-Timestamp": ts,
        "X-AM-Signature": C.sign(secret, "POST", "/prepare", ts, body)})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_signatures():
    body = b'{"a":1}'
    ts = str(int(time.time()))
    good = {"X-AM-Timestamp": ts, "X-AM-Signature": C.sign(SECRET, "POST", "/prepare", ts, body)}
    assert C.verify(SECRET, "POST", "/prepare", good, body)
    assert not C.verify(SECRET, "POST", "/prepare", good, body + b" ")             # body changed
    assert not C.verify("another-secret-0123456789", "POST", "/prepare", good, body)
    assert not C.verify(SECRET, "POST", "/prepare", good, body, now=int(ts) + 600)  # stale
    assert not C.verify(SECRET, "POST", "/prepare", {"X-AM-Timestamp": "x"}, body)


def test_only_signed_valid_jobs_are_accepted(service):
    _, base = service
    stub = Stub()
    body = json.dumps(stub.job("a.csv")).encode()
    assert signed_post(base, body, secret="wrong-secret-0123456789")[0] == 401
    assert signed_post(base, body, ts=int(time.time()) - 3600)[0] == 401
    status, data = signed_post(base, json.dumps({"job_id": "x"}).encode())
    assert status == 400 and "missing" in data["error"]
    bad_src = stub.job("a.csv", source={"kind": "file", "url": "file:///etc/passwd"})
    assert signed_post(base, json.dumps(bad_src).encode())[0] == 400
    assert urllib.request.urlopen(base + "/health").status == 200


@pytest.mark.parametrize("src", [SAMPLE_CSV, SAMPLE_PCAP], ids=["csv", "pcap"])
def test_a_job_prepares_like_the_server_and_reports_signed(service, tmp_path, src):
    if src.suffix == ".pcap":
        pytest.importorskip("scapy")
    _, base = service
    stub = Stub()
    stub.inputs[src.name] = src.read_bytes()
    status, data = signed_post(base, json.dumps(stub.job(src.name)).encode())
    assert status == 202 and data["accepted"]
    assert stub.done.wait(120), stub.reports
    final = stub.reports[-1]
    assert "done" in final, final
    assert stub.bad_reports == 0
    assert any(r.get("event", {}).get("phase") == "validating" for r in stub.reports)
    sid = final["done"]["set_name"]
    assert set(final["done"]["files"]) <= set(C.FILES) and "manifest.json" in final["done"]["files"]

    ref = P.prepare_file(src, source={"filename": src.name}, results_root=tmp_path / "s", name="ref",
                         max_flows=20, other_attack=False, limits=LIMITS)
    for f in ("features.csv", "labels.csv"):
        r = tmp_path / "s" / ref / f
        assert (f in stub.outputs) == r.exists(), f
        if r.exists():
            assert stub.outputs[f] == r.read_bytes(), f                  # byte-identical data
    man = json.loads(stub.outputs["manifest.json"])["prepared_set"]
    assert man["backend"]["provider"] == "cloud"
    assert man["source"]["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    ref_man = json.loads((tmp_path / "s" / ref / "manifest.json").read_text())["prepared_set"]
    assert man["content_sha256"] == ref_man["content_sha256"]            # same prepared-set hash

    files = {n: stub.outputs[n] for n in final["done"]["files"]}
    assert P.import_prepared(files, tmp_path / "gpu") == sid             # imported under its own name


def test_a_bad_file_is_reported_as_failed(service):
    _, base = service
    stub = Stub()
    stub.inputs["nolabel.csv"] = b"a,b,c\n1,2,3\n4,5,6\n"
    assert signed_post(base, json.dumps(stub.job("nolabel.csv")).encode())[0] == 202
    assert stub.done.wait(60)
    final = stub.reports[-1]
    assert "failed" in final and final["failed"]["code"] and final["failed"]["error"]
    assert not stub.outputs                                              # nothing stored


def test_an_input_over_the_limit_is_refused(service):
    _, base = service
    stub = Stub()
    stub.inputs["big.csv"] = SAMPLE_CSV.read_bytes()
    assert signed_post(base, json.dumps(stub.job("big.csv", max_bytes=100)).encode())[0] == 202
    assert stub.done.wait(60)
    assert stub.reports[-1]["failed"]["code"] == "too_large"


def test_the_hugging_face_space_bundle(tmp_path):
    """scripts/deploy_cloudprep_hf.py stages the Cloud Run files plus a Docker Space README."""
    s = importlib.util.spec_from_file_location("deploy_cloudprep_hf", PROJECT_ROOT / "scripts" / "deploy_cloudprep_hf.py")
    D = importlib.util.module_from_spec(s)
    s.loader.exec_module(D)
    d = D.stage(tmp_path / "space")
    names = sorted(p.name for p in d.iterdir())
    assert names == ["Dockerfile", "README.md", "agentmeter-prep.zip", "app.py", "requirements.txt", "urlfetch.py", "wheels"]
    assert len(list((d / "wheels").glob("*.whl"))) == 2
    readme = (d / "README.md").read_text(encoding="utf-8")
    assert "sdk: docker" in readme and "app_port: 8080" in readme
    assert "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080" in (d / "Dockerfile").read_text()
    assert not any("SECRET" in p.read_text(errors="ignore") and "secret" in p.name.lower() for p in d.iterdir() if p.is_file())
    assert D.space_url("Isma_Zahin/agentmeter-prep") == "https://isma-zahin-agentmeter-prep.hf.space"


# --- the traffic analysis: the job carries the IDS model, the service scores the pool ----------
DEMO_IDS = PROJECT_ROOT / "models" / "ids"


def _ids_job(stub: Stub, filename: str, sha: str | None = None, **kw) -> dict:
    for n, src in (("ids_rf.joblib", "demo_ids.joblib"), ("ids_rf.json", "demo_ids.json"),
                   ("ids_rf.hashes.npy", "demo_ids.hashes.npy")):
        stub.inputs[n] = (DEMO_IDS / src).read_bytes()
    real = hashlib.sha256(stub.inputs["ids_rf.joblib"]).hexdigest()
    outputs = {n: f"{stub.base}/out/{n}" for n in C.FILES + C.IDS_OUT}
    ids = {"sha256": sha or real, "files": {n: f"{stub.base}/in/{n}" for n in C.IDS_MODEL_FILES}}
    return stub.job(filename, outputs=outputs, ids=ids, **kw)


def test_a_job_with_the_ids_scores_the_pool_and_stores_the_analysis(service, tmp_path):
    svc, base = service
    stub = Stub()
    stub.inputs[SAMPLE_CSV.name] = SAMPLE_CSV.read_bytes()
    assert signed_post(base, json.dumps(_ids_job(stub, SAMPLE_CSV.name)).encode())[0] == 202
    assert stub.done.wait(120), stub.reports
    final = stub.reports[-1]
    assert "done" in final, final
    assert set(final["done"]["files"]) == set(C.FILES + C.IDS_OUT)
    assert any(r.get("event", {}).get("phase") == "ids" for r in stub.reports)
    info = json.loads(stub.outputs["ids_pool.json"])
    assert info["scope"] == "pool" and info["n_flows_in"] > 0
    # the same analysis as the server's own preparation with the same model
    ref = P.prepare_file(SAMPLE_CSV, source={"filename": SAMPLE_CSV.name}, results_root=tmp_path / "s", name="ref",
                         max_flows=20, other_attack=False, limits=LIMITS, ids=DEMO_IDS / "demo_ids.joblib")
    import io

    import pandas as pd
    a = pd.read_csv(tmp_path / "s" / ref / "ids_scores.csv")
    b = pd.read_csv(io.BytesIO(stub.outputs["ids_scores.csv"]))
    # same flows in the same order, same labels and selection; scores equal up to float noise
    # (the forest's trees may be summed in another order across threads)
    assert (a[["label", "selected"]].equals(b[["label", "selected"]]) and len(a) == len(b))
    assert (a["score"] - b["score"]).abs().max() < 1e-12
    # the model is cached by sha256 for the next job
    assert (svc.work_root / "ids-models" / info["model"]["sha256"] / "ids_rf.joblib").is_file()


def test_a_model_that_does_not_match_its_sha256_fails_the_job(service):
    _, base = service
    stub = Stub()
    stub.inputs[SAMPLE_CSV.name] = SAMPLE_CSV.read_bytes()
    assert signed_post(base, json.dumps(_ids_job(stub, SAMPLE_CSV.name, sha="0" * 64)).encode())[0] == 202
    assert stub.done.wait(60)
    assert stub.reports[-1]["failed"]["code"] == "ids_model_invalid"
    assert not stub.outputs


def test_an_ids_job_needs_its_model_urls_and_outputs(service):
    _, base = service
    stub = Stub()
    no_out = stub.job("a.csv", ids={"sha256": "a" * 64, "files": {"ids_rf.joblib": f"{stub.base}/in/x",
                                                                    "ids_rf.json": f"{stub.base}/in/y"}})
    status, data = signed_post(base, json.dumps(no_out).encode())
    assert status == 400 and "ids_scores.csv" in data["error"]
    bad = _ids_job(stub, "a.csv")
    bad["ids"]["files"]["ids_rf.json"] = "file:///etc/passwd"
    assert signed_post(base, json.dumps(bad).encode())[0] == 400
