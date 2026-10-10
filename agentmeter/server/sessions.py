"""Sessions, Compare and Leaderboard (Phase 46) — read-only views over benchmark jobs.

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product.
A "session" is one benchmark job (the prepare step is part of it, never shown on its
own). Nothing here is stored: every view is computed from the jobs store and each
finished job's session_results.json (persistence moves to a control plane later).

  session_summary(job, result)   one row for Home / Sessions
  compare(a, b)                  side-by-side efficiency metrics + the like-for-like check
                                 (same prepared-set hash? same GPU? same settings?)
  leaderboard(rows)              models ranked ONLY within a (prepared-set hash, GPU) group
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

STATUS = {"done": "Done", "queued": "Running", "running": "Running", "failed": "Failed",
          "interrupted": "Failed"}

# (key, label, unit, lower_is_better) — every efficiency metric a session reports
METRICS = [
    ("mean_latency_s", "Mean latency per flow", "s", True),
    ("median_latency_s", "Median latency per flow", "s", True),
    ("p95_latency_s", "p95 latency per flow", "s", True),
    ("p99_latency_s", "p99 latency per flow", "s", True),
    ("mean_peak_vram_mb", "Working VRAM, mean (excludes model weights)", "MB", True),
    ("max_peak_vram_mb", "Working VRAM, highest (excludes model weights)", "MB", True),
    ("total_peak_mb", "Total peak VRAM (weights + working)", "MB", True),
    ("weights_mb", "Model weights after load", "MB", True),
    ("mean_tokens_per_flow", "Tokens per flow", "tokens", True),
    ("mean_input_tokens_per_flow", "Input tokens per flow", "tokens", True),
    ("mean_output_tokens_per_flow", "Output tokens per flow", "tokens", True),
    ("handoff_share_of_total", "Agentic overhead (hand-off share)", "share", True),
    ("throughput_flows_per_s", "Throughput", "flows/s", False),
    ("cost_per_1k_flows_usd", "Cost per 1,000 flows", "USD", True),
    ("wh_per_flow", "Energy per flow", "Wh", True),
    ("wh_per_1k_flows", "Energy per 1,000 flows", "Wh", True),
    ("wh_per_flow_net_idle", "Energy per flow, net of idle", "Wh", True),
]
LEADERBOARD_SORTS = {"latency": "mean_latency_s", "vram": "mean_peak_vram_mb", "total_vram": "total_peak_mb",
                     "tokens": "mean_tokens_per_flow", "cost": "cost_per_1k_flows_usd", "energy": "wh_per_flow"}


def identity(job: dict[str, Any], res: Optional[dict[str, Any]]) -> dict[str, Any]:
    """session_identity from the results, or computed from the run dir for a session
    that predates Phase 46 (same functions, so the hashes agree)."""
    from ..session.analyses import PREPARED_SET_HASH_VERSION, identity_block, prepared_set_sha256
    run_dir = Path(job.get("run_dir") or "")
    if res and res.get("session_identity"):
        ident = res["session_identity"]
        if ident.get("prepared_set_hash_version") != PREPARED_SET_HASH_VERSION:
            # a session from before hash v2: recompute from its flows when they are still on disk
            # (the stored results file is never rewritten); otherwise keep the old hash, marked
            v2 = prepared_set_sha256(run_dir) if run_dir.name else None
            ident = {**ident, "prepared_set_sha256": v2 or ident.get("prepared_set_sha256"),
                     "prepared_set_hash_version": PREPARED_SET_HASH_VERSION if v2 else 1}
        return ident
    cfg = {}
    try:
        cfg = yaml.safe_load((run_dir / "session_config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        pass
    env = (res or {}).get("environment") or job.get("environment") or {}
    return identity_block(run_dir, cfg, env) if run_dir.name else {}


def model_metrics(res: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every efficiency metric per model, read from the stored results (nothing recomputed)."""
    out: dict[str, dict[str, Any]] = {}
    agents = {m["model"]: m for m in ((res.get("agents") or {}).get("per_model") or [])}
    lat = {m["model"]: m for m in ((res.get("latency_distribution") or {}).get("per_model") or [])}
    cost = {m["model"]: m for m in ((res.get("cost_energy") or {}).get("per_model") or [])}
    mem = {m["model"]: m for m in ((res.get("memory") or {}).get("per_model") or [])}
    for pm in res.get("per_model") or []:
        m = pm["model"]
        eff = pm.get("efficiency") or {}
        e2e = eff.get("end_to_end") or {}
        peaks = [a.get("max_peak_vram_mb") for a in (agents.get(m) or {}).get("agents", [])
                 if a.get("max_peak_vram_mb") is not None]
        row = {"n_flows": eff.get("n_flows"),
               **{k: e2e.get(k) for k in ("mean_latency_s", "median_latency_s", "p95_latency_s",
                                          "mean_peak_vram_mb", "mean_tokens_per_flow",
                                          "mean_input_tokens_per_flow", "mean_output_tokens_per_flow",
                                          "throughput_flows_per_s")},
               "p99_latency_s": (lat.get(m) or {}).get("p99"),
               "max_peak_vram_mb": max(peaks) if peaks else None,
               "handoff_share_of_total": ((agents.get(m) or {}).get("overhead") or {}).get("handoff_share_of_total"),
               "cost_per_1k_flows_usd": (cost.get(m) or {}).get("cost_per_1k_flows_usd"),
               "wh_per_flow": (cost.get(m) or {}).get("wh_per_flow"),
               "wh_per_1k_flows": (cost.get(m) or {}).get("wh_per_1k_flows"),
               "wh_per_flow_net_idle": (cost.get(m) or {}).get("wh_per_flow_net_idle"),
               "total_peak_mb": (mem.get(m) or {}).get("total_peak_mb"),
               "weights_mb": (mem.get(m) or {}).get("weights_mb"),
               "accuracy": (pm.get("accuracy") or {}).get("accuracy") if pm.get("accuracy") else None}
        out[m] = row
    return out


