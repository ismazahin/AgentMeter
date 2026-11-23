"""The data preparation service (cloudprep/app.py): it pulls waiting jobs from the control plane
(signed), only while no benchmark holds the machine (gpulock), reads a job's input from a single-use
URL, prepares it with the server's own code (byte-identical data files), PUTs the prepared set to
single-use upload URLs and reports every step to the control plane, signed."""
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
from agentmeter.server import gpulock
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
        self.queue: list[dict] = []                  # the control plane's waiting list
        self.states: list[dict] = []                 # what the service said it was doing (/pending)
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
                if self.path in ("/api/prep/pending", "/api/prep/claim"):
                    if not C.verify(SECRET, "POST", self.path, self.headers, body):
                        stub.bad_reports += 1
                        return self._reply(401)
                    if self.path == "/api/prep/pending":
                        stub.states.append(json.loads(body))
                        return self._reply(200, json.dumps({"pending": len(stub.queue)}).encode())
                    job = stub.queue.pop(0) if stub.queue else None
                    return self._reply(200, json.dumps({"job": job}).encode())
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
    """A service that pulls from its own stub control plane, with a lock of its own."""
    stub = Stub()
    svc = C.Service(SECRET, tmp_path / "work", cp=stub.base, lock_path=tmp_path / "busy.lock")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), C.make_handler(svc))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield svc, f"http://127.0.0.1:{srv.server_address[1]}", stub
    srv.shutdown()


def run(svc, stub: Stub, job: dict) -> dict:
    """Put one job on the waiting list, let the service pull and run it; the final report."""
    stub.queue.append(job)
    assert svc.work_once() == 1
    assert stub.done.is_set()
    return stub.reports[-1]


def nudge(base: str, secret: str = SECRET, ts: int | None = None, path: str = "/nudge") -> int:
    body = b""
    ts = str(int(time.time()) if ts is None else ts)
    req = urllib.request.Request(base + path, data=body, method="POST", headers={
        "X-AM-Timestamp": ts, "X-AM-Signature": C.sign(secret, "POST", path, ts, body)})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_signatures():
    body = b'{"a":1}'
    ts = str(int(time.time()))
    good = {"X-AM-Timestamp": ts, "X-AM-Signature": C.sign(SECRET, "POST", "/prepare", ts, body)}
    assert C.verify(SECRET, "POST", "/prepare", good, body)
    assert not C.verify(SECRET, "POST", "/prepare", good, body + b" ")             # body changed
    assert not C.verify("another-secret-0123456789", "POST", "/prepare", good, body)
    assert not C.verify(SECRET, "POST", "/prepare", good, body, now=int(ts) + 600)  # stale
    assert not C.verify(SECRET, "POST", "/prepare", {"X-AM-Timestamp": "x"}, body)


def test_only_a_signed_nudge_wakes_it_and_it_takes_no_pushed_jobs(service):
    svc, base, _ = service
    assert nudge(base, secret="wrong-secret-0123456789") == 401
    assert nudge(base, ts=int(time.time()) - 3600) == 401
    assert not svc.nudged.is_set()
    assert nudge(base) == 202
    assert svc.nudged.is_set()
    assert nudge(base, path="/prepare") == 404                          # the old push route is gone
    h = json.loads(urllib.request.urlopen(base + "/health").read())
    assert h["ok"] and h["state"] == "idle"


def test_nothing_waiting_means_nothing_runs_and_the_machine_is_not_held(service, tmp_path):
    svc, _, stub = service
    assert svc.work_once() == 0
    assert stub.states == [{"state": "idle"}]
    assert not gpulock.prep_wanted(tmp_path / "busy.lock")


def test_a_bad_job_is_reported_failed_and_the_next_one_still_runs(service):
    svc, _, stub = service
    stub.inputs[SAMPLE_CSV.name] = SAMPLE_CSV.read_bytes()
    stub.queue.append(stub.job("a.csv", source={"kind": "file", "url": "file:///etc/passwd"}))
    stub.queue.append(stub.job(SAMPLE_CSV.name))
    assert svc.work_once() == 1
    failed = [r for r in stub.reports if "failed" in r]
    assert failed and failed[0]["failed"]["code"] == "bad_request"
    assert "done" in stub.reports[-1]
    assert stub.bad_reports == 0


