"""Explicit mapping: CIC-IDS2017 feature columns  <-  Python CICFlowMeter keys.

The pipeline's dataset (data/cicids_full_300.csv, built by data/dataprep.py) uses
the 78 feature columns of the official CIC-IDS2017 MachineLearningCVE CSVs, which
were produced by the Java CICFlowMeter. Here PCAPs are converted by the Python port
(`cicflowmeter` 0.2.0 on PyPI). Every one of the 78 columns is mapped below, each
with a STATUS so any gap is visible rather than silently papered over:

  direct         same definition and units as CIC-IDS2017 (durations/IATs in µs)
  duplicate      CIC-IDS2017 itself duplicates a column (e.g. 'Fwd Header Length.1');
                 filled from the same source key
  approximate    the port fills it with a copy of a related value instead of
                 computing it the Java way (values are plausible but not identical)
  semantic_diff  computed, but on a different basis than CIC-IDS2017: the port
                 measures WHOLE FRAME length (len(packet), incl. Ethernet/IP/TCP
                 headers) where CIC-IDS2017's Java tool used PAYLOAD bytes, and its
                 header-length features count IP-header bytes (TCP) / a constant 8
                 (UDP). Values are internally consistent but not numerically
                 comparable with CIC-IDS2017 rows.

Statuses were set by reading the port's source (cicflowmeter/flow.py and
features/*.py, v0.2.0), not guessed. `tests/test_pcap_ingest.py` pins the list
of 78 names to the header of data/cicids_full_300.csv.
"""
from __future__ import annotations

from typing import Any

FRAME = ("port uses whole-frame length (len(packet), incl. L2/L3/L4 headers); "
         "CIC-IDS2017 used payload bytes")

