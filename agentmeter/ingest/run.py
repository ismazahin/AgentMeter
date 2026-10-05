"""Orchestrate the input layer for BOTH input roles.

    process_pcap(...)  raw PCAP  -> results/pcap_runs/<name>/   (efficiency only)
    process_csv(...)   CIC CSV   -> results/csv_runs/<name>/    (accuracy if labelled)
    process_input(...) detect the type by content and dispatch

Every run directory satisfies the unified contract (unified.py): input.json +
selected_flows.csv (+ labels.csv for labelled CSV). Path-specific extras:

  PCAP: manifest.json, flows.csv (all flows), feature_map.json/.md, selection_audit.json
  CSV:  manifest.json (validation report), schema_report.md, selection_audit.json

Nothing is written to the study DB; the 4-agent pipeline and SAW are not used.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ..config import PROJECT_ROOT
from . import csv_input, feature_map, flows, pcap, rules
from .unified import SELECTED_COLUMNS, InputRun, build_metadata, write_input_run

RESULTS_ROOT = PROJECT_ROOT / "results"
DEFAULT_OUT_ROOT = RESULTS_ROOT / "pcap_runs"
DEFAULT_CSV_OUT_ROOT = RESULTS_ROOT / "csv_runs"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_run_name(name: str) -> str:
    """Filesystem-safe run name (no separators / traversal)."""
    cleaned = _SAFE.sub("_", name).strip("._") or "capture"
    return cleaned[:80]


def _dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=float), encoding="utf-8")


def _selected_frame(sel: rules.SelectionResult):
    """The rule engine's selection reduced to the contract's column order."""
    return sel.selected[SELECTED_COLUMNS].copy()


def detect_input_type(path: str | Path) -> str:
    """'pcap' when the magic bytes say packet capture, else 'csv' (validated later)."""
    p = Path(path)
    if p.is_file():
        with p.open("rb") as fh:
            if fh.read(4) in pcap._MAGIC:
                return "pcap"
    return "csv"


# ---------------------------------------------------------------------------
# PCAP (raw) — efficiency only
# ---------------------------------------------------------------------------
def process_pcap(pcap_path: str | Path, *, name: Optional[str] = None,
                 out_root: str | Path | None = None, rules_path: str | Path | None = None,
                 max_flows: Optional[int] = None, max_packets: Optional[int] = None,
                 write: bool = True) -> dict[str, Any]:
    """Run the input layer on one capture. Raises PcapValidationError for a file
    that is not a usable capture and RuleConfigError for a bad rule-base."""
    src = Path(pcap_path)
    rb = rules.load_rulebase(rules_path)            # fail fast on a bad rule-base
    stats = pcap.validate_pcap(src, max_packets=max_packets)
    ext = flows.extract_flows(src, max_packets=max_packets)
    fmap = feature_map.mapping_report(ext.extracted_keys if len(ext.flows) else None)
    sel = rules.select_flows(ext.flows, rb, max_flows=max_flows)
    audit = sel.audit()

    run_name = safe_run_name(name or src.stem)
    out_dir = Path(out_root or DEFAULT_OUT_ROOT) / run_name
    c = fmap["counts"]
    metadata = build_metadata(
        source_type="pcap", source_file=src.name, run_name=run_name, labelled=False,
        feature_match={
            "status": "approximate", "reference": fmap["reference"], "extractor": fmap["extractor"],
            "comparable": fmap["comparable"], "counts": c,
            "note": (f"{c['semantic_diff']} features use whole-frame lengths (CIC-IDS2017 used "
                     f"payload bytes); see feature_map.md"),
        },
        feature_columns=feature_map.CIC_FEATURES, rows_total=int(len(ext.flows)),
        rows_selected=len(sel.selected), rules_fired=audit["rules_fired"],
        extra_reasons=[f"{c['semantic_diff']} of 78 features are not numerically comparable "
                       "with CIC-IDS2017 (whole-frame vs payload lengths)"],
    )
    manifest = {
        "run_name": run_name,
        "source_file": src.name,
        "input": {k: metadata[k] for k in ("source_type", "input_role", "evaluation_mode")},
        "capture": stats.to_dict(),
        "extraction": {
            "backend": ext.backend, "flows": int(len(ext.flows)),
            "packets_seen": ext.packets_seen, "packets_used": ext.packets_used,
            "packets_skipped": ext.packets_skipped,
            "skipped_note": "CICFlowMeter (Python port) builds flows from IPv4 TCP/UDP only",
        },
        "feature_map": {k: fmap[k] for k in ("reference", "extractor", "counts", "comparable",
                                             "unmapped_extractor_keys")},
        "selection": {"rulebase": rb.source, "max_flows": sel.max_flows,
                      "selected": len(sel.selected), "rules_fired": audit["rules_fired"]},
        "output_dir": str(out_dir) if write else None,
    }
    run = InputRun(metadata=metadata, selected=_selected_frame(sel))
    if write:
        write_input_run(out_dir, run)
        ext.flows.to_csv(out_dir / "flows.csv", index=False)
        _dump(out_dir / "feature_map.json", fmap)
        (out_dir / "feature_map.md").write_text(feature_map.mapping_markdown(fmap), encoding="utf-8")
        _dump(out_dir / "selection_audit.json", audit)
        _dump(out_dir / "manifest.json", manifest)
    return {"manifest": manifest, "flows": ext.flows, "selection": sel,
            "feature_map": fmap, "audit": audit, "input": run}


# ---------------------------------------------------------------------------
# CSV (CIC-IDS2017 format) — accuracy available when labelled
# ---------------------------------------------------------------------------
def schema_markdown(report: dict[str, Any], metadata: dict[str, Any]) -> str:
    cols, lab = report["columns"], report["label"]
    out = [
        "# CSV schema report (CIC-IDS2017 format)", "",
        f"- File: {report['file']} — {report['rows_read']:,} rows read, "
        f"{report['rows_usable']:,} usable",
        f"- Feature columns: {cols['present']}/{cols['expected']} present "
        f"(feature_match: **{metadata['feature_match']['status']}**)",
        f"- Missing: {', '.join(cols['missing']) or 'none'}",
        f"- Extra columns ignored: {', '.join(cols['extra_ignored']) or 'none'}",
        f"- Identification columns found: {', '.join(cols['meta_found']) or 'none'}",
        f"- Rows dropped (NaN/Inf/non-numeric feature): {report['rows_dropped_nan_inf']:,}",
        f"- Label column: {lab['column'] or 'NOT FOUND'} → evaluation_mode "
        f"**{metadata['evaluation_mode']}**",
    ] + [f"- Note: {n}" for n in report.get("notes", [])]
    if lab["found"]:
        out += [f"- Rows excluded (label outside the 5 classes): "
                f"{report['rows_excluded_out_of_taxonomy']:,} "
                f"{lab['excluded_out_of_taxonomy'] or ''}", "",
                "| class | usable rows | selected |", "|---|---|---|"]
        sel = metadata["label_distribution_selected"]
        for k, v in sorted(lab["distribution"].items()):
            out.append(f"| {k} | {v:,} | {sel.get(k, 0):,} |")
    return "\n".join(out) + "\n"


def process_csv(csv_path: str | Path, *, name: Optional[str] = None,
                out_root: str | Path | None = None, rules_path: str | Path | None = None,
                max_flows: Optional[int] = None, max_rows: Optional[int] = None,
                allow_partial: bool = False, config_path: Optional[str] = None,
                write: bool = True) -> dict[str, Any]:
    """Run the input layer on a CIC-IDS2017-format CSV. Raises CsvValidationError
    for a CSV that does not match the schema and RuleConfigError for a bad rule-base."""
    src = Path(csv_path)
    rb = rules.load_rulebase(rules_path)
    data = csv_input.load_csv(src, rule_fields=rules.rulebase_fields(rb), allow_partial=allow_partial,
                              max_rows=max_rows, config_path=config_path)
    # The rule engine sees features + META only — labels were split off in load_csv.
    sel = rules.select_flows(data.features, rb, max_flows=max_flows)
    audit = sel.audit()
    selected = _selected_frame(sel)

    labels = None
    sel_dist: dict[str, int] = {}
    if data.labelled:
        labels = (data.labels.set_index("flow_id").loc[selected["flow_id"]].reset_index())
        sel_dist = {str(k): int(v) for k, v in labels["label"].value_counts().items()}
        audit["label_distribution_selected"] = sel_dist  # transparency only; rules are label-blind

    rep = data.report
    n_present = rep["columns"]["present"]
    run_name = safe_run_name(name or src.stem)
    out_dir = Path(out_root or DEFAULT_CSV_OUT_ROOT) / run_name
    metadata = build_metadata(
        source_type="csv", source_file=src.name, run_name=run_name, labelled=data.labelled,
        feature_match={
            "status": "exact" if n_present == 78 else "partial",
            "reference": "CIC-IDS2017 MachineLearningCVE (78 features, Java CICFlowMeter)",
            "comparable": n_present, "missing": rep["columns"]["missing"],
            "note": ("features supplied in CIC-IDS2017 format — same definitions as the "
                     "reference dataset" if n_present == 78 else
                     f"only {n_present}/78 features present; missing ones are empty"),
        },
        feature_columns=[c for c in feature_map.CIC_FEATURES if c not in rep["columns"]["missing"]],
        rows_total=rep["rows_usable"], rows_selected=len(selected),
        rules_fired=audit["rules_fired"], label_distribution_selected=sel_dist,
    )
    manifest = {"run_name": run_name, "source_file": src.name,
                "input": {k: metadata[k] for k in ("source_type", "input_role", "evaluation_mode")},
                "validation": rep,
                "selection": {"rulebase": rb.source, "max_flows": sel.max_flows,
                              "selected": len(selected), "rules_fired": audit["rules_fired"]},
                "output_dir": str(out_dir) if write else None}
    run = InputRun(metadata=metadata, selected=selected, labels=labels)
    if write:
        write_input_run(out_dir, run)
        _dump(out_dir / "selection_audit.json", audit)
        _dump(out_dir / "manifest.json", manifest)
        (out_dir / "schema_report.md").write_text(schema_markdown(rep, metadata), encoding="utf-8")
    return {"manifest": manifest, "selection": sel, "audit": audit, "input": run, "report": rep}


def process_input(path: str | Path, **kw) -> dict[str, Any]:
    """Dispatch by content: packet capture -> process_pcap, anything else -> process_csv."""
    if detect_input_type(path) == "pcap":
        for k in ("max_rows", "allow_partial", "config_path"):
            kw.pop(k, None)
        return process_pcap(path, **kw)
    kw.pop("max_packets", None)
    return process_csv(path, **kw)
