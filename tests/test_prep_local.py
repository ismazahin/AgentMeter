"""scripts/prep_local.py: the preparation service (and traffic analysis) on the owner's computer,
started by the site's Start button: registration, restarts, Stop, the link type, the secret."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agentmeter.config import PROJECT_ROOT

spec = importlib.util.spec_from_file_location("prep_local", PROJECT_ROOT / "scripts" / "prep_local.py")
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)
SECRET = "test-secret-0123456789"


def test_the_secret_is_created_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOUD_PREP_SECRET", raising=False)
    f = tmp_path / ".cloudprep_secret"
    s = L.load_or_create_secret(f)
    assert len(s) >= 32 and L.load_or_create_secret(f) == s
    monkeypatch.setenv("CLOUD_PREP_SECRET", "from-the-environment-123")
    assert L.load_or_create_secret(f) == "from-the-environment-123"


def test_the_quick_tunnel_address_is_read_from_cloudflared_output():
    line = "2026-11-10T09:00:00Z INF |  https://brave-otter-lake-sun.trycloudflare.com                    |"
    assert L.parse_tunnel_url(line) == "https://brave-otter-lake-sun.trycloudflare.com"
    assert L.parse_tunnel_url("INF Requesting new quick Tunnel on trycloudflare.com...") is None


def test_the_signature_is_the_control_planes():
    body = b'{"url":"https://x.trycloudflare.com"}'
    sig = L.sign(SECRET, "POST", "/api/prep/register", "1700000000", body)
    import hmac
    want = hmac.new(SECRET.encode(), f"POST\n/api/prep/register\n1700000000\n{hashlib.sha256(body).hexdigest()}".encode(),
                    hashlib.sha256).hexdigest()
    assert sig == want


def test_only_start_and_stop_links_do_anything():
    assert L.parse_link("agentmeter-prep://start") == "start"
    assert L.parse_link("agentmeter-prep://start/") == "start"           # browsers may add a slash
    assert L.parse_link("AGENTMETER-PREP://STOP") == "stop"
    for bad in ("agentmeter-prep://start?x=1", "agentmeter-prep://run/calc.exe", "agentmeter-prep://", "https://start", ""):
        assert L.parse_link(bad) is None, bad


def test_the_link_type_runs_this_script_with_the_link_only():
    e = L.protocol_entries()
    root = r"Software\Classes\agentmeter-prep"
    assert e[root]["URL Protocol"] == "" and "AgentMeter" in e[root][""]
    cmd = e[root + r"\shell\open\command"][""]
    assert cmd.endswith('prep_local.py" link "%1"') and str(PROJECT_ROOT / "scripts" / "prep_local.py") in cmd


def test_cloudflared_is_found_in_the_repo_folder_without_path(tmp_path, monkeypatch):
    monkeypatch.setattr(L.shutil, "which", lambda name: None)
    monkeypatch.delenv("CLOUDFLARED", raising=False)
    monkeypatch.setattr(L, "REPO", tmp_path)
    assert L.find_cloudflared() is None
    (tmp_path / "cloudflared.exe").write_bytes(b"MZ")
    assert L.find_cloudflared() == str(tmp_path / "cloudflared.exe")


def test_the_control_plane_address_comes_from_the_sites_config(monkeypatch):
    monkeypatch.delenv("AGENTMETER_CONTROL_PLANE_URL", raising=False)
    cfg = json.loads((PROJECT_ROOT / "web" / "config.json").read_text())["control_plane"]
    assert L.control_plane_url() == cfg.rstrip("/")
    assert L.control_plane_url("https://cp.example.org/") == "https://cp.example.org"


def test_pid_checks():
    assert L.pid_alive(os.getpid()) and not L.pid_alive(0)


# --- the whole run: a fake cloudflared and a fake control plane ------------------------------
FAKE_CF = '''#!{py}
import sys, time, random
print("INF |  https://fake-%06d.trycloudflare.com  |" % random.randrange(10**6), file=sys.stderr, flush=True)
time.sleep(3600)
'''


class FakeCP:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        cp = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["content-length"]))
                ok = L.sign(SECRET, "POST", self.path, self.headers["X-AM-Timestamp"], body) == self.headers["X-AM-Signature"]
                cp.calls.append((self.path if ok else "BAD " + self.path, json.loads(body)))
                out = b'{"ok":true}'
                self.send_response(200 if ok else 401)
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(cond, s=60):
    end = time.monotonic() + s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def test_it_registers_beats_reopens_a_dropped_tunnel_and_stops_cleanly(tmp_path, monkeypatch):
    cf = tmp_path / "cloudflared"
    cf.write_text(FAKE_CF.format(py=sys.executable))
    cf.chmod(cf.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PREP_LOCAL_HEARTBEAT_S", "1")
    monkeypatch.setenv("PREP_LOCAL_PUBLIC_CHECK_S", "0")            # the fake address is not reachable
    cp = FakeCP()
    sup = L.Supervisor(cloudflared=str(cf), secret=SECRET, cp=cp.url, port=_free_port(), run_dir=tmp_path / "run",
                       child_out=subprocess.DEVNULL)
    t = threading.Thread(target=sup.run, daemon=True)
    t.start()
    try:
        assert _wait(lambda: any(p == "/api/prep/register" and "url" in b for p, b in cp.calls)), cp.calls
        first = next(b["url"] for p, b in cp.calls if p == "/api/prep/register")
        assert first.startswith("https://fake-")
        assert _wait(lambda: any(p == "/api/prep/heartbeat" for p, _ in cp.calls))
        assert json.loads((tmp_path / "run" / "prep_local.state.json").read_text())["url"] == first
        assert (tmp_path / "run" / "prep_local.pid").read_text() == str(os.getpid())
        # the tunnel drops -> a new tunnel, registered again
        sup.tunnel.kill()
        assert _wait(lambda: len([1 for p, b in cp.calls if p == "/api/prep/register" and "url" in b]) >= 2), cp.calls
        # Stop (the stop file, as `stop` writes it) -> 'stopping' sent, children ended, files cleaned
        (tmp_path / "run" / "prep_local.stop").write_text("stop")
        t.join(30)
        assert not t.is_alive()
        assert cp.calls[-1] == ("/api/prep/register", {"stopping": True})
        assert sup.service.poll() is not None and sup.tunnel.poll() is not None
        assert not (tmp_path / "run" / "prep_local.pid").exists()
        assert not any(p.startswith("BAD") for p, _ in cp.calls)
    finally:
        sup.stop_event.set()
        t.join(30)