# (CIC-IDS2017 column, cicflowmeter key, status, note) — in the dataset's column order.
FEATURE_MAP: list[tuple[str, str, str, str]] = [
    ("Destination Port", "dst_port", "direct", ""),
    ("Flow Duration", "flow_duration", "direct", "microseconds"),
    ("Total Fwd Packets", "tot_fwd_pkts", "direct", ""),
    ("Total Backward Packets", "tot_bwd_pkts", "direct", ""),
    ("Total Length of Fwd Packets", "totlen_fwd_pkts", "semantic_diff", FRAME),
    ("Total Length of Bwd Packets", "totlen_bwd_pkts", "semantic_diff", FRAME),
    ("Fwd Packet Length Max", "fwd_pkt_len_max", "semantic_diff", FRAME),
    ("Fwd Packet Length Min", "fwd_pkt_len_min", "semantic_diff", FRAME),
    ("Fwd Packet Length Mean", "fwd_pkt_len_mean", "semantic_diff", FRAME),
    ("Fwd Packet Length Std", "fwd_pkt_len_std", "semantic_diff", FRAME),
    ("Bwd Packet Length Max", "bwd_pkt_len_max", "semantic_diff", FRAME),
    ("Bwd Packet Length Min", "bwd_pkt_len_min", "semantic_diff", FRAME),
    ("Bwd Packet Length Mean", "bwd_pkt_len_mean", "semantic_diff", FRAME),
    ("Bwd Packet Length Std", "bwd_pkt_len_std", "semantic_diff", FRAME),
    ("Flow Bytes/s", "flow_byts_s", "semantic_diff", FRAME),
    ("Flow Packets/s", "flow_pkts_s", "direct", ""),
    ("Flow IAT Mean", "flow_iat_mean", "direct", "microseconds"),
    ("Flow IAT Std", "flow_iat_std", "direct", "microseconds"),
    ("Flow IAT Max", "flow_iat_max", "direct", "microseconds"),
    ("Flow IAT Min", "flow_iat_min", "direct", "microseconds"),
    ("Fwd IAT Total", "fwd_iat_tot", "direct", "microseconds"),
    ("Fwd IAT Mean", "fwd_iat_mean", "direct", "microseconds"),
    ("Fwd IAT Std", "fwd_iat_std", "direct", "microseconds"),
    ("Fwd IAT Max", "fwd_iat_max", "direct", "microseconds"),
    ("Fwd IAT Min", "fwd_iat_min", "direct", "microseconds"),
    ("Bwd IAT Total", "bwd_iat_tot", "direct", "microseconds"),
    ("Bwd IAT Mean", "bwd_iat_mean", "direct", "microseconds"),
    ("Bwd IAT Std", "bwd_iat_std", "direct", "microseconds"),
    ("Bwd IAT Max", "bwd_iat_max", "direct", "microseconds"),
    ("Bwd IAT Min", "bwd_iat_min", "direct", "microseconds"),
    ("Fwd PSH Flags", "fwd_psh_flags", "direct", ""),
    ("Bwd PSH Flags", "bwd_psh_flags", "direct", ""),
    ("Fwd URG Flags", "fwd_urg_flags", "direct", ""),
    ("Bwd URG Flags", "bwd_urg_flags", "direct", ""),
    ("Fwd Header Length", "fwd_header_len", "semantic_diff",
     "port sums IP-header bytes (ihl*4) for TCP / constant 8 for UDP"),
    ("Bwd Header Length", "bwd_header_len", "semantic_diff",
     "port sums IP-header bytes (ihl*4) for TCP / constant 8 for UDP"),
    ("Fwd Packets/s", "fwd_pkts_s", "direct", ""),
    ("Bwd Packets/s", "bwd_pkts_s", "direct", ""),
    ("Min Packet Length", "pkt_len_min", "semantic_diff", FRAME),
    ("Max Packet Length", "pkt_len_max", "semantic_diff", FRAME),
    ("Packet Length Mean", "pkt_len_mean", "semantic_diff", FRAME),
    ("Packet Length Std", "pkt_len_std", "semantic_diff", FRAME),
    ("Packet Length Variance", "pkt_len_var", "semantic_diff", FRAME),
    ("FIN Flag Count", "fin_flag_cnt", "direct", ""),
    ("SYN Flag Count", "syn_flag_cnt", "direct", ""),
    ("RST Flag Count", "rst_flag_cnt", "direct", ""),
    ("PSH Flag Count", "psh_flag_cnt", "direct", ""),
    ("ACK Flag Count", "ack_flag_cnt", "direct", ""),
    ("URG Flag Count", "urg_flag_cnt", "direct", ""),
    ("CWE Flag Count", "cwr_flag_count", "approximate",
     "port copies Fwd URG Flags into this column instead of counting CWR"),
    ("ECE Flag Count", "ece_flag_cnt", "direct", ""),
    ("Down/Up Ratio", "down_up_ratio", "direct", "backward/forward packet counts"),
    ("Average Packet Size", "pkt_size_avg", "semantic_diff", FRAME),
    ("Avg Fwd Segment Size", "fwd_seg_size_avg", "semantic_diff",
     "copy of Fwd Packet Length Mean; " + FRAME),
    ("Avg Bwd Segment Size", "bwd_seg_size_avg", "semantic_diff",
     "copy of Bwd Packet Length Mean; " + FRAME),
    ("Fwd Header Length.1", "fwd_header_len", "duplicate",
     "CIC-IDS2017 repeats 'Fwd Header Length'; same source key"),
    ("Fwd Avg Bytes/Bulk", "fwd_byts_b_avg", "direct", "payload-based, as in CIC-IDS2017"),
    ("Fwd Avg Packets/Bulk", "fwd_pkts_b_avg", "direct", ""),
    ("Fwd Avg Bulk Rate", "fwd_blk_rate_avg", "direct", ""),
    ("Bwd Avg Bytes/Bulk", "bwd_byts_b_avg", "direct", "payload-based, as in CIC-IDS2017"),
    ("Bwd Avg Packets/Bulk", "bwd_pkts_b_avg", "direct", ""),
    ("Bwd Avg Bulk Rate", "bwd_blk_rate_avg", "direct", ""),
    ("Subflow Fwd Packets", "subflow_fwd_pkts", "approximate",
     "port copies Total Fwd Packets (Java divides by the subflow count)"),
    ("Subflow Fwd Bytes", "subflow_fwd_byts", "approximate",
     "port copies Total Length of Fwd Packets; " + FRAME),
    ("Subflow Bwd Packets", "subflow_bwd_pkts", "approximate",
     "port copies Total Backward Packets (Java divides by the subflow count)"),
    ("Subflow Bwd Bytes", "subflow_bwd_byts", "approximate",
     "port copies Total Length of Bwd Packets; " + FRAME),
    ("Init_Win_bytes_forward", "init_fwd_win_byts", "direct", "0 for UDP"),
    ("Init_Win_bytes_backward", "init_bwd_win_byts", "direct",
     "0 for UDP; port takes the LAST backward window, Java the first"),
    ("act_data_pkt_fwd", "fwd_act_data_pkts", "direct", ""),
    ("min_seg_size_forward", "fwd_seg_size_min", "semantic_diff",
     "port: minimum forward IP-header size (same basis as Fwd Header Length)"),
    ("Active Mean", "active_mean", "direct", "microseconds"),
    ("Active Std", "active_std", "direct", "microseconds"),
    ("Active Max", "active_max", "direct", "microseconds"),
    ("Active Min", "active_min", "direct", "microseconds"),
    ("Idle Mean", "idle_mean", "direct", "microseconds"),
    ("Idle Std", "idle_std", "direct", "microseconds"),
    ("Idle Max", "idle_max", "direct", "microseconds"),
    ("Idle Min", "idle_min", "direct", "microseconds"),
]

