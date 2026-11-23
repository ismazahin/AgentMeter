// Data preparation with a waiting list. The preparation service (cloudprep/app.py, started by
// scripts/prep_local.py) runs on the rented GPU machine next to the GPU server, and never while a
// benchmark runs there (agentmeter/server/gpulock.py), so preparing cannot change a reading. An
// upload made while a benchmark runs is stored and WAITS; the service pulls it as soon as the
// machine is free. The web app never talks to the service: the control plane holds every key and
// hands out single-use Storage URLs; the service signs everything with CLOUD_PREP_SECRET.
//
//   POST /api/prep/jobs               {filename, size} | {url}, max_flows, other_attack, limits
//                                     -> {job_id, upload_url?}  (a file is PUT there, then /start)
//   POST /api/prep/jobs/:id/start     the upload is in Storage -> waiting
//   POST /api/prep/jobs/:id/cancel    the owner, while uploading or waiting
//   GET  /api/prep/jobs/:id           status, phase, progress log, why it waits; set_name + files when done
//   GET  /api/prep/mine               the caller's uploads not yet benchmarked (New benchmark › Your uploads)
//   GET  /api/prep/jobs/:id/files/:n  manifest.json | features.csv | labels.csv (the web app then imports
//                                     them into the GPU server, hash-checked), and the traffic analysis:
//                                     ids_scores.csv | ids_pool.json | ids_flows_identity.csv (owner only)
// Signed by the service:
//   POST /api/prep/register | heartbeat  {url, version} | {stopping: true}
//   POST /api/prep/pending            {state, holder?} -> {pending: n}  (state: idle | waiting_for_benchmark | preparing)
//   POST /api/prep/claim              -> {job: {...} | null}: the oldest waiting job, with fresh URLs
//   POST /api/prep/jobs/:id/events    its reports ({event}|{done}|{failed})
// The control plane nudges the service (signed POST <service>/nudge) when a job starts waiting.
//
// Each upload starts ONE benchmark: runs.ts marks the job used (markPrepUsed) when it authorises a
// benchmark on its prepared set, and refuses a second. Uploads expire after EXPIRE_DAYS.
import { requireUser } from "./auth.ts";
import { checkUrl, parseBody, verifySigned } from "./backend.ts";
import { hmacHex, randomHex, sha256Hex } from "./crypto.ts";
import { type Env, fail, intVar, json, nowIso, nowS, readJson } from "./util.ts";
import { loadLimits } from "./settings.ts";
import { idsModelForJob } from "./ids.ts";
import { loadSettings } from "./settings.ts";
import { sendTelegram } from "./notify.ts";

export const PREP_FILES = ["manifest.json", "features.csv", "labels.csv"] as const;
export const PREP_IDS_FILES = ["ids_scores.csv", "ids_pool.json", "ids_flows_identity.csv"] as const;
const OWNER_ONLY = "ids_flows_identity.csv";      // source/destination addresses: never another user, not admins
const ALL_FILES: readonly string[] = [...PREP_FILES, ...PREP_IDS_FILES];
const CONTENT_TYPE: Record<string, string> = { "manifest.json": "application/json", "features.csv": "text/csv", "labels.csv": "text/csv",
  "ids_scores.csv": "text/csv", "ids_pool.json": "application/json", "ids_flows_identity.csv": "text/csv" };
const LOG_MAX = 200;
const STALE_S = 15 * 60;              // a running job with no news for this long: the machine went away
const MAX_ATTEMPTS = 3;               // picked up this often without finishing: failed
const MAX_ACTIVE = 3;                 // unfinished uploads per user (uploading, waiting or preparing)
const URL_GET_S = 3600;               // the input's download URL lives an hour (handed out when it starts)
export const EXPIRE_DAYS = 7;         // an upload nobody benchmarked is deleted after this long
const UNFINISHED = "('uploading','waiting','running')";
/** config.yaml service: max_csv_rows, max_pcap_packets, max_pcap_flows, pcap_windows, url_timeout_s. */
export const DEFAULT_PREP_LIMITS: Record<string, number> = {
  max_csv_rows: 500_000, max_pcap_packets: 100_000, max_pcap_flows: 20_000, pcap_windows: 10, url_timeout_s: 3600,
};
const LIMIT_KEYS: Record<string, [number, number]> = {
  max_csv_rows: [1, 50_000_000], max_pcap_packets: [1, 100_000_000], max_pcap_flows: [1, 10_000_000],
  pcap_windows: [1, 1000], max_url_download_bytes: [1, 50 * 2 ** 30], url_timeout_s: [1, 3600],
};

