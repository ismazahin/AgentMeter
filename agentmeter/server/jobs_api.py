"""HTTP API for benchmark jobs (Phase 40). Same-origin routes on the existing
Flask app (scripts/serve.py); access.py (passcode, CORS, rate limits)
covers them like every other route.

  POST /api/jobs                {run, models[<=2], provider?, base_config?} -> 202 {job}
  GET  /api/jobs                ?limit=N  -> recent jobs, newest first
  GET  /api/jobs/<id>           status + live progress + result_ready / result_url
  GET  /api/jobs/<id>/result    session_results.json once done (409 until then)
  GET  /api/jobs/<id>/report.pdf  PDF benchmark report once done (409 until then)
  POST /api/jobs/<id>/resume    re-queue an interrupted/failed job -> 202 {job}

Errors are {"error": <message>, "code": <stable reason>} with codes such as
not_found, run_missing, too_many_models, bad_models, no_gpu, not_ready,
not_resumable, bad_request.
"""
from __future__ import annotations

from typing import Callable, Optional

from .jobs import JobError, JobManager


def register_jobs(app, get_manager: Callable[[], JobManager], provider: Optional[str] = None,
                  base_config: Optional[str] = None,
                  environment: Optional[Callable[[], dict]] = None) -> None:
    """provider/base_config: when given (Phase E), the SERVER decides them — a client
    can not run a mock job on a real-GPU server or vice versa. environment() is
    stamped into each new job (GPU, driver, CUDA, provider)."""
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
            asked = body.get("provider")
            if provider is not None:
                if asked not in (None, "", provider):
                    return fail(JobError(
                        f"this server runs provider {provider!r} only ({'real models on its GPU' if provider == 'hf' else 'mock demo'}); "
                        f"{asked!r} was requested", "provider_mismatch", 400))
                asked = provider
            job = mgr.create_job(run, models, provider=asked,
                                 base_config=base_config if provider is not None and base_config
                                 else body.get("base_config"),
                                 environment=environment() if environment else None)
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

    @app.route("/api/jobs/<job_id>/report.pdf", methods=["GET"])
    def job_report_pdf(job_id):
        """Phase 42: a PDF report built verbatim from the finished job's results."""
        import json as _json
        from pathlib import Path as _Path

        from flask import Response

        mgr = get_manager()
        try:
            res = mgr.result(job_id)                      # 409 not_ready / 404 not_found
            job = mgr.get(job_id)
        except JobError as e:
            return fail(e)
        try:
            from ..session.pdf_report import build_report_pdf
        except ImportError as e:                          # pragma: no cover - dependency missing
            return fail(JobError(f"PDF reports need reportlab on the server (pip install reportlab): {e}",
                                 "pdf_unavailable", 501))
        run_dir = _Path(job["run_dir"])

        def read(name):
            p = run_dir / name
            return _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        try:
            pdf = build_report_pdf(res, job=job, input_meta=read("input.json"),
                                   audit=read("selection_audit.json"),
                                   prepared=read("manifest.json").get("prepared_set"))
        except ImportError as e:
            return fail(JobError(f"PDF reports need reportlab on the server (pip install reportlab): {e}",
                                 "pdf_unavailable", 501))
        return Response(pdf, mimetype="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="agentmeter_report_{job_id}.pdf"'})

    @app.route("/api/jobs/<job_id>/resume", methods=["POST"])
    def job_resume(job_id):
        try:
            return jsonify(with_links(get_manager().resume(job_id))), 202
        except JobError as e:
            return fail(e)
