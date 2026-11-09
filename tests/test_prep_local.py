"""scripts/prep_local.py: the preparation service (and traffic analysis) on the owner's computer."""
from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys

from agentmeter.config import PROJECT_ROOT

spec = importlib.util.spec_from_file_location("prep_local", PROJECT_ROOT / "scripts" / "prep_local.py")
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)


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


def test_the_control_plane_is_told_the_address_and_secret():
    cmd = L.supabase_cmd("https://x.trycloudflare.com", "s3cret-0123456789")
    assert cmd[1:4] == ["supabase", "secrets", "set"]
    assert "CLOUD_PREP_URL=https://x.trycloudflare.com" in cmd and "CLOUD_PREP_SECRET=s3cret-0123456789" in cmd


def test_the_service_runs_straight_from_a_checkout(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "PORT": str(port), "CLOUD_PREP_SECRET": "test-secret-0123456789",
           "CLOUD_PREP_WORK_DIR": str(tmp_path), "PYTHONPATH": ""}
    p = subprocess.Popen([sys.executable, str(PROJECT_ROOT / "cloudprep" / "app.py")], env=env, cwd=str(tmp_path))
    try:
        assert L.wait_health(f"http://127.0.0.1:{port}", 60)
    finally:
        p.terminate()
        p.wait(10)
