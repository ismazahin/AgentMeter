"""Test helper: run Prepare through the real async API (/api/prepare + the job layer)
and return (status, body) the way a synchronous call would: (201, summary) when the
prepare job finishes, (status, {"error", "code"}) when the request or the job fails."""
from __future__ import annotations

import time


def prepare_sync(client, data, content_type="multipart/form-data", timeout=120, headers=None):
    r = client.post("/api/prepare", data=data, content_type=content_type, headers=headers or {})
    if r.status_code != 202:
        return r.status_code, r.get_json()
    jid = r.get_json()["job_id"]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = client.get(f"/api/jobs/{jid}").get_json()
        if job["status"] == "done":
            return 201, client.get(f"/api/jobs/{jid}/result").get_json()["summary"]
        if job["status"] in ("failed", "interrupted"):
            return job.get("error_status") or 500, {"error": job.get("error"), "code": job.get("error_code")}
        time.sleep(0.05)
    raise TimeoutError(f"prepare job {jid} did not finish in {timeout}s")


class Result:
    """Response-like (status_code, get_json()) for tests written against a sync call."""

    def __init__(self, status: int, body: dict):
        self.status_code, self._body = status, body

    def get_json(self):
        return self._body


def prepare(client, data, **kw) -> Result:
    return Result(*prepare_sync(client, data, **kw))
