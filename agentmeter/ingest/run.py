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
from . import csv_input, feature_map, flows, pcap, pcap_window, rules
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
DEFAULT_PCAP_WINDOWS = 10
DEFAULT_SEED = 42


def _sample_capture(src: Path, *, max_packets: Optional[int], windows: Optional[int],
                    work_dir: Path, progress) -> tuple[Path, dict[str, Any]]:
    """Return the capture to parse and how it was sampled. Within the packet
    budget the file is used as is; above it, K evenly spaced time windows are
    copied into a smaller capture (pcap_window.py)."""
    pcap.sniff_format(src)                          # wrong type / bad header: reject first
    idx = pcap_window.index(src, progress=progress)
    base = {"packets_in_capture": idx.packets, "capture_first_ts": idx.first_ts,
            "capture_last_ts": idx.last_ts, "capture_span_s": round(idx.span_s, 6),
            "max_packets": max_packets}
    if max_packets is None or idx.packets <= max_packets or not idx.span_s:
        return src, {**base, "method": "full_capture", "windows": [],
                     "packets_parsed": idx.packets,
                     "description": (f"the whole capture ({idx.packets:,} packets"
                                     + (f", within the {max_packets:,}-packet budget)" if max_packets else ")"))}
    k = int(windows or DEFAULT_PCAP_WINDOWS)
    plan = pcap_window.plan_windows(idx, k, max_packets)
    work_dir.mkdir(parents=True, exist_ok=True)
    reduced = work_dir / f"{src.stem}.windows{src.suffix or '.pcap'}"
    if progress:
        progress(phase="windowing", message=f"copying {len(plan.windows)} time windows")
    taken = pcap_window.write_windows(src, reduced, plan, progress=progress)
    return reduced, {**base, "method": "time_windows", "packets_parsed": taken,
                     "window_count": len(plan.windows), "budget_per_window": plan.budget_per_window,
                     "windows": plan.to_list(),
                     "description": (f"{len(plan.windows)} time windows spread evenly across the "
                                     f"{idx.span_s:,.0f} s capture, up to {plan.budget_per_window:,} "
                                     f"packets each ({taken:,} of {idx.packets:,} packets parsed)")}


def _cap_pool(flow_df, max_pool_flows: Optional[int], seed: int):
    """Uniform random subsample (seeded) when there are more flows than the pool cap;
    file order is kept. Never a first-N cut."""
    if max_pool_flows is None or len(flow_df) <= max_pool_flows:
        return flow_df, False
    keep = flow_df.sample(n=max_pool_flows, random_state=seed).index.sort_values()
    return flow_df.loc[keep].reset_index(drop=True), True


