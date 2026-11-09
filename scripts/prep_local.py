#!/usr/bin/env python3
"""Run AgentMeter's data-preparation service (and the traffic analysis) on YOUR computer.

No card, no rented GPU: this starts cloudprep/app.py here, opens a Cloudflare tunnel to it, and
tells the control plane (Supabase) where it is. While it runs, Analyse traffic and cloud
preparation work; when you stop it (Ctrl+C), the web app prepares in the browser as before.

    python scripts/prep_local.py                    # a quick tunnel (a new trycloudflare.com address each run)
    python scripts/prep_local.py --tunnel-token <t> --hostname prep.example.org   # a named tunnel (fixed address)

Needs: Python with requirements.txt (+ requirements-pcap.txt and cicflowmeter for PCAPs), cloudflared
(on PATH, or cloudflared.exe downloaded into the repo folder), and the Supabase CLI linked to your project
(npx supabase login; npx supabase link). The service's secret is created once and kept in
.cloudprep_secret (git-ignored); the control plane gets it with `supabase secrets set`.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
SECRET_FILE = REPO / ".cloudprep_secret"
QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def load_or_create_secret(path: Path = SECRET_FILE) -> str:
    """CLOUD_PREP_SECRET from the environment, else the saved one, else a new one (saved)."""
    env = (os.environ.get("CLOUD_PREP_SECRET") or "").strip()
    if env:
        return env
    if path.is_file():
        s = path.read_text(encoding="utf-8").strip()
        if len(s) >= 16:
            return s
    s = secrets.token_urlsafe(48)
    path.write_text(s + "\n", encoding="utf-8")
    return s


def parse_tunnel_url(line: str) -> Optional[str]:
    """The public address a quick tunnel prints (on stderr), or None."""
    m = QUICK_URL.search(line)
    return m.group(0) if m else None


def supabase_cmd(url: str, secret: str) -> list[str]:
    npx = shutil.which("npx") or "npx"           # npx.cmd on Windows
    return [npx, "supabase", "secrets", "set", f"CLOUD_PREP_URL={url}", f"CLOUD_PREP_SECRET={secret}"]


def find_cloudflared(given: Optional[str] = None) -> Optional[str]:
    """--cloudflared / CLOUDFLARED, else on PATH, else cloudflared(.exe) in the repo folder."""
    for c in (given, os.environ.get("CLOUDFLARED")):
        if c and Path(c).is_file():
            return str(Path(c))
    on_path = shutil.which("cloudflared")
    if on_path:
        return on_path
    for name in ("cloudflared.exe", "cloudflared"):
        if (REPO / name).is_file():
            return str(REPO / name)
    return None


def wait_health(url: str, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--tunnel-token", default=os.environ.get("CF_TUNNEL_TOKEN"),
                    help="a named Cloudflare tunnel's token (with --hostname: a fixed address)")
    ap.add_argument("--hostname", default=os.environ.get("CLOUD_PREP_HOSTNAME"),
                    help="the named tunnel's public hostname, e.g. prep.example.org")
    ap.add_argument("--cloudflared", help="path to the cloudflared program (default: on PATH, or in the repo folder)")
    ap.add_argument("--no-supabase", action="store_true", help="do not run `supabase secrets set` (print it instead)")
    a = ap.parse_args(argv)
    if a.tunnel_token and not a.hostname:
        ap.error("--tunnel-token needs --hostname (the address the tunnel serves)")

    cloudflared = find_cloudflared(a.cloudflared)
    if not cloudflared:
        print("cloudflared was not found. Windows (in this folder, PowerShell):\n"
              "  Invoke-WebRequest -Uri https://github.com/cloudflare/cloudflared/releases/latest/download/"
              "cloudflared-windows-amd64.exe -OutFile cloudflared.exe\n"
              "or: winget install --id Cloudflare.cloudflared; macOS: brew install cloudflared; Linux: "
              "https://github.com/cloudflare/cloudflared/releases", file=sys.stderr)
        return 2
    secret = load_or_create_secret()
    local = f"http://127.0.0.1:{a.port}"
    env = {**os.environ, "PORT": str(a.port), "CLOUD_PREP_SECRET": secret,
           "CLOUD_PREP_WORK_DIR": str(REPO / "results" / "cloudprep"),
           "CLOUD_PREP_WHERE": "the AgentMeter preparation service on the owner's computer"}
    procs: list[subprocess.Popen] = []
    try:
        print(f"Starting the preparation service on {local} ...")
        procs.append(subprocess.Popen([sys.executable, str(REPO / "cloudprep" / "app.py")], env=env, cwd=str(REPO)))
        if not wait_health(local, 60):
            print("The preparation service did not start (see its messages above).", file=sys.stderr)
            return 1

        print("Opening the Cloudflare tunnel ...")
        if a.tunnel_token:
            tun = subprocess.Popen([cloudflared, "tunnel", "--no-autoupdate", "run", "--token", a.tunnel_token])
            procs.append(tun)
            public = f"https://{a.hostname}"
        else:
            tun = subprocess.Popen([cloudflared, "tunnel", "--no-autoupdate", "--url", local],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            procs.append(tun)
            found: dict[str, str] = {}

            def read() -> None:
                for line in tun.stderr:                 # keep draining, or cloudflared blocks
                    if "url" not in found and (u := parse_tunnel_url(line)):
                        found["url"] = u
            threading.Thread(target=read, daemon=True).start()
            end = time.monotonic() + 60
            while "url" not in found and time.monotonic() < end and tun.poll() is None:
                time.sleep(0.5)
            if "url" not in found:
                print("cloudflared did not print a trycloudflare.com address.", file=sys.stderr)
                return 1
            public = found["url"]
        if not wait_health(public, 90):
            print(f"{public} does not answer yet (DNS can take a minute); the control plane retries.", file=sys.stderr)

        cmd = supabase_cmd(public, secret)
        if a.no_supabase:
            print("Tell the control plane where the service is:\n  " + " ".join(cmd[:4]) + f" CLOUD_PREP_URL={public} CLOUD_PREP_SECRET=<.cloudprep_secret>")
        else:
            print(f"Telling the control plane: CLOUD_PREP_URL={public}")
            if subprocess.run(cmd, cwd=str(REPO)).returncode != 0:
                print("`supabase secrets set` failed: run `npx supabase login` and `npx supabase link` in this folder, "
                      "then start this again.", file=sys.stderr)
                return 1
        print(f"\nReady: {public}\nAnalyse traffic works while this window stays open. Ctrl+C stops it.")
        while all(p.poll() is None for p in procs):
            time.sleep(2)
        print("A process stopped; shutting down.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopping.")
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
