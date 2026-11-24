"""scripts/rebuild_pdfs.py: stored sessions get their PDF rebuilt with the current layout."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rebuild_pdfs", ROOT / "scripts" / "rebuild_pdfs.py")
rb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rb)


class FakeClient:
    """The control plane's signed backend routes, in memory."""

    def __init__(self, sessions):
        self.sessions, self.put = sessions, {}

    def call(self, method, path, body=None, raw=None, content_type="application/json"):
        if method == "GET" and path == "/api/backend/sessions":
            return {"sessions": [{"id": k, "files": list(v["files"])} for k, v in self.sessions.items()]}
        if method == "PUT":
            self.put[path] = (raw, content_type)
            return {"ok": True}
        raise AssertionError(path)

    def request(self, method, path, body=None, raw=None, content_type="application/json"):
        sid, rest = path.split("/api/backend/sessions/")[1].split("/", 1)
        s = self.sessions[sid]
        if rest == "results":
            return json.dumps(s["results"]).encode()
        return json.dumps(s["files"][rest.split("files/")[1]]).encode()


def test_rebuilds_every_stored_session_and_keeps_going_after_a_bad_one():
    res = json.loads((ROOT / "tests" / "fixtures" / "report" / "mock-two.json").read_text())
    c = FakeClient({"job_a": {"results": res, "files": {"explanation.json": {"status": "skipped"}}},
                    "job_b": {"results": {"broken": True}, "files": {}}})
    logs = []
    out = rb.rebuild(c, log=logs.append)
    assert out == {"rebuilt": 1, "skipped": 0, "failed": 1}
    pdf, ctype = c.put["/api/backend/sessions/job_a/files/report.pdf"]
    assert pdf[:5] == b"%PDF-" and ctype == "application/pdf"
    assert any(l.startswith("job_b: FAILED") for l in logs)


def test_dry_run_changes_nothing_and_one_session_can_be_picked():
    c = FakeClient({"job_a": {"results": {}, "files": {}}, "job_b": {"results": {}, "files": {}}})
    assert rb.rebuild(c, dry_run=True, log=lambda s: None)["skipped"] == 2 and not c.put
    assert rb.rebuild(c, only="job_x", log=lambda s: None) == {"rebuilt": 0, "skipped": 0, "failed": 0}
