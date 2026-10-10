"""Phase 47: upload sessions finished BEFORE the control plane existed (or while it was
unreachable) to the Worker, so Home / Sessions / Compare / Leaderboard show them with the GPU off.

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product.

    export AGENTMETER_WORKER_URL=https://agentmeter-control-plane.<you>.workers.dev
    export AGENTMETER_BACKEND_SECRET=...            # the Worker's BACKEND_SECRET
    python scripts/backfill_sessions.py --owner alice [--jobs-dir results/jobs] [--dry-run] [--force]

Each finished benchmark job is uploaded once (job["cp_uploaded"] marks it; --force re-uploads):
session_results.json, report.pdf, manifest.json, features.csv (identification columns stripped
unless --keep-identification-columns), labels.csv, then the compact summary. Sessions with no
recorded user are attributed to --owner. Idempotent: the Worker overwrites the same keys.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agentmeter.server import control_plane  # noqa: E402
from agentmeter.server.jobs import DEFAULT_JOBS_DIR, DEFAULT_RESULTS_ROOT, JobManager  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--owner", required=True, help="username to attribute sessions with no recorded user to")
    ap.add_argument("--jobs-dir", default=str(DEFAULT_JOBS_DIR))
    ap.add_argument("--results-root", default=str(DEFAULT_RESULTS_ROOT))
    ap.add_argument("--worker", default=os.environ.get("AGENTMETER_WORKER_URL"), help="Worker URL (or AGENTMETER_WORKER_URL)")
    ap.add_argument("--keep-identification-columns", action="store_true",
                    help="keep IPs/ports/protocol/timestamp in the R2 copy of features.csv (default: stripped)")
    ap.add_argument("--force", action="store_true", help="re-upload sessions already marked as uploaded")
    ap.add_argument("--dry-run", action="store_true", help="list what would be uploaded")
    a = ap.parse_args(argv)
    secret = os.environ.get("AGENTMETER_BACKEND_SECRET", "")
    if not a.dry_run and (not a.worker or not secret):
        print("ERROR: set AGENTMETER_WORKER_URL (or --worker) and AGENTMETER_BACKEND_SECRET", file=sys.stderr)
        return 2
    mgr = JobManager(jobs_dir=a.jobs_dir, results_root=a.results_root, autostart=False)
    todo = [j for j in mgr._all() if (j.get("kind") or "benchmark") == "benchmark" and j.get("status") == "done"
            and j.get("result_path") and Path(j["result_path"]).exists() and (a.force or not j.get("cp_uploaded"))]
    print(f"{len(todo)} finished session(s) to upload from {a.jobs_dir}")
    if a.dry_run:
        for j in todo:
            print(f"  {j['job_id']}  {j.get('run_name')}  {', '.join(j.get('models') or [])}")
        return 0
    cp = control_plane.ControlPlane(control_plane.Client(a.worker, secret), get_manager=lambda: mgr, mode="backfill",
                                    environment=lambda: {})
    cp.limits = {"keep_identification_columns_in_r2": bool(a.keep_identification_columns)}
    ok = failed = 0
    for j in todo:
        try:
            cp.upload(j["job_id"], owner_username=a.owner, force=a.force)
            ok += 1
            print(f"  uploaded {j['job_id']}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAILED   {j['job_id']}: {e}", file=sys.stderr)
    print(f"done: {ok} uploaded, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