interface Row {
  id: string; owner_id: string; status: string; source_kind: string; filename: string | null; url: string | null;
  size_bytes: number | null; options_json: string; phase: string | null; message: string | null; log_json: string;
  set_name: string | null; files_json: string | null; error: string | null; error_code: string | null;
  created_at: string; updated_at: string; finished_at: string | null;
  used_at?: string | null; attempts?: number; claimed_at?: string | null; queued_at?: string | null;
}

const inputKey = (r: Pick<Row, "id" | "filename">) => `prep/${r.id}/input/${r.filename}`;
const outKey = (id: string, name: string) => `prep/${id}/out/${name}`;

/** The largest upload: the admin's upload limit, and one Storage file (50 MB on the free plan). */
async function maxUploadBytes(env: Env): Promise<number> {
  const lim = await loadLimits(env);
  return Math.min(lim.max_upload_mb, intVar(env.CLOUD_PREP_MAX_MB, 50)) * 1024 * 1024;
}

// ---------- where the preparation service is ----------
// It registers itself (scripts/prep_local.py on the GPU machine), signed with CLOUD_PREP_SECRET, and
// sends a heartbeat every minute; it is online while its last news is younger than PREP_ONLINE_S.
// Its state (what it is doing, and who holds the machine while it waits) is kept in gpu_json.
export const PREP_HEARTBEAT_S = 60;
const PREP_ONLINE_S = 180;
const secretOk = (env: Env) => (env.CLOUD_PREP_SECRET || "").length >= 16;

export interface PrepService { url: string; online: boolean; last_heartbeat_at: string | null; state: string | null;
  holder: Record<string, unknown> | null; registered_at: string | null }

export async function registeredPrep(env: Env): Promise<PrepService | null> {
  const r = await env.DB.prepare("SELECT url, last_heartbeat_at, gpu_json, registered_at FROM backends WHERE id='prep'")
    .first<{ url: string | null; last_heartbeat_at: string | null; gpu_json: string | null; registered_at: string | null }>();
  if (!r || !r.url) return null;
  const age = r.last_heartbeat_at ? (Date.now() - Date.parse(r.last_heartbeat_at)) / 1000 : Infinity;
  let st: Record<string, any> = {};
  try { st = r.gpu_json ? JSON.parse(r.gpu_json) : {}; } catch { /* none */ }
  return { url: r.url, online: age <= PREP_ONLINE_S, last_heartbeat_at: r.last_heartbeat_at, state: st.state ?? null,
           holder: st.holder ?? null, registered_at: r.registered_at };
}

/** The service's address while it is online and the secret is set, else null. */
export async function cloudPrepUrl(env: Env): Promise<string | null> {
  if (!secretOk(env)) return null;
  const reg = await registeredPrep(env);
  return reg && reg.online ? reg.url : null;
}

async function requireCloud(env: Env): Promise<string> {
  const url = await cloudPrepUrl(env);
  if (!url) fail(409, "cloud_prep_unavailable", "The GPU server is off, so new data cannot be prepared right now. Try again once it is back on.");
  return url!;
}

/** Ask the service to look for work now (best effort: it also looks every minute). */
async function nudge(env: Env): Promise<void> {
  const base = await cloudPrepUrl(env);
  if (!base) return;
  const body = new Uint8Array(0), ts = String(nowS());
  const sig = await hmacHex(env.CLOUD_PREP_SECRET!, `POST\n/nudge\n${ts}\n${await sha256Hex(body)}`);
  try {
    const r = await fetch(base + "/nudge", { method: "POST", body, headers: { "X-AM-Timestamp": ts, "X-AM-Signature": sig },
                                             signal: AbortSignal.timeout(4000) });
    await r.body?.cancel();
  } catch { /* it looks by itself within a minute */ }
}

/** POST /api/prep/register and /api/prep/heartbeat (signed by the service): {url, version} — or
 *  {stopping: true} when it stops, so the site shows it as not running at once. */
export async function prepRegister(req: Request, env: Env): Promise<Response> {
  if (!secretOk(env)) fail(409, "cloud_prep_unavailable", "set CLOUD_PREP_SECRET on the control plane first");
  const b = parseBody(await verifySigned(req, env.CLOUD_PREP_SECRET!, 16 * 1024, "preparation service"));
  const t = nowIso();
  if (b.stopping === true) {
    await env.DB.prepare("UPDATE backends SET last_heartbeat_at=NULL WHERE id='prep'").run();
    await requeueRunning(env, "the GPU server stopped");
    return json({ ok: true, stopped: true });
  }
  const url = checkUrl(env, b.url);
  const before = await registeredPrep(env);
  if (before && before.url !== url) await requeueRunning(env, "the GPU server restarted");
  await env.DB.prepare(
    `INSERT INTO backends (id,url,mode,gpu_json,creds_json,version,registered_at,last_heartbeat_at) VALUES ('prep',?,'prep',NULL,'{}',?,?,?)
     ON CONFLICT(id) DO UPDATE SET registered_at=CASE WHEN backends.url=excluded.url THEN backends.registered_at ELSE excluded.registered_at END,
       url=excluded.url, version=excluded.version, last_heartbeat_at=excluded.last_heartbeat_at`,
  ).bind(url, String(b.version || "").slice(0, 64), t, t).run();
  return json({ ok: true, url, heartbeat_s: PREP_HEARTBEAT_S, online_for_s: PREP_ONLINE_S, pending: await pendingCount(env) });
}

