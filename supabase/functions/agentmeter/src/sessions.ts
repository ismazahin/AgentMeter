// Persistent sessions: ONE database row per session (never one row per flow). The backend writes
// them (job reports + results upload, HMAC-signed); every logged-in user reads them (5-person team);
// only the owner or an admin deletes. session_results.json is stored and served UNPARSED (the
// function's CPU budget is 2 s): in the row up to RESULTS_INLINE_MAX bytes, otherwise in Storage.
import { requireUser } from "./auth.ts";
import { mapJobStatus as STATUS_FROM_JOB, parseBody, verifyBackend } from "./backend.ts";
import { ConstraintError, Summary, compare, compareMany, comparable, evaluateFacts, leaderboard, leaderboardOverall, LEADERBOARD_SORTS, OVERALL_BY } from "./analytics.ts";
import { notifyFinal } from "./notify.ts";
import { Env, fail, intVar, json, nowIso, readJson } from "./util.ts";

const SESSION_ID = /^job_\d{8}_\d{6}_([0-9a-f]{6}|[0-9a-f]{32})$/;
export const FILES = ["report.pdf", "manifest.json", "features.csv", "labels.csv", "flows.json"] as const;
const CONTENT_TYPE: Record<string, string> = {
  "report.pdf": "application/pdf", "manifest.json": "application/json", "features.csv": "text/csv",
  "labels.csv": "text/csv", "session_results.json": "application/json", "flows.json": "application/json",
};

interface Row {
  id: string; owner_id: string | null; owner_username: string | null; status: string; created_at: string;
  finished_at: string | null; input_type: string | null; n_flows: number | null; gpu: string | null; provider: string | null;
  models_json: string | null; more_efficient: string | null; prepared_set: string | null; summary_json: string | null;
  results_json: string | null; results_key: string | null; results_bytes: number | null; files_json: string | null;
  weights_json: string | null; deleted_at: string | null; notes: string | null; tags_json: string | null;
}

const checkId = (id: string) => { if (!SESSION_ID.test(id)) fail(404, "not_found", "no such session"); return id; };

async function getRow(env: Env, id: string, withResults = false): Promise<Row> {
  const cols = withResults ? "*" : "id, owner_id, owner_username, status, created_at, finished_at, input_type, n_flows, gpu, provider, models_json, more_efficient, prepared_set, summary_json, results_key, results_bytes, files_json, weights_json, deleted_at, notes, tags_json";
  const r = await env.DB.prepare(`SELECT ${cols} FROM sessions WHERE id=?`).bind(checkId(id)).first<Row>();
  if (!r || r.deleted_at) fail(404, "not_found", "no such session");
  return r!;
}

/** The list/detail record: the stored summary (same shape as the backend's session_summary)
 *  with the row's live status and owner. A running session has no summary yet. */
function view(r: Row): Summary {
  const s: Summary = r.summary_json ? JSON.parse(r.summary_json) : {
    session_id: r.id, job_id: r.id, models: r.models_json ? JSON.parse(r.models_json) : [], created_at: r.created_at,
    input_type: r.input_type, n_flows: r.n_flows, gpu: r.gpu, demo: r.provider === "mock", run_name: r.prepared_set,
  };
  delete s.facts;
  return { ...s, status: r.status, owner: r.owner_username, has_results: !!(r.summary_json && (r.results_bytes || 0) > 0),
           files: r.files_json ? Object.keys(JSON.parse(r.files_json)) : [], finished_at: r.finished_at ?? s.finished_at ?? null,
           notes: r.notes ?? "", tags: r.tags_json ? JSON.parse(r.tags_json) : [] };
}

