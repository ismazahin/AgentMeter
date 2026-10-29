"""AgentMeter cloud data preparation: a small CPU service (Google Cloud Run) that prepares a
dataset while the GPU server is busy benchmarking. AgentMeter MEASURES LLM resource efficiency;
it is not a threat-detection product.

It runs the SAME preparation code as the GPU server and the browser (agentmeter.server.prepare,
packaged by scripts/build_web_prep.py), so a set prepared here has byte-identical data files and
the same prepared-set hash. It never sees a user or a password: the control plane starts each job
and holds every key.

  POST /prepare   (signed by the control plane: X-AM-Timestamp, X-AM-Signature =
                   hex HMAC(CLOUD_PREP_SECRET, "POST\\n/prepare\\n" + ts + "\\n" + sha256(body)))
      {job_id, filename, source: {kind: "file", url} | {kind: "url", url},
       max_flows, other_attack, limits, max_bytes,
       outputs: {"manifest.json": put_url, "features.csv": put_url, "labels.csv": put_url},
       report_url, report_path}
    -> 202 at once; the job runs in a background thread (deploy with CPU always allocated).
       Every step is POSTed to report_url, signed the same way over report_path:
       {"event": {phase, message, ...}} while it runs, then
       {"done": {set_name, files}} or {"failed": {error, code}}.
       The input is read from a single-use download URL (or fetched SSRF-safe from the user's
       https link) and the prepared files are PUT to single-use upload URLs.
  GET  /health    -> {"ok": true}

  env: PORT (8080), CLOUD_PREP_SECRET (required), CLOUD_PREP_WORK_DIR (a temp dir)
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

from agentmeter.server import prepare as P
from agentmeter.server import urlfetch

log = logging.getLogger("cloudprep")
MAX_SKEW_S = 120
MAX_BODY = 64 * 1024
FILES = ("manifest.json", "features.csv", "labels.csv")
REPORT_EVERY_S = 1.0
ENVIRONMENT = {"provider": "cloud", "prepared_on": "the AgentMeter cloud preparation service (Google Cloud Run)",
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
        out_root = work / "results"
        sid = P.prepare_file(path, source=source, results_root=out_root, name=P.unique_name(filename),
                             max_flows=int(job["max_flows"]), other_attack=bool(job.get("other_attack")),
                             limits=lim, progress=rep.event, environment=ENVIRONMENT)
        run_dir = out_root / sid
        rep.event(phase="writing", message="storing the prepared set")
        stored = []
        for name in FILES:
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
    return None


class Service:
    def __init__(self, secret: str, work_root: Path, post: Optional[Callable] = None):
        self.secret, self.work_root, self.post = secret, Path(work_root), post
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.running: dict[str, threading.Thread] = {}
        self.lock = threading.Lock()

    def start(self, job: dict) -> tuple[int, dict]:
        err = validate(job)
        if err:
            return 400, {"error": err, "code": "bad_request"}
        jid = str(job["job_id"])
        with self.lock:
            t = self.running.get(jid)
            if t and t.is_alive():
                return 200, {"accepted": True, "already_running": True}
            rep = Reporter(self.secret, job["report_url"], job["report_path"], post=self.post)
            t = threading.Thread(target=self._run, args=(jid, job, rep), name=f"prep-{jid}", daemon=True)
            self.running[jid] = t
        t.start()
        return 202, {"accepted": True}

    def _run(self, jid: str, job: dict, rep: Reporter) -> None:
        try:
            run_job(job, rep, self.work_root)
        finally:
            with self.lock:
                self.running.pop(jid, None)


def make_handler(svc: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentMeterCloudPrep/1"

        def _json(self, status: int, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                return self._json(200, {"ok": True, "service": "AgentMeter cloud data preparation",
                                        "running": len(svc.running)})
            return self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/prepare":
                return self._json(404, {"error": "not found"})
            n = int(self.headers.get("content-length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "body too large"})
            body = self.rfile.read(n)
            if not verify(svc.secret, "POST", "/prepare", self.headers, body):
                return self._json(401, {"error": "bad signature", "code": "bad_signature"})
            try:
                job = json.loads(body)
            except ValueError:
                return self._json(400, {"error": "body must be JSON", "code": "bad_request"})
            status, data = svc.start(job)
            return self._json(status, data)

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
    svc = Service(secret, work)
    port = int(os.environ.get("PORT", "8080"))
    log.info("listening on :%d", port)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(svc)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