/** Jobs the service had picked up go back to the waiting list (the machine went away). */
async function requeueRunning(env: Env, why: string): Promise<void> {
  await env.DB.prepare(`UPDATE prep_jobs SET status='waiting', phase='waiting', message=?, updated_at=? WHERE status='running'`)
    .bind(`${why}: waiting to start again`, nowIso()).run();
}

const pendingCount = async (env: Env) =>
  (await env.DB.prepare("SELECT COUNT(*) AS n FROM prep_jobs WHERE status='waiting'").first<{ n: number }>())?.n || 0;

/** POST /api/prep/pending (signed by the service): what it is doing -> how many jobs wait. */
export async function prepPending(req: Request, env: Env): Promise<Response> {
  if (!secretOk(env)) fail(409, "cloud_prep_unavailable", "set CLOUD_PREP_SECRET on the control plane first");
  const b = parseBody(await verifySigned(req, env.CLOUD_PREP_SECRET!, 16 * 1024, "preparation service"));
  const state = ["idle", "waiting_for_benchmark", "preparing"].includes(b.state) ? b.state : "idle";
  const h = b.holder && typeof b.holder === "object" ? b.holder : null;
  const holder = h ? { kind: String(h.kind || "").slice(0, 20), models: Number.isFinite(h.models) ? h.models : null,
                       started_at: typeof h.started_at === "string" ? h.started_at.slice(0, 32) : null } : null;
  await env.DB.prepare("UPDATE backends SET gpu_json=?, last_heartbeat_at=? WHERE id='prep' AND url IS NOT NULL")
    .bind(JSON.stringify({ state, holder, at: nowIso() }), nowIso()).run();
  await housekeeping(env);
  return json({ ok: true, pending: await pendingCount(env) });
}

/** POST /api/prep/claim (signed by the service, which holds the machine): the oldest waiting job,
 *  with fresh single-use URLs, now running — or {job: null}. */
export async function prepClaim(req: Request, env: Env): Promise<Response> {
  if (!secretOk(env)) fail(409, "cloud_prep_unavailable", "set CLOUD_PREP_SECRET on the control plane first");
  await verifySigned(req, env.CLOUD_PREP_SECRET!, 16 * 1024, "preparation service");
  await housekeeping(env);
  for (let i = 0; i < 3; i++) {
    const r = await env.DB.prepare("SELECT * FROM prep_jobs WHERE status='waiting' ORDER BY queued_at ASC, id ASC LIMIT 1").first<Row>();
    if (!r) return json({ job: null });
    const t = nowIso();
    const got = await env.DB.prepare("UPDATE prep_jobs SET status='running', phase='starting', message='preparing', attempts=attempts+1, claimed_at=?, updated_at=? WHERE id=? AND status='waiting'")
      .bind(t, t, r.id).run();
    if (!got.meta.changes) continue;                  // someone else took it
    try {
      return json({ job: await jobBody(env, r) });
    } catch (e) {
      await finishFailed(env, { ...r, status: "running" }, `could not hand the job out: ${(e as Error).message}`, "cloud_prep_unavailable");
    }
  }
  return json({ job: null });
}

