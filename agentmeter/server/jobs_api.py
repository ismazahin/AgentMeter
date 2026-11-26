"""HTTP API for benchmark jobs (Phase 40). Same-origin routes on the existing
Flask app (scripts/serve.py); access.py (run tokens, CORS, rate limits)
covers them like every other route.

  POST /api/jobs                {run | after_prepare, models[<=4], provider?, base_config?, ids?}
                                -> 202 {job}   (the removed load_test, repeat_check and pipeline
                                fields are ignored, so an old client or schedule still runs)
  GET  /api/jobs                ?limit=N  -> recent jobs, newest first
  GET  /api/jobs/<id>           status + live progress + result_ready / result_url
  GET  /api/jobs/<id>/result    session_results.json once done (409 until then)
  GET  /api/jobs/<id>/report.pdf  PDF benchmark report once done (409 until then)
  GET  /api/jobs/<id>/constraints ?max_mean_latency_s=..  decision helper (Phase 46)
  GET  /api/jobs/<id>/telemetry ?since=N   live telemetry: events after the first N, status,
                                progress, and the GPU right now (utilisation, memory, power)
  POST /api/jobs/<id>/resume    re-queue an interrupted/failed job -> 202 {job}
  POST /api/jobs/<id>/stop      stop a queued or running job -> 202 {job} (final; never scored)

Errors are {"error": <message>, "code": <stable reason>} with codes such as
not_found, run_missing, too_many_models, bad_models, no_gpu, not_ready,
not_resumable, bad_pipeline, bad_request.
"""
from __future__ import annotations

import os

from typing import Callable, Optional

from . import runtoken
from .jobs import JobError, JobManager



def flows_path(job: dict, res: dict) -> Optional["Path"]:
    """flows_<run_id>.json of a finished session, or None (sessions from before it existed)."""
    from pathlib import Path as _Path
    run_id = (res.get("session") or {}).get("run_id")
    p = _Path(job.get("run_dir") or "") / f"flows_{run_id}.json"
    return p if run_id and p.is_file() else None

def report_pdf_bytes(mgr: JobManager, job_id: str, decision: Optional[dict] = None) -> bytes:
    """Phase 42: a PDF report built verbatim from the finished job's results (also uploaded to
    the control plane, Phase 47). Raises JobError (not_ready / not_found / pdf_unavailable)."""
    import json as _json
    from pathlib import Path as _Path

    res = mgr.result(job_id)                          # 409 not_ready / 404 not_found
    job = mgr.get(job_id)
    try:
        from ..session.pdf_report import build_report_pdf
    except ImportError as e:                          # pragma: no cover - dependency missing
        raise JobError(f"PDF reports need reportlab on the server (pip install reportlab): {e}",
                       "pdf_unavailable", 501) from e
    run_dir = _Path(job["run_dir"])

    def read(name):
        p = run_dir / name
        return _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    kw = dict(job=job, input_meta=read("input.json"), audit=read("selection_audit.json"),
              prepared=read("manifest.json").get("prepared_set"), decision=decision)
    explanation = read("explanation.json")
    # The full report (charts + every session tab, WeasyPrint). Any failure falls back to the plain
    # ReportLab report below, so a finished run always has a PDF.
    # AGENTMETER_PDF_LAYOUT=plain always serves the plain report.
    import os as _os
    if _os.environ.get("AGENTMETER_PDF_LAYOUT", "").strip().lower() != "plain":
        try:
            from ..session.report_html import build_report_pdf as build_full
            return build_full(res, explanation=explanation, **kw)
        except Exception as e:                        # noqa: BLE001 - missing dependency or a bad block
            import logging
            logging.getLogger(__name__).warning("full PDF report failed for %s, using the plain report: %s", job_id, e)
    try:
        return build_report_pdf(res, **kw)
    except ImportError as e:
        raise JobError(f"PDF reports need reportlab on the server (pip install reportlab): {e}",
                       "pdf_unavailable", 501) from e


def check_benchmark_token(mgr: JobManager, tok: Optional[dict], run: Optional[str], after: Optional[str]) -> None:
    """Control-plane mode: the request must be the one the token was issued for, and a free
    follow-up benchmark (prepare_jti) must follow the same user's own prepare / import."""
    if not tok:
        return
    if (tok.get("run") or None) != (run or None) or (tok.get("after_prepare") or None) != (after or None):
        raise JobError("the run token was issued for another prepared set / prepare job", "run_token_mismatch", 403)
    pj = tok.get("prepare_jti")
    if not pj:
        return
    if after:
        try:
            src = mgr._load(after)
        except JobError:
            src = {}
        owner = {"user": src.get("user"), "grant_jti": src.get("grant_jti")}
    else:
        owner = mgr.set_grant(str(run)) or {}
    if owner.get("grant_jti") != pj or (owner.get("user") or {}).get("sub") != tok.get("sub"):
        raise JobError("this follow-up benchmark does not belong to your own data preparation",
                       "run_token_mismatch", 403)


