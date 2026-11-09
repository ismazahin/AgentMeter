#!/usr/bin/env python3
"""Run AgentMeter's data-preparation service (and the traffic analysis) on YOUR computer.

No card, no rented GPU: this runs cloudprep/app.py here, opens a Cloudflare tunnel to it and tells
the control plane where it is (signed with the service's secret, like the GPU server). While it
runs, Analyse traffic works; the site's Start button starts it (after `install`, Windows).

    python scripts/prep_local.py                  # run here, in this terminal (Ctrl+C stops it)
    python scripts/prep_local.py install          # once: the site's Start / Stop buttons work on this PC
    python scripts/prep_local.py start | stop | status
    python scripts/prep_local.py set-secret       # once: give the control plane this service's secret
    python scripts/prep_local.py uninstall        # remove what install added
    python scripts/prep_local.py --tunnel-token <t> --hostname prep.example.org   # a named tunnel

Needs: Python with requirements.txt (+ requirements-pcap.txt and cicflowmeter for PCAPs) and
cloudflared (on PATH, or cloudflared.exe in the repo folder). The secret is created once in
.cloudprep_secret (git-ignored); the control plane must have the same CLOUD_PREP_SECRET
(`set-secret` runs `npx supabase secrets set` for it, once). The control plane's address comes
from --control-plane, AGENTMETER_CONTROL_PLANE_URL, or web/config.json.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
SECRET_FILE = REPO / ".cloudprep_secret"
RUN_DIR = REPO / "results"
QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
PROTOCOL = "agentmeter-prep"
VERSION = "prep_local/2"
WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if WIN else 0             # CREATE_NO_WINDOW: children open no console
log = logging.getLogger("prep_local")


def heartbeat_s() -> float:
    return float(os.environ.get("PREP_LOCAL_HEARTBEAT_S", "60"))


def public_check_s() -> float:
    """How often the public address is checked (0 = never); three misses in a row reopen the tunnel."""
    return float(os.environ.get("PREP_LOCAL_PUBLIC_CHECK_S", "300"))


def paths(run_dir: Path = RUN_DIR) -> dict[str, Path]:
    return {"pid": run_dir / "prep_local.pid", "stop": run_dir / "prep_local.stop",
            "state": run_dir / "prep_local.state.json", "log": run_dir / "prep_local.log"}


# ---------------------------------------------------------------- small helpers
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


def control_plane_url(given: Optional[str] = None) -> Optional[str]:
    for c in (given, os.environ.get("AGENTMETER_CONTROL_PLANE_URL")):
        if c and c.strip():
            return c.strip().rstrip("/")
    try:
        c = json.loads((REPO / "web" / "config.json").read_text(encoding="utf-8")).get("control_plane")
        return c.strip().rstrip("/") if c else None
    except (OSError, ValueError):
        return None


def sign(secret: str, method: str, path: str, ts: str, body: bytes) -> str:
    """The control plane's check (backend.ts verifySigned): HMAC(secret, method\\npath\\nts\\nsha256(body))."""
    msg = f"{method}\n{path}\n{ts}\n{hashlib.sha256(body).hexdigest()}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def tell_control_plane(cp: str, secret: str, route: str, payload: dict, timeout: float = 15) -> tuple[int, dict]:
    """POST a signed JSON body to <cp>/api/prep/<route>; (status, body). Never raises for HTTP/network."""
    path = f"/api/prep/{route}"
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    req = urllib.request.Request(cp + path, data=body, method="POST", headers={
        "content-type": "application/json", "X-AM-Timestamp": ts, "X-AM-Signature": sign(secret, "POST", path, ts, body)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError) as e:
        return 0, {"error": str(e)}


def wait_health(url: str, timeout_s: float, stop: Optional[threading.Event] = None) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end and not (stop and stop.is_set()):
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if WIN:                                       # os.kill(pid, 0) would TERMINATE the process on Windows
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259     # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def running_pid(run_dir: Path = RUN_DIR) -> Optional[int]:
    p = paths(run_dir)["pid"]
    try:
        pid = int(p.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def parse_link(url: str) -> Optional[str]:
    """agentmeter-prep://start or ://stop (a browser may add a slash); anything else: None."""
    m = re.fullmatch(rf"{PROTOCOL}:/*(start|stop)/?", (url or "").strip().lower())
    return m.group(1) if m else None


# ---------------------------------------------------------------- the supervisor
class Supervisor:
    """Runs the service and the tunnel, registers with the control plane, sends heartbeats, and
    restarts whichever stops. Ends on Ctrl+C, a stop file (`stop`) or stop_event."""

    def __init__(self, *, cloudflared: str, secret: str, cp: Optional[str], port: int = 8090,
                 tunnel_token: Optional[str] = None, hostname: Optional[str] = None,
                 run_dir: Path = RUN_DIR, child_out=None):
        self.cloudflared, self.secret, self.cp, self.port = cloudflared, secret, cp, port
        self.tunnel_token, self.hostname, self.run_dir = tunnel_token, hostname, run_dir
        self.child_out = child_out                 # where children write (a log file in the background)
        self.local = f"http://127.0.0.1:{port}"
        self.service: Optional[subprocess.Popen] = None
        self.tunnel: Optional[subprocess.Popen] = None
        self.public: Optional[str] = None
        self.stop_event = threading.Event()
        self.p = paths(run_dir)

    # children
    def start_service(self) -> bool:
        env = {**os.environ, "PORT": str(self.port), "CLOUD_PREP_SECRET": self.secret,
               "CLOUD_PREP_WORK_DIR": str(self.run_dir / "cloudprep"),
               "CLOUD_PREP_WHERE": "the AgentMeter preparation service on the owner's computer"}
        exe = sys.executable
        if WIN and exe.lower().endswith("pythonw.exe") and Path(exe[:-5] + ".exe").exists():
            exe = exe[:-5] + ".exe"                # the service's own output goes to the log either way
        self.service = subprocess.Popen([exe, str(REPO / "cloudprep" / "app.py")], env=env, cwd=str(REPO),
                                        stdout=self.child_out, stderr=self.child_out, creationflags=NO_WINDOW)
        ok = wait_health(self.local, 60, self.stop_event)
        log.info("preparation service %s on %s", "started" if ok else "did NOT start", self.local)
        return ok

    def start_tunnel(self) -> Optional[str]:
        if self.tunnel_token:
            self.tunnel = subprocess.Popen([self.cloudflared, "tunnel", "--no-autoupdate", "run", "--token", self.tunnel_token],
                                           stdout=self.child_out, stderr=self.child_out, creationflags=NO_WINDOW)
            return f"https://{self.hostname}"
        tun = subprocess.Popen([self.cloudflared, "tunnel", "--no-autoupdate", "--url", self.local],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, creationflags=NO_WINDOW)
        self.tunnel = tun
        found: dict[str, str] = {}

        def read() -> None:
            for line in tun.stderr:                # keep draining, or cloudflared blocks
                if "url" not in found and (u := parse_tunnel_url(line)):
                    found["url"] = u
        threading.Thread(target=read, daemon=True).start()
        end = time.monotonic() + 60
        while "url" not in found and time.monotonic() < end and tun.poll() is None and not self.stop_event.is_set():
            time.sleep(0.3)
        return found.get("url")

    # the control plane
    def register(self) -> bool:
        if not self.cp or not self.public:
            return False
        st, d = tell_control_plane(self.cp, self.secret, "register", {"url": self.public, "version": VERSION})
        if st == 200:
            log.info("registered with the control plane: %s", self.public)
            return True
        hint = {401: " (the control plane has another CLOUD_PREP_SECRET: run `python scripts/prep_local.py set-secret` once)",
                409: " (the control plane has no CLOUD_PREP_SECRET: run `python scripts/prep_local.py set-secret` once)"}.get(st, "")
        log.warning("the control plane refused the registration: HTTP %s %s%s", st, d.get("error", ""), hint)
        return False

    def heartbeat(self) -> None:
        if self.cp and self.public:
            st, d = tell_control_plane(self.cp, self.secret, "heartbeat", {"url": self.public, "version": VERSION})
            if st != 200:
                log.warning("heartbeat: HTTP %s %s", st, d.get("error", ""))

    def save_state(self, **extra) -> None:
        state = {"pid": os.getpid(), "url": self.public, "local": self.local, "control_plane": self.cp,
                 "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), **extra}
        self.p["state"].write_text(json.dumps(state, indent=2), encoding="utf-8")

    def open_tunnel(self) -> bool:
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()
        self.public = self.start_tunnel()
        if not self.public:
            log.warning("cloudflared did not give a public address; retrying soon")
            return False
        log.info("tunnel open: %s", self.public)
        self.register()
        self.save_state()
        return True

    # the loop
    def run(self) -> int:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.p["stop"].unlink(missing_ok=True)
        self.p["pid"].write_text(str(os.getpid()))
        if not self.cp:
            log.warning("no control plane address (web/config.json control_plane): the site will not find this service")
        try:
            if not self.start_service():
                return 1
            self.open_tunnel()
            last_beat = last_check = time.monotonic()
            misses = 0
            while not self.stop_event.is_set():
                if self.p["stop"].exists():
                    log.info("stop requested")
                    break
                now = time.monotonic()
                if self.service.poll() is not None:
                    log.warning("the preparation service stopped (exit %s): restarting it", self.service.returncode)
                    self.start_service()
                if self.tunnel is None or self.tunnel.poll() is not None or not self.public:
                    log.warning("the tunnel is down: reopening it")
                    self.open_tunnel()
                    last_beat = last_check = now
                if now - last_beat >= heartbeat_s():
                    self.heartbeat(); self.save_state(); last_beat = now
                if public_check_s() and now - last_check >= public_check_s() and self.public:
                    last_check = now
                    misses = 0 if wait_health(self.public, 10, self.stop_event) else misses + 1
                    if misses >= 3:
                        log.warning("%s has not answered three times: reopening the tunnel", self.public)
                        misses = 0
                        self.open_tunnel()
                self.stop_event.wait(1)
            return 0
        except KeyboardInterrupt:
            return 0
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self.cp and self.public:
            tell_control_plane(self.cp, self.secret, "register", {"stopping": True}, timeout=5)
        for proc in (self.tunnel, self.service):
            if proc and proc.poll() is None:
                proc.terminate()
        for proc in (self.tunnel, self.service):
            if proc:
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for k in ("pid", "stop"):
            self.p[k].unlink(missing_ok=True)
        try:
            self.save_state(stopped=True, url=None)
        except OSError:
            pass
        log.info("stopped")


# ---------------------------------------------------------------- commands
def setup_logging(background: bool, run_dir: Path = RUN_DIR):
    run_dir.mkdir(parents=True, exist_ok=True)
    log.handlers.clear()
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if background:
        h = logging.handlers.RotatingFileHandler(paths(run_dir)["log"], maxBytes=5 * 2 ** 20, backupCount=3, encoding="utf-8")
        h.setFormatter(fmt)
        log.addHandler(h)
        return open(run_dir / "prep_local.service.log", "ab")      # the children's own output
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(fmt)
    log.addHandler(h)
    return None


def cmd_run(a) -> int:
    child_out = setup_logging(a.background)
    pid = running_pid()
    if pid and pid != os.getpid():
        log.info("already running (pid %s)", pid)
        return 0
    cf = find_cloudflared(a.cloudflared)
    if not cf:
        log.error("cloudflared was not found. Windows (in the repo folder, PowerShell): Invoke-WebRequest -Uri "
                  "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe "
                  "-OutFile cloudflared.exe")
        return 2
    sup = Supervisor(cloudflared=cf, secret=load_or_create_secret(), cp=control_plane_url(a.control_plane), port=a.port,
                     tunnel_token=a.tunnel_token, hostname=a.hostname, child_out=child_out)
    if not a.background:
        log.info("Analyse traffic works while this runs. Ctrl+C stops it.")
    return sup.run()


def background_command() -> list[str]:
    exe = sys.executable
    if WIN and exe.lower().endswith("python.exe") and Path(exe[:-4] + "w.exe").exists():
        exe = exe[:-4] + "w.exe"                    # pythonw: no console window
    return [exe, str(Path(__file__).resolve()), "run", "--background"]


def cmd_start(_a=None) -> int:
    pid = running_pid()
    if pid:
        print(f"Already running (pid {pid}).")
        return 0
    flags = (0x00000008 | 0x00000200 | NO_WINDOW) if WIN else 0     # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen(background_command(), cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags, start_new_session=not WIN)
    print("Starting in the background; the log is results/prep_local.log.")
    return 0


def cmd_stop(_a=None, wait_s: float = 20) -> int:
    pid = running_pid()
    if not pid:
        print("Not running.")
        return 0
    paths()["stop"].write_text("stop")
    end = time.monotonic() + wait_s
    while time.monotonic() < end and pid_alive(pid):
        time.sleep(0.5)
    print("Stopped." if not pid_alive(pid) else f"Asked it to stop (pid {pid} still running).")
    return 0


def cmd_status(_a=None) -> int:
    pid = running_pid()
    try:
        st = json.loads(paths()["state"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    print(f"Running (pid {pid}) at {st.get('url') or '(no public address yet)'}" if pid else "Not running.")
    print(f"Start / Stop buttons: {'installed' if protocol_installed() else 'not installed (run: python scripts/prep_local.py install)'}")
    return 0


def protocol_entries() -> dict[str, dict[str, str]]:
    """The registry keys `install` writes (HKEY_CURRENT_USER): the link type the site's buttons open."""
    cmd = background_command()[:2]
    root = rf"Software\Classes\{PROTOCOL}"
    return {root: {"": "URL:AgentMeter preparation service", "URL Protocol": ""},
            root + r"\shell\open\command": {"": f'"{cmd[0]}" "{cmd[1]}" link "%1"'}}


def protocol_installed() -> bool:
    if not WIN:
        return False
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{PROTOCOL}"))
        return True
    except OSError:
        return False


def cmd_install(_a=None) -> int:
    if not WIN:
        print("The Start button works on Windows only. Here, run: python scripts/prep_local.py start")
        return 1
    import winreg
    for key, values in protocol_entries().items():
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key)
        for name, val in values.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        winreg.CloseKey(k)
    load_or_create_secret()
    print("Installed: the site's Start / Stop buttons now work on this PC (your browser asks once; tick Always allow).")
    print("If the control plane does not have this service's secret yet, run once: python scripts/prep_local.py set-secret")
    return 0


def cmd_uninstall(_a=None) -> int:
    cmd_stop()
    if not WIN:
        return 0
    import winreg
    for key in sorted(protocol_entries(), key=len, reverse=True) + [rf"Software\Classes\{PROTOCOL}\shell\open", rf"Software\Classes\{PROTOCOL}\shell"]:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{PROTOCOL}")
    except OSError:
        pass
    print("Uninstalled: the Start / Stop buttons no longer start anything on this PC.")
    return 0


def cmd_link(a) -> int:
    what = parse_link(a.url)
    if what == "start":
        return cmd_start()
    if what == "stop":
        return cmd_stop()
    return 2                                          # anything else: ignored


def cmd_set_secret(_a=None) -> int:
    npx = shutil.which("npx") or "npx"
    secret = load_or_create_secret()
    print("Giving the control plane this service's secret (Supabase asks to confirm) ...")
    r = subprocess.run([npx, "supabase", "secrets", "set", f"CLOUD_PREP_SECRET={secret}"], cwd=str(REPO))
    if r.returncode:
        print("Failed: run `npx supabase login` and `npx supabase link` in this folder first.", file=sys.stderr)
        return 1
    print("Done. If an old CLOUD_PREP_URL is set from earlier runs, remove it: npx supabase secrets unset CLOUD_PREP_URL")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "start", "stop", "status", "install", "uninstall", "link", "set-secret"])
    ap.add_argument("url", nargs="?", help="link: the agentmeter-prep:// link the browser opened")
    ap.add_argument("--background", action="store_true", help="run: no console output; log to results/prep_local.log")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--control-plane", help="the control plane's URL (default: web/config.json)")
    ap.add_argument("--tunnel-token", default=os.environ.get("CF_TUNNEL_TOKEN"),
                    help="a named Cloudflare tunnel's token (with --hostname: a fixed address)")
    ap.add_argument("--hostname", default=os.environ.get("CLOUD_PREP_HOSTNAME"),
                    help="the named tunnel's public hostname, e.g. prep.example.org")
    ap.add_argument("--cloudflared", help="path to the cloudflared program (default: on PATH, or in the repo folder)")
    a = ap.parse_args(argv)
    if a.tunnel_token and not a.hostname:
        ap.error("--tunnel-token needs --hostname (the address the tunnel serves)")
    return {"run": cmd_run, "start": cmd_start, "stop": cmd_stop, "status": cmd_status, "install": cmd_install,
            "uninstall": cmd_uninstall, "link": cmd_link, "set-secret": cmd_set_secret}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