/** Expire old uploads and re-queue (or fail) jobs whose machine went away. Cheap; called often. */
async function housekeeping(env: Env): Promise<void> {
  const t = nowIso(), stale = nowIso(Date.now() - STALE_S * 1000), old = nowIso(Date.now() - EXPIRE_DAYS * 86400 * 1000);
  await env.DB.prepare(`UPDATE prep_jobs SET status='waiting', phase='waiting', message='the GPU server stopped answering: waiting to start again', updated_at=?
                         WHERE status='running' AND updated_at < ? AND attempts < ?`).bind(t, stale, MAX_ATTEMPTS).run();
  const dead = await env.DB.prepare(`SELECT * FROM prep_jobs WHERE (status='running' AND updated_at < ?) OR (status='uploading' AND updated_at < ?)`)
    .bind(stale, stale).all<Row>();
  for (const r of dead.results)
    await finishFailed(env, r, r.status === "uploading" ? "the upload was not finished" : "the preparation stopped answering several times", "cloud_prep_stalled");
  const gone = await env.DB.prepare(`SELECT * FROM prep_jobs WHERE (status='waiting' AND created_at < ?) OR (status='done' AND used_at IS NULL AND finished_at < ?)`)
    .bind(old, old).all<Row>();
  for (const r of gone.results) {
    const files = JSON.parse(r.files_json || "[]") as string[];
    await env.STORE.delete([...(r.source_kind === "file" && r.filename ? [inputKey(r)] : []), ...files.map((f) => outKey(r.id, f))]).catch(() => {});
    await env.DB.prepare("UPDATE prep_jobs SET status='expired', files_json='[]', message=?, updated_at=? WHERE id=?")
      .bind(`not benchmarked within ${EXPIRE_DAYS} days: deleted`, t, r.id).run();
  }
}

function safeName(name: unknown): string {
  const base = String(name || "").split(/[\\/]/).pop()!.replace(/[^A-Za-z0-9_.-]/g, "_").replace(/^\.+/, "").slice(-120);
  if (!/\.(csv|pcap|pcapng)$/i.test(base)) fail(400, "bad_file", "the file must be a .csv, .pcap or .pcapng");
  return base;
}

function cleanLimits(v: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  const src = (v && typeof v === "object" ? v : {}) as Record<string, unknown>;
  for (const [k, [lo, hi]] of Object.entries(LIMIT_KEYS)) {
    const n = Number(src[k]);
    if (Number.isFinite(n) && n >= lo) out[k] = Math.min(Math.floor(n), hi);
  }
  // A limit the web app did not send (the GPU server, which hands it the limits, is off: the traffic
  // analysis needs no GPU) takes the default of config.yaml's service section.
  for (const [k, v] of Object.entries(DEFAULT_PREP_LIMITS)) if (!(k in out)) out[k] = v;
  return out;
}

async function load(env: Env, id: string): Promise<Row> {
  if (!/^[0-9a-f]{32}$/.test(id)) fail(404, "not_found", "no such preparation");
  const r = await env.DB.prepare("SELECT * FROM prep_jobs WHERE id=?").bind(id).first<Row>();
  if (!r) fail(404, "not_found", "no such preparation");
  return r!;
}

async function mine(req: Request, env: Env, id: string): Promise<Row> {
  const a = await requireUser(req, env);
  const r = await load(env, id);
  if (r.owner_id !== a.user.id && a.user.role !== "admin") fail(404, "not_found", "no such preparation");
  return r;
}

/** Why a waiting job waits: the GPU server is off, a benchmark holds the machine (with what the
 *  service last saw of it: kind, number of models, start time, never whose), or it is about to start. */
async function waitingFor(env: Env, r: Row, svc?: PrepService | null) {
  if (r.status !== "waiting") return null;
  const reg = svc === undefined ? await registeredPrep(env) : svc;
  const q = r.queued_at || r.created_at;
  const ahead = (await env.DB.prepare("SELECT COUNT(*) AS n FROM prep_jobs WHERE status IN ('waiting','running') AND (queued_at < ? OR (queued_at = ? AND id < ?))")
    .bind(q, q, r.id).first<{ n: number }>())?.n || 0;
  if (!reg || !reg.online || !secretOk(env)) return { reason: "gpu_off", ahead };
  if (reg.state === "waiting_for_benchmark") return { reason: "benchmark", holder: reg.holder, ahead };
  return { reason: ahead ? "queue" : "next", ahead };
}

function publicJob(r: Row, waiting: unknown = null) {
  const opts = JSON.parse(r.options_json || "{}");
  return {
    job_id: r.id, status: r.status, source: r.source_kind, filename: r.filename, url: r.url, size_bytes: r.size_bytes,
    max_flows: opts.max_flows ?? null, phase: r.phase, message: r.message, log: JSON.parse(r.log_json || "[]"), set_name: r.set_name,
    files: r.files_json ? JSON.parse(r.files_json) : [], error: r.error, code: r.error_code, waiting,
    used: !!r.used_at, attempts: r.attempts ?? 0,
    expires_at: ["waiting", "done"].includes(r.status) && !r.used_at
      ? nowIso(Date.parse(r.status === "done" ? (r.finished_at || r.created_at) : r.created_at) + EXPIRE_DAYS * 86400 * 1000) : null,
    created_at: r.created_at, updated_at: r.updated_at, finished_at: r.finished_at,
  };
}

