"""HTTP API for benchmark jobs (Phase 40). Same-origin routes on the existing
Flask app (scripts/pull_eval_server.py); the server's Basic Auth gate, when
enabled, covers them like every other route.

  POST /api/jobs                {run, models[<=2], provider?, base_config?} -> 202 {job}
  GET  /api/jobs                ?limit=N  -> recent jobs, newest first
  GET  /api/jobs/<id>           status + live progress + result_ready / result_url
  GET  /api/jobs/<id>/result    session_results.json once done (409 until then)
  POST /api/jobs/<id>/resume    re-queue an interrupted/failed job -> 202 {job}

Errors are {"error": <message>, "code": <stable reason>} with codes such as
not_found, run_missing, too_many_models, bad_models, no_gpu, not_ready,
not_resumable, bad_request.
"""
from __future__ import annotations

from typing import Callable

from .jobs import JobError, JobManager


def register_jobs(app, get_manager: Callable[[], JobManager]) -> None:
    from flask import jsonify, request

    def fail(e: JobError):
        return jsonify({"error": str(e), "code": e.code}), e.status

    def with_links(job: dict) -> dict:
        job = dict(job)
        job.pop("error_trace", None)               # server-side detail; the message is enough
        job["status_url"] = f"/api/jobs/{job['job_id']}"
        job["result_url"] = f"/api/jobs/{job['job_id']}/result" if job.get("result_ready") else None
        return job

    @app.route("/api/jobs", methods=["GET", "POST"])
    def jobs_collection():
        mgr = get_manager()
        if request.method == "GET":
            try:
                limit = int(request.args.get("limit", 20))
            except ValueError:
                limit = 20
            return jsonify({"jobs": mgr.list_jobs(limit=limit), "running": mgr.running_job()})
        body = request.get_json(silent=True) or {}
        run = body.get("run") or body.get("run_name")
        if run and str(run).startswith("/"):
            return fail(JobError("give the run by name (csv_runs/<name> or pcap_runs/<name>), "
                                 "not a filesystem path", "bad_request", 400))
        models = body.get("models")
        if not isinstance(models, list):
            return fail(JobError("'models' must be a list of 1 or 2 model ids", "bad_models", 400))
        try:
            job = mgr.create_job(run, models, provider=body.get("provider"),
                                 base_config=body.get("base_config"))
        except JobError as e:
            return fail(e)
        return jsonify(with_links(job)), 202

    @app.route("/api/jobs/<job_id>", methods=["GET"])
    def job_status(job_id):
        try:
            return jsonify(with_links(get_manager().get(job_id)))
        except JobError as e:
            return fail(e)

    @app.route("/api/jobs/<job_id>/result", methods=["GET"])
    def job_result(job_id):
        try:
            return jsonify(get_manager().result(job_id))
        except JobError as e:
            return fail(e)

    @app.route("/api/jobs/<job_id>/resume", methods=["POST"])
    def job_resume(job_id):
        try:
            return jsonify(with_links(get_manager().resume(job_id))), 202
        except JobError as e:
            return fail(e)
