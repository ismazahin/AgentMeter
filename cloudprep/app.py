"""AgentMeter data preparation service: runs on the rented GPU machine next to the GPU server and
prepares uploaded datasets (and the IDS traffic analysis). AgentMeter MEASURES LLM resource
efficiency; it is not a threat-detection product.

It runs the SAME preparation code as the GPU server and the browser (agentmeter.server.prepare),
so a set prepared here has byte-identical data files and the same prepared-set hash. It never sees
a user or a password: the control plane holds every key and hands out single-use Storage URLs.

One at a time on the machine (agentmeter/server/gpulock.py): a preparation never runs while a
benchmark does, so it cannot change a reading. The service PULLS work from the control plane:

  every PULL_S seconds, or at once when the control plane nudges it (POST /nudge):
    POST <cp>/api/prep/pending  {state, holder?}       -> {pending: n}
    n > 0: mark "a preparation is waiting" (the worker starts no new benchmark), wait for the lock
           (reporting state "waiting_for_benchmark" and who holds it), then
    POST <cp>/api/prep/claim    {}                     -> {job: {...} | null}, until null:
       {job_id, filename, source: {kind: "file", url} | {kind: "url", url}, max_flows,
        other_attack, limits, max_bytes, outputs: {name: put_url}, report_url, report_path, ids?}
       Every step is POSTed to report_url, signed over report_path:
       {"event": {phase, message, ...}}, then {"done": {set_name, files}} or {"failed": {error, code}}.
  Every request to the control plane is signed: X-AM-Timestamp, X-AM-Signature =
  hex HMAC(CLOUD_PREP_SECRET, "POST\n" + path + "\n" + ts + "\n" + sha256(body)).

  Optional `ids` in the job: {sha256, files: {"ids_rf.joblib": url, "ids_rf.json": url,
  "ids_rf.hashes.npy": url?}}: the IDS then scores every candidate-pool flow. Cached by sha256.

  POST /nudge     (signed by the control plane, empty body) -> 202: look for work now
  GET  /health    -> {"ok": true, "state": ...}

  env: PORT (8080), CLOUD_PREP_SECRET (required), AGENTMETER_CONTROL_PLANE_URL (where to pull
       from), CLOUD_PREP_WORK_DIR (a temp dir), AGENTMETER_BUSY_LOCK (gpulock.py), PULL_S (60)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

# Run from a checkout (scripts/prep_local.py: the service on your own computer) the package is one
# folder up; in the Docker image it sits next to this file.
_REPO = Path(__file__).resolve().parents[1]
if (_REPO / "agentmeter").is_dir() and str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from agentmeter.server import gpulock  # noqa: E402
from agentmeter.server import prepare as P  # noqa: E402
from agentmeter.server import urlfetch  # noqa: E402

log = logging.getLogger("cloudprep")
MAX_SKEW_S = 120
MAX_BODY = 64 * 1024
FILES = ("manifest.json", "features.csv", "labels.csv")
# The traffic analysis (optional, when the job carries `ids`): the IDS scores every pool flow.
IDS_OUT = ("ids_scores.csv", "ids_pool.json", "ids_flows_identity.csv")
IDS_MODEL_FILES = ("ids_rf.joblib", "ids_rf.json", "ids_rf.hashes.npy")
IDS_MODEL_MAX = 60 * 2 ** 20            # per model file (the control plane stores up to 50 MB)
_ids_lock = threading.Lock()
KEEPALIVE_S = float(os.environ.get("CLOUD_PREP_KEEPALIVE_S", "120"))   # "still working" while a job runs
REPORT_EVERY_S = 1.0
ENVIRONMENT = {"provider": "cloud",
               "prepared_on": os.environ.get("CLOUD_PREP_WHERE") or "the AgentMeter preparation service on the GPU machine",
               "runtime": f"Python {platform.python_version()}"}


def sign(secret: str, method: str, path: str, ts: str, body: bytes) -> str:
    msg = f"{method}\n{path}\n{ts}\n{hashlib.sha256(body).hexdigest()}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify(secret: str, method: str, path: str, headers, body: bytes, now: Optional[float] = None) -> bool:
    ts = headers.get("X-AM-Timestamp") or ""
    sig = (headers.get("X-AM-Signature") or "").lower()
    try:
        t = int(ts)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - t) > MAX_SKEW_S:
        return False
    return hmac.compare_digest(sign(secret, method, path, ts, body), sig)


class Reporter:
    """Sends a job's progress to the control plane, signed. Progress is throttled (one event per
    second unless the phase changes); the final result is always sent, with retries."""

    def __init__(self, secret: str, url: str, path: str, post: Optional[Callable[[str, dict, dict], int]] = None):
        self.secret, self.url, self.path = secret, url, path
        self.post = post or _post_json
        self._last = (None, 0.0)

    def _send(self, payload: dict, tries: int = 1) -> bool:
        body = json.dumps(payload, default=str).encode()
        for i in range(tries):
            ts = str(int(time.time()))
            try:
                status = self.post(self.url, {"content-type": "application/json", "X-AM-Timestamp": ts,
                                              "X-AM-Signature": sign(self.secret, "POST", self.path, ts, body)}, body)
                if 200 <= status < 300:
                    return True
                log.warning("report answered HTTP %s", status)
            except OSError as e:
                log.warning("report failed: %s", e)
            if i + 1 < tries:
                time.sleep(min(2 ** i, 10))
        return False

    def event(self, **kw: Any) -> None:
        phase, last_t = self._last
        now = time.monotonic()
        if kw.get("phase") == phase and now - last_t < REPORT_EVERY_S:
            return
        self._last = (kw.get("phase"), now)
        ev = {k: v for k, v in kw.items() if k in ("phase", "message", "bytes", "total", "percent")}
        if ev:
            self._send({"event": ev})

    def keepalive(self) -> None:
        """Still working: tells the control plane the job is alive during a long silent step (a big
        prepare pass, IDS scoring), so it is not taken for a machine that went away."""
        self._send({"keepalive": True})

    def done(self, **kw: Any) -> bool:
        return self._send({"done": kw}, tries=5)

    def failed(self, error: str, code: str) -> bool:
        return self._send({"failed": {"error": error[:600], "code": code}}, tries=5)


def _post_json(url: str, headers: dict, body: bytes) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _download(url: str, dest: Path, max_bytes: int) -> tuple[int, str]:
    """Stream a single-use signed download URL to dest. Returns (bytes, sha256)."""
    h, n = hashlib.sha256(), 0
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "AgentMeter-cloudprep/1"}), timeout=60) as r, \
            open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            n += len(chunk)
            if n > max_bytes:
                raise P.PrepareError(f"the file is larger than the cloud preparation limit ({max_bytes // 2 ** 20} MB)",
                                     "too_large", 413)
            h.update(chunk)
            f.write(chunk)
    return n, h.hexdigest()


def _put(url: str, data: bytes, content_type: str) -> None:
    """PUT one file to a single-use upload URL (Supabase Storage signed upload, or the dev store)."""
    req = urllib.request.Request(url, data=data, method="PUT",
                                 headers={"content-type": content_type, "x-upsert": "true"})
    with urllib.request.urlopen(req, timeout=120) as r:
        if not 200 <= r.status < 300:
            raise OSError(f"upload answered HTTP {r.status}")


def ids_model(ids: dict, cache_root: Path) -> Path:
    """The job's IDS model, downloaded once per sha256 into cache_root/<sha256>/ (reused by later
    jobs). The .joblib must hash to the sha256 the control plane named; load_model checks the rest
    (metadata, scikit-learn version) when the pool is scored."""
    sha = str(ids["sha256"]).lower()
    d = cache_root / sha
    joblib_p = d / "ids_rf.joblib"
    with _ids_lock:
        if joblib_p.is_file() and P.sha256_file(joblib_p) == sha:
            return joblib_p
        tmp = Path(tempfile.mkdtemp(prefix="ids-", dir=cache_root))
        try:
            for name in IDS_MODEL_FILES:
                url = (ids.get("files") or {}).get(name)
                if url:
                    _download(url, tmp / name, IDS_MODEL_MAX)
            if P.sha256_file(tmp / "ids_rf.joblib") != sha:
                raise P.PrepareError("the IDS model's file does not match its sha256: upload it again in "
                                     "Admin › IDS model", "ids_model_invalid", 502)
            shutil.rmtree(d, ignore_errors=True)
            tmp.rename(d)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return joblib_p


def run_job(job: dict, rep: Reporter, work_root: Path) -> bool:
    """One preparation, start to finish. Returns True when it was stored and reported done."""
    work = Path(tempfile.mkdtemp(prefix=f"prep-{job['job_id'][:12]}-", dir=work_root))
    try:
        src, lim = job["source"], job["limits"]
        max_bytes = int(job.get("max_bytes") or 0) or 2 ** 30
        if src["kind"] == "url":
            rep.event(phase="downloading", message="connecting")
            d = urlfetch.download(src["url"], work, max_bytes=min(max_bytes, int(lim.get("max_url_download_bytes") or max_bytes)),
                                  timeout_s=float(lim.get("url_timeout_s") or 600), progress=rep.event, stem="input")
            path, filename = d.path, d.filename
            source = {k: v for k, v in {"kind": "url", "filename": d.filename, "url": src["url"], "final_url": d.final_url,
                                        "size_bytes": d.size_bytes, "sha256": d.sha256, "content_type": d.content_type,
                                        "redirects": d.redirects}.items() if v is not None}
        else:
            filename = Path(str(job.get("filename") or "upload")).name or "upload"
            rep.event(phase="downloading", message=f"reading {filename}")
            path = work / filename
            size, sha = _download(src["url"], path, max_bytes)
            source = {"kind": "upload", "filename": filename, "size_bytes": size, "sha256": sha}
        model = None
        if job.get("ids"):
            rep.event(phase="ids", message="loading the IDS model")
            cache = work_root / "ids-models"
            cache.mkdir(parents=True, exist_ok=True)
            model = ids_model(job["ids"], cache)
        out_root = work / "results"
        sid = P.prepare_file(path, source=source, results_root=out_root, name=P.unique_name(filename),
                             max_flows=int(job["max_flows"]), other_attack=bool(job.get("other_attack")),
                             limits=lim, progress=rep.event, environment=ENVIRONMENT, ids=model)
        run_dir = out_root / sid
        rep.event(phase="writing", message="storing the prepared set")
        stored = []
        for name in FILES + (IDS_OUT if model else ()):
            p = run_dir / name
            if p.is_file():
                _put(job["outputs"][name], p.read_bytes(), "application/json" if name.endswith(".json") else "text/csv")
                stored.append(name)
        return rep.done(set_name=sid, files=stored)
    except P.PrepareError as e:
        rep.failed(str(e), e.code)
    except urlfetch.UrlImportError as e:
        rep.failed(str(e), getattr(e, "code", "url_import_failed"))
    except Exception as e:  # noqa: BLE001 — the user gets the reason, the log the traceback
        log.exception("job %s failed", job.get("job_id"))
        rep.failed(f"cloud preparation failed: {type(e).__name__}: {e}", "cloud_prepare_failed")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return False


REQUIRED = ("job_id", "source", "max_flows", "limits", "outputs", "report_url", "report_path")


def validate(job: Any) -> Optional[str]:
    if not isinstance(job, dict):
        return "body must be a JSON object"
    missing = [k for k in REQUIRED if k not in job]
    if missing:
        return "missing: " + ", ".join(missing)
    src = job["source"]
    if not isinstance(src, dict) or src.get("kind") not in ("file", "url") or not str(src.get("url", "")).startswith(("https://", "http://")):
        return "source must be {kind: file|url, url}"
    if not isinstance(job["outputs"], dict) or not all(isinstance(job["outputs"].get(n), str) for n in FILES):
        return "outputs needs an upload URL for " + ", ".join(FILES)
    ids = job.get("ids")
    if ids is not None:
        files = ids.get("files") if isinstance(ids, dict) else None
        sha = str(ids.get("sha256") or "") if isinstance(ids, dict) else ""
        if not (len(sha) == 64 and all(c in "0123456789abcdefABCDEF" for c in sha)) or not isinstance(files, dict) \
                or not all(str(files.get(n, "")).startswith(("https://", "http://")) for n in IDS_MODEL_FILES[:2]):
            return "ids must be {sha256, files: {ids_rf.joblib: url, ids_rf.json: url}}"
        if not all(isinstance(job["outputs"].get(n), str) for n in IDS_OUT):
            return "outputs needs an upload URL for " + ", ".join(IDS_OUT)
    return None


def pull_s() -> float:
    return float(os.environ.get("PULL_S", "60"))


def _post_signed(cp: str, secret: str, path: str, payload: dict, post: Optional[Callable] = None) -> tuple[int, dict]:
    """POST a signed JSON body to <cp><path>; (status, body). Never raises for HTTP/network."""
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    headers = {"content-type": "application/json", "X-AM-Timestamp": ts, "X-AM-Signature": sign(secret, "POST", path, ts, body)}
    if post is not None:                     # tests
        return post(cp + path, headers, body)
    req = urllib.request.Request(cp + path, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError) as e:
        return 0, {"error": str(e)}


class Service:
    """Pulls waiting preparations from the control plane and runs them one at a time, only while
    the machine's lock is free (no benchmark running)."""

    def __init__(self, secret: str, work_root: Path, cp: Optional[str] = None, post: Optional[Callable] = None,
                 cp_post: Optional[Callable] = None, lock_path: Optional[Path] = None):
        self.secret, self.work_root, self.post = secret, Path(work_root), post
        self.cp = cp.rstrip("/") if cp else None
        self.cp_post = cp_post                       # tests: (url, headers, body) -> (status, dict)
        self.lock_path = lock_path
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.nudged = threading.Event()
        self.stop = threading.Event()
        self.state = "idle"
        self._thread: Optional[threading.Thread] = None

    def _cp(self, path: str, payload: dict) -> tuple[int, dict]:
        return _post_signed(self.cp, self.secret, path, payload, self.cp_post)

    def report_state(self, state: str, holder: Optional[dict] = None) -> int:
        """Tell the control plane what this service is doing; returns how many jobs wait (0 on error)."""
        self.state = state
        st, d = self._cp("/api/prep/pending", {"state": state, **({"holder": holder} if holder else {})})
        if st != 200:
            log.warning("pending: HTTP %s %s", st, d.get("error", ""))
            return 0
        return int(d.get("pending") or 0)

    def start(self) -> None:
        if self.cp and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self.loop, name="prep-puller", daemon=True)
            self._thread.start()

    def loop(self) -> None:
        while not self.stop.is_set():
            self.nudged.wait(pull_s())
            self.nudged.clear()
            if self.stop.is_set():
                break
            try:
                self.work_once()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                log.exception("pulling work failed")

    def work_once(self) -> int:
        """Look for waiting work; when there is some, wait for the machine and run all of it.
        Returns how many jobs ran."""
        if not self.report_state("idle"):
            return 0
        ran = 0
        keep = threading.Event()

        def mark() -> None:                          # keep "a preparation is waiting" fresh
            while not keep.wait(5.0):
                gpulock.want(True, self.lock_path)
        gpulock.want(True, self.lock_path)
        ticker = threading.Thread(target=mark, daemon=True)
        ticker.start()
        last = [0.0]

        def waiting(h: Optional[dict]) -> None:      # every 5 s while a benchmark holds the machine
            if time.monotonic() - last[0] >= 30:
                last[0] = time.monotonic()
                self.report_state("waiting_for_benchmark", h)
        try:
            with gpulock.hold({"kind": "prepare"}, on_wait=waiting, stop=self.stop, path=self.lock_path) as got:
                if not got:
                    return 0
                self.report_state("preparing")
                while not self.stop.is_set():
                    st, d = self._cp("/api/prep/claim", {})
                    job = d.get("job") if st == 200 else None
                    if not job:
                        break
                    err = validate(job)
                    if err:
                        log.error("the control plane sent a bad job: %s", err)
                        if job.get("report_url") and job.get("report_path"):
                            Reporter(self.secret, job["report_url"], job["report_path"], post=self.post).failed(err, "bad_request")
                        continue
                    rep = Reporter(self.secret, job["report_url"], job["report_path"], post=self.post)
                    alive = threading.Event()

                    def _beat(rep=rep, alive=alive):
                        while not alive.wait(KEEPALIVE_S):
                            rep.keepalive()
                    threading.Thread(target=_beat, daemon=True).start()
                    try:
                        run_job(job, rep, self.work_root)
                    finally:
                        alive.set()
                    ran += 1
        finally:
            keep.set()
            gpulock.want(False, self.lock_path)
            self.report_state("idle")
        return ran