async function finishFailed(env: Env, r: Row, error: string, code: string) {
  const t = nowIso();
  await env.DB.prepare("UPDATE prep_jobs SET status='failed', error=?, error_code=?, updated_at=?, finished_at=? WHERE id=? AND status IN ('uploading','waiting','running')")
    .bind(error.slice(0, 600), code.slice(0, 64), t, t, r.id).run();
  if (r.source_kind === "file" && r.filename) await env.STORE.delete([inputKey(r)]).catch(() => {});
}

function publicBase(env: Env): string {
  if (env.PUBLIC_BASE_URL) return env.PUBLIC_BASE_URL.replace(/\/+$/, "");
  if (env.SUPABASE_URL) return `${env.SUPABASE_URL.replace(/\/+$/, "")}/functions/v1/agentmeter`;
  return fail(500, "misconfigured", "set PUBLIC_BASE_URL (this API's public address) for cloud preparation");
}

/** What the service needs for one job: single-use URLs for its input and outputs, where to report. */
async function jobBody(env: Env, r: Row): Promise<Record<string, unknown>> {
  const opts = JSON.parse(r.options_json);
  const ids = await idsModelForJob(env);
  const outputs: Record<string, string> = {};
  for (const n of ids ? ALL_FILES : PREP_FILES) {
    const u = await env.STORE.signedUploadUrl(outKey(r.id, n));
    if (!u) throw new Error("this storage cannot hand out upload URLs");
    outputs[n] = u;
  }
  let source: Record<string, unknown>;
  if (r.source_kind === "file") {
    const u = await env.STORE.signedDownloadUrl(inputKey(r), URL_GET_S);
    if (!u) throw new Error("this storage cannot hand out download URLs");
    source = { kind: "file", url: u };
  } else source = { kind: "url", url: r.url };
  const reportPath = `/api/prep/jobs/${r.id}/events`;
  return {
    job_id: r.id, filename: r.filename, source, max_flows: opts.max_flows, other_attack: opts.other_attack,
    limits: opts.limits, max_bytes: opts.max_bytes, outputs, report_url: publicBase(env) + reportPath, report_path: reportPath,
    ...(ids ? { ids } : {}),
  };
}

/** The job is ready to prepare: onto the waiting list, and tell the service. */
async function enqueue(env: Env, id: string): Promise<void> {
  // queued_at in milliseconds: the list's order (a job the machine dropped keeps its place)
  await env.DB.prepare("UPDATE prep_jobs SET status='waiting', phase='waiting', message='waiting for the GPU server', updated_at=?, queued_at=? WHERE id=?")
    .bind(nowIso(), new Date().toISOString(), id).run();
  await nudge(env);
}

/** POST /api/prep/jobs — a new upload for the signed-in user. */
export async function createPrepJob(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  await requireCloud(env);
  publicBase(env);                                    // the service needs somewhere to report to
  const b = await readJson<Record<string, any>>(req);
  const maxFlows = Number(b.max_flows);
  if (!Number.isInteger(maxFlows) || maxFlows < 1 || maxFlows > 500) fail(400, "bad_request", "max_flows: 1 to 500");
  const limits = cleanLimits(b.limits);
  await housekeeping(env);
  const active = await env.DB.prepare(`SELECT COUNT(*) AS n FROM prep_jobs WHERE owner_id=? AND status IN ${UNFINISHED}`)
    .bind(a.user.id).first<{ n: number }>();
  if ((active?.n || 0) >= MAX_ACTIVE) fail(429, "too_many_preparations", `You have ${MAX_ACTIVE} uploads waiting or being prepared. Wait for one to finish, or cancel one.`);
  const maxBytes = await maxUploadBytes(env);
  const id = randomHex(16), t = nowIso();
  let row: Row;
  if (typeof b.url === "string" && b.url) {
    if (!/^https:\/\/[^\s]{4,2000}$/i.test(b.url)) fail(400, "bad_url", "give a direct https:// link");
    row = { id, owner_id: a.user.id, status: "waiting", source_kind: "url", filename: null, url: b.url, size_bytes: null,
            options_json: JSON.stringify({ max_flows: maxFlows, other_attack: !!b.other_attack, limits,
                                           max_bytes: Math.min(limits.max_url_download_bytes ?? Infinity, intVar(env.CLOUD_PREP_MAX_URL_MB, 10240) * 1024 * 1024) }),
            phase: "waiting", message: null, log_json: "[]", set_name: null, files_json: null, error: null, error_code: null,
            created_at: t, updated_at: t, finished_at: null };
  } else {
    const filename = safeName(b.filename);
    const size = Number(b.size);
    if (!Number.isInteger(size) || size < 1) fail(400, "bad_request", "size: the file's size in bytes");
    if (size > maxBytes) fail(413, "too_large", `Uploads are limited to ${Math.round(maxBytes / 2 ** 20)} MB. Use Import from a link for a bigger file.`);
    // Waiting files are kept in Storage: keep them inside the free plan's room (1 GB in all)
    const held = await env.DB.prepare(`SELECT COALESCE(SUM(size_bytes),0) AS n FROM prep_jobs WHERE source_kind='file' AND status IN ${UNFINISHED}`)
      .first<{ n: number }>();
    if ((held?.n || 0) + size > intVar(env.PREP_STORAGE_MAX_MB, 800) * 1024 * 1024)
      fail(507, "storage_full", "Too many uploads are waiting. Try again later.");
    row = { id, owner_id: a.user.id, status: "uploading", source_kind: "file", filename, url: null, size_bytes: size,
            options_json: JSON.stringify({ max_flows: maxFlows, other_attack: !!b.other_attack, limits, max_bytes: maxBytes }),
            phase: "uploading", message: null, log_json: "[]", set_name: null, files_json: null, error: null, error_code: null,
            created_at: t, updated_at: t, finished_at: null };
  }
  await env.DB.prepare(
    `INSERT INTO prep_jobs (id, owner_id, status, source_kind, filename, url, size_bytes, options_json, phase, log_json, created_at, updated_at)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`,
  ).bind(row.id, row.owner_id, row.status, row.source_kind, row.filename, row.url, row.size_bytes, row.options_json,
         row.phase, row.log_json, row.created_at, row.updated_at).run();
  if (row.source_kind === "url") {
    await enqueue(env, id);
    return json({ job_id: id, status: "waiting" }, 201);
  }
  const upload = await env.STORE.signedUploadUrl(inputKey(row));
  if (!upload) fail(409, "cloud_prep_unavailable", "this storage cannot hand out upload URLs");
  return json({ job_id: id, status: "uploading", upload_url: upload, max_bytes: maxBytes }, 201);
}

