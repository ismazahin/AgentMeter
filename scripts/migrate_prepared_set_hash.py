"""Phase 47 follow-up: recompute the prepared-set hash (version 2) of sessions already stored in
the control plane, so Compare / Leaderboard group them correctly with newer sessions.

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product.

Version 2 hashes only what the models see and are scored against (model-visible feature columns +
labels), so the stored copy (identification columns stripped) gives the same hash as the full
copy on the backend. For each stored session still on version 1 this:
  1. downloads its stored manifest.json / features.csv / labels.csv from the Worker (R2),
     or, with --jobs-dir, reads the run folder of a backend that still has it;
  2. recomputes the hash (agentmeter.session.analyses.prepared_set_content_hash);
  3. replaces ONLY the stored hash (no status change, no notification) and adds the content hash
     to the stored manifest, so that copy can be re-imported on a later rental.
No score, SAW value, rank or verdict is touched. Idempotent: version-2 sessions are skipped.

    export AGENTMETER_WORKER_URL=https://agentmeter-control-plane.<you>.workers.dev
    export AGENTMETER_BACKEND_SECRET=...            # the Worker's BACKEND_SECRET
    python scripts/migrate_prepared_set_hash.py [--dry-run] [--jobs-dir results/jobs]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agentmeter.server import control_plane  # noqa: E402
from agentmeter.session.analyses import (PREPARED_SET_HASH_VERSION, prepared_set_content_hash,  # noqa: E402
                                         prepared_set_sha256)


def from_stored(client: control_plane.Client, sid: str, files: list[str]) -> tuple[str, bytes] | None:
    """(v2 hash, manifest with the content hash) from the session's stored copy, or None."""
    if not {"manifest.json", "features.csv"} <= set(files):
        return None
    get = lambda n: client.request("GET", f"/api/backend/sessions/{sid}/files/{n}")  # noqa: E731
    man = json.loads(get("manifest.json").decode("utf-8"))
    ps = man.get("prepared_set") or {}
    cols = list((ps.get("input_metadata") or {}).get("feature_columns") or [])
    if not cols:
        return None
    labels = get("labels.csv").decode("utf-8") if "labels.csv" in files else None
    h = prepared_set_content_hash(get("features.csv").decode("utf-8"), labels, cols)
    ps.update(content_sha256=h, content_hash_version=PREPARED_SET_HASH_VERSION)
    return h, json.dumps(man, indent=2).encode("utf-8")


def from_jobs_dir(jobs_dir: Path, sid: str) -> str | None:
    p = jobs_dir / f"{sid}.json"
    if not p.is_file():
        return None
    run_dir = json.loads(p.read_text(encoding="utf-8")).get("run_dir")
    return prepared_set_sha256(run_dir) if run_dir and Path(run_dir).is_dir() else None


def migrate(client: control_plane.Client, jobs_dir: Path | None = None, dry_run: bool = False,
            log=print) -> dict[str, int]:
    out = {"migrated": 0, "already": 0, "skipped": 0}
    for s in client.call("GET", "/api/backend/sessions").get("sessions", []):
        sid = s["id"]
        if s.get("prepared_set_hash_version") == PREPARED_SET_HASH_VERSION:
            out["already"] += 1
            continue
        got = from_stored(client, sid, s.get("files") or [])
        h, manifest = (got if got else (from_jobs_dir(jobs_dir, sid) if jobs_dir else None, None))
        if not h:
            out["skipped"] += 1
            log(f"  skipped  {sid}: no stored copy of its flows (and no run folder) — it keeps its old hash")
            continue
        log(f"  {'would set' if dry_run else 'migrated'} {sid}: {str(s.get('prepared_set_sha256'))[:12]} -> {h[:12]}")
        if not dry_run:
            client.call("POST", f"/api/backend/sessions/{sid}/identity",
                        {"prepared_set_sha256": h, "prepared_set_hash_version": PREPARED_SET_HASH_VERSION})
            if manifest is not None:
                client.call("PUT", f"/api/backend/sessions/{sid}/files/manifest.json", raw=manifest)
        out["migrated"] += 1
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--worker", default=os.environ.get("AGENTMETER_WORKER_URL"))
    ap.add_argument("--jobs-dir", default=None, help="a backend's results/jobs, for sessions with no stored copy")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    secret = os.environ.get("AGENTMETER_BACKEND_SECRET", "")
    if not a.worker or not secret:
        print("ERROR: set AGENTMETER_WORKER_URL (or --worker) and AGENTMETER_BACKEND_SECRET", file=sys.stderr)
        return 2
    r = migrate(control_plane.Client(a.worker, secret), Path(a.jobs_dir) if a.jobs_dir else None, a.dry_run)
    print(f"done: {r['migrated']} {'to migrate' if a.dry_run else 'migrated'}, {r['already']} already on version "
          f"{PREPARED_SET_HASH_VERSION}, {r['skipped']} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
