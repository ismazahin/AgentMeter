"""Phase 43b §2: URL import for Prepare — SSRF protection, size caps, content checks,
and an end-to-end import from a local https server.

Unit tests drive urlfetch.download() with a fake resolver + fake connection, so no
network is used. The E2E tests serve large synthetic files from a loopback https
server; loopback is reachable only because the test sets the TEST-ONLY env switch
(AGENTMETER_TEST_ALLOW_LOOPBACK_URLS), which is off by default.
"""
from __future__ import annotations

import http.server
import io
import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import pytest

from agentmeter.server import urlfetch
from agentmeter.server.urlfetch import UrlImportError, address_refusal, check_url, download

from synth import big_csv, big_pcap

LOOPBACK = urlfetch.TEST_LOOPBACK_ENV
CAFILE = urlfetch.TEST_CAFILE_ENV


@pytest.fixture(autouse=True)
def _no_test_switch(monkeypatch):
    monkeypatch.delenv(LOOPBACK, raising=False)
    monkeypatch.delenv(CAFILE, raising=False)


# ---------------------------------------------------------------------------
# address policy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ip, why", [
    ("127.0.0.1", "loopback"), ("127.8.9.10", "loopback"), ("::1", "loopback"),
    ("10.1.2.3", "private"), ("172.16.5.4", "private"), ("192.168.1.1", "private"),
    ("169.254.169.254", "link-local"), ("169.254.0.1", "link-local"), ("fe80::1", "link-local"),
    ("100.64.0.1", "carrier-grade NAT"), ("100.127.255.254", "carrier-grade NAT"),
    ("224.0.0.1", "multicast"), ("ff02::1", "multicast"), ("239.255.255.250", "multicast"),
    ("0.0.0.0", "unspecified"), ("::", "unspecified"),
    ("fc00::1", "private"), ("fd12:3456::1", "private"),
    ("::ffff:127.0.0.1", "loopback"), ("::ffff:169.254.169.254", "link-local"),
    ("64:ff9b::a9fe:a9fe", "link-local"),                   # NAT64 of 169.254.169.254
    ("2002:c0a8:0101::1", "private"),                       # 6to4 of 192.168.1.1
    ("240.0.0.1", "address"), ("198.18.0.1", "private"),
])
def test_non_public_addresses_are_refused(ip, why):
    assert why in (address_refusal(ip) or "")


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_pass(ip):
    assert address_refusal(ip) is None


def test_loopback_switch_only_opens_loopback():
    assert address_refusal("127.0.0.1", allow_loopback=True) is None
    assert address_refusal("::1", allow_loopback=True) is None
    for ip in ("10.0.0.1", "169.254.169.254", "192.168.0.1", "100.64.0.1", "::ffff:10.0.0.1"):
        assert address_refusal(ip, allow_loopback=True)


@pytest.mark.parametrize("url, code", [
    ("http://example.org/x.pcap", "bad_url"), ("ftp://example.org/x", "bad_url"),
    ("file:///etc/passwd", "bad_url"), ("gopher://x/", "bad_url"), ("", "bad_url"),
    ("https://user:pw@example.org/x", "bad_url"), ("https:///nohost", "bad_url"),
    ("https://example.org:8443/x", "blocked_url"), ("https://example.org:22/x", "blocked_url"),
])
def test_url_syntax_rules(url, code):
    with pytest.raises(UrlImportError) as e:
        check_url(url)
    assert e.value.code == code


def test_any_private_answer_in_dns_refuses_the_host():
    fake = lambda h, p, type=0: [(2, 1, 6, "", ("93.184.216.34", p)), (2, 1, 6, "", ("10.0.0.9", p))]  # noqa: E731
    with pytest.raises(UrlImportError, match="10.0.0.9 is a private address") as e:
        urlfetch.resolve_public("mixed.example", 443, resolver=fake)
    assert e.value.code == "blocked_url"


