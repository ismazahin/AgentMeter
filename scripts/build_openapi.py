"""Build the AgentMeter API description (OpenAPI 3.1) from one catalogue of every endpoint.

    python scripts/build_openapi.py          # writes web/docs/openapi-*.json (shown at /docs/)

Three APIs, one document:
  * the control plane    (Supabase Edge Function, supabase/functions/agentmeter/src/app.ts)
  * the GPU backend       (Flask, scripts/serve.py + agentmeter/server/*)
  * the cloud preparation (cloudprep/app.py)
Each operation is tagged with its server (x-server) and a topic. tests/test_openapi.py fails when a
route exists in the code but not here (or the other way round), and when the written file is stale.
API_VERSION follows semantic versioning: a new endpoint or field raises the minor number; a removed
or changed one raises the major number (and the old one is kept for a while, marked deprecated).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from agentmeter.server.apiversion import API_VERSION  # noqa: E402

OUT = REPO / "web" / "docs"

CP, BE, PREP = "control-plane", "gpu-backend", "cloud-prep"
# who may call: the security requirement of each operation
NONE, USER, ADMIN = [], [{"userToken": []}], [{"userToken": ["admin"]}]
BACKEND_SIGNED, PREP_SIGNED, CP_SIGNED = [{"backendSignature": []}], [{"prepSignature": []}], [{"controlPlaneSignature": []}]


def RUN(kind: str):
    return [{"runToken": [kind]}]


def p(name, where="query", desc="", required=False, schema=None):
    return {"name": name, "in": where, "required": required or where == "path", "description": desc, "schema": schema or {"type": "string"}}


ID = p("id", "path", "Session id (job_YYYYMMDD_HHMMSS_<32 hex>)")
UID = p("id", "path", "User id (32 hex)")
PREP_ID = p("id", "path", "Cloud preparation job id (32 hex)")
JOB = p("job_id", "path", "Job id")
FILE = p("name", "path", "File name: manifest.json | features.csv | labels.csv (and flows.json, report.pdf where stored)")

# (server, method, path, tag, summary, security, parameters, body, responses, description)
E: list[tuple] = []


def op(server, method, path, tag, summary, security, params=(), body=None, ok="200", errors=(), desc=""):
    E.append((server, method, path, tag, summary, security, list(params), body, ok, list(errors), desc))


# ------------------------------------------------------------------ control plane: health
op(CP, "get", "/api/health", "Accounts", "Is the control plane up (and its api_version)", NONE)
# ------------------------------------------------------------------ control plane: accounts
op(CP, "post", "/api/auth/login", "Accounts", "Log in", NONE, body={"username": "string", "password": "string", "remember": "boolean"},
   errors=["401 bad_credentials", "429 too_many_attempts"],
   desc="Returns a 15-minute access token and a refresh token (rotated on every use). Failed logins are throttled per user and per address.")
op(CP, "post", "/api/auth/refresh", "Accounts", "Exchange a refresh token for new tokens", NONE, body={"refresh_token": "string"},
   errors=["401 session_expired", "401 session_revoked"], desc="The old refresh token stops working; presenting it again revokes the session (reuse detection).")
op(CP, "post", "/api/auth/logout", "Accounts", "Log out (revoke this session)", USER)
op(CP, "get", "/api/me", "Accounts", "Who am I", USER)
op(CP, "get", "/api/me/devices", "Accounts", "My logged-in devices", USER)
op(CP, "delete", "/api/me/devices/{id}", "Accounts", "Log a device out", USER, [p("id", "path", "Device (session) id")])
op(CP, "post", "/api/me/password", "Accounts", "Change my password", USER, body={"current_password": "string", "new_password": "string"},
   errors=["403 bad_credentials"], desc="Other devices are logged out. Recorded in the audit log.")
op(CP, "get", "/api/settings", "Settings", "My settings", USER)
op(CP, "put", "/api/settings", "Settings", "Change my settings", USER, body={"theme": "string", "default_models": "array", "default_flows": "integer",
                                                                            "notify_on": "string", "telegram_chat_id": "string", "alert_on_drift": "boolean", "weight_presets": "array"})
op(CP, "post", "/api/settings/telegram-test", "Settings", "Send a Telegram test message", USER)
# ------------------------------------------------------------------ control plane: admin
op(CP, "get", "/api/admin/users", "Admin", "List users", ADMIN)
op(CP, "post", "/api/admin/users", "Admin", "Create a user", ADMIN, body={"username": "string", "password": "string", "role": "admin | user"}, ok="201",
   desc="The new user must change the password at the first login. Audit: user.create.")
op(CP, "patch", "/api/admin/users/{id}", "Admin", "Change a user (role, disabled, password reset)", ADMIN, [UID], body={"role": "string", "disabled": "boolean", "password": "string"},
   errors=["409 last_admin", "409 self"], desc="Audit: user.update (never the password).")
op(CP, "delete", "/api/admin/users/{id}", "Admin", "Delete a disabled user", ADMIN, [UID], errors=["409 not_disabled"], desc="Their sessions stay. Audit: user.delete.")
op(CP, "get", "/api/admin/credentials", "Admin", "Which credentials are set (never their values)", ADMIN)
op(CP, "get", "/api/admin/audit", "Admin", "The audit log, newest first", ADMIN, [p("limit", desc="1–500 (100)", schema={"type": "integer"}), p("before", desc="id: older entries (paging)", schema={"type": "integer"})])
op(CP, "get", "/api/admin/limits", "Admin", "System limits", ADMIN)
op(CP, "put", "/api/admin/limits", "Admin", "Change system limits", ADMIN, body={"sessions_per_hour": "integer", "max_queued_sessions": "integer", "session_idle_minutes": "integer"},
   desc="Pushed to the GPU backend with the next heartbeat. Audit: limits.update.")
op(CP, "get", "/api/admin/models", "Admin", "The model catalogue state (enabled, added)", ADMIN)
op(CP, "put", "/api/admin/models", "Admin", "Enable, disable, add or remove models", ADMIN, body={"set": "object", "add": "array", "remove": "array"},
   desc="Added models start disabled; pre-quantised repositories are refused. Audit: models.update.")
op(CP, "get", "/api/admin/ids-model", "Admin", "The stored IDS model (version, threshold, hold-out metrics, file sizes)", ADMIN)
op(CP, "post", "/api/admin/ids-model/upload-url", "Admin", "A single-use URL to upload one IDS model file to Storage", ADMIN,
   body={"name": "ids_rf.joblib | ids_rf.json | ids_rf.hashes.npy", "bytes": "integer (≤ 50 MB)"})
op(CP, "post", "/api/admin/ids-model/commit", "Admin", "Check the uploaded IDS model and make it the one new GPUs download", ADMIN,
   errors=["409 not_uploaded", "409 sha_mismatch", "400 bad_model"], desc="The .joblib must match the sha256 in its .json. Audit: ids_model.commit.")
op(CP, "delete", "/api/admin/ids-model", "Admin", "Remove the stored IDS model (new GPUs offer the demo IDS only)", ADMIN, desc="Audit: ids_model.delete.")
# ------------------------------------------------------------------ control plane: runs, backend status
op(CP, "get", "/api/backend", "GPU backend", "Where the GPU backend is and whether it is online", USER)
op(CP, "post", "/api/runs/authorize", "GPU backend", "Get a run token for the GPU backend", USER,
   body={"kind": "prepare | import | benchmark | resume | read | files | stop | verify | schedule", "run": "string", "after_prepare": "string", "job_id": "string", "set": "string", "model": "string"},
   errors=["429 rate_limited", "503 backend_offline"],
   desc="Signed, 5 minutes, single use for actions; bound to the user, the action and the job. Benchmarks count against the hourly limit (a wizard session counts once).")
# ------------------------------------------------------------------ control plane: cloud preparation
op(CP, "post", "/api/prep/jobs", "Data preparation", "Start a cloud preparation (returns an upload URL or uses a link)", USER,
   body={"filename": "string", "size_bytes": "integer", "url": "string", "max_flows": "integer", "other_attack": "boolean"}, ok="201")
op(CP, "post", "/api/prep/wake", "Data preparation", "Wake the cloud preparation service (cold start)", USER)
op(CP, "get", "/api/prep/jobs/{id}", "Data preparation", "A cloud preparation's state and progress", USER, [PREP_ID])
op(CP, "post", "/api/prep/jobs/{id}/start", "Data preparation", "Start preparing (after the upload)", USER, [PREP_ID])
op(CP, "get", "/api/prep/jobs/{id}/files/{name}", "Data preparation", "A prepared file", USER, [PREP_ID, FILE], errors=["403 owner_only"],
   desc="manifest.json, features.csv, labels.csv; a traffic analysis adds ids_scores.csv, ids_pool.json and ids_flows_identity.csv (source/destination addresses: the job's owner only, not admins).")
op(CP, "post", "/api/prep/register", "Data preparation", "The preparation service says where it is (or that it is stopping)", PREP_SIGNED,
   body={"url": "string", "version": "string", "stopping": "boolean"},
   desc="Sent by scripts/prep_local.py on the owner's PC (the Start button). It is online while its heartbeats are under 3 minutes old; jobs then go to this address instead of CLOUD_PREP_URL.")
op(CP, "post", "/api/prep/heartbeat", "Data preparation", "The preparation service is still there (every minute)", PREP_SIGNED, body={"url": "string", "version": "string"})
op(CP, "post", "/api/prep/jobs/{id}/events", "Data preparation", "Progress, done or failed (from the preparation service)", PREP_SIGNED, [PREP_ID],
   body={"event": "object", "done": "object", "failed": "object"})
# ------------------------------------------------------------------ control plane: sessions and analysis
op(CP, "get", "/api/sessions", "Sessions", "List sessions, newest first (paged)", USER,
   [p("limit", desc="1–200 (50)", schema={"type": "integer"}), p("offset", desc="skip this many (paging; next_offset says where the next page starts, null at the end)", schema={"type": "integer"}),
    p("status", desc="Done | Demo | Running | Failed | Interrupted | Stopped"), p("input", desc="CSV | PCAP"), p("q", desc="every word in models, set, notes, tags, owner or id"), p("tag", desc="exact tag")])
op(CP, "get", "/api/sessions/{id}", "Sessions", "One session's summary", USER, [ID])
op(CP, "patch", "/api/sessions/{id}", "Sessions", "Name, notes and tags (owner or admin)", USER, [ID], body={"title": "string", "notes": "string", "tags": "array"}, errors=["403 forbidden"],
   desc="title: up to 80 characters on one line; \"\" removes the name.")
op(CP, "get", "/api/datasets", "Data sets", "Names and deletions of data sets", USER,
   desc="Data sets are built from the sessions and the GPU server's prepared sets; this lists only what users changed: {datasets: [{key, set, label, deleted_at}]}.")
op(CP, "post", "/api/datasets/rename", "Data sets", "Rename a data set (owner or admin)", USER, body={"key": "string", "set": "string", "label": "string"},
   errors=["403 forbidden"], desc="key: the set's fingerprint (sha256) or name:<kind>/<name>. Owner: whoever ran the first session on it. Audit: dataset.rename.")
op(CP, "post", "/api/datasets/delete", "Data sets", "Remove a data set from Data sets (owner or admin)", USER, body={"key": "string", "set": "string"},
   errors=["403 forbidden"], desc="Sessions that ran on it keep their results. The web app first deletes the set's files on the GPU server (POST /api/prepared/{kind}/{name}/delete). A traffic analysis's stored files (the owner-only address file included) are removed too. Audit: dataset.delete.")
op(CP, "get", "/api/analyses", "Analyse traffic", "Your traffic analyses (an admin: everyone's)", USER,
   desc="Finished cloud preparations whose service scored the whole candidate pool with the IDS (no GPU): {analyses: [{job_id, owner, mine, filename, url, set, key, ids: {threshold, n_flows_in, n_flagged, n_selected, labelled, model}, files, finished_at}]}. key: the data set's fingerprint (or name:<kind>/<name>).")
op(CP, "delete", "/api/sessions/{id}", "Sessions", "Delete a session (owner or admin)", USER, [ID], errors=["403 forbidden"], desc="Results and stored files are removed for everyone. Audit: session.delete.")
op(CP, "get", "/api/sessions/{id}/results", "Sessions", "The full stored results (session_results.json)", USER, [ID])
op(CP, "get", "/api/sessions/{id}/constraints", "Sessions", "Fit check of the results against limits (query: the limits)", USER, [ID])
op(CP, "get", "/api/sessions/{id}/files/{name}", "Sessions", "A file stored with the session", USER, [ID, FILE])
op(CP, "get", "/api/compare", "Compare", "Two sessions side by side (older links)", USER, [p("a", required=True), p("b", required=True)])
op(CP, "get", "/api/compare/candidates", "Compare", "Sessions on the same setup as one session", USER, [p("session", required=True)])
op(CP, "get", "/api/compare/merged", "Compare", "Every model of 2–20 sessions on one setup", USER, [p("sessions", desc="comma-separated ids", required=True)],
   errors=["400 not_same_setup"], desc="All sessions must share the prepared set (hash), the GPU and the settings fingerprint.")
op(CP, "get", "/api/leaderboard", "Compare", "Rank models per setup, or count wins overall", USER,
   [p("view", desc="per_setup | overall"), p("sort", desc="per_setup: latency | vram | tokens | efficiency | …"), p("by", desc="overall: saw"), p("demo", desc="1: include demo sessions")])
# ------------------------------------------------------------------ control plane: called by the GPU backend (signed)
for m, path, s, b in [("post", "/api/backend/register", "Register the GPU backend (URL, GPU, version)", {"url": "string", "mode": "string", "gpu": "object", "version": "string"}),
                      ("get", "/api/backend/model-state", "Read the model catalogue state", None),
                      ("get", "/api/backend/ids-model", "The stored IDS model's metadata and short-lived download URLs (404 no_ids_model)", None),
                      ("post", "/api/backend/models/verified", "Report a model's 4-bit verification", {"model": "string", "result": "object"}),
                      ("post", "/api/backend/heartbeat", "Heartbeat: still online; job states; credentials set", {"url": "string", "jobs": "array", "creds": "object"}),
                      ("post", "/api/backend/jobs", "A job was created on the backend", {"job_id": "string", "kind": "string", "status": "string", "jti": "string"})]:
    op(CP, m, path, "Backend ↔ control plane", s, BACKEND_SIGNED, body=b)
op(CP, "post", "/api/backend/jobs/{job_id}/status", "Backend ↔ control plane", "A job's status changed", BACKEND_SIGNED, [JOB], body={"status": "string", "message": "string"})
for m, path, s in [("put", "/api/backend/sessions/{id}/results", "Store a session's results"),
                   ("put", "/api/backend/sessions/{id}/summary", "Store a session's summary (what lists, Compare and Leaderboard read)"),
                   ("post", "/api/backend/sessions/{id}/upload-url", "A signed URL to upload a large file straight to Storage"),
                   ("post", "/api/backend/sessions/{id}/commit", "Record files uploaded with upload-url"),
                   ("post", "/api/backend/sessions/{id}/identity", "Set a session's prepared-set hash")]:
    op(CP, m, path, "Backend ↔ control plane", s, BACKEND_SIGNED, [ID])
op(CP, "put", "/api/backend/sessions/{id}/files/{name}", "Backend ↔ control plane", "Store a small file with a session", BACKEND_SIGNED, [ID, FILE])
op(CP, "get", "/api/backend/sessions", "Backend ↔ control plane", "Sessions the control plane holds (backfill)", BACKEND_SIGNED)
op(CP, "get", "/api/backend/sessions/{id}/files/{name}", "Backend ↔ control plane", "Read a stored file (restore a prepared set on a new rental)", BACKEND_SIGNED, [ID, FILE])

# ------------------------------------------------------------------ GPU backend
RT = "Authorization: Bearer <run token from POST /api/runs/authorize>. Without a control plane (one-user mode) no token is needed."
op(BE, "get", "/health", "GPU backend", "Is the backend up: provider, GPU, queue, api_version, the IDS model it offers", NONE)
op(BE, "get", "/api/service/config", "New benchmark", "Models offered, limits, demo mode", NONE)
op(BE, "post", "/api/prepare", "Data preparation", "Prepare an uploaded file or a link into a set of flows", RUN("prepare"),
   body={"file": "binary (multipart)", "url": "string", "max_flows": "integer", "other_attack": "boolean"}, ok="202", desc=RT)
op(BE, "post", "/api/prepared/import", "Data preparation", "Import a set prepared elsewhere (browser or cloud)", RUN("import"),
   body={"manifest": "binary", "features": "binary", "labels": "binary"}, ok="201", desc="The prepared-set hash is recomputed and must match. " + RT)
op(BE, "get", "/api/prepared/{kind}/{name}", "Data sets", "A prepared set's summary (rows, class counts, files)", RUN("read"),
   [p("kind", "path", "csv_runs | pcap_runs"), p("name", "path", "set name")])
op(BE, "get", "/api/prepared/{kind}/{name}/{fname}", "Data sets", "A prepared file", RUN("files"),
   [p("kind", "path", "csv_runs | pcap_runs"), p("name", "path", "set name"), p("fname", "path", "features.csv | labels.csv | manifest.json")])
op(BE, "post", "/api/prepared/{kind}/{name}/delete", "Data sets", "Delete a prepared set's files from this server (owner or admin)", RUN("delete_set"),
   [p("kind", "path", "csv_runs | pcap_runs"), p("name", "path", "set name")], errors=["403 forbidden", "404 not_found", "409 set_in_use"],
   desc="Refused while a queued or running benchmark uses the set. Sessions that ran on it keep their results. " + RT)
op(BE, "get", "/api/jobs", "Benchmarks", "Jobs on this server, newest first", RUN("read"), [p("limit", schema={"type": "integer"})])
op(BE, "post", "/api/jobs", "Benchmarks", "Start a benchmark", RUN("benchmark"),
   body={"run": "string", "after_prepare": "string", "models": "array (1–4)", "pipeline": "array", "load_test": "boolean", "repeat_check": "boolean", "ids": "boolean | object {threshold: number}"}, ok="202", desc=RT)
op(BE, "get", "/api/jobs/{job_id}", "Benchmarks", "A job's status and progress", NONE, [JOB], desc="Open for 128-bit job ids (the id is the secret); older short ids need a read token.")
op(BE, "get", "/api/jobs/{job_id}/result", "Benchmarks", "A finished job's results", RUN("read"), [JOB], errors=["409 not_ready"])
op(BE, "get", "/api/jobs/{job_id}/report.pdf", "Benchmarks", "The PDF report", RUN("read"), [JOB])
op(BE, "get", "/api/jobs/{job_id}/constraints", "Benchmarks", "Fit check against limits", RUN("read"), [JOB])
op(BE, "get", "/api/jobs/{job_id}/flows", "Benchmarks", "Every flow's answers per agent (flow explorer)", RUN("read"), [JOB])
op(BE, "get", "/api/jobs/{job_id}/telemetry", "Benchmarks", "Live progress events and a GPU reading", RUN("read"), [JOB, p("since", schema={"type": "integer"})])
op(BE, "post", "/api/jobs/{job_id}/resume", "Benchmarks", "Resume a failed or interrupted benchmark", RUN("resume"), [JOB])
op(BE, "post", "/api/jobs/{job_id}/stop", "Benchmarks", "Stop a running benchmark (owner or admin)", RUN("stop"), [JOB])
op(BE, "get", "/api/gpu", "GPU & Queue", "The GPU now, the running job, the queue, the last finished jobs", RUN("read"),
   desc="A live reading (NVML); none in demo mode. The page polls it every 3 s.")
op(BE, "get", "/api/logs/recent", "GPU & Queue", "Warnings, errors and failed runs of the last hours (admins)", RUN("read"), [p("hours", desc="1–168 (24)", schema={"type": "number"})],
   errors=["403 forbidden"], desc="Kept in the server's memory since it started. With a control plane, admins only.")
op(BE, "get", "/api/sessions", "Sessions", "Sessions on this server (one-user mode), paged", RUN("read"), [p("limit", schema={"type": "integer"}), p("offset", schema={"type": "integer"}), p("status"), p("input"), p("q"), p("tag")])
op(BE, "get", "/api/sessions/{job_id}", "Sessions", "One session (one-user mode)", RUN("read"), [JOB])
op(BE, "patch", "/api/sessions/{job_id}", "Sessions", "Name, notes and tags (one-user mode)", NONE, [JOB], body={"title": "string", "notes": "string", "tags": "array"})
op(BE, "get", "/api/datasets", "Data sets", "Names and deletions of data sets (one-user mode)", NONE)
op(BE, "post", "/api/datasets/rename", "Data sets", "Rename a data set (one-user mode)", NONE, body={"key": "string", "set": "string", "label": "string"})
op(BE, "post", "/api/datasets/delete", "Data sets", "Remove a data set from Data sets (one-user mode)", NONE, body={"key": "string", "set": "string"})
op(BE, "get", "/api/compare", "Compare", "Two sessions side by side (one-user mode)", RUN("read"), [p("a", required=True), p("b", required=True)])
op(BE, "get", "/api/compare/candidates", "Compare", "Sessions on the same setup (one-user mode)", RUN("read"), [p("session", required=True)])
op(BE, "get", "/api/compare/merged", "Compare", "Every model of 2–20 sessions on one setup (one-user mode)", RUN("read"), [p("sessions", required=True)])
op(BE, "get", "/api/leaderboard", "Compare", "Leaderboard (one-user mode)", RUN("read"), [p("view"), p("sort"), p("by"), p("demo")])
op(BE, "get", "/api/models/catalog", "New benchmark", "Models offered on this server (study + extra), with download sizes", RUN("read"))
op(BE, "post", "/api/models/verify", "Admin", "4-bit test-load of a catalogue model", RUN("verify"), body={"model": "string"}, ok="202")
op(BE, "get", "/api/model-metadata", "New benchmark", "Hugging Face metadata of a model (licence, size)", NONE, [p("model", required=True)])
op(BE, "get", "/api/schedules", "Repeatability", "Scheduled re-runs", RUN("read"))
op(BE, "post", "/api/schedules", "Repeatability", "Repeat a finished session on a schedule", RUN("schedule"), body={"job_id": "string", "every_hours": "integer", "runs": "integer"}, ok="201")
op(BE, "post", "/api/schedules/{schedule_id}/cancel", "Repeatability", "Cancel a schedule", RUN("schedule"), [p("schedule_id", "path", "sch_<12 hex>")], body={"job_id": "string"})
op(BE, "get", "/api/notify-status", "Settings", "Local Telegram notifier status (one-user mode)", NONE)
op(BE, "post", "/api/notify-test", "Settings", "Local Telegram test (one-user mode)", NONE)
op(BE, "post", "/api/notify-check", "Settings", "Look for new results to notify about (one-user mode)", NONE)

# ------------------------------------------------------------------ cloud preparation
op(PREP, "get", "/health", "Data preparation", "Is the preparation service up", NONE)
op(PREP, "post", "/prepare", "Data preparation", "Prepare one input (a signed job from the control plane)", CP_SIGNED,
   body={"job_id": "string", "filename": "string", "source": "object", "max_flows": "integer", "limits": "object", "outputs": "object", "report_url": "string"}, ok="202",
   desc="Reads the input from a single-use URL, runs the server's own preparation code (identical flows and hash), uploads the files to single-use URLs and reports progress, signed.")

SERVERS = {CP: {"url": "https://{project}.supabase.co/functions/v1/agentmeter", "description": "Control plane (Supabase Edge Function): accounts, sessions, Compare, Leaderboard",
                "variables": {"project": {"default": "your-project-ref"}}},
           BE: {"url": "{backend}", "description": "GPU backend (Flask on the rented GPU): preparation, benchmarks, live GPU and queue",
                "variables": {"backend": {"default": "https://your-gpu-backend.example"}}},
           PREP: {"url": "{prep}", "description": "Cloud preparation service (Cloud Run / Space)", "variables": {"prep": {"default": "https://your-cloudprep.example"}}}}
TYPES = {"string": {"type": "string"}, "integer": {"type": "integer"}, "number": {"type": "number"}, "boolean": {"type": "boolean"}, "object": {"type": "object"}}


def schema_of(t: str) -> dict:
    base = t.split(" ")[0]
    if base == "array":
        return {"type": "array", "description": t}
    if base == "binary":
        return {"type": "string", "format": "binary", "description": t}
    s = dict(TYPES.get(base, {"type": "string"}))
    if t != base:
        s["description"] = t
    return s


def build(server: str) -> dict:
    """One OpenAPI document per service (the same path can exist on two services)."""
    paths: dict = {}
    ops = [x for x in E if x[0] == server]
    for _srv, method, path, tag, summary, sec, params, body, ok, errors, desc in ops:
        opid = (method + "_" + path).replace("/", "_").replace("{", "").replace("}", "").replace(".", "_").replace("-", "_").strip("_")
        o = {"operationId": opid, "tags": [tag], "summary": summary, "security": sec, "responses": {ok: {"description": "OK"}}}
        if desc:
            o["description"] = desc
        if params:
            o["parameters"] = params
        if body:
            o["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {k: schema_of(v) for k, v in body.items()}}}}}
        codes = {"401": "Not logged in / bad or missing token or signature"} if sec else {}
        if sec == ADMIN:
            codes["403"] = "Not an admin"
        for e in errors:
            c, code = e.split(" ", 1)
            codes[c] = (codes[c] + " · " if c in codes else "") + code
        for c, d in codes.items():
            o["responses"][c] = {"description": d, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
        entry = paths.setdefault(path, {})
        assert method not in entry, f"{server} {method} {path} described twice"
        entry[method] = o
    sv = SERVERS[server]
    return {
        "openapi": "3.1.0",
        "info": {"title": "AgentMeter API — " + TITLES[server], "version": API_VERSION, "description": INTRO[server],
                 "license": {"name": "See the repository"}},
        "servers": [{"url": sv["url"], "description": sv["description"], "variables": sv["variables"]}],
        "tags": [{"name": t} for t in dict.fromkeys(x[3] for x in ops)],
        "paths": dict(sorted(paths.items())),
        "components": {
            "securitySchemes": {
                "userToken": {"type": "http", "scheme": "bearer", "description": "Access token from POST /api/auth/login on the control plane (15 minutes). Scope admin: the user's role must be admin."},
                "runToken": {"type": "http", "scheme": "bearer", "description": "Run token from POST /api/runs/authorize on the control plane (5 minutes; single use for actions); its scope is the token's kind. Not needed in one-user mode (no control plane)."},
                "backendSignature": {"type": "apiKey", "in": "header", "name": "X-AM-Signature",
                                     "description": "hex HMAC-SHA256(BACKEND_SECRET, method\\npath\\nX-AM-Timestamp\\nsha256(body)); X-AM-Timestamp (unix seconds) within 120 s."},
                "prepSignature": {"type": "apiKey", "in": "header", "name": "X-AM-Signature", "description": "The same signature with CLOUD_PREP_SECRET (preparation service → control plane)."},
                "controlPlaneSignature": {"type": "apiKey", "in": "header", "name": "X-AM-Signature", "description": "The same signature with CLOUD_PREP_SECRET (control plane → preparation service)."},
            },
            "schemas": {"Error": {"type": "object", "properties": {"error": {"type": "string", "description": "A sentence for people"},
                                                                   "code": {"type": "string", "description": "A stable code for programs"}}}},
        },
    }


TITLES = {CP: "control plane", BE: "GPU backend", PREP: "cloud preparation"}
INTRO = {CP: "Accounts, stored sessions, Compare and the Leaderboard; it hands out run tokens for the GPU backend and receives the backend's signed calls. "
             "Runs as a Supabase Edge Function.",
         BE: "Data preparation, benchmarks, live progress and the live GPU, on the rented GPU. With a control plane every call needs a run token; "
             "in one-user mode (no control plane) the same routes are open and also serve sessions, Compare and the Leaderboard.",
         PREP: "Prepares an uploaded file or a link into a set of flows while the GPU is busy, with the same code as the GPU backend (identical flows and hash)."}
FILES = {CP: "openapi-control-plane.json", BE: "openapi-gpu-backend.json", PREP: "openapi-cloud-prep.json"}


def exported() -> dict[str, str]:
    return {FILES[k]: json.dumps(build(k), indent=1, ensure_ascii=False) + "\n" for k in FILES}


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in exported().items():
        (OUT / name).write_text(text, encoding="utf-8")
    print(f"wrote {len(FILES)} documents to {OUT.relative_to(REPO)} ({len(E)} operations, API {API_VERSION})")