/** POST /api/prep/jobs/:id/start — the file is in Storage: onto the waiting list. */
export async function startPrepJob(req: Request, env: Env, id: string): Promise<Response> {
  const r = await mine(req, env, id);
  if (r.status !== "uploading") fail(409, "not_uploading", `this upload is ${r.status}`);
  const size = await env.STORE.size(inputKey(r));
  if (size === null) fail(409, "not_uploaded", "the file is not uploaded yet");
  if (size !== r.size_bytes) fail(409, "size_mismatch", `storage holds ${size} bytes, expected ${r.size_bytes}`);
  await enqueue(env, id);
  return json({ job_id: id, status: "waiting" });
}

/** POST /api/prep/jobs/:id/cancel — the owner gives up an upload that has not started preparing. */
export async function cancelPrepJob(req: Request, env: Env, id: string): Promise<Response> {
  const a = await requireUser(req, env);
  const r = await load(env, id);
  if (r.owner_id !== a.user.id) fail(404, "not_found", "no such preparation");
  const t = nowIso();
  const c = await env.DB.prepare("UPDATE prep_jobs SET status='cancelled', message='cancelled', updated_at=?, finished_at=? WHERE id=? AND status IN ('uploading','waiting')")
    .bind(t, t, id).run();
  if (!c.meta.changes) fail(409, "not_waiting", r.status === "running" ? "It is being prepared already, so it can no longer be cancelled." : `this upload is ${r.status}`);
  if (r.source_kind === "file" && r.filename) await env.STORE.delete([inputKey(r)]).catch(() => {});
  return json({ ok: true, status: "cancelled" });
}

/** GET /api/prep/jobs/:id — the job, its progress log, why it waits and, when done, its files. */
export async function getPrepJob(req: Request, env: Env, id: string): Promise<Response> {
  await mine(req, env, id);
  await housekeeping(env);
  const r = await load(env, id);
  return json(publicJob(r, await waitingFor(env, r)));
}

/** GET /api/prep/mine — the caller's uploads not yet benchmarked, newest first (Your uploads). */
export async function listMyPrep(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  await housekeeping(env);
  const r = await env.DB.prepare(
    `SELECT * FROM prep_jobs WHERE owner_id=? AND (status IN ${UNFINISHED} OR (status='done' AND used_at IS NULL)) ORDER BY COALESCE(queued_at, created_at) DESC, created_at DESC LIMIT 50`,
  ).bind(a.user.id).all<Row>();
  const svc = await registeredPrep(env);
  const out = [];
  for (const x of r.results) out.push(publicJob(x, await waitingFor(env, x, svc)));
  return json({ uploads: out, expire_days: EXPIRE_DAYS });
}

