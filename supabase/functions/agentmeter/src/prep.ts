// Cloud data preparation: while the GPU server is busy benchmarking, the web app hands a new
// dataset to the control plane, which runs it on the cloud preparation service (cloudprep/,
// Google Cloud Run) with the same Python code the GPU server uses. The web app never talks to
// that service: the control plane signs every job (CLOUD_PREP_SECRET) and gives it only
// single-use Storage URLs; the service reports each step back, signed with the same secret.
//
//   POST /api/prep/jobs               {filename, size} | {url}, max_flows, other_attack, limits
//                                     -> {job_id, upload_url?}  (a file is PUT there, then /start)
//   POST /api/prep/jobs/:id/start     the upload is in Storage -> the cloud service starts
//   GET  /api/prep/jobs/:id           status, phase, progress log; set_name + files when done
//   GET  /api/prep/jobs/:id/files/:n  manifest.json | features.csv | labels.csv (the web app
//                                     then imports them into the GPU server, hash-checked), and the
//                                     traffic analysis: ids_scores.csv | ids_pool.json |
//                                     ids_flows_identity.csv (addresses: the job's owner only)
//
// The traffic analysis (no GPU): when an IDS model is stored (Admin › IDS model), every job also
// gets single-use download URLs for it, and the service scores the whole candidate pool.
//   POST /api/prep/jobs/:id/events    the cloud service's signed reports ({event}|{done}|{failed})
import { requireUser } from "./auth.ts";
import { checkUrl, parseBody, verifySigned } from "./backend.ts";
import { hmacHex, randomHex, sha256Hex } from "./crypto.ts";
import { type Env, cloudPrepConfigured, fail, intVar, json, nowIso, nowS, readJson } from "./util.ts";
import { loadLimits } from "./settings.ts";
import { idsModelForJob } from "./ids.ts";

export const PREP_FILES = ["manifest.json", "features.csv", "labels.csv"] as const;
export const PREP_IDS_FILES = ["ids_scores.csv", "ids_pool.json", "ids_flows_identity.csv"] as const;
const OWNER_ONLY = "ids_flows_identity.csv";      // source/destination addresses: never another user, not admins
const ALL_FILES: readonly string[] = [...PREP_FILES, ...PREP_IDS_FILES];
const CONTENT_TYPE: Record<string, string> = { "manifest.json": "application/json", "features.csv": "text/csv", "labels.csv": "text/csv",
  "ids_scores.csv": "text/csv", "ids_pool.json": "application/json", "ids_flows_identity.csv": "text/csv" };
const LOG_MAX = 200;
const STALE_S = 15 * 60;              // a running job with no news for this long has died
const MAX_ACTIVE = 2;                 // unfinished cloud jobs per user
const URL_GET_S = 3600;               // the input's download URL lives an hour
const WAKE_TRIES = 4;                 // attempts while a sleeping service wakes up
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
}

const inputKey = (r: Pick<Row, "id" | "filename">) => `prep/${r.id}/input/${r.filename}`;
const outKey = (id: string, name: string) => `prep/${id}/out/${name}`;

/** The largest upload prepared in the cloud: the admin's upload limit, and one Storage file. */
async function maxUploadBytes(env: Env): Promise<number> {
  const lim = await loadLimits(env);
  return Math.min(lim.max_upload_mb, intVar(env.CLOUD_PREP_MAX_MB, 50)) * 1024 * 1024;
}

// ---------- where the preparation service is ----------
// It registers itself (scripts/prep_local.py: the service on the owner's PC, started by the Start
// button), signed with CLOUD_PREP_SECRET, and sends a heartbeat every minute; it is online while its
// last heartbeat is younger than PREP_ONLINE_S. Otherwise the fixed CLOUD_PREP_URL (Cloud Run), if set.
export const PREP_HEARTBEAT_S = 60;
const PREP_ONLINE_S = 180;
const secretOk = (env: Env) => (env.CLOUD_PREP_SECRET || "").length >= 16;

