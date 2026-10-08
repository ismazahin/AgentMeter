"""Service API for the web flow (Phase 41): upload + ingest, run summaries, and
the service config the UI needs. Same-origin routes on the existing Flask app.

  POST /api/ingest              multipart: file (.csv/.pcap/.pcapng), max_flows?, other_attack?
                                -> runs the EXISTING ingestion (ingest/run.py) into
                                   results/csv_runs|pcap_runs/<name>/ and returns its summary
  GET  /api/runs/<kind>/<name>  the same summary, rebuilt from the run's own files
                                (input.json, manifest.json, selection_audit.json)
  GET  /api/service/config      canonical models, max 2, provider/base config, demo flag,
                                and the input limits (Phase 43)

Input guards (Phase 43, config.yaml `service:`): an upload above max_upload_mb is
rejected with 413 BEFORE its body is read (Content-Length check, plus Flask's
MAX_CONTENT_LENGTH for bodies without one), and the body is streamed to disk in
chunks with a running byte count. A large but accepted capture/CSV is parsed only
up to max_pcap_packets / max_pcap_flows / max_csv_rows; the summary's
`large_input` says when a cap was applied. Sampling (rules.py) is unchanged.

Nothing here computes a benchmark: ingestion is ingest/run.py; jobs are jobs.py.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .jobs import RUN_ROOTS, JobManager

DEFAULT_SERVICE_MAX_FLOWS = 50
# config.yaml `service:` key -> (env override, default when neither is set)
_LIMITS = {"max_upload_mb": ("AGENTMETER_MAX_UPLOAD_MB", 200),
           "max_pcap_packets": ("AGENTMETER_MAX_PCAP_PACKETS", 100_000),
           "max_pcap_flows": ("AGENTMETER_MAX_PCAP_FLOWS", 20_000),
           "max_csv_rows": ("AGENTMETER_MAX_CSV_ROWS", 500_000)}
_MULTIPART_SLACK = 1024 ** 2   # form fields + boundaries around the file part
_CHUNK = 1024 ** 2
ALLOWED_EXT = {".csv", ".pcap", ".pcapng"}
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class ServiceError(ValueError):
    def __init__(self, message: str, code: str, status: int):
        super().__init__(message)
        self.code = code
        self.status = status


def service_limits(config_path: str | None = None) -> dict[str, int]:
    """The web service's input limits: env override > config.yaml `service:` > default.
    AGENTMETER_MAX_UPLOAD_BYTES (pre-Phase 43, exact bytes) still wins for the upload."""
    from ..config import load_config

    try:
        cfg = load_config(config_path)
    except FileNotFoundError:
        cfg = None
    out: dict[str, int] = {}
    for key, (env, default) in _LIMITS.items():
        raw = os.environ.get(env) or (cfg.get(f"service.{key}") if cfg is not None else None)
        raw = default if raw is None else raw
        try:
            val = int(raw)
        except (TypeError, ValueError):
            raise ServiceError(f"service limit {key} must be a whole number, got {raw!r}",
                               "server_config", 500) from None
        if val < 1:
            raise ServiceError(f"service limit {key} must be at least 1, got {val}", "server_config", 500)
        out[key] = val
    legacy = os.environ.get("AGENTMETER_MAX_UPLOAD_BYTES")
    out["max_upload_bytes"] = int(legacy) if legacy else out["max_upload_mb"] * 1024 ** 2
    return out


def _fmt_size(n: int) -> str:
    return f"{n / 1024 ** 2:,.0f} MB" if n >= 1024 ** 2 else f"{n:,} bytes"


def too_large(limit: int) -> "ServiceError":
    return ServiceError(f"file too large: the upload limit is {_fmt_size(limit)}. Upload a smaller "
                        "file (e.g. a slice of the capture/CSV) — AgentMeter benchmarks a bounded "
                        "sample of flows anyway.", "too_large", 413)


def _large_input(meta: dict[str, Any], man: dict[str, Any]) -> dict[str, Any]:
    """Whether an extraction cap was applied, stated plainly for the summary/UI."""
    if meta["source_type"] == "csv":
        cap = (man.get("validation") or {}).get("truncated_at")
        if cap:
            return {"capped": True, "unit": "rows", "cap": cap,
                    "message": f"Large input: selection drawn from the first {cap:,} rows of the "
                               f"file (row cap {cap:,})."}
        return {"capped": False}
    ext = man.get("extraction") or {}
    if ext.get("capped"):
        cap = ext.get("flow_cap")
        return {"capped": True, "unit": "flows", "cap": cap,
                "message": f"Large input: selection drawn from the first {cap:,} flows of the "
                           f"capture (flow cap {cap:,}); the rest of the capture was not parsed."}
    if ext.get("packets_capped"):
        cap = ext.get("packet_cap")
        return {"capped": True, "unit": "packets", "cap": cap,
                "message": f"Large input: flows built from the first {cap:,} packets of the "
                           f"capture (packet cap {cap:,}); the rest was not parsed."}
    return {"capped": False}


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def summarize_run(run_dir: Path) -> dict[str, Any]:
    """Validation summary of a prepared run, read from the files ingestion wrote."""
    meta = _read(run_dir / "input.json")
    if not meta:
        raise ServiceError(f"prepared run not found: {run_dir.name}", "run_missing", 404)
    man = _read(run_dir / "manifest.json")
    audit = _read(run_dir / "selection_audit.json")
    rules = [{"id": r["id"], "type": r["type"], "kind": r.get("kind", "admission"),
              "enabled": r.get("enabled", True), "fired": r.get("fired", False),
              "matched": r.get("matched", 0), "admitted": r.get("admitted", 0),
              "description": r.get("description", "")} for r in audit.get("rules", [])]
    out: dict[str, Any] = {
        "run": f"{run_dir.parent.name}/{run_dir.name}",
        "source_type": meta["source_type"], "source_file": meta.get("source_file"),
        "input_role": meta["input_role"], "evaluation_mode": meta["evaluation_mode"],
        "accuracy_available": bool(meta["capabilities"]["accuracy"]),
        "accuracy_unavailable_reasons": meta.get("accuracy_unavailable_reasons", []),
        "class_scheme": meta.get("class_scheme"), "selection_mode": meta.get("selection_mode"),
        "feature_match": meta.get("feature_match"),
        "rows_total": meta.get("rows_total"), "rows_selected": meta.get("rows_selected"),
        "label_distribution_selected": meta.get("label_distribution_selected") or {},
        "rules": rules, "rules_fired": audit.get("rules_fired", []),
        "notes": [], "non_validated": True,
        "large_input": _large_input(meta, man),
        "file_size_bytes": None,
    }
    if meta["source_type"] == "csv":
        v = man.get("validation", {})
        out.update({
            "rows_read": v.get("rows_read"), "rows_usable": v.get("rows_usable"),
            "rows_dropped": v.get("rows_dropped_nan_inf"),
            "rows_excluded_out_of_taxonomy": v.get("rows_excluded_out_of_taxonomy"),
            "rows_excluded_missing_label": v.get("rows_excluded_missing_label"),
            "columns_present": (v.get("columns") or {}).get("present"),
            "columns_missing": (v.get("columns") or {}).get("missing", []),
            "label_column": (v.get("label") or {}).get("column"),
            "class_distribution": (v.get("label") or {}).get("distribution", {}),
            "other_attack": (v.get("label") or {}).get("other_attack"),
            "file_size_bytes": v.get("file_size_bytes"),
        })
        out["notes"] = list(v.get("notes") or [])
    else:
        cap, ext = man.get("capture", {}), man.get("extraction", {})
        out.update({
            "packets": cap.get("packet_count"), "capture_duration_s": cap.get("duration_s"),
            "flows_extracted": ext.get("flows"), "packets_skipped": ext.get("packets_skipped"),
            "class_distribution": {}, "file_size_bytes": cap.get("file_size_bytes"),
        })
        out["notes"] = list(cap.get("notes") or []) + ([ext["skipped_note"]] if ext.get("skipped_note") else [])
    return out


def _unique_run_name(filename: str) -> str:
    from ..ingest.run import safe_run_name

    stem = safe_run_name(Path(filename).stem)[:48]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stem}_{stamp}_{uuid.uuid4().hex[:4]}"


def save_stream(stream, dest: Path, max_bytes: int) -> int:
    """Copy `stream` to `dest` in chunks, never holding the file in memory; abort
    (and delete the partial file) as soon as it passes max_bytes."""
    n = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                n += len(chunk)
                if n > max_bytes:
                    raise too_large(max_bytes)
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return n


def ingest_upload(fileobj, filename: str, results_root: Path, uploads_dir: Path,
                  max_flows: int, other_attack: bool,
                  limits: dict[str, int] | None = None) -> dict[str, Any]:
    """Save an upload and run the EXISTING ingestion on it; return the run summary.
    `limits` (service_limits()) bounds the upload and the parse of a large input."""
    lim = limits or service_limits()
    from ..ingest import run as ingest_run
    from ..ingest.csv_input import CsvValidationError
    from ..ingest.pcap import PcapValidationError
    from ..ingest.rules import RuleConfigError

    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ServiceError(f"unsupported file type {ext or '(none)'}: upload a .csv (CIC-IDS2017 "
                           "format) or a .pcap/.pcapng capture", "invalid_file", 400)
    name = _unique_run_name(filename)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    saved = uploads_dir / f"{name}{ext}"
    save_stream(getattr(fileobj, "stream", fileobj), saved, lim["max_upload_bytes"])
    if saved.stat().st_size == 0:
        saved.unlink(missing_ok=True)
        raise ServiceError("the uploaded file is empty", "invalid_file", 400)

    kind = ingest_run.detect_input_type(saved)
    out_root = results_root / ("pcap_runs" if kind == "pcap" else "csv_runs")
    try:
        if kind == "pcap":
            ingest_run.process_pcap(saved, name=name, out_root=out_root, max_flows=max_flows,
                                    max_packets=lim["max_pcap_packets"],
                                    max_extract_flows=lim["max_pcap_flows"])
        else:
            ingest_run.process_csv(saved, name=name, out_root=out_root, max_flows=max_flows,
                                   other_attack=other_attack, max_rows=lim["max_csv_rows"])
    except (CsvValidationError, PcapValidationError) as e:
        raise ServiceError(str(e).replace(saved.name, filename), "invalid_file", 400) from e
    except RuleConfigError as e:
        raise ServiceError(f"rule-base error: {e}", "server_config", 500) from e
    except ImportError as e:            # scapy / cicflowmeter not installed on this server
        raise ServiceError("PCAP ingestion is not installed on this server "
                           "(pip install -r requirements-pcap.txt; pip install --no-deps "
                           f"cicflowmeter==0.2.0): {e}", "pcap_unsupported", 501) from e
    return summarize_run(out_root / name)


def service_config(gpu_available: bool, limits: dict[str, int] | None = None) -> dict[str, Any]:
    from ..session.benchmark import MAX_MODELS, canonical_models

    lim = limits or service_limits()
    forced = os.environ.get("AGENTMETER_SERVICE_PROVIDER")      # "mock" | "hf" (ops override)
    provider = forced or ("hf" if gpu_available else "mock")
    demo = provider == "mock"
    return {
        "canonical_models": canonical_models(), "max_models": MAX_MODELS,
        "provider": provider, "base_config": "run_full_l4.yaml" if provider == "hf" else None,
        "gpu_available": gpu_available, "demo_mode": demo,
        "default_max_flows": DEFAULT_SERVICE_MAX_FLOWS,
        "limits": {**lim, "max_upload_label": _fmt_size(lim["max_upload_bytes"])},
        "note": ("No GPU on this server: runs use the MOCK provider (a deterministic heuristic, not a "
                 "language model). Use it to try the flow; numbers are not model measurements."
                 if demo else "Real models, 4-bit NF4, one model at a time on this server's GPU."),
    }


def register_service(app, get_manager: Callable[[], JobManager]) -> None:
    from flask import jsonify, request
    from werkzeug.exceptions import RequestEntityTooLarge

    limits = service_limits()       # read once at startup, like the rest of the app config
    body_cap = limits["max_upload_bytes"] + _MULTIPART_SLACK
    # Werkzeug enforces this while it parses the body, so an over-limit upload sent
    # without a Content-Length (chunked) is cut off too. Never loosen a lower cap.
    if not app.config.get("MAX_CONTENT_LENGTH") or app.config["MAX_CONTENT_LENGTH"] > body_cap:
        app.config["MAX_CONTENT_LENGTH"] = body_cap

    def fail(e: ServiceError):
        return jsonify({"error": str(e), "code": e.code}), e.status

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(e):  # noqa: ANN001
        if request.path.startswith("/api/"):
            return fail(too_large(limits["max_upload_bytes"]))
        return e

    @app.route("/api/service/config", methods=["GET"])
    def svc_config():
        return jsonify(service_config(get_manager().gpu_available(), limits))

    @app.route("/api/ingest", methods=["POST"])
    def svc_ingest():
        mgr = get_manager()
        # Rejected on the declared size alone: the body is never read.
        if request.content_length and request.content_length > body_cap:
            return fail(too_large(limits["max_upload_bytes"]))
        f = request.files.get("file")
        if f is None or not f.filename:
            return fail(ServiceError("no file uploaded (form field 'file')", "invalid_file", 400))
        try:
            max_flows = int(request.form.get("max_flows") or DEFAULT_SERVICE_MAX_FLOWS)
        except ValueError:
            return fail(ServiceError("max_flows must be a whole number", "bad_request", 400))
        if not 1 <= max_flows <= 500:
            return fail(ServiceError("max_flows must be between 1 and 500", "bad_request", 400))
        other = str(request.form.get("other_attack", "")).lower() in ("1", "true", "on", "yes")
        try:
            summary = ingest_upload(f, f.filename, mgr.results_root, mgr.results_root / "uploads",
                                    max_flows, other, limits)
        except ServiceError as e:
            return fail(e)
        return jsonify(summary), 201

    @app.route("/api/runs/<kind>/<name>", methods=["GET"])
    def svc_run(kind, name):
        if kind not in RUN_ROOTS or not _NAME.match(name):
            return fail(ServiceError("invalid run name", "bad_request", 400))
        try:
            return jsonify(summarize_run(get_manager().results_root / kind / name))
        except ServiceError as e:
            return fail(e)
