"""Prepare step (Phase 43b): raw PCAP/CSV -> a downloadable PREPARED SET.

Prepare is data preparation for benchmarking, not analysis: it validates the
input, draws a bounded representative candidate pool, and applies the existing
flow_rules.yaml selection (ingest/run.py — the Phase 35-37 code, unchanged in
what it computes). The Benchmark step then reads only the small prepared set.

A prepared set IS a run directory under results/csv_runs|pcap_runs/<name>/
(its id is "<kind>/<name>"). On top of the unified contract files it holds:

    features.csv    the selected flows: identification columns + the 78
                    CIC-IDS2017 features + selection provenance. NO labels.
    labels.csv      labelled CSV only, kept separate (label isolation).
    manifest.json   the ingestion manifest + a `prepared_set` block: source
                    (filename or URL, size, sha256), input type, caps and
                    sampling used, every rule (fired, flows admitted), class
                    counts before/after selection, classes lost, timestamps,
                    tool versions, and the input.json record (for re-import).

The same set can be re-uploaded (import_prepared) to benchmark it on another
server; the import checks the contract and the manifest's file hashes.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

PREPARED_SCHEMA = 1
FEATURES_CSV = "features.csv"
LABELS_CSV = "labels.csv"
MANIFEST = "manifest.json"
DOWNLOADABLE = (FEATURES_CSV, LABELS_CSV, MANIFEST)
MAX_IMPORT_BYTES = 20 * 1024 ** 2        # a prepared set is <= 500 flows: a few MB at most
MAX_SELECTED = 500
FRAMING = ("Prepared set for BENCHMARKING LLM resource efficiency (and accuracy where labels "
           "exist). Data preparation only: not an analysis and not a threat-detection result.")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

Progress = Callable[..., None]


class PrepareError(ValueError):
    """A prepare or import request cannot be completed. `code`/`status` map to HTTP."""

    def __init__(self, message: str, code: str = "invalid_file", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 ** 2), b""):
            h.update(chunk)
    return h.hexdigest()


def unique_name(filename: str) -> str:
    from ..ingest.run import safe_run_name

    stem = safe_run_name(Path(filename or "input").stem)[:48]
    return f"{stem}_{datetime.now(timezone.utc):%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:4]}"


def tool_versions() -> dict[str, Optional[str]]:
    from importlib import metadata

    def ver(dist: str) -> Optional[str]:
        try:
            return metadata.version(dist)
        except metadata.PackageNotFoundError:
            return None
    out = {"python": platform.python_version()}
    for d in ("agentmeter", "pandas", "numpy", "scapy", "cicflowmeter"):
        out[d] = ver(d)
    if out["agentmeter"] is None:                  # running from a checkout: read pyproject
        try:
            import tomllib

            from ..config import PROJECT_ROOT
            out["agentmeter"] = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"]["version"]
        except Exception:  # noqa: BLE001 — informational only
            pass
    return out


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=float), encoding="utf-8")


# ---------------------------------------------------------------------------
# Prepare: source file on disk -> prepared set
# ---------------------------------------------------------------------------
def prepare_file(src: Path, *, source: dict[str, Any], results_root: Path, name: str,
                 max_flows: int, other_attack: bool, limits: dict[str, int],
                 progress: Optional[Progress] = None, environment: Optional[dict] = None) -> str:
    """Run the existing ingestion on `src` and finish the prepared set. Returns
    its id ("csv_runs/<name>" or "pcap_runs/<name>"). Raises PrepareError."""
    from ..ingest import run as ingest_run
    from ..ingest.csv_input import CsvValidationError
    from ..ingest.pcap import PcapValidationError
    from ..ingest.rules import RuleConfigError

    report = progress or (lambda **kw: None)
    started = _now()
    src = Path(src)
    if not src.is_file() or src.stat().st_size == 0:
        raise PrepareError("the input file is empty", "invalid_file", 400)
    kind = ingest_run.detect_input_type(src)
    out_root = Path(results_root) / ("pcap_runs" if kind == "pcap" else "csv_runs")
    label = source.get("filename") or src.name
    report(phase="validating", message=f"validating {label} ({kind.upper()})")
    try:
        if kind == "pcap":
            ingest_run.process_pcap(src, name=name, out_root=out_root, max_flows=max_flows,
                                    max_packets=limits["max_pcap_packets"],
                                    max_pool_flows=limits["max_pcap_flows"],
                                    windows=limits.get("pcap_windows"), progress=report)
        else:
            ingest_run.process_csv(src, name=name, out_root=out_root, max_flows=max_flows,
                                   other_attack=other_attack, max_rows=limits["max_csv_rows"],
                                   progress=report, max_bytes=src.stat().st_size)
    except (CsvValidationError, PcapValidationError) as e:
        raise PrepareError(str(e).replace(src.name, label), "invalid_file", 400) from e
    except RuleConfigError as e:
        raise PrepareError(f"rule-base error: {e}", "server_config", 500) from e
    except ImportError as e:            # scapy / cicflowmeter not installed on this server
        raise PrepareError("PCAP ingestion is not installed on this server "
                           "(pip install -r requirements-pcap.txt; pip install --no-deps "
                           f"cicflowmeter==0.2.0): {e}", "pcap_unsupported", 501) from e
    run_dir = out_root / name
    report(phase="writing", message="writing the prepared set")
    finish_prepared_set(run_dir, source=source, limits=limits, started_at=started, environment=environment)
    return f"{out_root.name}/{name}"


def _class_counts(meta: dict, man: dict) -> dict[str, Any]:
    """Class counts before/after selection and classes lost (labelled CSV only)."""
    if meta.get("source_type") != "csv" or not meta.get("labelled"):
        return {"available": False, "reason": "no ground-truth labels in this input"}
    v = man.get("validation") or {}
    lab = v.get("label") or {}
    samp = v.get("sampling") or {}
    pool = {k: int(n) for k, n in (lab.get("distribution") or {}).items()}
    in_file = samp.get("class_counts_in_file") or pool
    selected = {k: int(n) for k, n in (meta.get("label_distribution_selected") or {}).items()}
    scheme = (meta.get("class_scheme") or {}).get("classes") or sorted(pool)
    return {
        "available": True, "class_set": scheme,
        "in_file": in_file,                    # usable rows per class across the whole file
        "in_pool": pool,                       # candidate pool after sampling + validation
        "selected": selected,                  # the prepared set
        "absent_in_file": [c for c in scheme if not in_file.get(c)],
        "lost_in_sampling": [c for c in scheme if in_file.get(c) and not pool.get(c)],
        "lost_in_selection": [c for c in scheme if pool.get(c) and not selected.get(c)],
    }


def finish_prepared_set(run_dir: Path, *, source: dict[str, Any], limits: dict[str, int],
                        started_at: Optional[str] = None,
                        environment: Optional[dict] = None) -> dict[str, Any]:
    """Write features.csv and the manifest's prepared_set block for a run dir."""
    run_dir = Path(run_dir)
    meta = _read(run_dir / "input.json")
    man = _read(run_dir / MANIFEST)
    audit = _read(run_dir / "selection_audit.json")
    shutil.copyfile(run_dir / "selected_flows.csv", run_dir / FEATURES_CSV)
    files = {FEATURES_CSV: {"rows": int(meta.get("rows_selected") or 0),
                            "sha256": sha256_file(run_dir / FEATURES_CSV)}}
    if (run_dir / LABELS_CSV).exists():
        files[LABELS_CSV] = {"rows": int(meta.get("rows_selected") or 0),
                             "sha256": sha256_file(run_dir / LABELS_CSV)}
    if meta.get("source_type") == "csv":
        sampling = (man.get("validation") or {}).get("sampling") or {}
    else:
        sampling = man.get("sampling") or {}
    classes = _class_counts(meta, man)
    man["prepared_set"] = {
        "schema": PREPARED_SCHEMA,
        "id": f"{run_dir.parent.name}/{run_dir.name}",
        "framing": FRAMING,
        "source": source,
        "input_type": meta.get("source_type"), "input_role": meta.get("input_role"),
        "evaluation_mode": meta.get("evaluation_mode"),
        "limits": {k: limits.get(k) for k in ("max_upload_mb", "max_url_download_gb", "max_csv_rows",
                                               "max_pcap_packets", "max_pcap_flows", "pcap_windows")},
        "sampling": sampling,
        "selection": {
            "rulebase": (man.get("selection") or {}).get("rulebase"),
            "max_flows": (man.get("selection") or {}).get("max_flows"),
            "selected": int(meta.get("rows_selected") or 0),
            "pool_size": int(meta.get("rows_total") or 0),
            "selection_mode": meta.get("selection_mode"),
            "rules_fired": audit.get("rules_fired", []),
            "rules": [{"id": r["id"], "type": r["type"], "kind": r.get("kind", "admission"),
                       "enabled": r.get("enabled", True), "fired": r.get("fired", False),
                       "matched": r.get("matched", 0), "admitted": r.get("admitted", 0)}
                      for r in audit.get("rules", [])],
        },
        "class_counts": classes,
        "files": files,
        "started_at": started_at, "finished_at": _now(),
        "tool_versions": tool_versions(),
        "backend": environment,                # provider / GPU of the server that prepared it
        "input_metadata": meta,                # the input.json record, for re-import
    }
    _dump(run_dir / MANIFEST, man)
    return man["prepared_set"]


