"""Synthetic large inputs for the Phase 43b tests (fast, no scapy).

big_csv():  CIC-IDS2017-format rows replicated from the committed sample CSV,
            with the label of every row chosen by position, so a test can tell
            where in the file a pooled row came from (flow_id = row number).
big_pcap(): a libpcap capture of n UDP "flows" (2 packets each, distinct
            source address per flow), timestamps evenly spaced over `span_s`.
"""
from __future__ import annotations

import struct
from pathlib import Path

from agentmeter.config import PROJECT_ROOT

SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"


def big_csv(path: Path, n_rows: int, rare_tail: int = 0, rare_label: str = "PortScan") -> Path:
    """n_rows data rows cycling through the sample's rows; the last `rare_tail`
    rows get `rare_label` (a class that then only exists at the END of the file)."""
    lines = SAMPLE_CSV.read_text().splitlines()
    hdr, rows = lines[0], lines[1:]
    cols = [c.strip() for c in hdr.split(",")]
    li = next(i for i, c in enumerate(cols) if c.lower() == "label")
    clean = [r for r in rows if r.split(",")[li].strip() not in (rare_label,)]
    with Path(path).open("w") as f:
        f.write(hdr + "\n")
        for i in range(n_rows):
            parts = clean[i % len(clean)].split(",")
            if i >= n_rows - rare_tail:
                parts[li] = rare_label
            f.write(",".join(parts) + "\n")
    return Path(path)


def _csum(b: bytes) -> int:
    s = sum(struct.unpack("!%dH" % (len(b) // 2), b))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return (~s) & 0xFFFF


def big_pcap(path: Path, n_flows: int, span_s: float = 1000.0, t0: float = 1_500_000_000.0) -> Path:
    step = span_s / max(1, n_flows)
    with Path(path).open("wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for i in range(n_flows):
            sip = bytes([10, (i >> 16) & 255, (i >> 8) & 255, i & 255])
            for k in range(2):
                payload = b"x" * 32
                udp = struct.pack("!HHHH", 10000 + (i % 50000), 53, 8 + len(payload), 0) + payload
                ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), i & 0xFFFF, 0, 64, 17, 0,
                                 sip, bytes([192, 168, 1, 1]))
                ip = ip[:10] + struct.pack("!H", _csum(ip)) + ip[12:]
                frame = b"\x02" * 6 + b"\x04" * 6 + b"\x08\x00" + ip + udp
                ts = t0 + i * step + k * 0.0001
                f.write(struct.pack("<IIII", int(ts), int(round((ts % 1) * 1e6)) % 1_000_000,
                                    len(frame), len(frame)))
                f.write(frame)
    return Path(path)
