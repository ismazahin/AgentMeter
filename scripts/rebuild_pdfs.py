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
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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


def rebuild(client: control_plane.Client, only: Optional[str] = None, dry_run: bool = False,
            log: Callable[[str], None] = print) -> dict[str, int]:
    out = {"rebuilt": 0, "skipped": 0, "failed": 0}
    for s in client.call("GET", "/api/backend/sessions").get("sessions", []):
        sid, files = s["id"], set(s.get("files") or [])
        if only and sid != only:
            continue
        if dry_run:
            log(f"{sid}: would rebuild ({'with' if 'explanation.json' in files else 'without'} written explanations)")
            out["skipped"] += 1
            continue
        try:
            res = json.loads(client.request("GET", f"/api/backend/sessions/{sid}/results").decode("utf-8"))
            get = lambda n: json.loads(client.request("GET", f"/api/backend/sessions/{sid}/files/{n}").decode("utf-8"))  # noqa: E731
            explanation = get("explanation.json") if "explanation.json" in files else None
            prepared = (get("manifest.json") or {}).get("prepared_set") if "manifest.json" in files else None
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
    ap.add_argument("--worker", default=os.environ.get("AGENTMETER_CONTROL_PLANE_URL") or os.environ.get("AGENTMETER_WORKER_URL"))
    a = ap.parse_args(argv)
    secret = os.environ.get("AGENTMETER_BACKEND_SECRET")
    if not a.worker or not secret:
        print("set AGENTMETER_CONTROL_PLANE_URL and AGENTMETER_BACKEND_SECRET (on the GPU machine: . /root/agentmeter.env)")
        return 2
    r = rebuild(control_plane.Client(a.worker, secret), a.session, a.dry_run)
    print(f"done: {r['rebuilt']} rebuilt, {r['failed']} failed" + (f", {r['skipped']} listed" if a.dry_run else ""))
    return 1 if r["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
