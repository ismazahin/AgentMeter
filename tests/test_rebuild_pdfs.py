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
        v = s["files"][rest.split("files/")[1]]
        return v if isinstance(v, bytes) else json.dumps(v).encode()


def test_rebuilds_every_stored_session_and_keeps_going_after_a_bad_one():
    res = json.loads((ROOT / "tests" / "fixtures" / "report" / "mock-two.json").read_text())
    c = FakeClient({"job_a": {"results": res, "files": {"explanation.json": {"status": "skipped"}}},
                    "job_b": {"results": {"broken": True}, "files": {}}})
    logs = []
    out = rb.rebuild(c, log=logs.append)
    assert out == {"rebuilt": 1, "skipped": 0, "failed": 1, "current": 0}
    pdf, ctype = c.put["/api/backend/sessions/job_a/files/report.pdf"]
    assert pdf[:5] == b"%PDF-" and ctype == "application/pdf"
    assert any(l.startswith("job_b: FAILED") for l in logs)


def test_dry_run_changes_nothing_and_one_session_can_be_picked():
    c = FakeClient({"job_a": {"results": {}, "files": {}}, "job_b": {"results": {}, "files": {}}})
    assert rb.rebuild(c, dry_run=True, log=lambda s: None)["skipped"] == 2 and not c.put
    assert rb.rebuild(c, only="job_x", log=lambda s: None) == {"rebuilt": 0, "skipped": 0, "failed": 0, "current": 0}


def test_stale_only_rebuilds_old_pdfs_once_and_skips_current_ones(monkeypatch, tmp_path):
    """Every rental runs this at start-up: an old PDF is rebuilt, a current one is left alone,
    and a PDF made before the session's written explanations existed is rebuilt to include them."""
    pytest.importorskip("pypdf")
    from agentmeter.session.report_html import build_report_pdf
    monkeypatch.setenv("AGENTMETER_BUSY_LOCK", str(tmp_path / "busy.lock"))
    res = json.loads((ROOT / "tests" / "fixtures" / "report" / "mock-one.json").read_text())
    current = build_report_pdf(res)
    c = FakeClient({"job_old": {"results": res, "files": {"report.pdf": b"%PDF-1.4 an old ReportLab report"}},
                    "job_new": {"results": res, "files": {"report.pdf": current}},
                    "job_explained_later": {"results": res, "files": {"report.pdf": current,
                                                                       "explanation.json": {"report": {"cards": {"x": {}}}}}}})
    out = rb.rebuild(c, stale_only=True, gentle=True, log=lambda s: None)
    assert out["rebuilt"] == 2 and out["current"] == 1 and out["failed"] == 0
    assert set(c.put) == {"/api/backend/sessions/job_old/files/report.pdf",
                          "/api/backend/sessions/job_explained_later/files/report.pdf"}
    assert not (tmp_path / "busy.lock.info").exists()                      # the machine lock was released
