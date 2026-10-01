"""Phase 29 — remote runner for a Vast.ai (or any SSH) GPU box.

Option 1 from the Vast discussion: you rent the GPU box yourself, set its SSH
details ONCE in .env, and the web app generates a double-click script that SSHes
into the box, runs the EXISTING benchmark there, and copies the results back so the
local dashboard's "Local results" discovers them.

Single source of truth = .env (read LIVE per request, so an edit is picked up
without restarting the server). The web app only GENERATES a FILE — it never runs
anything itself and never triggers a run from the browser.

SECURITY: the generated script embeds only non-secret connection details — host,
port, user, the PATH to your private key, and the remote directory. It NEVER embeds
the private key's contents or VAST_API_KEY. Running still happens when you
double-click the file on your own machine.

Assumptions (Option 1): the box is already rented and reachable, has AgentMeter
installed with its Python env + CUDA, and the config has been copied there (or the
repo is kept in sync). This module does not provision, install, or rent anything.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from . import config_builder as cb
from ..util import envtools

# .env keys that describe the SSH target. Only HOST + PORT are required; the rest
# have sensible defaults. VAST_API_KEY is intentionally NOT used here (never embedded).
REQUIRED_KEYS = ("VAST_SSH_HOST", "VAST_SSH_PORT")
OPTIONAL_KEYS = ("VAST_SSH_USER", "VAST_SSH_KEY", "VAST_REMOTE_DIR")
ALL_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS


def ssh_config(env: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Resolve the SSH target from .env (live). Returns a dict with host/port/user/
    key/remote_dir, plus `missing` (required keys not set) and `ready` (bool)."""
    env = env if env is not None else envtools.read_dotenv_live()
    get = lambda k: (env.get(k) or "").strip()
    cfg = {
        "host": get("VAST_SSH_HOST"),
        "port": get("VAST_SSH_PORT"),
        "user": get("VAST_SSH_USER") or "root",
        "key": get("VAST_SSH_KEY"),                       # path to the PRIVATE key (not its contents)
        "remote_dir": get("VAST_REMOTE_DIR") or "~/AgentMeter",
    }
    cfg["missing"] = [k for k in REQUIRED_KEYS if not get(k)]
    cfg["ready"] = not cfg["missing"]
    return cfg


