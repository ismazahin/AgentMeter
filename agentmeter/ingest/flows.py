"""Flow extraction: PCAP -> per-flow CIC-IDS2017-aligned feature table.

Backend: the Python CICFlowMeter port (`cicflowmeter` 0.2.0, MIT). Its own CLI
reads offline files through scapy's AsyncSniffer with a BPF filter, which needs
the `tcpdump` binary and a sniffer thread — fragile on servers/CI. Instead we
stream packets with scapy's PcapReader, apply the same filter in Python
(IPv4 + TCP/UDP), and feed them to the port's own FlowSession, so the flow
assembly and feature maths are exactly the port's. Only the I/O is ours.

Output columns: META_COLUMNS (identification, not features) followed by the 78
CIC-IDS2017 features in the dataset's exact order (see feature_map.py).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from .feature_map import CIC_FEATURES, FEATURE_MAP, META_COLUMNS
from .pcap import iter_packets

_PROTO_NAMES = {6: "TCP", 17: "UDP"}


@dataclass
class FlowExtraction:
    flows: pd.DataFrame
    packets_seen: int
    packets_used: int            # IPv4 TCP/UDP packets fed to CICFlowMeter
    packets_skipped: int         # non-IPv4 or not TCP/UDP (CICFlowMeter ignores them)
    extracted_keys: set[str] = field(default_factory=set)
    backend: str = "cicflowmeter-python"
    flow_cap: Optional[int] = None       # the max_extract_flows bound in force, if any
    capped: bool = False                 # True when the cap stopped the read early


class _Collector:
    """Minimal OutputWriter for the port: keep finished flows in memory."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write(self, data: dict) -> None:
        self.rows.append(data)


def _make_session():
    from cicflowmeter.flow_session import FlowSession  # lazy: optional dependency

    class _OfflineFlowSession(FlowSession):
        # Skip FlowSession/DefaultSession.__init__: they build a CSV/HTTP writer
        # and sniffer plumbing we don't use. These are the attributes the port's
        # on_packet_received()/garbage_collect() read (pinned: cicflowmeter==0.2.0).
        def __init__(self) -> None:  # noqa: D401 — deliberate partial init
            self.flows = {}
            self.packets_count = 0
            self.logger = logging.getLogger("cicflowmeter")
            self.output_writer = _Collector()

    return _OfflineFlowSession()


def extract_flows(pcap_path: str | Path, max_packets: Optional[int] = None,
                  max_extract_flows: Optional[int] = None) -> FlowExtraction:
    """Run CICFlowMeter over `pcap_path` and return the normalised flow table.

    max_extract_flows bounds the work on a large capture: once that many flows
    (finished + still open) exist, reading stops, the open flows are flushed and
    the table holds the FIRST N flows of the capture (capped=True says so).
    Selection runs on that table unchanged."""
    from scapy.layers.inet import IP, TCP, UDP

    session = _make_session()
    seen = used = 0
    capped = False
    for pkt in iter_packets(pcap_path, max_packets=max_packets):
        seen += 1
        if IP in pkt and (TCP in pkt or UDP in pkt):
            session.on_packet_received(pkt)
            used += 1
            # Stop only once a flow BEYOND the cap exists, so a capture with exactly
            # max_extract_flows flows is not reported as capped.
            if (max_extract_flows is not None
                    and len(session.output_writer.rows) + len(session.flows) > max_extract_flows):
                capped = True
                break
    session.garbage_collect(None)  # flush every still-open flow
    raw = session.output_writer.rows
    if max_extract_flows is not None and len(raw) > max_extract_flows:
        # Finished flows come first, then open ones in creation order, so this drops
        # the newest flow(s) — the one that crossed the cap (or a timeout split).
        raw = raw[:max_extract_flows]
        capped = True

    extracted_keys = set(raw[0].keys()) if raw else set()
    df = normalise(raw)
    return FlowExtraction(flows=df, packets_seen=seen, packets_used=used,
                          packets_skipped=seen - used, extracted_keys=extracted_keys,
                          flow_cap=max_extract_flows, capped=capped)


def normalise(raw_rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Port keys -> META_COLUMNS + 78 CIC-IDS2017 columns (numeric, finite)."""
    cols = META_COLUMNS + CIC_FEATURES
    if not raw_rows:
        return pd.DataFrame(columns=cols)
    src = pd.DataFrame(raw_rows)
    out = pd.DataFrame(index=src.index)
    out["flow_id"] = [f"f{i:05d}" for i in range(len(src))]
    out["src_ip"] = src["src_ip"].astype(str)
    out["src_port"] = pd.to_numeric(src["src_port"], errors="coerce").astype("Int64")
    out["dst_ip"] = src["dst_ip"].astype(str)
    out["protocol"] = pd.to_numeric(src["protocol"], errors="coerce").astype("Int64")
    out["protocol_name"] = [_PROTO_NAMES.get(int(p), str(p)) if pd.notna(p) else "?"
                            for p in out["protocol"]]
    out["timestamp"] = src["timestamp"].astype(str)
    for cic, key, _status, _note in FEATURE_MAP:
        # scapy timestamps are Decimal-like; coerce everything to float. A key the
        # port did not emit becomes NaN and is reported as `missing` by the map.
        out[cic] = pd.to_numeric(src[key], errors="coerce").astype(float) if key in src else float("nan")
    # Same Inf policy as dataprep: CIC-IDS2017 rows with Inf were dropped there;
    # here a rate over a zero-length flow is set to 0 instead of dropping the flow
    # (the port already returns 0 for those; this only guards stray infinities).
    out[CIC_FEATURES] = out[CIC_FEATURES].replace([np.inf, -np.inf], 0.0)
    return out[cols]