// notes and tags: limits shared with the backend (agentmeter/server/sessions.py clean_annotations)
const NOTES_MAX = 2000, TAGS_MAX = 10, TAG = /^[\p{L}\p{N}][\p{L}\p{N} _.\-]{0,29}$/u;
export function cleanAnnotations(body: Record<string, unknown>): { notes?: string; tags?: string[] } {
  const out: { notes?: string; tags?: string[] } = {};
  if (body.notes !== undefined) {
    if (typeof body.notes !== "string") fail(400, "bad_request", "notes must be text");
    const n = (body.notes as string).trim();
    if (n.length > NOTES_MAX) fail(400, "bad_request", `notes are limited to ${NOTES_MAX} characters`);
    out.notes = n;
  }
  if (body.tags !== undefined) {
    if (!Array.isArray(body.tags) || body.tags.some((t) => typeof t !== "string")) fail(400, "bad_request", "tags must be a list of words");
    const seen = new Set<string>(), tags: string[] = [];
    for (const raw of body.tags as string[]) {
      const t = raw.trim().replace(/\s+/g, " ");
      if (!t) continue;
      if (!TAG.test(t)) fail(400, "bad_request", `tag "${t.slice(0, 40)}": letters, digits, space, _ . - only, up to 30 characters`);
      if (!seen.has(t.toLowerCase())) { seen.add(t.toLowerCase()); tags.push(t); }
    }
    if (tags.length > TAGS_MAX) fail(400, "bad_request", `at most ${TAGS_MAX} tags`);
    out.tags = tags;
  }
  return out;
}

/** PATCH /api/sessions/:id {notes?, tags?} — the owner or an admin. */
export async function patchSession(req: Request, env: Env, id: string): Promise<Response> {
  const a = await requireUser(req, env);
  const r = await getRow(env, id);
  if (a.user.role !== "admin" && (!r.owner_id || r.owner_id !== a.user.id)) fail(403, "forbidden", "only the owner or an admin can edit a session's notes and tags");
  const c = cleanAnnotations(await readJson(req, 16 * 1024));
  await env.DB.prepare("UPDATE sessions SET notes=COALESCE(?, notes), tags_json=COALESCE(?, tags_json), updated_at=? WHERE id=?")
    .bind(c.notes ?? null, c.tags ? JSON.stringify(c.tags) : null, nowIso(), id).run();
  return json(view(await getRow(env, id)));
}

// --- users --------------------------------------------------------------------------------
export async function listSessions(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  const q = new URL(req.url).searchParams;
  const limit = Math.max(1, Math.min(200, intVar(q.get("limit") || "", 50)));
  const where = ["deleted_at IS NULL"], args: unknown[] = [];
  const st = (q.get("status") || "").trim();
  if (st) { where.push("LOWER(status)=LOWER(?)"); args.push(st); }
  const it = (q.get("input") || "").trim().toUpperCase();
  if (it) { where.push("input_type=?"); args.push(it); }
  // search: every word must appear in the models, prepared set, notes, tags, owner or session id
  const like = (w: string) => "%" + w.replace(/[\\%_]/g, (c) => "\\" + c) + "%";
  for (const w of (q.get("q") || "").trim().toLowerCase().split(/\s+/).filter(Boolean).slice(0, 8)) {
    where.push("(LOWER(COALESCE(models_json,'') || ' ' || COALESCE(prepared_set,'') || ' ' || COALESCE(notes,'') || ' ' || COALESCE(tags_json,'') || ' ' || COALESCE(owner_username,'') || ' ' || id) LIKE ? ESCAPE '\\')");
    args.push(like(w));
  }
  const tag = (q.get("tag") || "").trim().toLowerCase();
  if (tag) { where.push("LOWER(COALESCE(tags_json,'')) LIKE ? ESCAPE '\\'"); args.push(like(JSON.stringify(tag))); }
  const r = await env.DB.prepare(
    `SELECT id, owner_id, owner_username, status, created_at, finished_at, input_type, n_flows, gpu, provider, models_json, prepared_set, summary_json, results_bytes, files_json, notes, tags_json
     FROM sessions WHERE ${where.join(" AND ")} ORDER BY created_at DESC LIMIT ?`).bind(...args, limit).all<Row>();
  // can_edit: the owner or an admin may delete the session and edit its notes (the rows' buttons)
  const mine = (x: Row) => a.user.role === "admin" || (!!x.owner_id && x.owner_id === a.user.id);
  return json({ sessions: r.results.map((x) => ({ ...view(x), can_edit: mine(x) })), statuses: ["Done", "Demo", "Running", "Failed", "Interrupted", "Stopped"] });
}

export async function getSession(req: Request, env: Env, id: string): Promise<Response> {
  const a = await requireUser(req, env);
  const r = await getRow(env, id);
  const s = view(r);
  return json({ ...s, identity: r.summary_json ? JSON.parse(r.summary_json).identity ?? null : null,
                can_delete: a.user.role === "admin" || (!!r.owner_id && r.owner_id === a.user.id) });
}