def _verdict(res: dict[str, Any]) -> dict[str, Any]:
    rel = res.get("relative_comparison")
    if rel:
        ev = rel.get("efficiency_verdict") or {}
        return {"kind": ev.get("verdict"), "model": ev.get("model"), "text": rel.get("verdict")}
    return {"kind": "single_model", "model": None,
            "text": (res.get("comparison") or {}).get("statement") or "Single-model run"}


def session_summary(job: dict[str, Any], res: Optional[dict[str, Any]] = None,
                    prepare_job: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    s = (res or {}).get("session") or {}
    env = (res or {}).get("environment") or job.get("environment") or {}
    status = STATUS.get(job.get("status"), job.get("status"))
    demo = (env.get("provider") or job.get("effective_provider")) in ("mock",)
    if status == "Done" and demo:
        status = "Demo"
    run_name = job.get("run_name") or ""
    src = s.get("source_type") or (run_name.split("_runs/")[0] if "_runs/" in run_name else None)
    if src is None and prepare_job:
        fn = str((prepare_job.get("source") or {}).get("filename") or prepare_job.get("run_name") or "")
        src = "pcap" if fn.lower().endswith((".pcap", ".pcapng", ".cap")) else "csv" if fn else None
    ident = identity(job, res) if res else {}
    out = {
        "session_id": job["job_id"], "job_id": job["job_id"], "status": status, "raw_status": job.get("status"),
        "demo": demo, "created_at": job.get("created_at"), "finished_at": job.get("finished_at"),
        "models": job.get("models") or [], "run_name": run_name,
        "input_type": (src or "").upper() or None,
        "labelled": s.get("accuracy_available") if res else (job.get("evaluation_mode") == "accuracy_available"
                                                             if job.get("evaluation_mode") else None),
        "n_flows": s.get("n_flows") or job.get("n_flows"),
        "gpu": ("Demo (no GPU)" if demo else (env.get("gpu_name") or "GPU")),
        "message": job.get("message"), "error": job.get("error"),
        "waiting_for_prepare": job.get("after_prepare") if not job.get("run_dir") else None,
    }
    if res:
        v = _verdict(res)
        out.update(verdict=v, more_efficient=(v["model"] if v["kind"] == "more_efficient"
                                              else {"mixed": "mixed", "tie": "tie"}.get(v["kind"], "—")),
                   metrics=model_metrics(res),
                   prepared_set_sha256=ident.get("prepared_set_sha256"),
                   settings_fingerprint=ident.get("settings_fingerprint"))
    return out


def cp_summary(job: dict[str, Any], res: dict[str, Any]) -> dict[str, Any]:
    """The compact per-session record the control plane stores (Phase 47): session_summary +
    identity + the decision helper's facts + the SAW weights used. Compare, Leaderboard and the
    decision helper run on this alone — here (the reference) and in the Worker (a port with
    parity tests: worker/test/parity.test.ts)."""
    from ..session.constraints import facts_for
    out = session_summary(job, res)
    ident = identity(job, res) or {}
    p8 = res.get("phase8") or {}
    out.update(identity=ident, labelled=bool((res.get("session") or {}).get("accuracy_available")),
               provider=ident.get("provider") or ((res.get("environment") or {}).get("provider")),
               facts=facts_for(res),
               weights_used={"weights": p8.get("weights"), "basis": (res.get("notes") or {}).get("saw_mode"),
                             "note": "the weights the stored SAW scores were computed with; presets re-rank "
                                     "in the view only and never change a stored score"},
               owner_username=job.get("user", {}).get("uname") if isinstance(job.get("user"), dict) else None)
    return out


# ---------------------------------------------------------------------------
# Compare (on summaries)
# ---------------------------------------------------------------------------
def _canon(v: Any) -> str:
    import json
    return json.dumps(v, sort_keys=True, default=str)


def validity_s(sa: dict, sb: dict) -> dict[str, Any]:
    ia, ib = sa.get("identity") or {}, sb.get("identity") or {}
    checks = []
    ha, hb = ia.get("prepared_set_sha256"), ib.get("prepared_set_sha256")
    checks.append({"check": "prepared_set", "label": "Same prepared set (flows hash)",
                   "same": bool(ha and ha == hb), "a": (ha or "unknown")[:12], "b": (hb or "unknown")[:12]})
    ga, gb = ia.get("gpu_label"), ib.get("gpu_label")
    checks.append({"check": "gpu", "label": "Same GPU", "same": bool(ga and ga == gb), "a": ga, "b": gb})
    xa, xb = ia.get("settings") or {}, ib.get("settings") or {}
    diff = sorted(k for k in set(xa) | set(xb) if _canon(xa.get(k)) != _canon(xb.get(k)))
    checks.append({"check": "settings", "label": "Same settings (provider, quantisation, generation, "
                                                 "agents, token caps, classes)",
                   "same": not diff and bool(xa), "a": ia.get("settings_fingerprint"),
                   "b": ib.get("settings_fingerprint"), "differs": diff})
    ok = all(c["same"] for c in checks)
    return {"like_for_like": ok, "label": "like-for-like" if ok else "not like-for-like",
            "differences": [c["label"] for c in checks if not c["same"]], "checks": checks}


def compare_s(sa: dict, sb: dict) -> dict[str, Any]:
    va = validity_s(sa, sb)
    ma, mb = sa.get("metrics") or {}, sb.get("metrics") or {}
    cols = ([{"session": sa["session_id"], "side": "A", "model": m} for m in ma]
            + [{"session": sb["session_id"], "side": "B", "model": m} for m in mb])
    rows = []
    for key, label, unit, lower in METRICS:
        vals = [ma[c["model"]].get(key) if c["side"] == "A" else mb[c["model"]].get(key) for c in cols]
        present = [v for v in vals if v is not None]
        best = (min(present) if lower else max(present)) if len(present) > 1 else None
        rows.append({"metric": key, "label": label, "unit": unit, "lower_is_better": lower, "values": vals,
                     "best_index": [i for i, v in enumerate(vals) if best is not None and v == best]})
    if sa.get("labelled") and sb.get("labelled"):
        rows.append({"metric": "accuracy", "label": "Accuracy (context only)", "unit": "share",
                     "lower_is_better": False, "context_only": True,
                     "values": [(ma if c["side"] == "A" else mb)[c["model"]].get("accuracy") for c in cols],
                     "best_index": []})
    strip = lambda s: {k: v for k, v in s.items() if k not in ("identity", "facts")}   # noqa: E731
    return {"a": strip(sa), "b": strip(sb), "validity": va, "columns": cols, "rows": rows,
            "note": ("Like-for-like: same flows, same GPU, same settings — differences are the models'."
                     if va["like_for_like"] else
                     "NOT like-for-like: " + "; ".join(va["differences"]) + " differ, so differences between "
                     "the sessions are not only the models'. Read side by side, not as a ranking.")}


def validity(a_job: dict, a: dict, b_job: dict, b: dict) -> dict[str, Any]:
    return validity_s(cp_summary(a_job, a), cp_summary(b_job, b))


def compare(a_job: dict, a: dict, b_job: dict, b: dict) -> dict[str, Any]:
    return compare_s(cp_summary(a_job, a), cp_summary(b_job, b))


# ---------------------------------------------------------------------------
# Leaderboard (on summaries)
# ---------------------------------------------------------------------------
def _wmean(pairs: list[tuple[Optional[float], int]]) -> Optional[float]:
    ps = [(v, n) for v, n in pairs if v is not None and n]
    tot = sum(n for _, n in ps)
    return sum(v * n for v, n in ps) / tot if tot else None


def leaderboard_s(summaries: list[dict], sort: str = "latency") -> dict[str, Any]:
    """Rows are ranked only inside their (prepared-set hash, GPU) group; a group whose
    sessions used different settings is flagged."""
    key = LEADERBOARD_SORTS.get(sort, LEADERBOARD_SORTS["latency"])
    groups: dict[tuple, dict] = {}
    for s in summaries:
        ident = s.get("identity") or {}
        gk = (ident.get("prepared_set_sha256") or f"unknown:{s['session_id']}", ident.get("gpu_label") or "?")
        g = groups.setdefault(gk, {"prepared_set_sha256": gk[0], "gpu": gk[1], "sessions": [], "settings": [],
                                   "input_type": s.get("input_type") or "",
                                   "n_flows_per_session": s.get("n_flows"),
                                   "demo": ident.get("provider") == "mock", "models": {}})
        g["sessions"].append(s["session_id"])
        if ident.get("settings_fingerprint") not in g["settings"]:
            g["settings"].append(ident.get("settings_fingerprint"))
        for m, row in (s.get("metrics") or {}).items():
            g["models"].setdefault(m, []).append((s["session_id"], row))
    out = []
    for g in groups.values():
        rows = []
        for m, items in g["models"].items():
            n = [r.get("n_flows") or 0 for _, r in items]
            agg = {k: _wmean([(r.get(k), r.get("n_flows") or 0) for _, r in items]) for k in LEADERBOARD_SORTS.values()}
            rows.append({"model": m, "n_sessions": len(items), "n_flows": sum(n),
                         "sessions": [sid for sid, _ in items], **agg})
        ranked = [r for r in rows if r.get(key) is not None]
        ranked.sort(key=lambda r: r[key])
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
        rows = ranked + [dict(r, rank=None) for r in rows if r.get(key) is None]
        out.append({"prepared_set_sha256": g["prepared_set_sha256"], "gpu": g["gpu"], "demo": g["demo"],
                    "input_type": g["input_type"], "n_flows_per_session": g["n_flows_per_session"],
                    "n_sessions": len(g["sessions"]), "mixed_settings": len(g["settings"]) > 1,
                    "rows": rows})
    out.sort(key=lambda g: (g["demo"], -g["n_sessions"]))
    return {"sort": sort, "sort_metric": key, "sorts": LEADERBOARD_SORTS, "groups": out,
            "rule": "Models are ranked only against others measured on the same prepared set (flows hash) "
                    "and the same GPU; values are flow-weighted means over that group's sessions. Lower is "
                    "better for every sort key."}


def leaderboard(entries: list[tuple[dict, dict]], sort: str = "latency") -> dict[str, Any]:
    """entries: (job, result) of finished sessions."""
    return leaderboard_s([cp_summary(j, r) for j, r in entries], sort)


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------
def _benchmark_jobs(mgr) -> list[dict]:
    return [j for j in reversed(mgr._all()) if (j.get("kind") or "benchmark") == "benchmark"]


def _result(mgr, job: dict) -> Optional[dict]:
    if job.get("status") != "done":
        return None
    try:
        return mgr.result(job["job_id"])
    except Exception:  # noqa: BLE001 — a missing/broken results file shows as no data
        return None


def register_sessions(app, get_manager) -> None:
    """GET /api/sessions, /api/sessions/<id>, /api/compare?a=&b=, /api/leaderboard?sort=.
    Read-only; with a control plane they need a read token (access.py) — and the app reads
    persisted sessions from the control plane instead (they survive the GPU being off)."""
    from flask import jsonify, request

    from .jobs import JobError

    def fail(e: JobError):
        return jsonify({"error": str(e), "code": e.code}), e.status

    @app.route("/api/sessions", methods=["GET"])
    def sessions_list():
        mgr = get_manager()
        status = (request.args.get("status") or "").strip().lower()
        itype = (request.args.get("input") or "").strip().upper()
        try:
            limit = max(1, min(200, int(request.args.get("limit", 50))))
        except ValueError:
            limit = 50
        rows = []
        for j in _benchmark_jobs(mgr):
            prep = None
            if j.get("after_prepare") and not j.get("run_dir"):
                try:
                    prep = mgr._load(j["after_prepare"])
                except JobError:
                    prep = None
            r = session_summary(j, _result(mgr, j), prep)
            if status and r["status"].lower() != status:
                continue
            if itype and (r["input_type"] or "") != itype:
                continue
            rows.append(r)
            if len(rows) >= limit:
                break
        return jsonify({"sessions": rows, "statuses": ["Done", "Demo", "Running", "Failed"]})

    @app.route("/api/sessions/<job_id>", methods=["GET"])
    def session_one(job_id):
        mgr = get_manager()
        try:
            job = mgr.get(job_id)
        except JobError as e:
            return fail(e)
        if (job.get("kind") or "benchmark") != "benchmark":
            return fail(JobError(f"{job_id} is not a benchmark session", "not_found", 404))
        out = session_summary(job, _result(mgr, job))
        out["identity"] = identity(job, _result(mgr, job)) if job.get("status") == "done" else None
        return jsonify(out)

    @app.route("/api/compare", methods=["GET"])
    def sessions_compare():
        mgr = get_manager()
        a, b = request.args.get("a", ""), request.args.get("b", "")
        if not a or not b or a == b:
            return fail(JobError("pick two different sessions (?a=<id>&b=<id>)", "bad_request", 400))
        try:
            ja, jb = mgr.get(a), mgr.get(b)
            ra, rb = mgr.result(a), mgr.result(b)
        except JobError as e:
            return fail(e)
        if "per_model" not in ra or "per_model" not in rb:
            return fail(JobError("both must be finished benchmark sessions", "bad_request", 400))
        return jsonify(compare(ja, ra, jb, rb))

    @app.route("/api/leaderboard", methods=["GET"])
    def sessions_leaderboard():
        mgr = get_manager()
        sort = request.args.get("sort", "latency")
        if sort not in LEADERBOARD_SORTS:
            return fail(JobError(f"sort must be one of {sorted(LEADERBOARD_SORTS)}", "bad_request", 400))
        entries = []
        for j in _benchmark_jobs(mgr):
            r = _result(mgr, j)
            if r and "per_model" in r:
                entries.append((j, r))
        return jsonify(leaderboard(entries, sort))