/** Each upload starts ONE benchmark (runs.ts, when it authorises a benchmark on a prepared set).
 *  A retry is allowed only when the first authorisation produced no session (its token expired). */
export async function markPrepUsed(env: Env, userId: string, setName: string, retryAfterS: number): Promise<boolean> {
  const t = nowIso();
  const r = await env.DB.prepare(
    `UPDATE prep_jobs SET used_at=? WHERE owner_id=? AND set_name=? AND status='done'
       AND (used_at IS NULL OR (used_at < ? AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.prepared_set = prep_jobs.set_name)))`,
  ).bind(t, userId, setName, nowIso(Date.now() - retryAfterS * 1000)).run();
  return !!r.meta.changes;
}

/** GET /api/prep/jobs/:id/files/:name — one prepared file (for the import into the GPU server). */
export async function getPrepFile(req: Request, env: Env, id: string, name: string): Promise<Response> {
  const r = await mine(req, env, id);
  if (name === OWNER_ONLY && r.owner_id !== (await requireUser(req, env)).user.id)
    fail(403, "owner_only", "the flows' addresses are shown to the person who uploaded them only");
  if (r.status !== "done" || !(JSON.parse(r.files_json || "[]") as string[]).includes(name)) fail(404, "not_found", `${name} is not ready`);
  const o = await env.STORE.get(outKey(id, name));
  if (!o) fail(404, "not_found", `${name} is not stored`);
  return new Response(o!.body, { headers: { "content-type": CONTENT_TYPE[name], "cache-control": "no-store" } });
}

/** POST /api/prep/jobs/:id/events — the cloud service's progress and result (signed). */
export async function prepEvents(req: Request, env: Env, id: string): Promise<Response> {
  if (!secretOk(env)) fail(409, "cloud_prep_unavailable", "cloud preparation is not set up on this control plane");
  const raw = await verifySigned(req, env.CLOUD_PREP_SECRET!, 64 * 1024, "cloud preparation");
  let b: Record<string, any>;
  try { b = JSON.parse(new TextDecoder().decode(raw)); } catch { return fail(400, "bad_request", "body must be JSON"); }
  const r = await load(env, id);
  if (r.status !== "running") return json({ ok: true, ignored: r.status });   // finished (or never started): late news
  const t = nowIso();
  if (b.event && typeof b.event === "object") {
    const ev = { t, phase: String(b.event.phase || "").slice(0, 40) || null, message: String(b.event.message || "").slice(0, 300) || null,
                 ...(Number.isFinite(b.event.percent) ? { percent: b.event.percent } : {}) };
    const log = JSON.parse(r.log_json || "[]") as unknown[];
    log.push(ev);
    await env.DB.prepare("UPDATE prep_jobs SET phase=COALESCE(?, phase), message=?, log_json=?, updated_at=? WHERE id=? AND status='running'")
      .bind(ev.phase, ev.message, JSON.stringify(log.slice(-LOG_MAX)), t, id).run();
    return json({ ok: true });
  }
  if (b.done && typeof b.done === "object") {
    const files = Array.isArray(b.done.files) ? (b.done.files as unknown[]).map(String) : [];
    const setName = String(b.done.set_name || "");
    if (!/^(csv_runs|pcap_runs)\/[A-Za-z0-9_.-]{1,120}$/.test(setName) || !files.includes("manifest.json") || !files.includes("features.csv")
        || files.some((f) => !ALL_FILES.includes(f))) fail(400, "bad_request", "done: set_name and files");
    for (const f of files) if ((await env.STORE.size(outKey(id, f))) === null) fail(409, "not_uploaded", `${f} is not in storage`);
    const { sha, ids } = await analysisFacts(env, id, files);
    await env.DB.prepare("UPDATE prep_jobs SET status='done', phase='done', message=?, set_name=?, files_json=?, prepared_sha256=?, ids_json=?, updated_at=?, finished_at=? WHERE id=? AND status='running'")
      .bind(`prepared ${setName.split("/").pop()}`, setName, JSON.stringify(files), sha, ids, t, t, id).run();
    if (r.source_kind === "file") await env.STORE.delete([inputKey(r)]).catch(() => {});
    await tellOwner(env, r.owner_id, `AgentMeter: your data ${r.filename || r.url || ""} is prepared. Open New benchmark › Your uploads to choose the models.`);
    return json({ ok: true });
  }
  if (b.failed && typeof b.failed === "object") {
    await finishFailed(env, r, String(b.failed.error || "the preparation failed"), String(b.failed.code || "cloud_prepare_failed"));
    await tellOwner(env, r.owner_id, `AgentMeter: your data ${r.filename || r.url || ""} could not be prepared. ${String(b.failed.error || "").slice(0, 300)}`);
    return json({ ok: true });
  }
  return fail(400, "bad_request", "send {event}, {done} or {failed}");
}