export async function getResults(req: Request, env: Env, id: string): Promise<Response> {
  await requireUser(req, env);
  const r = await getRow(env, id, true);
  if (r.results_json) return new Response(r.results_json, { headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" } });
  if (r.results_key) {
    const o = await env.STORE.get(r.results_key);
    if (o) return new Response(o.body, { headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" } });
  }
  return fail(409, "not_ready", r.status === "Running" ? "the session is still running" : "no results stored for this session");
}

export async function getFile(req: Request, env: Env, id: string, name: string): Promise<Response> {
  await requireUser(req, env);
  if (!(FILES as readonly string[]).includes(name)) fail(404, "not_found", "no such file");
  const r = await getRow(env, id);
  const key = r.files_json ? JSON.parse(r.files_json)[name] : null;
  const o = key ? await env.STORE.get(key) : null;
  if (!o) fail(404, "not_found", `${name} is not stored for this session`);
  return new Response(o!.body, { headers: { "content-type": CONTENT_TYPE[name], "cache-control": "no-store",
    "content-disposition": `attachment; filename="${id}_${name}"` } });
}

export async function deleteSession(req: Request, env: Env, id: string): Promise<Response> {
  const a = await requireUser(req, env);
  const r = await getRow(env, id);
  if (a.user.role !== "admin" && (!r.owner_id || r.owner_id !== a.user.id)) fail(403, "forbidden", "only the owner or an admin can delete a session");
  const keys = [...Object.values(r.files_json ? JSON.parse(r.files_json) : {}), r.results_key].filter(Boolean) as string[];
  if (keys.length) await env.STORE.delete(keys);
  await env.DB.prepare("UPDATE sessions SET deleted_at=?, results_json=NULL, results_key=NULL, files_json=NULL, updated_at=? WHERE id=?")
    .bind(nowIso(), nowIso(), id).run();
  return json({ ok: true });
}

async function summaries(env: Env, ids?: string[]): Promise<Summary[]> {
  const base = "SELECT summary_json FROM sessions WHERE deleted_at IS NULL AND summary_json IS NOT NULL";
  const r = ids
    ? await env.DB.prepare(`${base} AND id IN (${ids.map(() => "?").join(",")})`).bind(...ids).all<{ summary_json: string }>()
    : await env.DB.prepare(`${base} ORDER BY created_at DESC`).all<{ summary_json: string }>();
  return r.results.map((x) => JSON.parse(x.summary_json));
}

export async function compareRoute(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env);
  const q = new URL(req.url).searchParams;
  const a = q.get("a") || "", b = q.get("b") || "";
  if (!a || !b || a === b) fail(400, "bad_request", "pick two different sessions (?a=<id>&b=<id>)");
  checkId(a); checkId(b);
  const ss = await summaries(env, [a, b]);
  const sa = ss.find((s) => s.session_id === a), sb = ss.find((s) => s.session_id === b);
  if (!sa || !sb) fail(400, "bad_request", "both must be finished sessions");
  return json(compare(sa!, sb!));
}

/** GET /api/compare/candidates?session=<id> — the finished sessions on the same setup. */
export async function compareCandidatesRoute(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env);
  const id = new URL(req.url).searchParams.get("session") || "";
  checkId(id);
  const all = await withSaw(env, await summaries(env));
  const target = all.find((s) => s.session_id === id);
  if (!target) fail(400, "bad_request", "pick a finished benchmark session");
  return json(comparable(target!, all));
}

/** GET /api/compare/merged?sessions=a,b,… — every model of those sessions side by side (same setup only). */
export async function compareMergedRoute(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env);
  const ids = (new URL(req.url).searchParams.get("sessions") || "").split(",").filter(Boolean);
  if (ids.length < 2 || new Set(ids).size !== ids.length || ids.length > 20) fail(400, "bad_request", "pick 2 to 20 different sessions (?sessions=a,b,…)");
  ids.forEach(checkId);
  const got = await withSaw(env, await summaries(env, ids));
  const byId = new Map(got.map((s) => [s.session_id, s]));
  if (ids.some((i) => !byId.has(i))) fail(400, "bad_request", "every session must be a finished benchmark session");
  const out: any = compareMany(ids.map((i) => byId.get(i)!));
  if (out.error) return json({ error: "These sessions do not share the same flows, GPU and settings.", code: "not_same_setup", differs: out.differs }, 400);
  return json(out);
}

/** Sessions stored before the summary carried the SAW score: copy it from their stored results (never recomputed). */
async function withSaw(env: Env, ss: Summary[]): Promise<Summary[]> {
  for (const s of ss) {
    const rows = Object.values((s.metrics || {}) as Record<string, any>);
    if (!rows.length || rows.every((r) => "saw_eff_composite_100" in r)) continue;
    const r = await getRow(env, s.session_id, true).catch(() => null);
    let res: any = null;
    try {
      if (r?.results_json) res = JSON.parse(r.results_json);
      else if (r?.results_key) { const o = await env.STORE.get(r.results_key); if (o) res = await o.json(); }
    } catch { res = null; }
    const saw = new Map(((res?.per_model || []) as any[]).map((p) => [p.model, p.saw || {}]));
    const eff = new Map(((res?.per_model || []) as any[]).map((p) => [p.model, p.saw_efficiency_only || {}]));
    for (const [m, row] of Object.entries(s.metrics as Record<string, any>)) {
      row.saw_composite_100 = saw.get(m)?.composite_100 ?? null;
      row.saw_tier = saw.get(m)?.tier ?? null;
      row.saw_eff_composite_100 = eff.get(m)?.composite_100 ?? null;
    }
  }
  return ss;
}

export async function leaderboardRoute(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env);
  const q = new URL(req.url).searchParams;
  const view = q.get("view") || "per_setup";
  // newest first — the same order the backend reference iterates its jobs in
  if (view === "overall") {
    const by = q.get("by") || "saw";
    if (!(by in OVERALL_BY)) fail(400, "bad_request", `by must be one of ${Object.keys(OVERALL_BY).sort().join(", ")}`);
    return json(leaderboardOverall(await withSaw(env, await summaries(env)), by, q.get("demo") === "1"));
  }
  if (view !== "per_setup") fail(400, "bad_request", "view must be per_setup or overall");
  const sort = q.get("sort") || "latency";
  if (!(sort in LEADERBOARD_SORTS)) fail(400, "bad_request", `sort must be one of ${Object.keys(LEADERBOARD_SORTS).sort().join(", ")}`);
  return json(leaderboard(await withSaw(env, await summaries(env)), sort));
}