def test_it_never_prepares_while_a_benchmark_holds_the_machine(service, tmp_path):
    """The lock: the service marks that it waits, reports who holds the machine, and starts only
    when the benchmark lets go."""
    svc, _, stub = service
    lock = tmp_path / "busy.lock"
    stub.inputs[SAMPLE_CSV.name] = SAMPLE_CSV.read_bytes()
    stub.queue.append(stub.job(SAMPLE_CSV.name))
    release, held = threading.Event(), threading.Event()

    def benchmark():
        with gpulock.hold({"kind": "benchmark", "job_id": "job_x", "models": 3}, path=lock):
            held.set()
            release.wait(20)
    threading.Thread(target=benchmark, daemon=True).start()
    assert held.wait(5)
    worker = threading.Thread(target=svc.work_once, daemon=True)
    worker.start()
    deadline = time.time() + 10
    while time.time() < deadline and not gpulock.prep_wanted(lock):
        time.sleep(0.1)
    assert gpulock.prep_wanted(lock)                                    # the worker will start no new job
    time.sleep(1.5)
    assert not stub.reports                                             # nothing prepared yet
    assert gpulock.holder(lock)["kind"] == "benchmark"
    release.set()
    worker.join(120)
    assert "done" in stub.reports[-1], stub.reports
    assert [x["state"] for x in stub.states][:2] == ["idle", "waiting_for_benchmark"]
    assert stub.states[1]["holder"]["models"] == 3
    assert stub.states[-1] == {"state": "idle"}
    assert not gpulock.prep_wanted(lock) and gpulock.holder(lock) is None


@pytest.mark.parametrize("src", [SAMPLE_CSV, SAMPLE_PCAP], ids=["csv", "pcap"])
def test_a_job_prepares_like_the_server_and_reports_signed(service, tmp_path, src):
    if src.suffix == ".pcap":
        pytest.importorskip("scapy")
    svc, _, stub = service
    stub.inputs[src.name] = src.read_bytes()
    final = run(svc, stub, stub.job(src.name))
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
    assert "GPU machine" in man["backend"]["prepared_on"]
    assert man["source"]["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    ref_man = json.loads((tmp_path / "s" / ref / "manifest.json").read_text())["prepared_set"]
    assert man["content_sha256"] == ref_man["content_sha256"]            # same prepared-set hash

    files = {n: stub.outputs[n] for n in final["done"]["files"]}
    assert P.import_prepared(files, tmp_path / "gpu") == sid             # imported under its own name


def test_a_bad_file_is_reported_as_failed(service):
    svc, _, stub = service
    stub.inputs["nolabel.csv"] = b"a,b,c\n1,2,3\n4,5,6\n"
    final = run(svc, stub, stub.job("nolabel.csv"))
    assert "failed" in final and final["failed"]["code"] and final["failed"]["error"]
    assert not stub.outputs                                              # nothing stored


def test_an_input_over_the_limit_is_refused(service):
    svc, _, stub = service
    stub.inputs["big.csv"] = SAMPLE_CSV.read_bytes()
    assert run(svc, stub, stub.job("big.csv", max_bytes=100))["failed"]["code"] == "too_large"


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
    svc, _, stub = service
    stub.inputs[SAMPLE_CSV.name] = SAMPLE_CSV.read_bytes()
    final = run(svc, stub, _ids_job(stub, SAMPLE_CSV.name))
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
    svc, _, stub = service
    stub.inputs[SAMPLE_CSV.name] = SAMPLE_CSV.read_bytes()
    assert run(svc, stub, _ids_job(stub, SAMPLE_CSV.name, sha="0" * 64))["failed"]["code"] == "ids_model_invalid"
    assert not stub.outputs


def test_an_ids_job_needs_its_model_urls_and_outputs(service):
    _, _, stub = service
    no_out = stub.job("a.csv", ids={"sha256": "a" * 64, "files": {"ids_rf.joblib": f"{stub.base}/in/x",
                                                                    "ids_rf.json": f"{stub.base}/in/y"}})
    assert "ids_scores.csv" in C.validate(no_out)
    bad = _ids_job(stub, "a.csv")
    bad["ids"]["files"]["ids_rf.json"] = "file:///etc/passwd"
    assert C.validate(bad)
    assert C.validate(_ids_job(stub, "a.csv")) is None
