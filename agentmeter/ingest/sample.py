"""Deterministic synthetic capture for tests and demos (no download needed).

Writes a small libpcap file with a known mix of traffic so every rule type has
something to act on:
  * short HTTP-like TCP flows to :80 and :443 of varying size
  * one long-lived SSH-like TCP flow (> 60 s)        -> very_long_lived / long_duration
  * one bulk TCP transfer (many large packets)        -> high_packet_count / high_volume
  * many short DNS-like UDP flows + a few NTP flows   -> protocol/port coverage
  * ICMP, ARP and an IPv6 packet                      -> counted, skipped by CICFlowMeter

Synthetic traffic between private addresses; it represents no real host or attack.

    python -m agentmeter.ingest.sample data/sample_pcaps/sample_small.pcap
    python -m agentmeter.ingest.sample data/sample_csv/cicids2017_sample.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

# Expected flow count for the default capture (asserted by the tests).
EXPECTED_FLOWS = 8 + 4 + 1 + 1 + 24 + 3


def build_packets():
    from scapy.layers.inet import ICMP, IP, TCP, UDP
    from scapy.layers.inet6 import IPv6
    from scapy.layers.l2 import ARP, Ether
    from scapy.packet import Raw

    # Fixed MACs: an Ether() with unset addresses is filled from THIS host's
    # routes, which would make the file differ between machines.
    def L2():
        return Ether(src="02:00:00:00:00:01", dst="02:00:00:00:00:02")

    pkts = []
    clock = [1_700_000_000.0]

    def emit(p, gap=0.002):
        clock[0] += gap
        p.time = clock[0]
        pkts.append(p)

    def tcp_flow(cli, srv, sport, dport, n_data, size, gap=0.002, close=True):
        e = L2()
        emit(e / IP(src=cli, dst=srv) / TCP(sport=sport, dport=dport, flags="S", window=64240), gap)
        emit(e / IP(src=srv, dst=cli) / TCP(sport=dport, dport=sport, flags="SA", window=65160), gap)
        emit(e / IP(src=cli, dst=srv) / TCP(sport=sport, dport=dport, flags="A"), gap)
        for i in range(n_data):
            if i % 2 == 0:
                emit(e / IP(src=cli, dst=srv) / TCP(sport=sport, dport=dport, flags="PA") / Raw(b"q" * size), gap)
            else:
                emit(e / IP(src=srv, dst=cli) / TCP(sport=dport, dport=sport, flags="PA") / Raw(b"r" * size), gap)
        if close:
            emit(e / IP(src=cli, dst=srv) / TCP(sport=sport, dport=dport, flags="FA"), gap)
            emit(e / IP(src=srv, dst=cli) / TCP(sport=dport, dport=sport, flags="FA"), gap)

    cli, web, ssh, dns, ntp, store = ("192.168.10.5", "192.168.20.10", "192.168.20.22",
                                      "192.168.20.53", "192.168.20.123", "192.168.20.40")
    for i in range(8):                       # 8 short HTTP-like flows, growing size
        tcp_flow(cli, web, 41000 + i, 80, n_data=2 + i, size=60 + 40 * i)
    for i in range(4):                       # 4 HTTPS-like flows
        tcp_flow(cli, web, 42000 + i, 443, n_data=4, size=200)
    tcp_flow(cli, ssh, 43000, 22, n_data=16, size=48, gap=5.0)          # ~95 s long-lived
    tcp_flow(cli, store, 44000, 8080, n_data=120, size=600, gap=0.001)  # bulk transfer
    for i in range(24):                      # 24 DNS-like UDP flows
        emit(L2() / IP(src=cli, dst=dns) / UDP(sport=50000 + i, dport=53) / Raw(b"d" * 32))
        emit(L2() / IP(src=dns, dst=cli) / UDP(sport=53, dport=50000 + i) / Raw(b"a" * 80))
    for i in range(3):                       # 3 NTP-like UDP flows
        emit(L2() / IP(src=cli, dst=ntp) / UDP(sport=123 + 1000 * (i + 1), dport=123) / Raw(b"n" * 48))
        emit(L2() / IP(src=ntp, dst=cli) / UDP(sport=123, dport=123 + 1000 * (i + 1)) / Raw(b"t" * 48))
    for _ in range(3):                       # ignored by CICFlowMeter
        emit(L2() / IP(src=cli, dst=web) / ICMP())
    # hwsrc pinned too: unset, scapy fills it from THIS host's MAC (CI differs from the
    # machine that wrote the committed sample). The value is the committed file's.
    emit(L2() / ARP(hwsrc="02:fc:00:00:00:01", psrc=cli, pdst=web))
    emit(L2() / IPv6(src="fd00::5", dst="fd00::53") / UDP(sport=50100, dport=53) / Raw(b"6" * 32))
    return pkts


def write_sample_pcap(path: str | Path) -> Path:
    from scapy.utils import wrpcap

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    wrpcap(str(p), build_packets())
    return p


# --- CIC-IDS2017-format sample CSV -------------------------------------------
# Raw-style (as the official MachineLearningCVE files): leading-space headers,
# the duplicated ' Fwd Header Length' column, raw labels in ' Label'. Rows are
# taken from data/cicids_full_300.csv (real CIC-IDS2017 rows already in the repo).
SAMPLE_CSV_PER_CLASS = 8
_RAW_LABELS = {"Benign": ["BENIGN"], "Brute Force": ["FTP-Patator", "SSH-Patator"],
               "DoS Hulk": ["DoS Hulk"], "Volumetric DDoS": ["DDoS"], "Port Scanning": ["PortScan"]}
# 5 classes x 8 usable rows, + 1 row with 'Infinity' (dropped) + 2 out-of-taxonomy rows.
EXPECTED_CSV_ROWS = 5 * SAMPLE_CSV_PER_CLASS + 3
EXPECTED_CSV_USABLE = 5 * SAMPLE_CSV_PER_CLASS


def build_sample_csv_text(source: str | Path | None = None) -> str:
    import pandas as pd

    from ..config import PROJECT_ROOT
    from .feature_map import CIC_FEATURES

    df = pd.read_csv(source or PROJECT_ROOT / "data" / "cicids_full_300.csv")
    rows: list[tuple[list, str]] = []
    for canon, raws in _RAW_LABELS.items():
        part = df[df["label"] == canon].head(SAMPLE_CSV_PER_CLASS)
        for i, (_, r) in enumerate(part.iterrows()):
            rows.append(([r[c] for c in CIC_FEATURES], raws[i % len(raws)]))
    inf_row = [df.iloc[0][c] for c in CIC_FEATURES]
    inf_row[CIC_FEATURES.index("Flow Bytes/s")] = "Infinity"      # as in the official files
    rows.insert(3, (inf_row, "BENIGN"))
    for k in (1, 2):                                             # outside the 5-class taxonomy
        rows.insert(10 * k, ([df.iloc[100 + k][c] for c in CIC_FEATURES], "DoS slowloris"))

    header = [" " + ("Fwd Header Length" if c == "Fwd Header Length.1" else c) for c in CIC_FEATURES]
    lines = [",".join(header + [" Label"])]
    for vals, lab in rows:
        lines.append(",".join(_csv_value(v) for v in vals) + "," + lab)
    return "\n".join(lines) + "\n"


def _csv_value(v) -> str:
    if isinstance(v, str):
        return v
    f = float(v)
    return str(int(f)) if f.is_integer() else repr(f)


def write_sample_csv(path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_sample_csv_text(), encoding="utf-8")
    return p


if __name__ == "__main__":  # pragma: no cover
    target = sys.argv[1] if len(sys.argv) > 1 else "data/sample_pcaps/sample_small.pcap"
    out = write_sample_csv(target) if target.endswith(".csv") else write_sample_pcap(target)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
