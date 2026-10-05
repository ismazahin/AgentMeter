"""Orchestrate the PCAP input layer: validate -> extract -> map -> select -> write.

Outputs go ONLY to results/pcap_runs/<name>/ (never the study DB):
    manifest.json          capture stats, extraction counts, paths, rule-base used
    flows.csv              all extracted flows (META_COLUMNS + 78 CIC-IDS2017 features)
    feature_map.json/.md   CIC-IDS2017 column mapping + gap report
    selected_flows.csv     the rule-selected subset (+ selection_rule/reason/matched_rules)
    selection_audit.json   which rules fired, computed thresholds, why each flow was taken
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ..config import PROJECT_ROOT
from . import feature_map, flows, pcap, rules

DEFAULT_OUT_ROOT = PROJECT_ROOT / "results" / "pcap_runs"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_run_name(name: str) -> str:
    """Filesystem-safe run name (no separators / traversal)."""
    cleaned = _SAFE.sub("_", name).strip("._") or "capture"
    return cleaned[:80]


def process_pcap(pcap_path: str | Path, *, name: Optional[str] = None,
                 out_root: str | Path | None = None, rules_path: str | Path | None = None,
                 max_flows: Optional[int] = None, max_packets: Optional[int] = None,
                 write: bool = True) -> dict[str, Any]:
    """Run the whole input layer on one capture. Raises PcapValidationError for a
    file that is not a usable capture and RuleConfigError for a bad rule-base."""
    src = Path(pcap_path)
    rb = rules.load_rulebase(rules_path)            # fail fast on a bad rule-base
    stats = pcap.validate_pcap(src, max_packets=max_packets)
    ext = flows.extract_flows(src, max_packets=max_packets)
    fmap = feature_map.mapping_report(ext.extracted_keys if len(ext.flows) else None)
    sel = rules.select_flows(ext.flows, rb, max_flows=max_flows)
    audit = sel.audit()

    run_name = safe_run_name(name or src.stem)
    out_dir = Path(out_root or DEFAULT_OUT_ROOT) / run_name
    manifest = {
        "run_name": run_name,
        "source_file": src.name,
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
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        ext.flows.to_csv(out_dir / "flows.csv", index=False)
        sel.selected.to_csv(out_dir / "selected_flows.csv", index=False)
        (out_dir / "feature_map.json").write_text(json.dumps(fmap, indent=2), encoding="utf-8")
        (out_dir / "feature_map.md").write_text(feature_map.mapping_markdown(fmap), encoding="utf-8")
        (out_dir / "selection_audit.json").write_text(json.dumps(audit, indent=2, default=float),
                                                      encoding="utf-8")
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=float),
                                               encoding="utf-8")
    return {"manifest": manifest, "flows": ext.flows, "selection": sel,
            "feature_map": fmap, "audit": audit}