# ---------------------------------------------------------------------------
# fake transport for download()
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status=200, headers=None, body=b"", reason="OK"):
        self.status, self.reason, self._h = status, reason, {k.lower(): v for k, v in (headers or {}).items()}
        self._b = io.BytesIO(body) if isinstance(body, (bytes, bytearray)) else body
        self.read_calls = 0

    def getheader(self, k, default=None):
        return self._h.get(k.lower(), default)

    def read(self, n=-1):
        self.read_calls += 1
        return self._b.read(n)


class Endless(io.RawIOBase):
    """A body that never ends (no Content-Length): CSV-looking bytes forever."""

    def __init__(self):
        self.first = True

    def read(self, n=-1):
        if self.first:
            self.first = False
            return b"a,b,c\n" + b"1,2,3\n" * (max(1, n) // 6)
        return b"1,2,3\n" * (max(1, n) // 6)


def transport(routes, dns):
    """routes: {(host, path): FakeResp}; dns: {host: ip}. Records every connection."""
    log = []

    def resolver(host, port, type=0):
        try:                                    # an IP literal resolves to itself, like real DNS
            import ipaddress
            ipaddress.ip_address(host)
            return [(2, 1, 6, "", (host, port))]
        except ValueError:
            pass
        if host not in dns:
            raise socket.gaierror("no such host")
        return [(2, 1, 6, "", (dns[host], port))]

    class Conn:
        def __init__(self, host, ip, port, timeout, context):
            self.host, self.ip = host, ip
            log.append((host, ip, port))

        def request(self, method, path, headers=None):
            self.path = path

        def getresponse(self):
            return routes[(self.host, self.path)]

        def close(self):
            pass
    return resolver, Conn, log


def fetch(tmp_path, url, routes, dns, max_bytes=10 * 1024 ** 2, **kw):
    resolver, conn, log = transport(routes, dns)
    d = download(url, tmp_path / "dl", max_bytes=max_bytes, timeout_s=30, resolver=resolver,
                 connection_cls=conn, stem="job1", **kw)
    return d, log


PCAP_BYTES = b"\xd4\xc3\xb2\xa1" + b"\x00" * 60
CSV_BYTES = b"Destination Port,Flow Duration,Label\n80,1,BENIGN\n"


def test_pcap_download_is_detected_hashed_and_pinned(tmp_path):
    d, log = fetch(tmp_path, "https://files.example/cap.pcap",
                   {("files.example", "/cap.pcap"): FakeResp(headers={"Content-Length": str(len(PCAP_BYTES))},
                                                             body=PCAP_BYTES)},
                   {"files.example": "93.184.216.34"})
    assert d.detected == "pcap" and d.filename == "cap.pcap" and d.size_bytes == len(PCAP_BYTES)
    assert d.path == tmp_path / "dl" / "job1.pcap" and d.path.read_bytes() == PCAP_BYTES
    import hashlib
    assert d.sha256 == hashlib.sha256(PCAP_BYTES).hexdigest()
    assert log == [("files.example", "93.184.216.34", 443)]          # connected to the checked IP


def test_redirect_to_a_private_address_is_refused_before_connecting(tmp_path):
    routes = {("public.example", "/f.csv"): FakeResp(302, {"Location": "https://internal.example/secret"})}
    with pytest.raises(UrlImportError, match="10.0.0.5 is a private address") as e:
        fetch(tmp_path, "https://public.example/f.csv", routes,
              {"public.example": "93.184.216.34", "internal.example": "10.0.0.5"})
    assert e.value.code == "blocked_url"


def test_redirect_to_the_metadata_service_and_to_http_are_refused(tmp_path):
    for loc, code in (("https://169.254.169.254/latest/meta-data/", "blocked_url"),
                      ("http://public.example/f.csv", "bad_url")):
        routes = {("public.example", "/f.csv"): FakeResp(301, {"Location": loc})}
        with pytest.raises(UrlImportError) as e:
            fetch(tmp_path, "https://public.example/f.csv", routes, {"public.example": "93.184.216.34"})
        assert e.value.code == code


def test_each_redirect_hop_is_rechecked_and_followed(tmp_path):
    routes = {("a.example", "/1"): FakeResp(302, {"Location": "https://b.example/2"}),
              ("b.example", "/2"): FakeResp(307, {"Location": "/3"}),
              ("b.example", "/3"): FakeResp(body=CSV_BYTES)}
    d, log = fetch(tmp_path, "https://a.example/1", routes, {"a.example": "93.184.216.34", "b.example": "1.1.1.1"})
    assert d.detected == "csv" and d.final_url == "https://b.example/3" and len(d.redirects) == 2
    assert [h for h, _, _ in log] == ["a.example", "b.example", "b.example"]


def test_redirect_loop_is_capped(tmp_path):
    routes = {("a.example", "/x"): FakeResp(302, {"Location": "https://a.example/x"})}
    with pytest.raises(UrlImportError) as e:
        fetch(tmp_path, "https://a.example/x", routes, {"a.example": "93.184.216.34"})
    assert e.value.code == "too_many_redirects"


def test_dns_rebinding_cannot_move_the_connection(monkeypatch):
    """The real connection class opens its socket to the pinned IP, whatever DNS says later."""
    seen = {}

    def fake_create(addr, timeout=None, *a, **k):
        seen["addr"] = addr
        raise OSError("stop here")
    monkeypatch.setattr(socket, "create_connection", fake_create)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    conn = urlfetch.PinnedHTTPSConnection("rebind.example", "93.184.216.34", 443, 5, ssl.create_default_context())
    with pytest.raises(OSError, match="stop here"):
        conn.connect()
    assert seen["addr"] == ("93.184.216.34", 443)


def test_content_length_over_the_cap_is_refused_before_reading(tmp_path):
    resp = FakeResp(headers={"Content-Length": str(11 * 1024 ** 3)}, body=PCAP_BYTES)
    with pytest.raises(UrlImportError, match="URL import limit") as e:
        fetch(tmp_path, "https://f.example/big.pcap", {("f.example", "/big.pcap"): resp},
              {"f.example": "93.184.216.34"}, max_bytes=10 * 1024 ** 3)
    assert e.value.code == "too_large" and e.value.status == 413 and resp.read_calls == 0
    assert not list((tmp_path / "dl").glob("*")) if (tmp_path / "dl").exists() else True


def test_size_cap_is_enforced_mid_stream_and_the_partial_file_deleted(tmp_path):
    resp = FakeResp(body=Endless())                                   # no Content-Length
    with pytest.raises(UrlImportError, match="was stopped") as e:
        fetch(tmp_path, "https://f.example/x.csv", {("f.example", "/x.csv"): resp},
              {"f.example": "93.184.216.34"}, max_bytes=3 * 1024 ** 2)
    assert e.value.code == "too_large"
    assert list((tmp_path / "dl").iterdir()) == []                   # .part removed


def test_lying_content_length_is_still_capped(tmp_path):
    resp = FakeResp(headers={"Content-Length": "100"}, body=Endless())
    with pytest.raises(UrlImportError) as e:
        fetch(tmp_path, "https://f.example/x.csv", {("f.example", "/x.csv"): resp},
              {"f.example": "93.184.216.34"}, max_bytes=2 * 1024 ** 2)
    assert e.value.code in ("too_large", "download_failed")
    assert list((tmp_path / "dl").iterdir()) == []


@pytest.mark.parametrize("host, headers, body, code, msg", [
    ("drive.google.com", {"Content-Type": "text/html; charset=utf-8"}, b"<!DOCTYPE html><html>...",
     "html_not_file", "Google Drive"),
    ("files.example", {"Content-Type": "application/octet-stream"}, b"  <!doctype html><html><body>login",
     "html_not_file", "direct download link"),
    ("files.example", {}, b"<html><head><title>Sign in</title>", "html_not_file", "web page"),
    ("files.example", {}, b"PK\x03\x04" + b"\x00" * 40, "unsupported_content", "zip archive"),
    ("files.example", {}, b"\x1f\x8b\x08\x00" + b"\x00" * 40, "unsupported_content", "gzip archive"),
    ("files.example", {}, b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 40, "unsupported_content", "binary content"),
    ("files.example", {}, b"just some text without separators\n", "unsupported_content", "header row"),
    ("files.example", {}, b"", "unsupported_content", "empty file"),
])
def test_bad_content_is_refused_with_a_clear_reason(tmp_path, host, headers, body, code, msg):
    with pytest.raises(UrlImportError) as e:
        fetch(tmp_path, f"https://{host}/f", {(host, "/f"): FakeResp(headers=headers, body=body)},
              {host: "93.184.216.34"})
    assert e.value.code == code and msg in str(e.value)
    assert not any((tmp_path / "dl").glob("*")) if (tmp_path / "dl").exists() else True


def test_http_error_status_is_reported(tmp_path):
    with pytest.raises(UrlImportError, match="HTTP 404") as e:
        fetch(tmp_path, "https://f.example/x", {("f.example", "/x"): FakeResp(404, reason="Not Found")},
              {"f.example": "93.184.216.34"})
    assert e.value.code == "download_failed"


def test_loopback_is_refused_without_the_test_switch(tmp_path):
    with pytest.raises(UrlImportError, match="loopback") as e:
        fetch(tmp_path, "https://localhost/x.csv", {}, {"localhost": "127.0.0.1"})
    assert e.value.code == "blocked_url"


# ---------------------------------------------------------------------------
# the web API cannot enable loopback; a blocked URL fails its job clearly
# ---------------------------------------------------------------------------
def test_prepare_job_with_a_private_url_fails_and_no_field_enables_loopback(tmp_path, monkeypatch):
    from test_prepare import _app
    c, mgr = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, type=0, **k: [(2, 1, 6, "", ("127.0.0.1", p))])
    r = c.post("/api/prepare", data={"url": "https://intranet.example/data.csv", "allow_loopback": "1",
                                     "test": "1", LOOPBACK: "1"}, content_type="multipart/form-data")
    assert r.status_code == 202
    job = mgr.wait(r.get_json()["job_id"], timeout=30)
    assert job["status"] == "failed" and job["error_code"] == "blocked_url" and "loopback" in job["error"]
    r = c.post("/api/prepare", json={"url": "https://127.0.0.1:8443/x.pcap"})
    assert r.status_code == 400 and r.get_json()["code"] == "blocked_url"   # odd port, switch off


# ---------------------------------------------------------------------------
# E2E: large synthetic files from a local https server (test switch ON)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def https_server(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl not available to make a test certificate")
    d = tmp_path_factory.mktemp("https")
    cert, key = d / "cert.pem", d / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    root = d / "www"
    root.mkdir()
    big_csv(root / "big.csv", 60_000, rare_tail=5)                    # ~20 MB
    big_pcap(root / "big.pcap", 30_000, span_s=3000)                  # 60k packets
    (root / "page.html").write_text("<!DOCTYPE html><html><body>Download</body></html>")

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/go":
                self.send_response(302)
                self.send_header("Location", "/big.csv")
                self.end_headers()
                return
            if self.path == "/to-private":
                self.send_response(302)
                self.send_header("Location", "https://10.0.0.1/big.csv")
                self.end_headers()
                return
            super().do_GET()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"https://127.0.0.1:{srv.server_address[1]}", cert, root
    srv.shutdown()


@pytest.fixture
def e2e(https_server, tmp_path, monkeypatch):
    base, cert, root = https_server
    monkeypatch.setenv(LOOPBACK, "1")
    monkeypatch.setenv(CAFILE, str(cert))
    monkeypatch.setenv("AGENTMETER_MAX_CSV_ROWS", "5000")
    monkeypatch.setenv("AGENTMETER_MAX_PCAP_PACKETS", "10000")
    monkeypatch.setenv("AGENTMETER_PCAP_WINDOWS", "5")
    from test_prepare import _app
    c, mgr = _app(tmp_path, monkeypatch)
    return c, mgr, base, root


def _url_prepare(c, mgr, url, **form):
    r = c.post("/api/prepare", data={"url": url, **form}, content_type="multipart/form-data")
    assert r.status_code == 202, r.get_json()
    return mgr.wait(r.get_json()["job_id"], timeout=300)


def test_e2e_large_csv_by_url_is_sampled_across_the_file(e2e):
    c, mgr, base, root = e2e
    t0 = time.monotonic()
    job = _url_prepare(c, mgr, base + "/go", max_flows="30")          # via a redirect
    assert job["status"] == "done", job
    assert job["progress"]["phase"] == "done" and job["source"]["final_url"].endswith("/big.csv")
    s = c.get(f"/api/prepared/{job['prepared']}").get_json()
    assert s["source"]["kind"] == "url" and s["source"]["size_bytes"] == (root / "big.csv").stat().st_size
    assert s["rows_read"] == 60_000 and s["rows_in_pool"] == 5000 and s["rows_selected"] == 30
    assert s["sampling"]["method"] == "stratified_reservoir"
    assert s["class_counts"]["in_file"]["Port Scanning"] == 5               # the rare tail class...
    assert s["class_counts"]["in_pool"]["Port Scanning"] == 5               # ...survives sampling
    assert time.monotonic() - t0 < 120
    j = c.post("/api/jobs", json={"run": job["prepared"], "models": ["mistralai/Mistral-7B-Instruct-v0.3"],
                                  "provider": "mock"})
    assert mgr.wait(j.get_json()["job_id"], timeout=120)["status"] == "done"


def test_e2e_large_pcap_by_url_uses_time_windows(e2e):
    pytest.importorskip("scapy")
    pytest.importorskip("cicflowmeter")
    c, mgr, base, _ = e2e
    job = _url_prepare(c, mgr, base + "/big.pcap", max_flows="20")
    assert job["status"] == "done", job
    s = c.get(f"/api/prepared/{job['prepared']}").get_json()
    assert s["source_type"] == "pcap" and s["packets"] == 60_000 and s["packets_parsed"] == 10_000
    assert s["sampling"]["method"] == "time_windows" and len(s["sampling"]["windows"]) == 5
    assert s["rows_selected"] == 20 and s["large_input"]["capped"] is True


def test_e2e_html_page_and_redirect_to_private_fail_clearly(e2e):
    c, mgr, base, _ = e2e
    job = _url_prepare(c, mgr, base + "/page.html")
    assert job["status"] == "failed" and job["error_code"] == "html_not_file"
    assert "direct download link" in job["error"]
    job = _url_prepare(c, mgr, base + "/to-private")
    assert job["status"] == "failed" and job["error_code"] == "blocked_url" and "10.0.0.1" in job["error"]


def test_e2e_download_cap_mid_stream(e2e, monkeypatch):
    c, mgr, base, root = e2e
    monkeypatch.setenv("AGENTMETER_MAX_URL_DOWNLOAD_GB", str(5 / 1024))   # 5 MB < the 20 MB CSV
    from test_prepare import _app
    other = Path(str(mgr.results_root)).parent / "capped"
    other.mkdir()
    c2, mgr2 = _app(other, monkeypatch)
    job = _url_prepare(c2, mgr2, base + "/big.csv")
    assert job["status"] == "failed" and job["error_code"] == "too_large"
    up = Path(mgr2.results_root) / "uploads"
    assert not up.exists() or not any(up.iterdir())                         # partial deleted
