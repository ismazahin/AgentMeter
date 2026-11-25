"""Rebuild the PDF report of sessions already stored in the control plane, with the current layout.

A session's PDF is built once, when it finishes, so sessions from before a report change keep the
old layout. Run this on the GPU machine (it has the backend's control-plane credentials and the
PDF libraries) to rebuild them from their stored results. A session whose explainer wrote the
report's reading paragraphs (explanation.json "report") gets them; older ones get the standard
one-line answers under each section. No score, rank or verdict is recomputed: the PDF only reads
the stored results.

    cd /workspace/AgentMeter && . /root/agentmeter.env
    python scripts/rebuild_pdfs.py --dry-run          # list what would be rebuilt
    python scripts/rebuild_pdfs.py                    # every stored session
    python scripts/rebuild_pdfs.py --session job_...  # one session
    python scripts/rebuild_pdfs.py --stale-only --gentle   # what scripts/vast_up.sh runs at start-up

--stale-only skips a session whose stored PDF already carries the current layout marker
(report_html.LAYOUT, in the PDF's Keywords) or, when the session has written explanations, the
explained marker. So every rental redoes only what changed. --gentle holds the machine's lock
(gpulock) for one PDF at a time at the lowest CPU priority and steps aside while a benchmark or a
preparation is waiting, so a rebuild never runs next to a measurement.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agentmeter.server import control_plane  # noqa: E402


def build_pdf(res: dict, job_id: str, explanation: Optional[dict], prepared: Optional[dict]) -> tuple[bytes, str]:
    """(pdf bytes, which layout): the full report, or the plain one when it cannot be built here."""
    try:
        from agentmeter.session.report_html import build_report_pdf
        return build_report_pdf(res, job={"job_id": job_id}, explanation=explanation, prepared=prepared), "full"
    except Exception as e:  # noqa: BLE001 - missing WeasyPrint/Pango/QuickJS: the plain report, said so
        from agentmeter.session.pdf_report import build_report_pdf as plain
        print(f"  full layout not available here ({e}), using the plain report")
        return plain(res, job={"job_id": job_id}, prepared=prepared), "plain"


def current_marker(pdf: bytes) -> str:
    """The layout marker a stored PDF carries ("" for an older or plain PDF)."""
    try:
        import io

        import pypdf
        return str((pypdf.PdfReader(io.BytesIO(pdf)).metadata or {}).get("/Keywords") or "")
    except Exception:  # noqa: BLE001 - unreadable or no pypdf: treat it as out of date
        return ""


def is_current(client, sid: str, files: set) -> bool:
    if "report.pdf" not in files:
        return False
    from agentmeter.session.report_html import LAYOUT
    mark = current_marker(client.request("GET", f"/api/backend/sessions/{sid}/files/report.pdf"))
    if not mark.startswith(LAYOUT):
        return False
    if "explanation.json" in files and not mark.endswith("-explained"):
        ex = json.loads(client.request("GET", f"/api/backend/sessions/{sid}/files/explanation.json").decode("utf-8"))
        return not (ex.get("report") or {}).get("cards")          # explanations exist that the PDF lacks
    return True


@contextlib.contextmanager
def _turn(gentle: bool):
    """One PDF's turn on the machine: with --gentle, wait for the shared lock and let a waiting
    benchmark or preparation go first."""
    if not gentle:
        yield
        return
    from agentmeter.server import gpulock
    gpulock.wait_for_prep()
    with gpulock.hold({"kind": "rebuild_pdf"}):
        yield
    time.sleep(2.0)                    # a gap, so a queued benchmark takes the lock before the next PDF


def rebuild(client: control_plane.Client, only: Optional[str] = None, dry_run: bool = False,
            log: Callable[[str], None] = print, stale_only: bool = False, gentle: bool = False) -> dict[str, int]:
    out = {"rebuilt": 0, "skipped": 0, "failed": 0, "current": 0}
    for s in client.call("GET", "/api/backend/sessions").get("sessions", []):
        sid, files = s["id"], set(s.get("files") or [])
        if only and sid != only:
            continue
        try:
            if stale_only and is_current(client, sid, files):
                out["current"] += 1
                continue
        except Exception as e:  # noqa: BLE001 - cannot tell: rebuild it
            log(f"{sid}: could not read the stored PDF ({e}), rebuilding")
        if dry_run:
            log(f"{sid}: would rebuild ({'with' if 'explanation.json' in files else 'without'} written explanations)")
            out["skipped"] += 1
            continue
        try:
            res = json.loads(client.request("GET", f"/api/backend/sessions/{sid}/results").decode("utf-8"))
            get = lambda n, sid=sid: json.loads(client.request("GET", f"/api/backend/sessions/{sid}/files/{n}").decode("utf-8"))  # noqa: E731
            explanation = get("explanation.json") if "explanation.json" in files else None
            prepared = (get("manifest.json") or {}).get("prepared_set") if "manifest.json" in files else None
            with _turn(gentle):
                pdf, layout = build_pdf(res, sid, explanation, prepared)
            client.call("PUT", f"/api/backend/sessions/{sid}/files/report.pdf", raw=pdf, content_type="application/pdf")
            log(f"{sid}: rebuilt ({layout} layout, {len(pdf):,} bytes"
                + (", with the reading paragraphs" if (explanation or {}).get("report") else "") + ")")
            out["rebuilt"] += 1
        except Exception as e:  # noqa: BLE001 - one bad session never stops the others
            log(f"{sid}: FAILED {type(e).__name__}: {e}")
            out["failed"] += 1
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--session", help="only this session id")
    ap.add_argument("--dry-run", action="store_true", help="list the sessions, change nothing")
    ap.add_argument("--stale-only", action="store_true", help="skip sessions whose PDF already has the current layout")
    ap.add_argument("--gentle", action="store_true", help="one PDF at a time under the machine lock, after any waiting benchmark")
    ap.add_argument("--worker", default=os.environ.get("AGENTMETER_CONTROL_PLANE_URL") or os.environ.get("AGENTMETER_WORKER_URL"))
    a = ap.parse_args(argv)
    secret = os.environ.get("AGENTMETER_BACKEND_SECRET")
    if not a.worker or not secret:
        print("set AGENTMETER_CONTROL_PLANE_URL and AGENTMETER_BACKEND_SECRET (on the GPU machine: . /root/agentmeter.env)")
        return 2
    if a.gentle:
        with contextlib.suppress(OSError):
            os.nice(19)                 # the lowest CPU priority: never in a benchmark's way
    r = rebuild(control_plane.Client(a.worker, secret), a.session, a.dry_run, stale_only=a.stale_only, gentle=a.gentle)
    print(f"done: {r['rebuilt']} rebuilt, {r['current']} already current, {r['failed']} failed"
          + (f", {r['skipped']} listed" if a.dry_run else ""))
    return 1 if r["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