export async function constraintsRoute(req: Request, env: Env, id: string): Promise<Response> {
  await requireUser(req, env);
  const r = await getRow(env, id);
  if (!r.summary_json) fail(409, "not_ready", "the session has no results yet");
  const facts = JSON.parse(r.summary_json!).facts;
  if (!Array.isArray(facts)) fail(409, "not_available", "this session was stored without decision-helper facts");
  const params = Object.fromEntries(new URL(req.url).searchParams.entries());
  try {
    return json(evaluateFacts(facts, params));
  } catch (e) {
    if (e instanceof ConstraintError) fail(400, "bad_request", e.message);
    throw e;
  }
}

// --- backend writes ---------------------------------------------------------------------------
async function ownerFor(env: Env, jti?: string, username?: string): Promise<{ id: string | null; name: string | null }> {
  if (jti) {
    const g = await env.DB.prepare("SELECT u.id, u.username FROM run_grants g JOIN users u ON u.id=g.user_id WHERE g.jti=?").bind(jti).first<{ id: string; username: string }>();
    if (g) return { id: g.id, name: g.username };
  }
  if (username) {
    const u = await env.DB.prepare("SELECT id, username FROM users WHERE username=?").bind(String(username).toLowerCase()).first<{ id: string; username: string }>();
    if (u) return { id: u.id, name: u.username };
    return { id: null, name: String(username) };
  }
  return { id: null, name: null };
}