def prepared_file(run_dir: Path, name: str) -> Path:
    """A downloadable file of a prepared set (features.csv, labels.csv, manifest.json)."""
    if name not in DOWNLOADABLE:
        raise PrepareError(f"unknown prepared-set file {name!r} (one of {', '.join(DOWNLOADABLE)})",
                           "not_found", 404)
    p = Path(run_dir) / name
    if not p.exists():
        raise PrepareError(f"{name} is not part of this prepared set", "not_found", 404)
    return p


# ---------------------------------------------------------------------------
# Re-upload of a prepared set (Benchmark page) — small files, strict checks
# ---------------------------------------------------------------------------
def import_prepared(files: dict[str, bytes], results_root: Path) -> str:
    """Store an uploaded prepared set (manifest.json + features.csv [+ labels.csv])
    as a new run dir after checking it is a well-formed, unmodified prepared set.
    Returns its id. Raises PrepareError naming what is wrong."""
    import io

    import pandas as pd

    from ..ingest.unified import (SELECTED_COLUMNS, InputContractError, load_input_run)

    total = sum(len(b) for b in files.values())
    if total > MAX_IMPORT_BYTES:
        raise PrepareError(f"a prepared set is small (<= {MAX_SELECTED} flows); these files are "
                           f"{total:,} bytes — prepare the raw file on the Prepare page instead",
                           "too_large", 413)
    for need in (MANIFEST, FEATURES_CSV):
        if not files.get(need):
            raise PrepareError(f"missing {need}: upload the manifest.json and features.csv "
                               "(and labels.csv for a labelled set) downloaded from Prepare",
                               "invalid_prepared_set", 400)
    try:
        man = json.loads(files[MANIFEST].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise PrepareError(f"manifest.json is not valid JSON ({e})", "invalid_prepared_set", 400) from e
    ps = man.get("prepared_set") if isinstance(man, dict) else None
    if not isinstance(ps, dict) or ps.get("schema") != PREPARED_SCHEMA or not isinstance(
            ps.get("input_metadata"), dict):
        raise PrepareError("manifest.json is not an AgentMeter prepared-set manifest "
                           f"(prepared_set.schema {PREPARED_SCHEMA} with input_metadata)",
                           "invalid_prepared_set", 400)
    meta = dict(ps["input_metadata"])
    labelled = bool((meta.get("capabilities") or {}).get("accuracy"))
    if labelled and not files.get(LABELS_CSV):
        raise PrepareError("this prepared set is labelled: upload its labels.csv too",
                           "invalid_prepared_set", 400)
    if not labelled and files.get(LABELS_CSV):
        raise PrepareError("labels.csv given, but the manifest says the set is unlabelled",
                           "invalid_prepared_set", 400)
    for fname, info in (ps.get("files") or {}).items():
        if fname in files and info.get("sha256") and \
                hashlib.sha256(files[fname]).hexdigest() != info["sha256"]:
            raise PrepareError(f"{fname} does not match its manifest (sha256 differs): the files "
                               "were edited or come from different prepared sets", "invalid_prepared_set", 400)
    try:
        head = pd.read_csv(io.BytesIO(files[FEATURES_CSV]), nrows=0)
    except Exception as e:  # noqa: BLE001
        raise PrepareError(f"features.csv is not a readable CSV ({e})", "invalid_prepared_set", 400) from e
    if any(str(c).strip().lower() == "label" for c in head.columns):
        raise PrepareError("features.csv contains a label column: labels must stay in labels.csv",
                           "invalid_prepared_set", 400)
    if list(head.columns) != SELECTED_COLUMNS:
        raise PrepareError("features.csv does not have the prepared-set columns (identification "
                           "columns + 78 CIC-IDS2017 features + selection columns)",
                           "invalid_prepared_set", 400)
    kind = {"csv": "csv_runs", "pcap": "pcap_runs"}.get(meta.get("source_type"))
    if kind is None:
        raise PrepareError("manifest input_metadata.source_type must be csv or pcap",
                           "invalid_prepared_set", 400)
    name = unique_name(f"{str(meta.get('run_name') or 'prepared')[:30]}_import")
    out = Path(results_root) / kind / name
    out.mkdir(parents=True, exist_ok=False)
    try:
        (out / "selected_flows.csv").write_bytes(files[FEATURES_CSV])
        (out / FEATURES_CSV).write_bytes(files[FEATURES_CSV])
        if labelled:
            (out / LABELS_CSV).write_bytes(files[LABELS_CSV])
        meta["run_name"] = name
        _dump(out / "input.json", meta)
        run = load_input_run(out)                       # the unified contract check
        if len(run.selected) > MAX_SELECTED or len(run.selected) != int(meta.get("rows_selected") or -1):
            raise InputContractError(f"features.csv has {len(run.selected)} flows; the manifest "
                                     f"says {meta.get('rows_selected')} (max {MAX_SELECTED})")
        if run.selected["flow_id"].duplicated().any():
            raise InputContractError("features.csv has duplicate flow_id values")
        man.setdefault("imported", {})
        man["imported"] = {"at": _now(), "from_prepared_set": ps.get("id")}
        ps["id"] = f"{kind}/{name}"
        _dump(out / MANIFEST, man)
        audit = {"rules": ps.get("selection", {}).get("rules", []),
                 "rules_fired": ps.get("selection", {}).get("rules_fired", [])}
        _dump(out / "selection_audit.json", audit)
    except (InputContractError, ValueError, KeyError) as e:
        shutil.rmtree(out, ignore_errors=True)
        raise PrepareError(f"not a usable prepared set: {e}", "invalid_prepared_set", 400) from e
    return f"{kind}/{name}"


# ---------------------------------------------------------------------------
# Summary of a prepared set (validation summary + sampling + downloads)
# ---------------------------------------------------------------------------
def _large_input(meta: dict[str, Any], man: dict[str, Any]) -> dict[str, Any]:
    """Whether the candidate pool is a sample of a larger input, stated plainly."""
    if meta["source_type"] == "csv":
        s = (man.get("validation") or {}).get("sampling") or {}
        if s.get("method") == "stratified_reservoir":
            return {"capped": True, "unit": "rows", "cap": s.get("max_pool_rows"),
                    "message": (f"Large input: the candidate pool is a stratified random sample of "
                                f"{s.get('pool_rows'):,} rows drawn from all {s.get('rows_in_file'):,} "
                                "rows of the file (not the first rows); the selection is drawn from it.")}
        return {"capped": False}
    s = man.get("sampling") or {}
    notes = []
    if s.get("method") == "time_windows":
        notes.append(f"Large input: flows come from {s.get('window_count')} time windows spread evenly "
                     f"across the {s.get('capture_span_s', 0):,.0f} s capture ({s.get('packets_parsed'):,} "
                     f"of {s.get('packets_in_capture'):,} packets parsed), not from its first packets.")
    if s.get("pool_capped"):
        notes.append(f"The {s.get('flows_extracted'):,} extracted flows were reduced to a random pool of "
                     f"{s.get('pool_flows'):,} (pool cap) before selection.")
    if notes:
        return {"capped": True, "unit": "windows" if s.get("method") == "time_windows" else "flows",
                "cap": s.get("max_packets") if s.get("method") == "time_windows" else s.get("max_pool_flows"),
                "message": " ".join(notes)}
    return {"capped": False}


def summarize(run_dir: Path) -> dict[str, Any]:
    """Validation summary of a prepared set, read from the files ingestion wrote."""
    run_dir = Path(run_dir)
    meta = _read(run_dir / "input.json")
    if not meta:
        raise PrepareError(f"prepared set not found: {run_dir.name}", "run_missing", 404)
    man = _read(run_dir / MANIFEST)
    audit = _read(run_dir / "selection_audit.json")
    ps = man.get("prepared_set") or {}
    rules = [{"id": r["id"], "type": r["type"], "kind": r.get("kind", "admission"),
              "enabled": r.get("enabled", True), "fired": r.get("fired", False),
              "matched": r.get("matched", 0), "admitted": r.get("admitted", 0),
              "description": r.get("description", "")} for r in audit.get("rules", [])]
    run_id = f"{run_dir.parent.name}/{run_dir.name}"
    out: dict[str, Any] = {
        "run": run_id, "prepared_set": run_id,
        "source_type": meta["source_type"], "source_file": meta.get("source_file"),
        "source": ps.get("source") or {},
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
        "sampling": ps.get("sampling") or {},
        "class_counts": ps.get("class_counts") or {},
        "file_size_bytes": None,
        "downloads": [f for f in DOWNLOADABLE if (run_dir / f).exists()] if ps else [],
        "imported": man.get("imported"),
        "framing": FRAMING,
    }
    if meta["source_type"] == "csv":
        v = man.get("validation", {})
        out.update({
            "rows_read": v.get("rows_read"), "rows_in_pool": v.get("rows_in_pool"),
            "rows_usable": v.get("rows_usable"),
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
        samp = man.get("sampling") or {}
        out.update({
            "packets": samp.get("packets_in_capture", cap.get("packet_count")),
            "packets_parsed": samp.get("packets_parsed", cap.get("packet_count")),
            "capture_duration_s": samp.get("capture_span_s", cap.get("duration_s")),
            "flows_extracted": ext.get("flows"), "packets_skipped": ext.get("packets_skipped"),
            "class_distribution": {}, "file_size_bytes": cap.get("file_size_bytes"),
        })
        out["notes"] = list(cap.get("notes") or []) + ([ext["skipped_note"]] if ext.get("skipped_note") else [])
    cc = out["class_counts"]
    lost = (cc.get("lost_in_sampling") or []) + (cc.get("lost_in_selection") or [])
    if lost:
        out["notes"].append("Classes in the file but absent from the prepared set: " + ", ".join(lost)
                            + " — accuracy cannot be measured for them.")
    if cc.get("absent_in_file"):
        out["notes"].append("Classes of the class set not present in the file at all: "
                            + ", ".join(cc["absent_in_file"]) + ".")
    return out
