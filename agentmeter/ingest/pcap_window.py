"""Time-window sampling for large captures (Phase 43b).

Packets are not flows, so a large capture cannot be sampled row by row. Instead:

  1. index()  — a fast pass over the record HEADERS only (no packet decoding):
                packet count, first/last timestamp, structural validation
                (a truncated or impossible record rejects the file).
  2. if the capture fits the packet budget it is processed in full, unchanged.
  3. otherwise write_windows() picks K windows spread evenly across the
     capture's time span: window i starts at first_ts + i * span / K and takes
     the next budget/K packets in capture order (fewer if the next window starts
     first). Those records are copied verbatim into a smaller capture of the
     same format, which the existing validation + CICFlowMeter extraction then
     read exactly as they would read the original.

Supported: libpcap (µs / ns, both byte orders) and pcapng (SHB/IDB/EPB/SPB/PB,
per-interface if_tsresol). Non-packet pcapng blocks are always copied so the
interface ids stay valid. Flows that cross a window edge are cut at that edge;
the manifest records every window.
"""
from __future__ import annotations

import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from .pcap import PcapValidationError

_LIBPCAP = {b"\xd4\xc3\xb2\xa1": ("<", 1e-6), b"\xa1\xb2\xc3\xd4": (">", 1e-6),
            b"\x4d\x3c\xb2\xa1": ("<", 1e-9), b"\xa1\xb2\x3c\x4d": (">", 1e-9)}
_PCAPNG = b"\x0a\x0d\x0d\x0a"
_MAX_RECORD = 64 * 1024 ** 2             # no real frame is this large: the file is corrupt
_PROGRESS_EVERY = 200_000


@dataclass
class CaptureIndex:
    file_format: str
    packets: int
    first_ts: Optional[float]
    last_ts: Optional[float]
    file_size_bytes: int

    @property
    def span_s(self) -> float:
        return (self.last_ts - self.first_ts) if self.first_ts is not None else 0.0


@dataclass
class Window:
    index: int
    start_ts: float                       # planned window start (evenly spaced)
    next_start_ts: Optional[float]        # where the next window starts (None for the last)
    packets_taken: int = 0
    first_packet_ts: Optional[float] = None
    last_packet_ts: Optional[float] = None


@dataclass
class WindowPlan:
    windows: list[Window] = field(default_factory=list)
    budget_per_window: int = 0

    def to_list(self) -> list[dict[str, Any]]:
        return [asdict(w) for w in self.windows]


# ---------------------------------------------------------------------------
# record iterators: yield (kind, ts, raw_bytes) — kind "packet" | "meta"
# ---------------------------------------------------------------------------
def _libpcap_records(fh, size: int, name: str, want_bytes: bool) -> Iterator[tuple]:
    gh = fh.read(24)
    endian, unit = _LIBPCAP[gh[:4]]
    if want_bytes:
        yield "meta", None, gh
    pos = 24
    hdr_fmt = endian + "IIII"
    while True:
        hdr = fh.read(16)
        if not hdr:
            return
        if len(hdr) < 16:
            raise PcapValidationError(f"{name}: corrupt or unreadable capture (truncated record header)")
        sec, frac, incl, _orig = struct.unpack(hdr_fmt, hdr)
        if incl > _MAX_RECORD:
            raise PcapValidationError(f"{name}: corrupt or unreadable capture (record of {incl:,} bytes)")
        pos += 16 + incl
        if pos > size:
            raise PcapValidationError(f"{name}: corrupt or unreadable capture (truncated last record)")
        if want_bytes:
            yield "packet", sec + frac * unit, hdr + fh.read(incl)
        else:
            fh.seek(incl, 1)
            yield "packet", sec + frac * unit, None


def _tsresol(opts: bytes, endian: str) -> float:
    """if_tsresol (option 9) from IDB options; default 10^-6."""
    i = 0
    while i + 4 <= len(opts):
        code, ln = struct.unpack(endian + "HH", opts[i:i + 4])
        if code == 0:
            break
        if code == 9 and ln >= 1:
            v = opts[i + 4]
            return 2.0 ** -(v & 0x7F) if v & 0x80 else 10.0 ** -v
        i += 4 + ((ln + 3) & ~3)
    return 1e-6