/** POST /api/backend/jobs — a job was created on the backend (benchmark jobs become sessions). */
export async function jobCreated(req: Request, env: Env): Promise<Response> {
  const b = parseBody(await verifyBackend(req, env, 64 * 1024));
  const id = checkId(String(b.job_id || ""));
  if (b.jti) await env.DB.prepare("UPDATE run_grants SET job_id=? WHERE jti=?").bind(id, String(b.jti)).run();
  if ((b.kind || "benchmark") !== "benchmark") return json({ ok: true, session: false });
  const owner = await ownerFor(env, b.jti, b.owner_username);
  await env.DB.prepare(
    `INSERT INTO sessions (id, owner_id, owner_username, status, created_at, input_type, n_flows, gpu, provider, models_json, prepared_set, updated_at)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING`,
  ).bind(id, owner.id, owner.name, STATUS_FROM_JOB(String(b.status || "queued")), String(b.created_at || nowIso()),
         b.input_type ? String(b.input_type).toUpperCase() : null, Number.isInteger(b.n_flows) ? b.n_flows : null,
         b.gpu ? String(b.gpu) : null, b.provider ? String(b.provider) : null, JSON.stringify(b.models || []),
         b.run_name ? String(b.run_name) : null, nowIso()).run();
  return json({ ok: true, session: true });
}

export async function jobStatus(req: Request, env: Env, id: string): Promise<Response> {
  const b = parseBody(await verifyBackend(req, env, 64 * 1024));
  checkId(id);
  const st = STATUS_FROM_JOB(String(b.status || ""));
  await env.DB.prepare("UPDATE sessions SET status=CASE WHEN status IN ('Done','Demo') THEN status ELSE ? END, updated_at=? WHERE id=?")
    .bind(st, nowIso(), id).run();
  if (st === "Failed" || st === "Interrupted" || st === "Stopped") await notifyFinal(env, id, st, b.stopped_by ? String(b.stopped_by) : undefined);   // Done: after the summary arrives
  return json({ ok: true });
}

async function ensureRow(env: Env, id: string) {
  await env.DB.prepare("INSERT INTO sessions (id, status, created_at, updated_at) VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING")
    .bind(id, "Running", nowIso(), nowIso()).run();
}

/** PUT /api/backend/sessions/:id/results — raw session_results.json, stored unparsed. */
export async function putResults(req: Request, env: Env, id: string): Promise<Response> {
  checkId(id);
  const body = await verifyBackend(req, env, 25 * 1024 * 1024);
  await ensureRow(env, id);
  const threshold = intVar(env.RESULTS_INLINE_MAX, 1_000_000);
  if (body.length > threshold) {
    const key = `sessions/${id}/session_results.json`;
    await env.STORE.put(key, body, "application/json");
    await env.DB.prepare("UPDATE sessions SET results_json=NULL, results_key=?, results_bytes=?, updated_at=? WHERE id=?")
      .bind(key, body.length, nowIso(), id).run();
    return json({ ok: true, stored: "storage", bytes: body.length });
  }
  await env.DB.prepare("UPDATE sessions SET results_json=?, results_key=NULL, results_bytes=?, updated_at=? WHERE id=?")
    .bind(new TextDecoder().decode(body), body.length, nowIso(), id).run();
  return json({ ok: true, stored: "db", bytes: body.length });
}

/** PUT /api/backend/sessions/:id/files/:name — PDF / manifest / prepared-set files -> Storage. */
export async function putFile(req: Request, env: Env, id: string, name: string): Promise<Response> {
  checkId(id);
  if (!(FILES as readonly string[]).includes(name)) fail(400, "bad_request", `file must be one of ${FILES.join(", ")}`);
  const body = await verifyBackend(req, env, 25 * 1024 * 1024);
  await ensureRow(env, id);
  const key = `sessions/${id}/${name}`;
  await env.STORE.put(key, body, CONTENT_TYPE[name]);
  const r = await env.DB.prepare("SELECT files_json FROM sessions WHERE id=?").bind(id).first<{ files_json: string | null }>();
  const files = { ...(r?.files_json ? JSON.parse(r.files_json) : {}), [name]: key };
  await env.DB.prepare("UPDATE sessions SET files_json=?, updated_at=? WHERE id=?").bind(JSON.stringify(files), nowIso(), id).run();
  return json({ ok: true, key, bytes: body.length });
}

