"""Service API for the web flow: Prepare -> Benchmark (Phases 41-43b).
Same-origin routes on the existing Flask app.

Prepare (data preparation for benchmarking, not analysis):
  POST /api/prepare             multipart file (.csv/.pcap/.pcapng) OR form/JSON {url}
                                + max_flows?, other_attack? -> 202 {job} (kind "prepare").
                                The request only saves the upload (or checks the URL)
                                and queues the job; ingestion runs on the job layer.
  GET  /api/prepared/<kind>/<name>          the prepared set's summary
  GET  /api/prepared/<kind>/<name>/<file>   features.csv | labels.csv | manifest.json
  POST /api/prepared/import     multipart manifest, features[, labels] -> 201 {summary}
                                (re-upload of a prepared set downloaded earlier)
Benchmark:
  POST /api/jobs                (jobs_api.py) {run: <prepared-set id>, models[<=2]}
Other:
  GET  /api/service/config      canonical models, max 2, provider, demo flag, limits

Input guards (config.yaml `service:`): an upload above max_upload_mb is rejected
with 413 before its body is read; a URL import above max_url_download_gb is
refused from Content-Length or stopped mid-stream. A large input is sampled
across its whole extent (CSV: stratified reservoir; PCAP: evenly spaced time
windows) — see ingest/csv_input.py and ingest/pcap_window.py.

Nothing here computes a benchmark: ingestion is ingest/run.py; jobs are jobs.py.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Callable

from . import runtoken
from .jobs import RUN_ROOTS, JobError, JobManager
from .prepare import (MAX_IMPORT_BYTES, PrepareError, prepared_file,
                      summarize, unique_name)

DEFAULT_SERVICE_MAX_FLOWS = 50
# config.yaml `service:` key -> (env override, default when neither is set, type)
_LIMITS = {"max_upload_mb": ("AGENTMETER_MAX_UPLOAD_MB", 90, int),
           "max_url_download_gb": ("AGENTMETER_MAX_URL_DOWNLOAD_GB", 10, float),
           "url_timeout_s": ("AGENTMETER_URL_TIMEOUT_S", 3600, int),
           "max_pcap_packets": ("AGENTMETER_MAX_PCAP_PACKETS", 100_000, int),
           "max_pcap_flows": ("AGENTMETER_MAX_PCAP_FLOWS", 20_000, int),
           "pcap_windows": ("AGENTMETER_PCAP_WINDOWS", 10, int),
           "max_csv_rows": ("AGENTMETER_MAX_CSV_ROWS", 500_000, int)}
_MULTIPART_SLACK = 1024 ** 2   # form fields + boundaries around the file part
_CHUNK = 1024 ** 2
ALLOWED_EXT = {".csv", ".pcap", ".pcapng"}
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class ServiceError(ValueError):
    def __init__(self, message: str, code: str, status: int):
        super().__init__(message)
        self.code = code
        self.status = status


def service_limits(config_path: str | None = None) -> dict[str, Any]:
    """The web service's input limits: env override > config.yaml `service:` > default.
    AGENTMETER_MAX_UPLOAD_BYTES (pre-Phase 43, exact bytes) still wins for the upload."""
    from ..config import load_config

    try:
        cfg = load_config(config_path)
    except FileNotFoundError:
        cfg = None
    out: dict[str, Any] = {}
    for key, (env, default, typ) in _LIMITS.items():
        raw = os.environ.get(env) or (cfg.get(f"service.{key}") if cfg is not None else None)
        raw = default if raw is None else raw
        try:
            val = typ(raw)
        except (TypeError, ValueError):
            raise ServiceError(f"service limit {key} must be a number, got {raw!r}",
                               "server_config", 500) from None
        if val <= 0:
            raise ServiceError(f"service limit {key} must be positive, got {val}", "server_config", 500)
        out[key] = val
    legacy = os.environ.get("AGENTMETER_MAX_UPLOAD_BYTES")
    out["max_upload_bytes"] = int(legacy) if legacy else out["max_upload_mb"] * 1024 ** 2
    out["max_url_download_bytes"] = int(out["max_url_download_gb"] * 1024 ** 3)
    return out


def _fmt_size(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:,.1f} GB".replace(".0 GB", " GB")
    return f"{n / 1024 ** 2:,.0f} MB" if n >= 1024 ** 2 else f"{n:,} bytes"


def too_large(limit: int) -> ServiceError:
    return ServiceError(f"file too large: the upload limit is {_fmt_size(limit)}. For a larger "
                        "file use \"Import from URL\" (a direct https link; the server downloads it), "
                        "or upload a slice — AgentMeter benchmarks a bounded sample of flows anyway.",
                        "too_large", 413)


def save_stream(stream, dest: Path, max_bytes: int) -> tuple[int, str]:
    """Copy `stream` to `dest` in chunks, never holding the file in memory; abort
    (and delete the partial file) as soon as it passes max_bytes. -> (bytes, sha256)."""
    n = 0
    h = hashlib.sha256()
    try:
        with dest.open("wb") as out:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                n += len(chunk)
                if n > max_bytes:
                    raise too_large(max_bytes)
                h.update(chunk)
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return n, h.hexdigest()


def _check_ext(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ServiceError(f"unsupported file type {ext or '(none)'}: upload a .csv (CIC-IDS2017 "
                           "format) or a .pcap/.pcapng capture", "invalid_file", 400)
    return ext


def save_upload(fileobj, filename: str, uploads_dir: Path, limits: dict[str, Any]) -> dict[str, Any]:
    """Save an uploaded raw file (streamed) -> a prepare-job source record."""
    ext = _check_ext(filename)
    name = unique_name(filename)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    saved = uploads_dir / f"{name}{ext}"
    n, sha = save_stream(getattr(fileobj, "stream", fileobj), saved, limits["max_upload_bytes"])
    if n == 0:
        saved.unlink(missing_ok=True)
        raise ServiceError("the uploaded file is empty", "invalid_file", 400)
    return {"name": name, "source": {"kind": "upload", "path": str(saved), "filename": filename,
                                     "size_bytes": n, "sha256": sha}}


def service_config(gpu_available: bool, limits: dict[str, Any] | None = None,
                   mode: str | None = None) -> dict[str, Any]:
    from ..session.benchmark import MAX_MODELS, canonical_models

    lim = limits or service_limits()
    from .runtime import job_provider, resolve_mode
    # Phase E: the server's startup decision (create_app); never re-decided per request.
    provider = job_provider(mode or resolve_mode(None, gpu_available=gpu_available))
    demo = provider == "mock"
    return {
        "canonical_models": canonical_models(), "max_models": MAX_MODELS,
        "provider": provider, "base_config": "run_full_l4.yaml" if provider == "hf" else None,
        "gpu_available": gpu_available, "demo_mode": demo,
        "default_max_flows": DEFAULT_SERVICE_MAX_FLOWS,
        "limits": {**lim, "max_upload_label": _fmt_size(lim["max_upload_bytes"]),
                   "max_url_download_label": _fmt_size(lim["max_url_download_bytes"])},
        "note": ("No GPU on this server: runs use the MOCK provider (a deterministic heuristic, not a "
                 "language model). Use it to try the flow; numbers are not model measurements."
                 if demo else "Real models, 4-bit NF4, one model at a time on this server's GPU."),
    }


def _form_options(form) -> tuple[int, bool]:
    try:
        max_flows = int(form.get("max_flows") or DEFAULT_SERVICE_MAX_FLOWS)
    except (TypeError, ValueError):
        raise ServiceError("max_flows must be a whole number", "bad_request", 400) from None
    if not 1 <= max_flows <= 500:
        raise ServiceError("max_flows must be between 1 and 500", "bad_request", 400)
    other = str(form.get("other_attack", "")).lower() in ("1", "true", "on", "yes")
    return max_flows, other


def register_service(app, get_manager: Callable[[], JobManager]) -> None:
    from flask import jsonify, request, send_file
    from werkzeug.exceptions import RequestEntityTooLarge

    limits = service_limits()       # read once at startup, like the rest of the app config
    body_cap = limits["max_upload_bytes"] + _MULTIPART_SLACK
    # Werkzeug enforces this while it parses the body, so an over-limit upload sent
    # without a Content-Length (chunked) is cut off too. Never loosen a lower cap.
    if not app.config.get("MAX_CONTENT_LENGTH") or app.config["MAX_CONTENT_LENGTH"] > body_cap:
        app.config["MAX_CONTENT_LENGTH"] = body_cap
    app.config["SERVICE_LIMITS"] = limits

    def set_upload_mb(mb: int) -> None:
        """Phase 47: the admin's upload limit, pushed by the control plane on each heartbeat."""
        mb = int(mb)
        if mb < 1 or mb == limits["max_upload_mb"]:
            return
        limits["max_upload_mb"], limits["max_upload_bytes"] = mb, mb * 1024 ** 2
        app.config["MAX_CONTENT_LENGTH"] = limits["max_upload_bytes"] + _MULTIPART_SLACK
    app.config["SET_UPLOAD_MB"] = set_upload_mb

    def fail(e):
        return jsonify({"error": str(e), "code": e.code}), e.status

    def run_dir(kind: str, name: str) -> Path:
        if kind not in RUN_ROOTS or not _NAME.match(name) or name.startswith("."):
            raise ServiceError("invalid prepared-set id (csv_runs/<name> or pcap_runs/<name>)",
                               "bad_request", 400)
        return get_manager().results_root / kind / name

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(e):  # noqa: ANN001
        if request.path.startswith("/api/"):
            return fail(too_large(limits["max_upload_bytes"]))
        return e

    @app.route("/api/service/config", methods=["GET"])
    def svc_config():
        return jsonify(service_config(get_manager().gpu_available(), limits,
                                      mode=app.config.get("SERVICE_MODE")))

    # --- Prepare -------------------------------------------------------------------------
    @app.route("/api/prepare", methods=["POST"])
    def svc_prepare():
        mgr = get_manager()
        if request.content_length and request.content_length > limits["max_upload_bytes"] + _MULTIPART_SLACK:
            return fail(too_large(limits["max_upload_bytes"]))   # body never read
        body = request.get_json(silent=True) if request.is_json else None
        form = body if isinstance(body, dict) else request.form
        try:
            max_flows, other = _form_options(form)
            url = (form.get("url") or "").strip()
            f = None if request.is_json else request.files.get("file")
            if url and f is not None and f.filename:
                raise ServiceError("give either a file or a URL, not both", "bad_request", 400)
            if url:
                from .urlfetch import UrlImportError, check_url, test_loopback_allowed
                try:
                    check_url(url, test_loopback_allowed())
                except UrlImportError as e:
                    raise ServiceError(str(e), e.code, e.status) from e
                stem = Path(url.split("?")[0].rstrip("/")).name or "url_import"
                name, source = unique_name(stem), {"kind": "url", "url": url}
            elif f is not None and f.filename:
                up = save_upload(f, f.filename, mgr.results_root / "uploads", limits)
                name, source = up["name"], up["source"]
            else:
                raise ServiceError("upload a file (form field 'file') or give an https 'url'",
                                   "invalid_file", 400)
            from .runtime import environment as _env
            job = mgr.create_prepare_job(source, name=name, max_flows=max_flows, other_attack=other,
                                         limits=limits,
                                         environment=_env(app.config.get("SERVICE_MODE") or "mock"),
                                         meta=runtoken.job_meta())
        except (ServiceError, JobError) as e:
            return fail(e)
        job.pop("error_trace", None)
        job["source"] = {k: v for k, v in job["source"].items() if k != "path"}
        job["status_url"] = f"/api/jobs/{job['job_id']}"
        return jsonify(job), 202

    @app.route("/api/prepared/<kind>/<name>", methods=["GET"])
    def svc_prepared(kind, name):
        try:
            return jsonify(summarize(run_dir(kind, name)))
        except (ServiceError, PrepareError) as e:
            return fail(e)

    @app.route("/api/prepared/<kind>/<name>/<fname>", methods=["GET"])
    def svc_prepared_file(kind, name, fname):
        try:
            p = prepared_file(run_dir(kind, name), fname)
        except (ServiceError, PrepareError) as e:
            return fail(e)
        mime = "application/json" if fname.endswith(".json") else "text/csv"
        return send_file(p, mimetype=mime, as_attachment=True, download_name=f"{name}_{fname}")

    @app.route("/api/prepared/import", methods=["POST"])
    def svc_prepared_import():
        mgr = get_manager()
        if request.content_length and request.content_length > MAX_IMPORT_BYTES + _MULTIPART_SLACK:
            return fail(PrepareError("a prepared set is small (<= 500 flows): these files are too "
                                     "large — prepare the raw file on the Prepare page instead",
                                     "too_large", 413))
        files = {}
        for field, fname in (("manifest", "manifest.json"), ("features", "features.csv"),
                             ("labels", "labels.csv")):
            f = request.files.get(field)
            if f is not None and f.filename:
                files[fname] = f.read(MAX_IMPORT_BYTES + 1)
        try:
            from .prepare import import_prepared
            prepared = import_prepared(files, mgr.results_root)
            if runtoken.current():                       # Phase 47: who imported it, with which grant
                mgr.record_set_grant(prepared, runtoken.job_meta())
            kind, name = prepared.split("/")
            return jsonify(summarize(mgr.results_root / kind / name)), 201
        except PrepareError as e:
            return fail(e)