export async function registeredPrep(env: Env): Promise<{ url: string; online: boolean; last_heartbeat_at: string | null } | null> {
  const r = await env.DB.prepare("SELECT url, last_heartbeat_at FROM backends WHERE id='prep'").first<{ url: string | null; last_heartbeat_at: string | null }>();
  if (!r || !r.url) return null;
  const age = r.last_heartbeat_at ? (Date.now() - Date.parse(r.last_heartbeat_at)) / 1000 : Infinity;
  return { url: r.url, online: age <= PREP_ONLINE_S, last_heartbeat_at: r.last_heartbeat_at };
}

/** The address to send preparation jobs to, or null when no service is available. */
export async function cloudPrepUrl(env: Env): Promise<string | null> {
  if (!secretOk(env)) return null;
  const reg = await registeredPrep(env);
  if (reg && reg.online) return reg.url;
  return cloudPrepConfigured(env) ? env.CLOUD_PREP_URL!.replace(/\/+$/, "") : null;
}

async function requireCloud(env: Env): Promise<string> {
  const url = await cloudPrepUrl(env);
  if (!url) fail(409, "cloud_prep_unavailable", "the preparation service is not running (Analyse traffic: Start the preparation service)");
  return url!;
}

/** POST /api/prep/register and /api/prep/heartbeat (signed by the service): {url, version} — or
 *  {stopping: true} when it stops, so the site shows it as not running at once. */