/** PUT /api/backend/sessions/:id/summary — the compact record; marks the session finished. */
export async function putSummary(req: Request, env: Env, id: string): Promise<Response> {
  checkId(id);
  const b = parseBody(await verifyBackend(req, env, 512 * 1024));
  const s = b.summary;
  if (!s || typeof s !== "object" || s.session_id !== id) fail(400, "bad_request", "summary.session_id must match the URL");
  await ensureRow(env, id);
  const cur = await env.DB.prepare("SELECT owner_id FROM sessions WHERE id=?").bind(id).first<{ owner_id: string | null }>();
  const owner = cur?.owner_id ? null : await ownerFor(env, undefined, b.owner_username || s.owner_username);
  const status = ["Done", "Demo", "Failed", "Interrupted", "Running"].includes(s.status) ? s.status : "Done";
  await env.DB.prepare(
    `UPDATE sessions SET status=?, finished_at=?, input_type=?, n_flows=?, gpu=?, provider=?, models_json=?, more_efficient=?,
       prepared_set=?, prepared_set_sha256=?, settings_fingerprint=?, weights_json=?, summary_json=?, updated_at=?,
       created_at=COALESCE(?, created_at), owner_id=COALESCE(owner_id, ?), owner_username=COALESCE(owner_username, ?) WHERE id=?`,
  ).bind(status, s.finished_at ?? null, s.input_type ?? null, Number.isInteger(s.n_flows) ? s.n_flows : null, s.gpu ?? null,
         s.provider ?? null, JSON.stringify(s.models || []), typeof s.more_efficient === "string" ? s.more_efficient : null,
         s.run_name ?? null, s.prepared_set_sha256 ?? null, s.settings_fingerprint ?? null, JSON.stringify(s.weights_used ?? null),
         JSON.stringify(s), nowIso(), s.created_at ?? null, owner?.id ?? null, owner?.name ?? null, id).run();
  await notifyFinal(env, id, status);
  return json({ ok: true });
}

// --- prepared-set hash migration (backend-signed; scripts/migrate_prepared_set_hash.py) -------
/** GET /api/backend/sessions — every stored session's id, hash, hash version and stored files. */
export async function backendListSessions(req: Request, env: Env): Promise<Response> {
  await verifyBackend(req, env, 0);
  const r = await env.DB.prepare("SELECT id, summary_json, files_json FROM sessions WHERE deleted_at IS NULL AND summary_json IS NOT NULL ORDER BY created_at")
    .all<{ id: string; summary_json: string; files_json: string | null }>();
  return json({ sessions: r.results.map((x) => {
    const ident = JSON.parse(x.summary_json).identity || {};
    return { id: x.id, prepared_set_sha256: ident.prepared_set_sha256 ?? null, prepared_set_hash_version: ident.prepared_set_hash_version ?? 1,
             files: x.files_json ? Object.keys(JSON.parse(x.files_json)) : [] };
  }) });
}

/** GET /api/backend/sessions/:id/files/:name — the stored copy, for recomputing its hash. */
export async function backendGetFile(req: Request, env: Env, id: string, name: string): Promise<Response> {
  await verifyBackend(req, env, 0);
  if (!(FILES as readonly string[]).includes(name)) fail(404, "not_found", "no such file");
  const r = await getRow(env, id);
  const key = r.files_json ? JSON.parse(r.files_json)[name] : null;
  const o = key ? await env.STORE.get(key) : null;
  if (!o) fail(404, "not_found", `${name} is not stored for this session`);
  return new Response(o!.body, { headers: { "content-type": CONTENT_TYPE[name], "cache-control": "no-store" } });
}

/** POST /api/backend/sessions/:id/identity {prepared_set_sha256, prepared_set_hash_version} —
 *  replaces ONLY the stored prepared-set hash (no status change, no notification). */
export async function backendSetIdentity(req: Request, env: Env, id: string): Promise<Response> {
  const b = parseBody(await verifyBackend(req, env, 4096));
  const h = String(b.prepared_set_sha256 || "");
  if (!/^[0-9a-f]{64}$/.test(h) || !Number.isInteger(b.prepared_set_hash_version)) fail(400, "bad_request", "prepared_set_sha256 (64 hex) and prepared_set_hash_version");
  const r = await getRow(env, id);
  if (!r.summary_json) fail(409, "not_ready", "the session has no summary yet");
  const s = JSON.parse(r.summary_json!);
  const before = (s.identity || {}).prepared_set_sha256 ?? null;
  s.identity = { ...(s.identity || {}), prepared_set_sha256: h, prepared_set_hash_version: b.prepared_set_hash_version };
  s.prepared_set_sha256 = h;
  await env.DB.prepare("UPDATE sessions SET summary_json=?, prepared_set_sha256=?, updated_at=? WHERE id=?").bind(JSON.stringify(s), h, nowIso(), id).run();
  return json({ ok: true, before, after: h });
}
