"""PCAP ingestion: validate that a file is a real capture and report basic stats.

Validation is two-step:
  1. Magic bytes — the file must start with a libpcap (µs or ns, either byte
     order) or pcapng signature. Anything else (text, zip, CSV …) is rejected
     without being parsed.
  2. A full streaming parse with scapy. A header or record that cannot be parsed
     rejects the file as corrupt; a capture with zero packets is rejected too.

The same pass collects the stats (packet count, capture duration, IP/TCP/UDP
counts), so a valid file is read exactly once here.
"""
from __future__ import annotations

import logging
import struct
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# libpcap (µs / ns resolution, both byte orders) and pcapng section header.
_MAGIC = {
    b"\xd4\xc3\xb2\xa1": "pcap",
    b"\xa1\xb2\xc3\xd4": "pcap",
    b"\x4d\x3c\xb2\xa1": "pcap-ns",
    b"\xa1\xb2\x3c\x4d": "pcap-ns",
    b"\x0a\x0d\x0d\x0a": "pcapng",
}

DEFAULT_MAX_BYTES = 2 * 1024 ** 3  # 2 GiB — a service-side upload bound


class PcapValidationError(ValueError):
    """The file is not a usable packet capture (wrong type, corrupt, or empty)."""


@dataclass
class PcapStats:
    path: str
    file_format: str
    file_size_bytes: int
    packet_count: int
    first_ts: Optional[float]
    last_ts: Optional[float]
    duration_s: float
    ip_packets: int
    tcp_packets: int
    udp_packets: int
    other_packets: int          # non-IP or IP without TCP/UDP (ignored by flow extraction)
    truncated_at: Optional[int] = None  # set when max_packets stopped the read early
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sniff_format(path: str | Path) -> str:
    """Return 'pcap' / 'pcap-ns' / 'pcapng' from the magic bytes, or raise.
    For libpcap files the 24-byte global header is sanity-checked too."""
    p = Path(path)
    if not p.is_file():
        raise PcapValidationError(f"not a file: {p}")
    with p.open("rb") as fh:
        head = fh.read(24)
    fmt = _MAGIC.get(head[:4])
    if fmt is None:
        raise PcapValidationError(
            f"{p.name}: not a pcap/pcapng capture (magic bytes {head[:4].hex() or 'empty'})")
    if fmt.startswith("pcap") and fmt != "pcapng":
        if len(head) < 24:
            raise PcapValidationError(f"{p.name}: corrupt or unreadable capture (header too short)")
        endian = "<" if head[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
        major, _minor, _tz, _sig, snaplen, _link = struct.unpack(endian + "HHiIII", head[4:24])
        if major != 2 or snaplen == 0:
            raise PcapValidationError(
                f"{p.name}: corrupt or unreadable capture (bad global header: "
                f"version {major}.x, snaplen {snaplen})")
    return fmt


# scapy reports malformed input as log warnings (and then stops as if at EOF),
# not exceptions. These messages mean the capture is not usable as-is.
_FATAL_WARNINGS = ("unknown LL type", "has been truncated", "Invalid pcapng block",
                   "Could not read blocklen", "bad blocklen")


class _ScapyWarnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _capture_scapy_warnings():
    log = logging.getLogger("scapy.runtime")
    h = _ScapyWarnings()
    log.addHandler(h)
    try:
        yield h
    finally:
        log.removeHandler(h)


def iter_packets(path: str | Path, max_packets: Optional[int] = None):
    """Stream packets from a pcap/pcapng file (scapy picks the reader by magic)."""
    from scapy.utils import PcapReader  # lazy: optional dependency

    reader = PcapReader(str(path))
    try:
        for i, pkt in enumerate(reader):
            if max_packets is not None and i >= max_packets:
                break
            yield pkt
    finally:
        reader.close()


def validate_pcap(path: str | Path, max_bytes: int = DEFAULT_MAX_BYTES,
                  max_packets: Optional[int] = None) -> PcapStats:
    """Validate `path` as a real capture and return its stats, or raise
    PcapValidationError naming why it was rejected."""
    from scapy.error import Scapy_Exception
    from scapy.layers.inet import IP, TCP, UDP
    from scapy.layers.inet6 import IPv6

    p = Path(path)
    fmt = sniff_format(p)
    size = p.stat().st_size
    if size > max_bytes:
        raise PcapValidationError(
            f"{p.name}: {size:,} bytes exceeds the {max_bytes:,}-byte limit")

    n = ip = tcp = udp = other = 0
    first = last = None
    notes: list[str] = []
    try:
        with _capture_scapy_warnings() as warned:
            for pkt in iter_packets(p, max_packets=max_packets):
                n += 1
                t = float(pkt.time)
                first = t if first is None else min(first, t)
                last = t if last is None else max(last, t)
                if IP in pkt or IPv6 in pkt:
                    ip += 1
                    if TCP in pkt:
                        tcp += 1
                    elif UDP in pkt:
                        udp += 1
                    else:
                        other += 1
                else:
                    other += 1
    except (Scapy_Exception, EOFError, OSError, ValueError, TypeError) as e:
        raise PcapValidationError(f"{p.name}: corrupt or unreadable capture ({e})") from e
    except Exception as e:  # noqa: BLE001 — any parser crash means "not a usable capture"
        raise PcapValidationError(f"{p.name}: corrupt or unreadable capture ({type(e).__name__}: {e})") from e

    fatal = [m for m in warned.messages if any(k in m for k in _FATAL_WARNINGS)]
    if fatal:
        raise PcapValidationError(f"{p.name}: corrupt or unreadable capture ({fatal[0]})")
    if n == 0:
        raise PcapValidationError(f"{p.name}: capture contains no packets")
    if tcp + udp == 0:
        notes.append("no TCP/UDP packets — CICFlowMeter will produce no flows")
    truncated = max_packets if (max_packets is not None and n >= max_packets) else None
    if truncated:
        notes.append(f"read stopped at max_packets={max_packets}")
    return PcapStats(
        path=str(p), file_format=fmt, file_size_bytes=size, packet_count=n,
        first_ts=first, last_ts=last,
        duration_s=round((last - first), 6) if first is not None else 0.0,
        ip_packets=ip, tcp_packets=tcp, udp_packets=udp, other_packets=other,
        truncated_at=truncated, notes=notes,
    )
