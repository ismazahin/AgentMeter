"""Server-to-server URL import for the Prepare step (Phase 43b), SSRF-safe.

Rules (each is tested in tests/test_url_import.py):
  * https only. Any other scheme (http, ftp, file, data, ...) is refused, and so
    are URLs with credentials ("user:pass@") or an unusual port.
  * The host is resolved with DNS and EVERY address must be a public unicast
    address: loopback, private (RFC 1918 / ULA), link-local (incl. the cloud
    metadata address 169.254.169.254), CGNAT (100.64/10), multicast, reserved,
    unspecified, and IPv6 forms that embed such an IPv4 address (mapped, NAT64,
    6to4, Teredo) are all refused.
  * The connection goes to the IP that was checked (pinned): the TCP socket is
    opened to that address and TLS is verified against the hostname (SNI +
    certificate), so a DNS answer that changes between check and use (DNS
    rebinding) cannot redirect the request.
  * Redirects are followed manually, at most MAX_REDIRECTS, and every hop is
    re-checked from scratch (scheme, credentials, port, DNS, IPs).
  * Size: a Content-Length over the cap is refused before the body is read; the
    cap is enforced again while streaming, whatever the headers said. A total
    time limit applies. The partial file is deleted on any failure.
  * Content: an HTML page (e.g. a Google Drive "share" link or a login page) is
    refused with "use a direct download link"; compressed archives are refused;
    the body must start like a pcap/pcapng capture or a CSV with a header row.

Test-only switch: AGENTMETER_TEST_ALLOW_LOOPBACK_URLS=1 (environment of the
SERVER process) additionally allows loopback addresses (127.0.0.0/8, ::1) and
any port, so the E2E test can serve files from a local https server;
AGENTMETER_TEST_URL_CAFILE then names that server's CA. Both are OFF by default,
are read only from the server's environment (no request or UI field can set
them), never allow private / link-local ranges, and log a warning when used.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import logging
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit

log = logging.getLogger("agentmeter.urlfetch")

MAX_REDIRECTS = 5
CHUNK = 1024 ** 2
READ_TIMEOUT_S = 60
ALLOWED_PORTS = {443}
TEST_LOOPBACK_ENV = "AGENTMETER_TEST_ALLOW_LOOPBACK_URLS"
TEST_CAFILE_ENV = "AGENTMETER_TEST_URL_CAFILE"
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_HTML_HINTS = (b"<!doctype html", b"<html", b"<head", b"<!--", b"<script", b"<body")
_DRIVE_HOSTS = ("drive.google.com", "docs.google.com", "drive.usercontent.google.com",
                "dropbox.com", "www.dropbox.com", "onedrive.live.com", "1drv.ms")
_ARCHIVES = {b"PK\x03\x04": "zip", b"\x1f\x8b": "gzip", b"BZh": "bzip2", b"\xfd7zXZ": "xz",
             b"7z\xbc\xaf": "7z", b"Rar!": "rar", b"\x28\xb5\x2f\xfd": "zstd"}
_CAPTURE_MAGIC = (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1",
                  b"\xa1\xb2\x3c\x4d", b"\x0a\x0d\x0d\x0a")


class UrlImportError(ValueError):
    """The URL cannot be imported. `code` is a stable reason; `status` an HTTP status."""

    def __init__(self, message: str, code: str = "bad_url", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class Download:
    path: Path
    url: str
    final_url: str
    size_bytes: int
    sha256: str
    content_type: Optional[str]
    filename: str
    detected: str                         # "pcap" | "csv"
    redirects: list[str] = field(default_factory=list)


def test_loopback_allowed() -> bool:
    return os.environ.get(TEST_LOOPBACK_ENV) == "1"


# ---------------------------------------------------------------------------
# address policy
# ---------------------------------------------------------------------------
def _embedded_v4(ip: ipaddress.IPv6Address) -> Optional[ipaddress.IPv4Address]:
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    return None


def address_refusal(ip_text: str, allow_loopback: bool = False) -> Optional[str]:
    """None when the address is a public unicast address; otherwise why not."""
    try:
        ip = ipaddress.ip_address(ip_text.split("%")[0])
    except ValueError:
        return f"{ip_text!r} is not an IP address"
    if isinstance(ip, ipaddress.IPv6Address):
        v4 = _embedded_v4(ip)
        if v4 is not None:
            why = address_refusal(str(v4), allow_loopback)
            return f"{ip} embeds {v4}: {why}" if why else None
    if ip.is_loopback:
        return None if allow_loopback else f"{ip} is a loopback address"
    if ip.is_link_local:
        return f"{ip} is link-local (includes the cloud metadata service)"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return f"{ip} is carrier-grade NAT (100.64.0.0/10) space"
    if ip.is_multicast:
        return f"{ip} is a multicast address"
    if ip.is_unspecified:
        return f"{ip} is the unspecified address"
    if ip.is_private:
        return f"{ip} is a private address"
    if ip.is_reserved:
        return f"{ip} is a reserved address"
    if not ip.is_global:
        return f"{ip} is not a public internet address"
    return None


def check_url(url: str, allow_loopback: bool = False) -> tuple[str, int, str]:
    """Syntax/scheme checks. Returns (host, port, path_with_query) or raises."""
    if not isinstance(url, str) or not url.strip():
        raise UrlImportError("give an https:// link to the file", "bad_url", 400)
    if len(url) > 4096:
        raise UrlImportError("URL is too long", "bad_url", 400)
    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "https":
        raise UrlImportError(f"only https:// links are accepted (got {parts.scheme or 'no'} scheme)",
                             "bad_url", 400)
    if parts.username or parts.password:
        raise UrlImportError("links with a username/password are not accepted", "bad_url", 400)
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UrlImportError("the link has no host name", "bad_url", 400)
    try:
        port = parts.port or 443
    except ValueError as e:
        raise UrlImportError(f"bad port in the link ({e})", "bad_url", 400) from e
    if port not in ALLOWED_PORTS and not allow_loopback:
        raise UrlImportError(f"only the standard https port 443 is accepted (got {port})",
                             "blocked_url", 400)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return host, port, path


def resolve_public(host: str, port: int, allow_loopback: bool = False,
                   resolver: Optional[Callable] = None) -> str:
    """Resolve `host`; every address must pass address_refusal. Returns the IP to pin."""
    resolver = resolver or socket.getaddrinfo
    try:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as e:
        raise UrlImportError(f"cannot resolve {host}: {e}", "dns_failed", 400) from e
    addrs = []
    for info in infos:
        a = info[4][0]
        if a not in addrs:
            addrs.append(a)
    if not addrs:
        raise UrlImportError(f"{host} has no addresses", "dns_failed", 400)
    for a in addrs:
        why = address_refusal(a, allow_loopback)
        if why:
            raise UrlImportError(f"refusing to fetch from {host}: {why}. Only files on the public "
                                 "internet can be imported.", "blocked_url", 400)
    return addrs[0]


# ---------------------------------------------------------------------------
# pinned HTTPS connection
# ---------------------------------------------------------------------------
class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to a pre-checked IP; TLS (SNI + certificate) uses the hostname."""

    def __init__(self, host: str, ip: str, port: int, timeout: float, context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _ssl_context(allow_loopback: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    cafile = os.environ.get(TEST_CAFILE_ENV) if allow_loopback else None
    if cafile:
        ctx.load_verify_locations(cafile)
    return ctx


# ---------------------------------------------------------------------------
# content checks
# ---------------------------------------------------------------------------
def _html_error(host: str) -> UrlImportError:
    hint = (" Google Drive, Dropbox and OneDrive share links open a web page; use the provider's "
            "direct-download link (or host the file where it is served as-is)."
            if any(host == h or host.endswith("." + h) for h in _DRIVE_HOSTS) else "")
    return UrlImportError("the link returned a web page (HTML), not the file — use a direct "
                          "download link to the .pcap/.pcapng/.csv file." + hint, "html_not_file", 400)


def classify_head(head: bytes, content_type: Optional[str], host: str) -> str:
    """'pcap' | 'csv' from the first bytes, or raise naming what was received."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if head[:4] in _CAPTURE_MAGIC:
        return "pcap"
    low = head.lstrip()[:512].lower()
    if ctype in ("text/html", "application/xhtml+xml") or any(low.startswith(h) for h in _HTML_HINTS):
        raise _html_error(host)
    for magic, kind in _ARCHIVES.items():
        if head.startswith(magic):
            raise UrlImportError(f"the link returned a {kind} archive; compressed files are not "
                                 "accepted — link the uncompressed .pcap/.pcapng/.csv",
                                 "unsupported_content", 400)
    if b"\x00" in head[:4096]:
        raise UrlImportError("the link returned binary content that is neither a pcap/pcapng "
                             "capture nor a CSV", "unsupported_content", 400)
    try:
        text = head[:8192].decode("utf-8", errors="strict" if len(head) < 8192 else "ignore")
    except UnicodeDecodeError:
        text = head[:8192].decode("latin-1")
    first = text.lstrip("﻿").splitlines()[0] if text.strip() else ""
    if first.count(",") < 2:
        raise UrlImportError("the link did not return a pcap/pcapng capture or a CSV with a header "
                             "row (first line: " + repr(first[:80]) + ")", "unsupported_content", 400)
    return "csv"


def _filename(final_url: str, cd: Optional[str], detected: str) -> str:
    name = ""
    if cd and "filename=" in cd:
        name = cd.split("filename=")[-1].strip().strip('";\'')
    if not name:
        name = Path(urlsplit(final_url).path).name
    stem = Path(name).stem or "download"
    ext = Path(name).suffix.lower()
    if detected == "pcap":
        ext = ext if ext in (".pcap", ".pcapng") else ".pcap"
    else:
        ext = ".csv"
    return f"{stem}{ext}"


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------
def download(url: str, dest_dir: Path, *, max_bytes: int, timeout_s: float,
             progress: Optional[Callable[..., None]] = None, stem: str = "download",
             resolver: Optional[Callable] = None,
             connection_cls=PinnedHTTPSConnection) -> Download:
    """Fetch `url` into dest_dir/<stem>.<ext> under every rule in the module docstring."""
    allow_lb = test_loopback_allowed()
    if allow_lb:
        log.warning("%s=1: loopback URLs are allowed (TEST ONLY — never set this in production)",
                    TEST_LOOPBACK_ENV)
    ctx = _ssl_context(allow_lb)
    deadline = time.monotonic() + float(timeout_s)
    current, hops = url.strip(), []
    report = progress or (lambda **kw: None)
    for _ in range(MAX_REDIRECTS + 1):
        host, port, path = check_url(current, allow_lb)
        ip = resolve_public(host, port, allow_lb, resolver=resolver)
        conn = connection_cls(host, ip, port, timeout=READ_TIMEOUT_S, context=ctx)
        try:
            conn.request("GET", path, headers={"User-Agent": "AgentMeter-prepare/1",
                                               "Accept": "*/*", "Accept-Encoding": "identity"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location")
                if not loc:
                    raise UrlImportError(f"redirect {resp.status} without a Location", "bad_response", 502)
                hops.append(current)
                current = urljoin(current, loc)
                continue
            if resp.status != 200:
                raise UrlImportError(f"the server answered HTTP {resp.status} {resp.reason}",
                                     "download_failed", 502)
            return _stream(resp, url, current, host, hops, dest_dir, stem, max_bytes, deadline, report)
        except (OSError, http.client.HTTPException, ssl.SSLError) as e:
            raise UrlImportError(f"download failed: {type(e).__name__}: {e}", "download_failed", 502) from e
        finally:
            conn.close()
    raise UrlImportError(f"too many redirects (more than {MAX_REDIRECTS})", "too_many_redirects", 400)


def _stream(resp, url, final_url, host, hops, dest_dir: Path, stem: str, max_bytes: int,
            deadline: float, report) -> Download:
    total = resp.getheader("Content-Length")
    total_n = int(total) if total and total.isdigit() else None
    if total_n is not None and total_n > max_bytes:
        raise UrlImportError(f"the file is {total_n / 1024 ** 3:,.2f} GB; the URL import limit is "
                             f"{max_bytes / 1024 ** 3:,.2f} GB", "too_large", 413)
    ctype = resp.getheader("Content-Type")
    dest_dir.mkdir(parents=True, exist_ok=True)
    part = dest_dir / f"{stem}.part"
    h = hashlib.sha256()
    n = 0
    detected = None
    last_report = 0.0
    try:
        with part.open("wb") as out:
            head = b""
            while True:
                if time.monotonic() > deadline:
                    raise UrlImportError("download timed out (the configured URL download time limit "
                                         "was reached)", "timeout", 504)
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                n += len(chunk)
                if n > max_bytes:
                    raise UrlImportError(f"the download passed the {max_bytes / 1024 ** 3:,.2f} GB URL "
                                         "import limit and was stopped", "too_large", 413)
                if detected is None:
                    head += chunk
                    if len(head) >= 8192 or n == total_n:
                        detected = classify_head(head, ctype, host)
                h.update(chunk)
                out.write(chunk)
                now = time.monotonic()
                if now - last_report >= 0.5:
                    last_report = now
                    report(phase="downloading", bytes=n, total=total_n,
                           message=f"downloading ({n / 1024 ** 2:,.1f} MB"
                                   + (f" of {total_n / 1024 ** 2:,.1f} MB)" if total_n else ")"))
            if detected is None:
                if n == 0:
                    raise UrlImportError("the link returned an empty file", "unsupported_content", 400)
                detected = classify_head(head, ctype, host)
        if total_n is not None and n != total_n:
            raise UrlImportError(f"the download ended early ({n:,} of {total_n:,} bytes)",
                                 "download_failed", 502)
        name = _filename(final_url, resp.getheader("Content-Disposition"), detected)
        final = dest_dir / f"{stem}{Path(name).suffix}"
        os.replace(part, final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    report(phase="downloaded", bytes=n, total=total_n, message=f"downloaded {n / 1024 ** 2:,.1f} MB")
    return Download(path=final, url=url, final_url=final_url, size_bytes=n, sha256=h.hexdigest(),
                    content_type=ctype, filename=name, detected=detected, redirects=hops)
