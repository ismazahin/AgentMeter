"""Phase 47: this GPU backend's link to the control plane (Cloudflare Worker + D1 + R2).

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product.

  * register at start and heartbeat every 60 s with the backend's PUBLIC URL (quick tunnel
    https://<random>.trycloudflare.com, or a named tunnel on your domain — the same call), its
    mode, GPU, version, which credentials are set (booleans only — never a value) and the jobs
    it knows; the Worker answers with the system limits (sessions/hour, queue cap, upload MB,
    idle minutes), which are applied here;
  * report each new job and every status change;
  * when a benchmark finishes, upload the session ONCE: session_results.json (raw), report.pdf,
    the prepared set's manifest.json / features.csv / labels.csv, then the compact summary
    (sessions.cp_summary). Identification columns (IPs, ports, protocol, timestamp) are
    stripped from the R2 copy of features.csv unless the admin setting
    keep_identification_columns_in_r2 is on — they stay in the backend's run folder only.
    A failed upload is retried on the next heartbeat (job["cp_uploaded"] marks success).

Requests are signed: X-AM-Timestamp + X-AM-Signature = hex HMAC-SHA256(BACKEND_SECRET,
"METHOD\\nPATH\\nTS\\nsha256hex(body)") — worker/src/backend.ts verifies it.

Environment:
  AGENTMETER_WORKER_URL        https://agentmeter-control-plane.<you>.workers.dev
  AGENTMETER_BACKEND_SECRET    shared with the Worker secret BACKEND_SECRET
  AGENTMETER_PUBLIC_URL        this backend's public URL, or
  AGENTMETER_PUBLIC_URL_FILE   a file holding it (scripts/vast_up.sh writes the quick-tunnel URL)
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("agentmeter.control_plane")

HEARTBEAT_S = max(1, int(os.environ.get("AGENTMETER_HEARTBEAT_S") or 60))   # the Worker marks it offline after 3 missed
# The identification columns of a prepared set (ingest.feature_map.META_COLUMNS minus flow_id).
# "Destination Port" is one of the 78 CIC features the models see, so it stays.
from .prepare import IDENTIFICATION_COLUMNS  # noqa: E402 — one list for strip and import
SESSION_FILES = ("manifest.json", "features.csv", "labels.csv")


class ControlPlaneError(RuntimeError):
    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status, self.code = status, code


def configured() -> bool:
    return bool(os.environ.get("AGENTMETER_WORKER_URL") and os.environ.get("AGENTMETER_BACKEND_SECRET"))


def public_url() -> str:
    url = (os.environ.get("AGENTMETER_PUBLIC_URL") or "").strip()
    f = os.environ.get("AGENTMETER_PUBLIC_URL_FILE")
    if not url and f:
        try:
            url = Path(f).read_text(encoding="utf-8").strip()
        except OSError:
            url = ""
    return url.rstrip("/")


def strip_identification(data: bytes) -> bytes:
    """features.csv without the identification columns (flow_id stays: it joins labels.csv)."""
    text = data.decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return data
    keep = [i for i, c in enumerate(rows[0]) if c not in IDENTIFICATION_COLUMNS]
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    for r in rows:
        w.writerow([r[i] for i in keep if i < len(r)])
    return out.getvalue().encode("utf-8")


def with_content_hash(manifest: bytes, run_dir: Path) -> bytes:
    """The stored manifest carries the prepared set's content hash (v2), so its stripped
    features.csv can be re-imported and checked on a later rental. Sets prepared before the
    hash existed get it computed here from the run folder."""
    from ..session.analyses import PREPARED_SET_HASH_VERSION, prepared_set_sha256
    try:
        man = json.loads(manifest.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return manifest
    ps = man.get("prepared_set") if isinstance(man, dict) else None
    if not isinstance(ps, dict) or ps.get("content_hash_version") == PREPARED_SET_HASH_VERSION:
        return manifest
    h = prepared_set_sha256(run_dir)
    if not h:
        return manifest
    ps.update(content_sha256=h, content_hash_version=PREPARED_SET_HASH_VERSION)
    return json.dumps(man, indent=2).encode("utf-8")


def sign(secret: str, method: str, path: str, ts: str, body: bytes) -> str:
    msg = f"{method}\n{path}\n{ts}\n{hashlib.sha256(body).hexdigest()}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


class Client:
    """Signed HTTP calls to the Worker (stdlib only)."""

    def __init__(self, worker_url: str, secret: str, timeout: float = 20.0):
        self.base = worker_url.rstrip("/")
        self.secret = secret
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None, raw: Optional[bytes] = None,
                content_type: str = "application/json") -> bytes:
        """Signed request; returns the raw response body. GET sends no body (signed over b"")."""
        data = b"" if method == "GET" else raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        ts = str(int(time.time()))
        req = urllib.request.Request(self.base + path, data=None if method == "GET" else data, method=method, headers={
            "Content-Type": content_type, "X-AM-Timestamp": ts,
            "X-AM-Signature": sign(self.secret, method, path, ts, data),
            "User-Agent": "agentmeter-backend"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:     # noqa: S310 — configured https URL
                return r.read()
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8") or "{}")
            except ValueError:
                err = {}
            raise ControlPlaneError(f"{method} {path}: HTTP {e.code} {err.get('error') or ''}".strip(),
                                    e.code, err.get("code", "")) from e
        except (urllib.error.URLError, OSError) as e:
            raise ControlPlaneError(f"{method} {path}: {e}") from e

    def call(self, method: str, path: str, body: Any = None, raw: Optional[bytes] = None,
             content_type: str = "application/json") -> dict:
        txt = self.request(method, path, body, raw, content_type).decode("utf-8") or "{}"
        try:
            return json.loads(txt)
        except ValueError:
            return {}


def _job_summary_status(job: dict) -> str:
    return str(job.get("status") or "")


def job_created_payload(job: dict) -> dict:
    run = job.get("run_name") or ""
    src = run.split("_runs/")[0] if "_runs/" in run and job.get("run_dir") else None
    user = job.get("user") if isinstance(job.get("user"), dict) else {}
    env = job.get("environment") or {}
    return {"job_id": job["job_id"], "kind": job.get("kind") or "benchmark", "status": job.get("status"),
            "jti": job.get("grant_jti"), "owner_username": user.get("uname"),
            "models": job.get("models") or [], "input_type": src, "n_flows": job.get("n_flows"),
            "provider": job.get("effective_provider") or env.get("provider"),
            "gpu": env.get("gpu_name"), "run_name": run, "created_at": job.get("created_at")}


def session_upload(mgr, job: dict, *, keep_identification: bool = False,
                   owner_username: Optional[str] = None) -> tuple[bytes, dict[str, bytes], dict]:
    """(results bytes, files, summary) of a finished benchmark job — what the Worker stores."""
    from .sessions import cp_summary

    res_path = Path(job.get("result_path") or "")
    raw = res_path.read_bytes()
    res = json.loads(raw.decode("utf-8"))
    run_dir = Path(job.get("run_dir") or "")
    files: dict[str, bytes] = {}
    for name in SESSION_FILES:
        p = run_dir / name
        if p.is_file():
            data = p.read_bytes()
            if name == "features.csv" and not keep_identification:
                data = strip_identification(data)
            if name == "manifest.json":
                data = with_content_hash(data, run_dir)
            files[name] = data
    try:
        from .jobs_api import report_pdf_bytes
        files["report.pdf"] = report_pdf_bytes(mgr, job["job_id"])
    except Exception as e:  # noqa: BLE001 — a missing PDF dependency never blocks the upload
        log.warning("report.pdf not uploaded for %s: %s", job["job_id"], e)
    j = dict(job)
    if owner_username and not (isinstance(j.get("user"), dict) and j["user"].get("uname")):
        j["user"] = {"sub": None, "uname": owner_username}
    summary = cp_summary(j, res)
    return raw, files, summary


class ControlPlane:
    """Registration, heartbeat, job events and session uploads (background threads)."""

    def __init__(self, client: Client, *, get_manager: Callable[[], Any], mode: str,
                 environment: Callable[[], dict], version: str = "",
                 apply_limits: Optional[Callable[[dict], None]] = None,
                 url_fn: Callable[[], str] = public_url, creds_fn: Optional[Callable[[], dict]] = None):
        self.client = client
        self.get_manager = get_manager
        self.mode = mode
        self.environment = environment
        self.version = version
        self.apply_limits = apply_limits
        self.url_fn = url_fn
        self.creds_fn = creds_fn or default_creds
        self.limits: dict[str, Any] = {}
        self.registered = False
        self._events: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._stop = threading.Event()
        self._upload_lock = threading.Lock()

    # --- protocol --------------------------------------------------------------------
    def _jobs(self) -> list[dict]:
        return [{"job_id": j["job_id"], "status": j["status"]} for j in self.get_manager().list_jobs(limit=200)]

    def _got_limits(self, r: dict) -> None:
        lim = r.get("limits")
        if isinstance(lim, dict):
            self.limits = lim
            if self.apply_limits:
                self.apply_limits(lim)

    def register(self) -> dict:
        if not self.url_fn():
            raise ControlPlaneError("no public URL yet (waiting for the tunnel)", 0, "no_url")
        env = self.environment() or {}
        gpu = {"name": env.get("gpu_name"), "vram_total_mb": env.get("gpu_vram_total_mb")} if self.mode == "real" else None
        r = self.client.call("POST", "/api/backend/register", {
            "url": self.url_fn(), "mode": self.mode, "gpu": gpu, "version": self.version,
            "creds": self.creds_fn(), "jobs": self._jobs()})
        self.registered = True
        self._got_limits(r)
        return r

    def heartbeat(self) -> dict:
        if not self.registered:
            return self.register()
        try:
            r = self.client.call("POST", "/api/backend/heartbeat", {"url": self.url_fn(), "jobs": self._jobs()})
        except ControlPlaneError as e:
            if e.code == "not_registered":
                return self.register()
            raise
        self._got_limits(r)
        return r

    def on_job_event(self, job: dict, created: bool) -> None:
        """JobManager.on_event: queue it; the sender thread delivers it (never blocks a job)."""
        self._events.put(("created" if created else "status", job))

    def _deliver(self, what: str, job: dict) -> None:
        if what == "created":
            self.client.call("POST", "/api/backend/jobs", job_created_payload(job))
        else:
            self.client.call("POST", f"/api/backend/jobs/{job['job_id']}/status", {"status": _job_summary_status(job)})
            if job.get("status") == "done" and (job.get("kind") or "benchmark") == "benchmark":
                self.upload(job["job_id"])

    def upload(self, job_id: str, owner_username: Optional[str] = None, force: bool = False) -> bool:
        """Upload one finished session (idempotent). True when it is stored in the control plane."""
        mgr = self.get_manager()
        with self._upload_lock:
            job = mgr._load(job_id)
            if job.get("status") != "done" or (job.get("kind") or "benchmark") != "benchmark":
                return False
            if job.get("cp_uploaded") and not force:
                return True
            keep = bool(self.limits.get("keep_identification_columns_in_r2", False))
            raw, files, summary = session_upload(mgr, job, keep_identification=keep, owner_username=owner_username)
            base = f"/api/backend/sessions/{job_id}"
            self.client.call("PUT", f"{base}/results", raw=raw)
            for name, data in files.items():
                ctype = {"report.pdf": "application/pdf", "manifest.json": "application/json"}.get(name, "text/csv")
                self.client.call("PUT", f"{base}/files/{name}", raw=data, content_type=ctype)
            self.client.call("PUT", f"{base}/summary", {"summary": summary, "owner_username": summary.get("owner_username")})
            mgr._update(job_id, cp_uploaded=True, cp_identification_stripped=not keep)
            return True

    def upload_pending(self) -> int:
        n = 0
        for j in self.get_manager()._all():
            if j.get("status") == "done" and (j.get("kind") or "benchmark") == "benchmark" and not j.get("cp_uploaded") \
                    and j.get("result_path") and Path(j["result_path"]).exists():
                try:
                    n += bool(self.upload(j["job_id"]))
                except Exception as e:  # noqa: BLE001
                    log.warning("upload of %s failed (retried on the next heartbeat): %s", j["job_id"], e)
        return n

    # --- threads ---------------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._heartbeat_loop, name="agentmeter-cp-heartbeat", daemon=True).start()
        threading.Thread(target=self._event_loop, name="agentmeter-cp-events", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.heartbeat()
                self.upload_pending()
            except Exception as e:  # noqa: BLE001 — the control plane being down never stops the backend
                log.warning("control plane heartbeat failed: %s", e)
            # not registered yet (tunnel URL not known / Worker unreachable): retry soon
            self._stop.wait(HEARTBEAT_S if self.registered else 5)

    def _event_loop(self) -> None:
        while not self._stop.is_set():
            try:
                what, job = self._events.get(timeout=1.0)
            except queue.Empty:
                continue
            for attempt in range(3):
                try:
                    self._deliver(what, job)
                    break
                except Exception as e:  # noqa: BLE001
                    log.warning("control plane event %s %s failed (attempt %d): %s", what, job.get("job_id"),
                                attempt + 1, e)
                    time.sleep(2 ** attempt)


def default_creds() -> dict[str, bool]:
    """Which credentials this backend has — booleans only, never a value."""
    from ..util import envtools
    try:
        env = {**envtools.read_dotenv_live(), **os.environ}
    except Exception:  # noqa: BLE001
        env = dict(os.environ)
    return {"hf_token": bool((env.get("HF_TOKEN") or env.get("HUGGING_FACE_HUB_TOKEN") or "").strip()),
            "vast_api_key": bool((env.get("VAST_API_KEY") or "").strip())}


def from_env(**kw) -> Optional[ControlPlane]:
    if not configured():
        return None
    return ControlPlane(Client(os.environ["AGENTMETER_WORKER_URL"], os.environ["AGENTMETER_BACKEND_SECRET"]), **kw)