def won_t_fit(exp: dict, real: bool) -> set:
    """(model, precision) pairs that will not fit this GPU's memory, from each model's parameter
    count in its local config (unknown: assumed to fit, the benchmark reports it if not)."""
    if not real or exp["precisions"] == ["4bit"]:
        return set()
    try:
        import torch
        total = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3 if torch.cuda.is_available() else None
    except Exception:  # noqa: BLE001
        total = None
    if not total:
        return set()
    from ..session.experiment import vram_need_gb
    from ..session.model_design import read_design
    out = set()
    for m in exp["models"]:
        try:
            pb = (read_design(m) or {}).get("params_b")
        except Exception:  # noqa: BLE001
            pb = None
        for p in exp["precisions"]:
            need = vram_need_gb(pb, p)
            if need is not None and need > total * 0.95:
                out.add((m, p))
    return out


def register_jobs(app, get_manager: Callable[[], JobManager], provider: Optional[str] = None,
                  base_config: Optional[str] = None,
                  environment: Optional[Callable[[], dict]] = None) -> None:
    """provider/base_config: when given (Phase E), the SERVER decides them — a client
    can not run a mock job on a real-GPU server or vice versa. environment() is
    stamped into each new job (GPU, driver, CUDA, provider)."""
    from flask import jsonify, request

    def fail(e: JobError):
        return jsonify({"error": str(e), "code": e.code}), e.status

    def readable(job_id: str) -> dict:
        """The job, when the caller may read it (a user's own, an admin any); otherwise 404 as if
        it did not exist (runtoken.may_read; no control plane: always)."""
        job = get_manager().get(job_id)
        if not runtoken.may_read(job):
            raise JobError(f"no such job: {job_id}", "not_found", 404)
        return job

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
            mine = mgr.running_job()
            if mine and not runtoken.may_read(mgr.get(mine)):
                mine = None                               # someone else's run: not named here
            return jsonify({"jobs": mgr.list_jobs(limit=limit, visible=runtoken.may_read), "running": mine})
        body = request.get_json(silent=True) or {}
        run = body.get("run") or body.get("run_name")
        if run and str(run).startswith("/"):
            return fail(JobError("give the run by name (csv_runs/<name> or pcap_runs/<name>), "
                                 "not a filesystem path", "bad_request", 400))
        after = body.get("after_prepare")                 # Phase 46: queue behind a prepare job
        if after is not None and not isinstance(after, str):
            return fail(JobError("'after_prepare' must be a prepare job id", "bad_request", 400))
        ids = body.get("ids")                             # the IDS gate before the LLM triage (agentmeter/ids)
        try:
            from ..ids.stage import normalize_ids_option
            ids = normalize_ids_option(ids)
        except Exception as e:                            # IdsError: a bad option
            return fail(JobError(str(e), "bad_request", 400))
        if ids is not None:
            from ..ids.model import model_available
            if not model_available():
                return fail(JobError("this server has no IDS model: train one with scripts/train_ids.py "
                                     "(models/ids/ids_rf.*) or set AGENTMETER_IDS_MODEL", "no_ids_model", 409))
        models = body.get("models")
        if not isinstance(models, list):
            return fail(JobError("'models' must be a list of 1 to 4 model ids", "bad_models", 400))
        try:
            asked = body.get("provider")
            if provider is not None:
                if asked not in (None, "", provider):
                    return fail(JobError(
                        f"this server runs provider {provider!r} only ({'real models on its GPU' if provider == 'hf' else 'mock demo'}); "
                        f"{asked!r} was requested", "provider_mismatch", 400))
                asked = provider
            if provider == "hf":                      # real GPU: study models + the model list only
                from .model_catalog import job_model_problem
                bad = job_model_problem(models)
                if bad:
                    raise JobError(*bad)
            check_benchmark_token(mgr, runtoken.current(), run, after)
            # the model sizes and pipeline sizes to compare (an experiment): one job per
            # combination, all on this upload; a plain benchmark is 4-bit, 4 agents
            from ..session.experiment import ExperimentError, plan as plan_experiment
            try:
                exp = plan_experiment(models, body.get("precisions") or body.get("precision"), body.get("agents"))
            except ExperimentError as e:
                raise JobError(str(e), e.code, 400) from e
            engine = (os.environ.get("AGENTMETER_ENGINE") or "").strip().lower()
            if engine == "vllm" and exp["precisions"] != ["4bit"]:
                raise JobError("this GPU server runs the vLLM engine, which is 4-bit only; 8-bit and 16-bit "
                               "need the transformers engine", "precision_unsupported", 400)
            skipped = won_t_fit(exp, provider == "hf")
            specs = [dict(j, models=[m for m in j["models"] if (m, j["precision"]) not in skipped]) for j in exp["jobs"]]
            specs = [j for j in specs if j["models"]]
            if not specs:
                raise JobError("no model fits this GPU at the chosen sizes", "wont_fit", 400)
            cfg = base_config if provider is not None and base_config else body.get("base_config")
            env = environment() if environment else None
            info = None
            if exp["is_experiment"]:
                info = {"id": exp["id"], "precisions": exp["precisions"], "agents": exp["agents"],
                        "models": exp["models"], "total": len(specs),
                        "skipped": [{"model": m, "precision": p, "reason": "does not fit this GPU's memory"}
                                    for m, p in sorted(skipped)]}
            jobs = []
            for i, spec in enumerate(specs):
                meta = {**runtoken.job_meta(), "precision": spec["precision"], "agents": spec["agents"]}
                if info:
                    meta["experiment"] = {**info, "index": i + 1}
                jobs.append(mgr.create_job(run, spec["models"], provider=asked, base_config=cfg, environment=env,
                                           after_prepare=after, meta=meta, ids=ids))
        except JobError as e:
            return fail(e)
        out = with_links(jobs[0])
        if info:
            out["experiment_jobs"] = [j["job_id"] for j in jobs]
        return jsonify(out), 202

    @app.route("/api/experiments/<exp_id>", methods=["GET"])
    def experiment_status(exp_id):
        """Every run of an experiment (model size x number of agents), in run order."""
        jobs = [j for j in get_manager().experiment(exp_id) if runtoken.may_read(j)]
        if not jobs:
            return fail(JobError(f"no experiment {exp_id}", "not_found", 404))
        return jsonify({"experiment": jobs[0].get("experiment"), "jobs": [with_links(j) for j in jobs]})

    @app.route("/api/jobs/<job_id>", methods=["GET"])
    def job_status(job_id):
        try:
            return jsonify(with_links(get_manager().get(job_id)))
        except JobError as e:
            return fail(e)

    @app.route("/api/jobs/<job_id>/result", methods=["GET"])
    def job_result(job_id):
        try:
            readable(job_id)
            return jsonify(get_manager().result(job_id))
        except JobError as e:
            return fail(e)

    @app.route("/api/jobs/<job_id>/flows", methods=["GET"])
    def job_flows(job_id):
        """The flow explorer's data (flows_<run_id>.json beside the results), once done."""
        from flask import send_file
        mgr = get_manager()
        try:
            readable(job_id)
            res = mgr.result(job_id)
            p = flows_path(mgr.get(job_id), res)
            if p is None:
                raise JobError("this session has no per-flow record (it predates the flow explorer)", "not_found", 404)
        except JobError as e:
            return fail(e)
        return send_file(p, mimetype="application/json", max_age=0)

    @app.route("/api/jobs/<job_id>/explanation", methods=["GET"])
    def job_explanation(job_id):
        """The session's explanations written by the explainer (explanation.json), once done.
        404 {"status": "none"} for a session without one (older, or still running)."""
        from pathlib import Path as _Path
        from ..session.explain import read as read_explanation
        mgr = get_manager()
        try:
            readable(job_id)
            mgr.result(job_id)
            data = read_explanation(_Path(mgr.get(job_id).get("run_dir") or ""))
        except JobError as e:
            return fail(e)
        if data is None:
            return jsonify({"status": "none", "error": "this session has no written explanation"}), 404
        return jsonify(data)

    @app.route("/api/jobs/<job_id>/report.pdf", methods=["GET"])
    def job_report_pdf(job_id):
        """Phase 42: a PDF report built verbatim from the finished job's results."""
        from flask import Response

        mgr = get_manager()
        decision = None
        try:
            readable(job_id)
            if any(request.args.get(k) for k in request.args):       # Phase 46: the user's limits
                from ..session.constraints import ConstraintError, evaluate_constraints
                try:
                    decision = evaluate_constraints(mgr.result(job_id), request.args)
                except ConstraintError as e:
                    raise JobError(str(e), "bad_request", 400) from e
            pdf = report_pdf_bytes(mgr, job_id, decision)
        except JobError as e:
            return fail(e)
        return Response(pdf, mimetype="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="agentmeter_report_{job_id}.pdf"'})

    @app.route("/api/jobs/<job_id>/constraints", methods=["GET"])
    def job_constraints(job_id):
        """Phase 46 decision helper: evaluate the finished session's measured models against
        the limits in the query string (configs/constraint_rules.yaml). Read-only; changes
        no score, rank or verdict."""
        from ..session.constraints import ConstraintError, evaluate_constraints
        try:
            readable(job_id)
            res = get_manager().result(job_id)
        except JobError as e:
            return fail(e)
        if "per_model" not in res:
            return fail(JobError("not a benchmark session", "bad_request", 400))
        try:
            return jsonify(evaluate_constraints(res, request.args))
        except ConstraintError as e:
            return fail(JobError(str(e), "bad_request", 400))

    @app.route("/api/jobs/<job_id>/telemetry", methods=["GET"])
    def job_telemetry(job_id):
        from .. import telemetry
        mgr = get_manager()
        try:
            job = readable(job_id)
        except JobError as e:
            return fail(e)
        try:
            since = max(0, int(request.args.get("since", 0)))
        except ValueError:
            since = 0
        events, nxt = telemetry.read(mgr.events_path(job_id), since)
        live = job.get("status") in ("queued", "running")
        gpu = telemetry.gpu_sample() if live and job.get("effective_provider") != "mock" else None
        return jsonify({"job_id": job_id, "status": job.get("status"), "message": job.get("message"),
                        "events": events, "next": nxt, "progress": job.get("progress"),
                        "model_download": job.get("model_download"), "models": job.get("models"),
                        "gpu": gpu, "gpu_reason": None if gpu else ("demo mode: no GPU" if job.get("effective_provider") == "mock"
                                                                  else "no live GPU reading" if live else "finished")})

    @app.route("/api/gpu", methods=["GET"])
    def gpu_status():
        """The GPU & Queue page: this server's one GPU right now, what runs on it, what waits and
        what finished last. A live reading, never part of a measurement; no GPU (demo mode, no
        driver) gives no reading rather than an invented one."""
        from .. import telemetry
        mgr = get_manager()
        env = environment() if environment else {}

        def brief(j: dict) -> dict:
            full = mgr.get(j["job_id"])
            if not runtoken.may_read(full):
                # another user's run: that the GPU is busy and where it is in the queue, nothing more
                p = full.get("progress") or {}
                return {"job_id": None, "kind": full.get("kind") or "benchmark", "status": full.get("status"), "other": True,
                        "queue_position": full.get("queue_position"), "started_at": full.get("started_at"),
                        "created_at": full.get("created_at"), "finished_at": full.get("finished_at"),
                        "progress": {"percent": p.get("percent")}, "owner": None, "models": None, "run_name": None}
            u = full.get("user") if isinstance(full.get("user"), dict) else {}
            out = {k: full.get(k) for k in ("job_id", "kind", "status", "models", "run_name", "prepared", "created_at",
                                            "started_at", "finished_at", "message", "schedule_id", "queue_position",
                                            "n_flows")}
            out["kind"] = full.get("kind") or "benchmark"
            out["owner"] = u.get("uname")
            p = full.get("progress") or {}
            out["progress"] = {k: p.get(k) for k in ("completed", "total", "percent", "current_model", "phase")}
            return out

        jobs = mgr.list_jobs(limit=500)
        running = [brief(j) for j in jobs if j["status"] == "running"]
        queued = sorted((brief(j) for j in jobs if j["status"] == "queued"), key=lambda x: x.get("queue_position") or 0)
        recent = [brief(j) for j in mgr.list_jobs(limit=500, visible=runtoken.may_read)
                  if j["status"] not in ("running", "queued")][:8]          # finished: only the caller's own
        mock = provider == "mock" or env.get("provider") == "mock"
        gpu = None if mock else telemetry.gpu_sample()
        return jsonify({
            "provider": provider or env.get("provider"), "engine": env.get("engine"),
            "gpu": gpu, "gpu_reason": None if gpu else ("demo mode: no GPU" if mock else "no GPU reading on this server"),
            "device": {"name": env.get("gpu_name"), "vram_total_mb": env.get("gpu_vram_total_mb"),
                       "driver_version": env.get("driver_version"), "cuda": env.get("cuda_runtime_version")},
            "running": running, "queued": queued, "recent": recent,
        })

    @app.route("/api/jobs/<job_id>/stop", methods=["POST"])
    def job_stop(job_id):
        mgr = get_manager()
        tok = runtoken.current()
        try:
            job = mgr.get(job_id)
            if tok is not None:                    # control-plane mode: the owner or an admin
                owner = (job.get("user") or {}).get("sub") if isinstance(job.get("user"), dict) else None
                if not tok.get("admin") and (not owner or owner != tok.get("sub")):
                    raise JobError("only the person who started this benchmark (or an admin) can stop it",
                                   "forbidden", 403)
            by = (tok or {}).get("uname")
            return jsonify(with_links(mgr.stop(job_id, by=by))), 202
        except JobError as e:
            return fail(e)

    @app.route("/api/jobs/<job_id>/resume", methods=["POST"])
    def job_resume(job_id):
        try:
            return jsonify(with_links(get_manager().resume(job_id))), 202
        except JobError as e:
            return fail(e)