def ssh_status(env: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Readiness for the dashboard — PRESENCE only, never values (safe to return to
    the browser). Lists which .env keys are set and which required ones are missing."""
    env = env if env is not None else envtools.read_dotenv_live()
    present = {k: bool((env.get(k) or "").strip()) for k in ALL_KEYS}
    missing = [k for k in REQUIRED_KEYS if not present[k]]
    return {"ready": not missing, "missing": missing, "present": present,
            "required": list(REQUIRED_KEYS), "optional": list(OPTIONAL_KEYS)}


def _analysis_rel(slug: str, base_dir: Optional[Path] = None) -> str:
    """Repo-relative analysis dir for a user config, derived from its
    storage.sqlite_path (matches run-full's auto-analyze dir). POSIX separators, as
    it is a path ON the box and an scp target."""
    base = base_dir or cb.USER_CONFIG_DIR
    cfg_path = base / f"{slug}.yaml"
    sqlite_rel = f"results/user_runs/{slug}.db"           # builder's default
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        sp = (((data.get("storage") or {}).get("sqlite_path")) or "").strip()
        if sp:
            sqlite_rel = sp.replace("\\", "/")
    except (OSError, yaml.YAMLError):
        pass
    p = sqlite_rel[:-3] if sqlite_rel.lower().endswith(".db") else sqlite_rel
    # <dir>/<stem>_analysis
    parent, _, stem = p.rpartition("/")
    return (f"{parent}/{stem}_analysis" if parent else f"{stem}_analysis")


def remote_runner_script(name: str, os_kind: str = "win",
                         env: Optional[dict[str, str]] = None,
                         base_dir: Optional[Path] = None) -> tuple[str, str]:
    """Generate a remote runner (.bat/.sh) for a USER config. Returns (filename, content).

    It SSHes into the configured box, runs
        cd <remote_dir> && python main.py --config configs/user/<slug>.yaml run-full
    then scps the analysis results back into the local results/ so the dashboard
    finds them. Raises ConfigBuildError for a bad os, a protected/missing config, or
    missing SSH details in .env.
    """
    os_kind = (os_kind or "win").lower()
    if os_kind not in ("win", "unix"):
        raise cb.ConfigBuildError("os must be 'win' or 'unix'")
    slug, _ = cb._resolve_existing_user_config(name, base_dir)   # validates + existence
    ssh = ssh_config(env)
    if not ssh["ready"]:
        raise cb.ConfigBuildError(
            "SSH details missing in .env: set " + ", ".join(ssh["missing"]) +
            " (VAST_SSH_HOST, VAST_SSH_PORT; optional VAST_SSH_USER, VAST_SSH_KEY, VAST_REMOTE_DIR).")

    cfg_rel = f"configs/user/{slug}.yaml"
    remote_dir = ssh["remote_dir"]
    analysis_rel = _analysis_rel(slug, base_dir)              # e.g. results/user_runs/<slug>_analysis
    local_parent = analysis_rel.rsplit("/", 1)[0] if "/" in analysis_rel else "results"
    target = f"{ssh['user']}@{ssh['host']}"
    # Unquoted so the remote shell expands ~ and absolute paths alike (the whole
    # command is already wrapped in the ssh argument's double quotes).
    remote_run = f"cd {remote_dir} && python main.py --config {cfg_rel} run-full"
    remote_analysis = f"{remote_dir}/{analysis_rel}"
    # key is OPTIONAL (ssh may use an agent / default key); include -i only if given
    ssh_i = f' -i "{ssh["key"]}"' if ssh["key"] else ""
    scp_i = ssh_i
    hostkey = "-o StrictHostKeyChecking=accept-new"

    if os_kind == "win":
        lines = [
            "@echo off",
            f"REM AgentMeter REMOTE runner for {cfg_rel} (generated from .env; edit freely).",
            "echo ================================================================",
            f"echo   AgentMeter - running {cfg_rel} on {target}",
            "echo   Runs the full benchmark ON THE REMOTE GPU, then copies the",
            "echo   results back into results\\ so the dashboard shows them.",
            "echo   Requires: the box is running and has AgentMeter + its env set up,",
            "echo   and OpenSSH (ssh/scp) on this machine (Windows 10+ has it).",
            "echo ================================================================",
            f'ssh {hostkey}{ssh_i} -p {ssh["port"]} {target} "{remote_run}"',
            "if errorlevel 1 goto failed",
            "echo.",
            "echo Run finished on the box. Copying results back...",
            f'if not exist "{local_parent.replace("/", chr(92))}" mkdir "{local_parent.replace("/", chr(92))}"',
            f'scp {hostkey}{scp_i} -P {ssh["port"]} -r "{target}:{remote_analysis}" "{local_parent.replace("/", chr(92))}\\"',
            "echo Done. Open the dashboard Local results tab to see it.",
            "goto done",
            ":failed",
            "echo.",
            "echo The remote run did not finish cleanly. Check the SSH details in .env",
            "echo and that the box is running with AgentMeter installed.",
            ":done",
            "echo.",
            "pause",
        ]
        return (f"run_remote_{slug}.bat", "\r\n".join(lines) + "\r\n")

    lines = [
        "#!/usr/bin/env bash",
        f"# AgentMeter REMOTE runner for {cfg_rel} (generated from .env; edit freely).",
        "set -o pipefail",
        'echo "================================================================"',
        f'echo "  AgentMeter - running {cfg_rel} on {target}"',
        'echo "  Runs the full benchmark ON THE REMOTE GPU, then copies the"',
        'echo "  results back into results/ so the dashboard shows them."',
        'echo "  Requires: the box is running with AgentMeter + env set up, and ssh/scp here."',
        'echo "================================================================"',
        f'ssh {hostkey}{ssh_i} -p {ssh["port"]} {target} "{remote_run}"',
        "rc=$?",
        'if [ "$rc" -ne 0 ]; then',
        '  echo "The remote run did not finish cleanly. Check .env SSH details and the box."',
        '  read -r -p "Press Enter to close..." _; exit "$rc"',
        "fi",
        'echo "Run finished on the box. Copying results back..."',
        f'mkdir -p "{local_parent}"',
        f'scp {hostkey}{scp_i} -P {ssh["port"]} -r "{target}:{remote_analysis}" "{local_parent}/"',
        'echo "Done. Open the dashboard Local results tab to see it."',
        'read -r -p "Press Enter to close..." _',
    ]
    return (f"run_remote_{slug}.sh", "\n".join(lines) + "\n")