CIC_FEATURES: list[str] = [c for c, _, _, _ in FEATURE_MAP]

# Identification columns kept alongside the features. They are NOT part of the 78
# CIC-IDS2017 features (the MachineLearningCVE CSVs dropped them) and a later phase
# must hide them from the model the same way dataset.drop_columns does.
META_COLUMNS: list[str] = ["flow_id", "src_ip", "src_port", "dst_ip", "protocol",
                           "protocol_name", "timestamp"]

STATUSES = ("direct", "duplicate", "approximate", "semantic_diff")


def mapping_report(extracted_keys: set[str] | None = None) -> dict[str, Any]:
    """Feature-by-feature mapping plus a summary. With `extracted_keys` (the keys
    the port actually produced for this PCAP) any mapped key that was absent is
    reported as `missing`, and port keys that map to nothing as `unmapped`."""
    rows = []
    for cic, key, status, note in FEATURE_MAP:
        present = extracted_keys is None or key in extracted_keys
        rows.append({"cic_ids2017": cic, "cicflowmeter": key,
                     "status": status if present else "missing", "note": note})
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES + ("missing",)}
    used = {k for _, k, _, _ in FEATURE_MAP}
    meta_src = {"src_ip", "dst_ip", "src_port", "protocol", "timestamp"}
    unmapped = sorted((extracted_keys or set()) - used - meta_src)
    return {
        "reference": "CIC-IDS2017 MachineLearningCVE (78 features, Java CICFlowMeter)",
        "extractor": "cicflowmeter (Python port) 0.2.0",
        "n_reference_features": len(FEATURE_MAP),
        "counts": counts,
        "comparable": counts["direct"] + counts["duplicate"],
        "unmapped_extractor_keys": unmapped,
        "features": rows,
    }


def mapping_markdown(report: dict[str, Any]) -> str:
    """Human-readable table of the mapping report (written next to flows.csv)."""
    c = report["counts"]
    out = [
        "# Feature mapping: CIC-IDS2017 vs extracted flows", "",
        f"Reference: {report['reference']}  ",
        f"Extractor: {report['extractor']}", "",
        f"- {report['n_reference_features']} reference features, all present: "
        f"{'yes' if c['missing'] == 0 else 'NO — ' + str(c['missing']) + ' missing'}",
        f"- direct (comparable): {c['direct']} · duplicate: {c['duplicate']} · "
        f"approximate: {c['approximate']} · semantic_diff: {c['semantic_diff']}",
        f"- extractor keys with no CIC-IDS2017 column: "
        f"{', '.join(report['unmapped_extractor_keys']) or 'none'}", "",
        "| CIC-IDS2017 column | cicflowmeter key | status | note |",
        "|---|---|---|---|",
    ]
    for r in report["features"]:
        out.append(f"| {r['cic_ids2017']} | `{r['cicflowmeter']}` | {r['status']} | {r['note']} |")
    return "\n".join(out) + "\n"