def process_pcap(pcap_path: str | Path, *, name: Optional[str] = None,
                 out_root: str | Path | None = None, rules_path: str | Path | None = None,
                 max_flows: Optional[int] = None, max_packets: Optional[int] = None,
                 max_pool_flows: Optional[int] = None, windows: Optional[int] = None,
                 seed: int = DEFAULT_SEED, progress=None, write: bool = True) -> dict[str, Any]:
    """Run the input layer on one capture. Raises PcapValidationError for a file
    that is not a usable capture and RuleConfigError for a bad rule-base.

    max_packets is the parse budget: a larger capture is sampled as `windows`
    evenly spaced time windows (not its first packets). max_pool_flows caps the
    candidate pool by a seeded uniform subsample. Selection then runs unchanged."""
    src = Path(pcap_path)
    rb = rules.load_rulebase(rules_path)            # fail fast on a bad rule-base
    run_name = safe_run_name(name or src.stem)
    out_dir = Path(out_root or DEFAULT_OUT_ROOT) / run_name
    parse_src, sampling = _sample_capture(src, max_packets=max_packets, windows=windows,
                                          work_dir=out_dir.parent / ".work", progress=progress)
    try:
        if progress:
            progress(phase="validating", message="validating packets")
        stats = pcap.validate_pcap(parse_src)
        if progress:
            progress(phase="extracting", message="extracting flows (CICFlowMeter)")
        ext = flows.extract_flows(parse_src)
    finally:
        if parse_src != src:
            parse_src.unlink(missing_ok=True)
    stats.path, stats.file_size_bytes = str(src), src.stat().st_size
    pool, pool_capped = _cap_pool(ext.flows, max_pool_flows, seed)
    sampling.update({"flows_extracted": int(len(ext.flows)), "pool_flows": int(len(pool)),
                     "max_pool_flows": max_pool_flows, "pool_capped": pool_capped,
                     "pool_method": (f"seeded uniform random subsample (seed {seed})" if pool_capped
                                     else "all extracted flows")})
    if progress:
        progress(phase="selecting", message="applying the flow-selection rules")
    fmap = feature_map.mapping_report(ext.extracted_keys if len(ext.flows) else None)
    sel = rules.select_flows(pool, rb, max_flows=max_flows)
    audit = sel.audit()

    c = fmap["counts"]
    metadata = build_metadata(
        source_type="pcap", source_file=src.name, run_name=run_name, labelled=False,
        feature_match={
            "status": "approximate", "reference": fmap["reference"], "extractor": fmap["extractor"],
            "comparable": fmap["comparable"], "counts": c,
            "note": (f"{c['semantic_diff']} features use whole-frame lengths (CIC-IDS2017 used "
                     f"payload bytes); see feature_map.md"),
        },
        feature_columns=feature_map.CIC_FEATURES, rows_total=int(len(pool)),
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
        "sampling": sampling,
        "feature_map": {k: fmap[k] for k in ("reference", "extractor", "counts", "comparable",
                                             "unmapped_extractor_keys")},
        "selection": {"rulebase": rb.source, "max_flows": sel.max_flows,
                      "selected": len(sel.selected), "rules_fired": audit["rules_fired"]},
        "output_dir": str(out_dir) if write else None,
    }
    run = InputRun(metadata=metadata, selected=_selected_frame(sel))
    if write:
        write_input_run(out_dir, run)
        pool.to_csv(out_dir / "flows.csv", index=False)
        _dump(out_dir / "feature_map.json", fmap)
        (out_dir / "feature_map.md").write_text(feature_map.mapping_markdown(fmap), encoding="utf-8")
        _dump(out_dir / "selection_audit.json", audit)
        _dump(out_dir / "manifest.json", manifest)
    return {"manifest": manifest, "flows": pool, "selection": sel,
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
        f"- Class scheme: **{(metadata['class_scheme'] or {}).get('name', 'n/a (unlabelled)')}** · "
        f"selection: **{metadata['selection_mode']}**",
        f"- Candidate pool: {(report.get('sampling') or {}).get('description', 'whole file')}",
    ] + [f"- Note: {n}" for n in report.get("notes", [])]
    if lab["found"]:
        oa = lab["other_attack"]
        out += [f"- Other Attack: {'enabled' if oa['enabled'] else 'off'} — {oa['rows']:,} rows "
                f"{oa['sources'] or ''}",
                f"- Rows excluded (label outside the 5 classes): "
                f"{report['rows_excluded_out_of_taxonomy']:,} "
                f"{lab['excluded_out_of_taxonomy'] or ''}",
                f"- Rows excluded (missing label): {report['rows_excluded_missing_label']:,}", "",
                "| class | usable rows | selected |", "|---|---|---|"]
        sel = metadata["label_distribution_selected"]
        for k, v in sorted(lab["distribution"].items()):
            out.append(f"| {k} | {v:,} | {sel.get(k, 0):,} |")
    return "\n".join(out) + "\n"


def process_csv(csv_path: str | Path, *, name: Optional[str] = None,
                out_root: str | Path | None = None, rules_path: str | Path | None = None,
                max_flows: Optional[int] = None, max_rows: Optional[int] = None,
                allow_partial: bool = False, other_attack: bool = False, label_blind: bool = False,
                config_path: Optional[str] = None, seed: int = DEFAULT_SEED, progress=None,
                max_bytes: Optional[int] = None, write: bool = True) -> dict[str, Any]:
    """Run the input layer on a CIC-IDS2017-format CSV. Raises CsvValidationError
    for a CSV that does not match the schema and RuleConfigError for a bad rule-base.

    other_attack: keep named attack labels outside the 5 study classes as the 6th
                  class "Other Attack" (user runs only) instead of excluding them.
    label_blind:  do not give the class_balance rule the labels (pure statistical
                  selection, as on the PCAP path)."""
    src = Path(csv_path)
    rb = rules.load_rulebase(rules_path)
    data = csv_input.load_csv(src, rule_fields=rules.rulebase_fields(rb), allow_partial=allow_partial,
                              other_attack=other_attack, max_rows=max_rows, config_path=config_path,
                              seed=seed, progress=progress,
                              max_bytes=max_bytes or csv_input.DEFAULT_MAX_BYTES)
    if progress:
        progress(phase="selecting", message="applying the flow-selection rules")
    # Operators see features + META only. Labels go to the engine as a SEPARATE
    # array, read only by the class_balance constraint, and never reach the
    # selected table (the model's input).
    sel_labels = None if (label_blind or not data.labelled) else data.labels["label"].to_numpy()
    sel = rules.select_flows(data.features, rb, max_flows=max_flows, labels=sel_labels)
    audit = sel.audit()
    selected = _selected_frame(sel)

    labels = None
    sel_dist: dict[str, int] = {}
    if data.labelled:
        labels = (data.labels.set_index("flow_id").loc[selected["flow_id"]].reset_index())
        sel_dist = {str(k): int(v) for k, v in labels["label"].value_counts().items()}
        audit["label_distribution_selected"] = sel_dist

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
        class_scheme=rep["class_scheme"], label_aware_selection=sel.label_aware,
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
        for k in ("max_rows", "allow_partial", "config_path", "other_attack", "label_blind"):
            kw.pop(k, None)
        return process_pcap(path, **kw)
    for k in ("max_packets", "max_pool_flows", "windows"):
        kw.pop(k, None)
    return process_csv(path, **kw)