export async function prepRegister(req: Request, env: Env): Promise<Response> {
  if (!secretOk(env)) fail(409, "cloud_prep_unavailable", "set CLOUD_PREP_SECRET on the control plane first");
  const b = parseBody(await verifySigned(req, env.CLOUD_PREP_SECRET!, 16 * 1024, "preparation service"));
  const t = nowIso();
  if (b.stopping === true) {
    await env.DB.prepare("UPDATE backends SET last_heartbeat_at=NULL WHERE id='prep'").run();
    return json({ ok: true, stopped: true });
  }
  const url = checkUrl(env, b.url);
  await env.DB.prepare(
    `INSERT INTO backends (id,url,mode,gpu_json,creds_json,version,registered_at,last_heartbeat_at) VALUES ('prep',?,'prep',NULL,'{}',?,?,?)
     ON CONFLICT(id) DO UPDATE SET registered_at=CASE WHEN backends.url=excluded.url THEN backends.registered_at ELSE excluded.registered_at END,
       url=excluded.url, version=excluded.version, last_heartbeat_at=excluded.last_heartbeat_at`,
  ).bind(url, String(b.version || "").slice(0, 64), t, t).run();
  return json({ ok: true, url, heartbeat_s: PREP_HEARTBEAT_S, online_for_s: PREP_ONLINE_S });
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

function publicJob(r: Row) {
  return {
    job_id: r.id, status: r.status, source: r.source_kind, filename: r.filename, url: r.url, size_bytes: r.size_bytes,
    phase: r.phase, message: r.message, log: JSON.parse(r.log_json || "[]"), set_name: r.set_name,
    files: r.files_json ? JSON.parse(r.files_json) : [], error: r.error, code: r.error_code,
    created_at: r.created_at, updated_at: r.updated_at, finished_at: r.finished_at,
  };
}

async function finishFailed(env: Env, r: Row, error: string, code: string) {
  const t = nowIso();
  await env.DB.prepare("UPDATE prep_jobs SET status='failed', error=?, error_code=?, updated_at=?, finished_at=? WHERE id=? AND status IN ('uploading','running')")
    .bind(error.slice(0, 600), code.slice(0, 64), t, t, r.id).run();
  if (r.source_kind === "file" && r.filename) await env.STORE.delete([inputKey(r)]).catch(() => {});
}

function publicBase(env: Env): string {
  if (env.PUBLIC_BASE_URL) return env.PUBLIC_BASE_URL.replace(/\/+$/, "");
  if (env.SUPABASE_URL) return `${env.SUPABASE_URL.replace(/\/+$/, "")}/functions/v1/agentmeter`;
  return fail(500, "misconfigured", "set PUBLIC_BASE_URL (this API's public address) for cloud preparation");
}

/** Hand the job to the cloud service: signed, with single-use URLs for its input and outputs. */
async function dispatch(env: Env, r: Row): Promise<void> {
  const opts = JSON.parse(r.options_json);
  const base = await requireCloud(env);
  const ids = await idsModelForJob(env);
  const outputs: Record<string, string> = {};
  for (const n of ids ? ALL_FILES : PREP_FILES) {
    const u = await env.STORE.signedUploadUrl(outKey(r.id, n));
    if (!u) fail(409, "cloud_prep_unavailable", "this storage cannot hand out upload URLs");
    outputs[n] = u!;
  }
  let source: Record<string, unknown>;
  if (r.source_kind === "file") {
    const u = await env.STORE.signedDownloadUrl(inputKey(r), URL_GET_S);
    if (!u) fail(409, "cloud_prep_unavailable", "this storage cannot hand out download URLs");
    source = { kind: "file", url: u };
  } else source = { kind: "url", url: r.url };
  const reportPath = `/api/prep/jobs/${r.id}/events`;
  const body = new TextEncoder().encode(JSON.stringify({
    job_id: r.id, filename: r.filename, source, max_flows: opts.max_flows, other_attack: opts.other_attack,
    limits: opts.limits, max_bytes: opts.max_bytes, outputs, report_url: publicBase(env) + reportPath, report_path: reportPath,
    ...(ids ? { ids } : {}),
  }));
  const ts = String(nowS());
  const sig = await hmacHex(env.CLOUD_PREP_SECRET!, `POST\n/prepare\n${ts}\n${await sha256Hex(body)}`);
  // A free Hugging Face Space that slept answers 502/503 for a few seconds while it wakes: retry.
  let res: Response | null = null, lastErr = "";
  for (let i = 0; i < WAKE_TRIES; i++) {
    if (i) await new Promise((ok) => setTimeout(ok, intVar(env.CLOUD_PREP_RETRY_MS, 5000)));
    try {
      res = await fetch(base + "/prepare", {
        method: "POST", headers: { "content-type": "application/json", "X-AM-Timestamp": ts, "X-AM-Signature": sig }, body,
      });
      if (![502, 503, 504].includes(res.status)) break;
      await res.body?.cancel();
    } catch (e) {
      res = null; lastErr = (e as Error).message;
    }
  }
  if (!res) {
    await finishFailed(env, r, `the cloud preparation service could not be reached: ${lastErr}`, "cloud_prep_unreachable");
    fail(502, "cloud_prep_unreachable", "the cloud preparation service could not be reached");
  }
  if (!res!.ok) {
    const text = (await res!.text()).slice(0, 200);
    await finishFailed(env, r, `the cloud preparation service answered HTTP ${res!.status} ${text}`, "cloud_prep_refused");
    fail(502, "cloud_prep_refused", `the cloud preparation service answered HTTP ${res!.status}`);
  }
  await res!.body?.cancel();
  const t = nowIso();
  await env.DB.prepare("UPDATE prep_jobs SET status='running', phase='queued', message='sent to the cloud', updated_at=? WHERE id=?").bind(t, r.id).run();
}

/** POST /api/prep/jobs — a new cloud preparation for the signed-in user. */
export async function createPrepJob(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  await requireCloud(env);
  publicBase(env);                                    // the service needs somewhere to report to
  const b = await readJson<Record<string, any>>(req);
  const maxFlows = Number(b.max_flows);
  if (!Number.isInteger(maxFlows) || maxFlows < 1 || maxFlows > 500) fail(400, "bad_request", "max_flows: 1 to 500");
  const limits = cleanLimits(b.limits);
  const active = await env.DB.prepare("SELECT COUNT(*) AS n FROM prep_jobs WHERE owner_id=? AND status IN ('uploading','running') AND updated_at > ?")
    .bind(a.user.id, nowIso(Date.now() - STALE_S * 1000)).first<{ n: number }>();
  if ((active?.n || 0) >= MAX_ACTIVE) fail(429, "too_many_preparations", `at most ${MAX_ACTIVE} preparations at a time — wait for one to finish`);
  const maxBytes = await maxUploadBytes(env);
  const id = randomHex(16), t = nowIso();
  let row: Row;
  if (typeof b.url === "string" && b.url) {
    if (!/^https:\/\/[^\s]{4,2000}$/i.test(b.url)) fail(400, "bad_url", "give a direct https:// link");
    row = { id, owner_id: a.user.id, status: "running", source_kind: "url", filename: null, url: b.url, size_bytes: null,
            options_json: JSON.stringify({ max_flows: maxFlows, other_attack: !!b.other_attack, limits,
                                           max_bytes: Math.min(limits.max_url_download_bytes ?? Infinity, intVar(env.CLOUD_PREP_MAX_URL_MB, 2048) * 1024 * 1024) }),
            phase: "queued", message: null, log_json: "[]", set_name: null, files_json: null, error: null, error_code: null,
            created_at: t, updated_at: t, finished_at: null };
  } else {
    const filename = safeName(b.filename);
    const size = Number(b.size);
    if (!Number.isInteger(size) || size < 1) fail(400, "bad_request", "size: the file's size in bytes");
    if (size > maxBytes) fail(413, "too_large", `cloud preparation takes files up to ${Math.round(maxBytes / 2 ** 20)} MB`);
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
    await dispatch(env, row);
    return json({ job_id: id, status: "running" }, 201);
  }
  const upload = await env.STORE.signedUploadUrl(inputKey(row));
  if (!upload) fail(409, "cloud_prep_unavailable", "this storage cannot hand out upload URLs");
  return json({ job_id: id, status: "uploading", upload_url: upload, max_bytes: maxBytes }, 201);
}

/** POST /api/prep/jobs/:id/start — the file is in Storage: start the cloud job. */
export async function startPrepJob(req: Request, env: Env, id: string): Promise<Response> {
  const r = await mine(req, env, id);
  await requireCloud(env);
  if (r.status !== "uploading") fail(409, "not_uploading", `this preparation is ${r.status}`);
  const size = await env.STORE.size(inputKey(r));
  if (size === null) fail(409, "not_uploaded", "the file is not uploaded yet");
  if (size !== r.size_bytes) fail(409, "size_mismatch", `storage holds ${size} bytes, expected ${r.size_bytes}`);
  await dispatch(env, r);
  return json({ job_id: id, status: "running" });
}

/** GET /api/prep/jobs/:id — the job, its progress log and, when done, its prepared files. */
export async function getPrepJob(req: Request, env: Env, id: string): Promise<Response> {
  let r = await mine(req, env, id);
  if ((r.status === "running" || r.status === "uploading") && Date.now() - Date.parse(r.updated_at) > STALE_S * 1000) {
    await finishFailed(env, r, r.status === "uploading" ? "the upload was not finished" : "the cloud preparation stopped answering", "cloud_prep_stalled");
    r = await load(env, id);
  }
  return json(publicJob(r));
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
    return json({ ok: true });
  }
  if (b.failed && typeof b.failed === "object") {
    await finishFailed(env, r, String(b.failed.error || "the cloud preparation failed"), String(b.failed.code || "cloud_prepare_failed"));
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

/** POST /api/prep/wake — New benchmark opened: nudge a sleeping cloud service (free Hugging Face
 *  Space) awake before a dataset is uploaded. Best effort, answers at once. */
export async function wakePrep(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env);
  const base = await cloudPrepUrl(env);
  if (!base) return json({ ok: false, configured: false });
  let up = false;
  try {
    const r = await fetch(base + "/health", { signal: AbortSignal.timeout(4000) });
    up = r.ok;
    await r.body?.cancel();
  } catch { /* still waking: the request itself wakes it */ }
  return json({ ok: true, configured: true, up });
}
