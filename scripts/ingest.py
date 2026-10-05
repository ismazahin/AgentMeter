"""Input layer CLI for BOTH input roles — the type is detected from the content.

    python scripts/ingest.py data/sample_csv/cicids2017_sample.csv     # labelled CSV
    python scripts/ingest.py data/sample_pcaps/sample_small.pcap        # raw PCAP
    python scripts/ingest.py day.csv --max-flows 300 --allow-partial

CSV (CIC-IDS2017 format)  -> results/csv_runs/<name>/   accuracy available if labelled
PCAP (raw)                -> results/pcap_runs/<name>/  resource efficiency only
Both write the unified contract (input.json + selected_flows.csv [+ labels.csv]).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pcap_ingest  # noqa: E402  (PCAP path: same output as scripts/pcap_ingest.py)

from agentmeter.ingest import rules  # noqa: E402
from agentmeter.ingest.csv_input import CsvValidationError  # noqa: E402
from agentmeter.ingest.run import detect_input_type, process_csv  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file", help=".csv (CIC-IDS2017 format) or .pcap/.pcapng")
    ap.add_argument("--rules", default=None, help="rule-base YAML (default configs/flow_rules.yaml)")
    ap.add_argument("--max-flows", type=int, default=None, help="override budget.max_flows")
    ap.add_argument("--max-rows", type=int, default=None, help="CSV: read only the first N rows")
    ap.add_argument("--max-packets", type=int, default=None, help="PCAP: stop after N packets")
    ap.add_argument("--allow-partial", action="store_true",
                    help="CSV: accept a subset of the 78 features (marked 'partial')")
    ap.add_argument("--name", default=None, help="run name (default: file name)")
    ap.add_argument("--out-root", default=None, help="override the results/<type>_runs root")
    ap.add_argument("--no-write", action="store_true", help="print only, write nothing")
    args = ap.parse_args(argv)

    if detect_input_type(args.file) == "pcap":
        fwd = [args.file]
        for flag, val in (("--rules", args.rules), ("--max-flows", args.max_flows),
                          ("--max-packets", args.max_packets), ("--name", args.name),
                          ("--out-root", args.out_root)):
            if val is not None:
                fwd += [flag, str(val)]
        if args.no_write:
            fwd.append("--no-write")
        return pcap_ingest.main(fwd)

    try:
        res = process_csv(args.file, name=args.name, out_root=args.out_root, rules_path=args.rules,
                          max_flows=args.max_flows, max_rows=args.max_rows,
                          allow_partial=args.allow_partial, write=not args.no_write)
    except CsvValidationError as e:
        print(f"Rejected: {e}", file=sys.stderr)
        return 2
    except rules.RuleConfigError as e:
        print(f"Rule-base error: {e}", file=sys.stderr)
        return 2

    rep, meta, m = res["report"], res["input"].metadata, res["manifest"]
    cols, lab = rep["columns"], rep["label"]
    print(f"Input     {meta['input_role']} -> {meta['evaluation_mode']}"
          + (f" ({'; '.join(meta['accuracy_unavailable_reasons'])})"
             if meta['accuracy_unavailable_reasons'] else ""))
    print(f"CSV       {rep['file']} ({rep['file_size_bytes']:,} bytes): {rep['rows_read']:,} rows read, "
          f"{rep['rows_usable']:,} usable ({rep['rows_dropped_nan_inf']:,} NaN/Inf dropped, "
          f"{rep['rows_excluded_out_of_taxonomy']:,} out-of-taxonomy excluded)")
    print(f"Features  {cols['present']}/78 CIC-IDS2017 columns present -> feature_match "
          f"{meta['feature_match']['status']}"
          + (f"; missing: {', '.join(cols['missing'])}" if cols["missing"] else ""))
    if lab["found"]:
        print(f"Labels    column '{lab['column']}' (held out, never a feature): "
              + ", ".join(f"{k} {v:,}" for k, v in sorted(lab["distribution"].items())))
    else:
        print("Labels    none found -> efficiency only")
    print(f"Selection {meta['rows_selected']:,} of {meta['rows_total']:,} rows "
          f"(budget {m['selection']['max_flows']}); rules fired: "
          f"{', '.join(meta['rules_fired']) or 'none'}")
    if meta["label_distribution_selected"]:
        print("          selected classes: " + ", ".join(
            f"{k} {v}" for k, v in sorted(meta["label_distribution_selected"].items())))
    pcap_ingest.print_rule_table(res["audit"])
    if m["output_dir"]:
        print(f"Wrote     {m['output_dir']}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
