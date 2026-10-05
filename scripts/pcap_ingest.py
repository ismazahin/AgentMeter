"""PCAP input layer CLI: validate -> CICFlowMeter flows -> rule-based selection.

    python scripts/pcap_ingest.py data/sample_pcaps/sample_small.pcap
    python scripts/pcap_ingest.py my_capture.pcapng --max-flows 200 --name lab1
    python scripts/pcap_ingest.py --show-rules            # print the rule-base only

Writes results/pcap_runs/<name>/ (flows.csv, selected_flows.csv, feature_map.md,
selection_audit.json, manifest.json). Never touches the study DB or the pipeline.
Needs the optional deps:  pip install -r requirements-pcap.txt  (see that file).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agentmeter.ingest import rules  # noqa: E402
from agentmeter.ingest.pcap import PcapValidationError  # noqa: E402


def _print_rules(rb: rules.RuleBase) -> None:
    print(f"Rule-base: {rb.source}")
    print(f"  budget.max_flows={rb.max_flows}  seed={rb.seed}  derived={list(rb.derived)}")
    for r in rb.rules:
        state = "" if r.enabled else "  [disabled]"
        print(f"  - {r.id} ({r.type}, quota={r.quota}, reserve={r.reserve}){state}")
        print(f"      {r.description}")
        print(f"      params: {r.params}")


def print_rule_table(audit: dict) -> None:
    print(f"  {'rule':<20}{'matched':>8}{'admitted':>10}{'already':>9}  computed")
    for r in audit["rules"]:
        if not r["enabled"]:
            print(f"  {r['id']:<20}{'—':>8}{'—':>10}{'—':>9}  (disabled)")
            continue
        comp = {k: v for k, v in r["computed"].items() if k != "bounds"}
        print(f"  {r['id']:<20}{r['matched']:>8}{r['admitted']:>10}{r['already_selected']:>9}  {comp or ''}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pcap", nargs="?", help=".pcap / .pcapng file")
    ap.add_argument("--rules", default=None, help="rule-base YAML (default configs/flow_rules.yaml)")
    ap.add_argument("--max-flows", type=int, default=None, help="override budget.max_flows")
    ap.add_argument("--max-packets", type=int, default=None, help="stop reading after N packets")
    ap.add_argument("--name", default=None, help="run name (default: file name)")
    ap.add_argument("--out-root", default=None, help="default results/pcap_runs")
    ap.add_argument("--no-write", action="store_true", help="print only, write nothing")
    ap.add_argument("--show-rules", action="store_true", help="print the rule-base and exit")
    args = ap.parse_args(argv)

    try:
        rb = rules.load_rulebase(args.rules)
    except rules.RuleConfigError as e:
        print(f"Rule-base error: {e}", file=sys.stderr)
        return 2
    if args.show_rules:
        _print_rules(rb)
        return 0
    if not args.pcap:
        ap.error("a pcap file is required (or --show-rules)")

    from agentmeter.ingest.run import process_pcap  # after arg parsing: needs scapy

    try:
        res = process_pcap(args.pcap, name=args.name, out_root=args.out_root,
                           rules_path=args.rules, max_flows=args.max_flows,
                           max_packets=args.max_packets, write=not args.no_write)
    except PcapValidationError as e:
        print(f"Rejected: {e}", file=sys.stderr)
        return 2
    except rules.RuleConfigError as e:
        print(f"Rule-base error: {e}", file=sys.stderr)
        return 2

    m = res["manifest"]
    cap, ext, fm, sel = m["capture"], m["extraction"], m["feature_map"], m["selection"]
    meta = res["input"].metadata
    print(f"Input     {meta['input_role']} -> {meta['evaluation_mode']} "
          f"({'; '.join(meta['accuracy_unavailable_reasons'])})")
    print(f"Capture   {m['source_file']} ({cap['file_format']}, {cap['file_size_bytes']:,} bytes)")
    print(f"          {cap['packet_count']:,} packets over {cap['duration_s']:.3f} s "
          f"(TCP {cap['tcp_packets']:,} · UDP {cap['udp_packets']:,} · other {cap['other_packets']:,})")
    for note in cap["notes"]:
        print(f"          note: {note}")
    print(f"Flows     {ext['flows']:,} extracted by {ext['backend']} "
          f"({ext['packets_used']:,} packets used, {ext['packets_skipped']:,} skipped: not IPv4 TCP/UDP)")
    c = fm["counts"]
    print(f"Features  78 CIC-IDS2017 columns: direct {c['direct']} · duplicate {c['duplicate']} · "
          f"approximate {c['approximate']} · semantic_diff {c['semantic_diff']} · missing {c['missing']}")
    print(f"Selection {sel['selected']:,} of {ext['flows']:,} flows (budget {sel['max_flows']}); "
          f"rules fired: {', '.join(sel['rules_fired']) or 'none'}")
    print_rule_table(res["audit"])
    if m["output_dir"]:
        print(f"Wrote     {m['output_dir']}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