def _pcapng_records(fh, size: int, name: str, want_bytes: bool) -> Iterator[tuple]:
    endian = "<"
    resol: list[float] = []
    last_ts: Optional[float] = None
    pos = 0
    while pos < size:
        head = fh.read(8)
        if len(head) < 8:
            raise PcapValidationError(f"{name}: corrupt or unreadable capture (truncated block header)")
        btype_raw = head[:4]
        if btype_raw == _PCAPNG:                    # section header: byte order from its magic
            bom = fh.read(4)
            endian = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">" if bom == b"\x1a\x2b\x3c\x4d" else None
            if endian is None:
                raise PcapValidationError(f"{name}: corrupt or unreadable capture (bad pcapng byte order)")
            blen = struct.unpack(endian + "I", head[4:8])[0]
            body = bom + fh.read(blen - 12) if blen >= 12 else b""
            resol = []
        else:
            blen = struct.unpack(endian + "I", head[4:8])[0]
            body = fh.read(blen - 8) if blen >= 12 else b""
        if blen < 12 or blen % 4 or blen > _MAX_RECORD or len(body) != blen - 8:
            raise PcapValidationError(f"{name}: corrupt or unreadable capture (bad pcapng block length {blen})")
        pos += blen
        btype = struct.unpack(endian + "I", btype_raw)[0]
        raw = head + body if want_bytes else None
        if btype == 1:                               # interface description
            resol.append(_tsresol(body[8:-4], endian))
            yield "meta", None, raw
        elif btype in (6, 2):                        # enhanced / obsolete packet block
            if btype == 6:
                iface, hi, lo = struct.unpack(endian + "III", body[:12])
            else:
                iface, _drops, hi, lo = struct.unpack(endian + "HHII", body[:12])
            unit = resol[iface] if iface < len(resol) else 1e-6
            last_ts = ((hi << 32) | lo) * unit
            yield "packet", last_ts, raw
        elif btype == 3:                             # simple packet block: no timestamp
            yield "packet", last_ts, raw
        else:
            yield "meta", None, raw


def _records(path: Path, want_bytes: bool):
    p = Path(path)
    size = p.stat().st_size
    fh = p.open("rb")
    magic = fh.read(4)
    fh.seek(0)
    if magic in _LIBPCAP:
        return "pcap", fh, _libpcap_records(fh, size, p.name, want_bytes)
    if magic == _PCAPNG:
        return "pcapng", fh, _pcapng_records(fh, size, p.name, want_bytes)
    fh.close()
    raise PcapValidationError(f"{p.name}: not a pcap/pcapng capture (magic bytes {magic.hex() or 'empty'})")


def index(path: str | Path, progress: Optional[Callable] = None) -> CaptureIndex:
    """Header-only pass: packet count + time span, rejecting a structurally broken file."""
    p = Path(path)
    fmt, fh, recs = _records(p, want_bytes=False)
    n, first, last = 0, None, None
    try:
        for kind, ts, _ in recs:
            if kind != "packet":
                continue
            n += 1
            if ts is not None:
                first = ts if first is None else min(first, ts)
                last = ts if last is None else max(last, ts)
            if progress and n % _PROGRESS_EVERY == 0:
                progress(phase="indexing", packets_scanned=n,
                         message=f"indexing the capture ({n:,} packets so far)")
    finally:
        fh.close()
    return CaptureIndex(fmt, n, first, last, p.stat().st_size)


def plan_windows(idx: CaptureIndex, k: int, budget: int) -> WindowPlan:
    k = max(1, min(int(k), max(1, idx.packets)))
    step = idx.span_s / k
    starts = [idx.first_ts + i * step for i in range(k)]
    wins = [Window(i, s, starts[i + 1] if i + 1 < k else None) for i, s in enumerate(starts)]
    return WindowPlan(wins, max(1, budget // k))


def write_windows(path: str | Path, out_path: str | Path, plan: WindowPlan,
                  progress: Optional[Callable] = None) -> int:
    """Copy the planned windows' packets (and all non-packet blocks) to out_path.
    Returns the number of packets written. Fills each Window's actual stats."""
    fmt, fh, recs = _records(Path(path), want_bytes=True)
    wins, b = plan.windows, plan.budget_per_window
    written = seen = 0
    w = 0
    try:
        with Path(out_path).open("wb") as out:
            for kind, ts, raw in recs:
                if kind != "packet":
                    out.write(raw)                   # global header / SHB / IDB / stats ...
                    continue
                seen += 1
                if ts is None:                       # a pcapng SPB before any timestamp
                    continue
                while w + 1 < len(wins) and ts >= wins[w + 1].start_ts:
                    w += 1                           # entered the next window
                win = wins[w]
                if ts < win.start_ts or win.packets_taken >= b:
                    continue
                out.write(raw)
                written += 1
                win.packets_taken += 1
                win.first_packet_ts = ts if win.first_packet_ts is None else win.first_packet_ts
                win.last_packet_ts = ts
                if progress and seen % _PROGRESS_EVERY == 0:
                    progress(phase="windowing", packets_scanned=seen,
                             message=f"copying the sampled time windows ({written:,} packets taken)")
    finally:
        fh.close()
    return written