def make_handler(svc: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentMeterPrep/2"

        def _json(self, status: int, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                return self._json(200, {"ok": True, "service": "AgentMeter data preparation", "state": svc.state})
            return self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/nudge":
                return self._json(404, {"error": "not found"})
            n = int(self.headers.get("content-length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "body too large"})
            body = self.rfile.read(n)
            if not verify(svc.secret, "POST", "/nudge", self.headers, body):
                return self._json(401, {"error": "bad signature", "code": "bad_signature"})
            svc.nudged.set()
            return self._json(202, {"ok": True})

        def log_message(self, fmt, *args):  # noqa: D401 — quieter than the default
            log.info("%s %s", self.address_string(), fmt % args)

    return Handler


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    secret = os.environ.get("CLOUD_PREP_SECRET", "")
    if len(secret) < 16:
        print("CLOUD_PREP_SECRET is required (at least 16 characters)", file=sys.stderr)
        return 2
    work = Path(os.environ.get("CLOUD_PREP_WORK_DIR") or tempfile.gettempdir()) / "agentmeter-prep"
    cp = (os.environ.get("AGENTMETER_CONTROL_PLANE_URL") or "").strip() or None
    if not cp:
        log.warning("AGENTMETER_CONTROL_PLANE_URL is not set: there is nowhere to pull work from")
    svc = Service(secret, work, cp=cp)
    svc.start()
    port = int(os.environ.get("PORT", "8080"))
    log.info("listening on :%d", port)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(svc)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