/** What a finished job records for listings: the prepared set's fingerprint (the Data sets key) and
 *  the IDS analysis summary. Best effort: a file that cannot be read leaves the field null. */
async function analysisFacts(env: Env, id: string, files: string[]): Promise<{ sha: string | null; ids: string | null }> {
  const read = async (n: string) => {
    if (!files.includes(n)) return null;
    try { return await (await env.STORE.get(outKey(id, n)))?.json<Record<string, any>>() ?? null; } catch { return null; }
  };
  const man = await read("manifest.json"), pool = await read("ids_pool.json");
  const sha = man?.prepared_set?.content_sha256;
  const ids = pool ? JSON.stringify({ model: pool.model ?? null, threshold: pool.threshold ?? null, n_flows_in: pool.n_flows_in ?? null,
    n_flagged: pool.n_flagged ?? null, n_selected: pool.n_selected ?? null, labelled: !!pool.labelled }) : null;
  return { sha: typeof sha === "string" && /^[0-9a-f]{64}$/.test(sha) ? sha : null, ids };
}

/** GET /api/analyses — the caller's finished traffic analyses (an admin: everyone's), newest first:
 *  the Analyse traffic list, including sets nobody has benchmarked yet. */
export async function listAnalyses(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  const admin = a.user.role === "admin";
  const r = await env.DB.prepare(
    `SELECT p.id, p.owner_id, u.username, p.filename, p.url, p.set_name, p.prepared_sha256, p.ids_json, p.files_json, p.finished_at
       FROM prep_jobs p LEFT JOIN users u ON u.id = p.owner_id
      WHERE p.status='done' AND p.ids_json IS NOT NULL ${admin ? "" : "AND p.owner_id=?"}
      ORDER BY p.finished_at DESC LIMIT 500`,
  ).bind(...(admin ? [] : [a.user.id])).all<Record<string, any>>();
  return json({ analyses: r.results.map((x) => ({
    job_id: x.id, owner: x.username ?? null, mine: x.owner_id === a.user.id, filename: x.filename ?? null, url: x.url ?? null,
    set: x.set_name, key: x.prepared_sha256 ?? (x.set_name ? `name:${x.set_name}` : null), ids: JSON.parse(x.ids_json),
    files: JSON.parse(x.files_json || "[]"), finished_at: x.finished_at,
  })) });
}

/** Delete the stored files of every finished job that prepared this data set (found by its key: the
 *  fingerprint, or the set name of a name:<kind>/<name> key). Returns how many jobs, and whether an
 *  address file was among the files. Called by POST /api/datasets/delete after its owner check. */
export async function deletePrepOutputs(env: Env, key: string, ownerId: string): Promise<{ jobs: number; identity_file_deleted: boolean }> {
  const byName = key.startsWith("name:");
  // one person's analyses only: removing a data set from your view never deletes someone else's files
  const r = await env.DB.prepare(`SELECT id, files_json FROM prep_jobs WHERE status='done' AND owner_id=? AND ${byName ? "set_name" : "prepared_sha256"}=?`)
    .bind(ownerId, byName ? key.slice(5) : key).all<{ id: string; files_json: string | null }>();
  let identity = false;
  for (const j of r.results) {
    const files = JSON.parse(j.files_json || "[]") as string[];
    identity ||= files.includes(OWNER_ONLY);
    await env.STORE.delete(files.map((f) => outKey(j.id, f)));
    await env.DB.prepare("UPDATE prep_jobs SET files_json='[]', ids_json=NULL WHERE id=?").bind(j.id).run();
  }
  return { jobs: r.results.length, identity_file_deleted: identity };
}

/** The person who uploaded this data set for a traffic analysis (by its key alone), or null. */
export async function prepOwner(env: Env, key: string): Promise<string | null> {
  const byName = key.startsWith("name:");
  const r = await env.DB.prepare(
    `SELECT owner_id FROM prep_jobs WHERE status='done' AND ${byName ? "set_name" : "prepared_sha256"}=? ORDER BY finished_at ASC, id ASC LIMIT 1`,
  ).bind(byName ? key.slice(5) : key).first<{ owner_id: string }>();
  return r ? r.owner_id : null;
}

/** A Telegram message to the person who uploaded (when they set a chat id). Best effort. */
async function tellOwner(env: Env, ownerId: string, text: string): Promise<void> {
  try {
    const st = await loadSettings(env, ownerId);
    if (st.telegram_chat_id) await sendTelegram(env, st.telegram_chat_id, text);
  } catch { /* never fails the report */ }
}
