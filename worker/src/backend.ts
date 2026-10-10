// The GPU backend's side: HMAC-signed registration, heartbeat and job reports. Users only READ
// the current backend URL/state. Offline = no heartbeat for BACKEND_OFFLINE_AFTER_S.
import { requireUser } from "./auth";
import { hmacHex, sha256Hex, timingSafeEqual } from "./crypto";
import { loadLimits } from "./settings";
import { Env, fail, intVar, json, nowIso, nowS } from "./util";

export const HEARTBEAT_S = 60;
export const MAX_SKEW_S = 120;

/** Verify X-AM-Timestamp / X-AM-Signature = hex HMAC(BACKEND_SECRET, method\npath\nts\nsha256(body)).
 *  Returns the raw body bytes. Writes are idempotent, so a replay inside the window changes nothing. */
export async function verifyBackend(req: Request, env: Env, maxBytes = 30 * 1024 * 1024): Promise<Uint8Array> {
  const ts = req.headers.get("X-AM-Timestamp") || "";
  const sig = req.headers.get("X-AM-Signature") || "";
  const t = parseInt(ts, 10);
  if (!Number.isFinite(t) || Math.abs(nowS() - t) > MAX_SKEW_S) fail(401, "bad_timestamp", "missing or stale X-AM-Timestamp");
  const len = Number(req.headers.get("content-length") || "0");
  if (len > maxBytes) fail(413, "too_large", "body too large");
  const body = new Uint8Array(await req.arrayBuffer());
  if (body.length > maxBytes) fail(413, "too_large", "body too large");
  const path = new URL(req.url).pathname;
  const expect = await hmacHex(env.BACKEND_SECRET, `${req.method}\n${path}\n${ts}\n${await sha256Hex(body)}`);
  if (!timingSafeEqual(expect, sig.toLowerCase())) fail(401, "bad_signature", "backend signature invalid");
  return body;
}

export const parseBody = (b: Uint8Array): Record<string, any> => {
  try {
    const v = JSON.parse(new TextDecoder().decode(b));
    if (v && typeof v === "object" && !Array.isArray(v)) return v;
  } catch { /* fall through */ }
  return fail(400, "bad_request", "body must be a JSON object");
};

function checkUrl(env: Env, url: unknown): string {
  if (typeof url !== "string" || url.length > 300) fail(400, "bad_url", "url missing");
  let u: URL;
  try {
    u = new URL(url as string);
  } catch {
    return fail(400, "bad_url", "url is not a URL");
  }
  const devHttp = env.ALLOW_HTTP_BACKEND === "1" && u.protocol === "http:" && ["127.0.0.1", "localhost"].includes(u.hostname);
  if (u.protocol !== "https:" && !devHttp) fail(400, "bad_url", "backend url must be https (quick or named Cloudflare tunnel)");
  return u.origin;
}

const STATUS = (s: string) => ({ queued: "Running", running: "Running", done: "Done", failed: "Failed", interrupted: "Interrupted" } as Record<string, string>)[s] || "Running";

async function reconcile(env: Env, jobs: unknown) {
  if (!Array.isArray(jobs)) return;
  const known = new Map<string, string>();
  for (const j of jobs as { job_id?: string; status?: string }[]) if (j && typeof j.job_id === "string") known.set(j.job_id, String(j.status || ""));
  const open = await env.DB.prepare("SELECT id FROM sessions WHERE status='Running' AND deleted_at IS NULL").all<{ id: string }>();
  const stmts = [];
  for (const r of open.results) {
    const st = known.get(r.id);
    if (st === undefined || st === "interrupted" || st === "failed")
      stmts.push(env.DB.prepare("UPDATE sessions SET status=?, updated_at=? WHERE id=?").bind(st === "failed" ? "Failed" : "Interrupted", nowIso(), r.id));
  }
  if (stmts.length) await env.DB.batch(stmts);
}

export async function register(req: Request, env: Env): Promise<Response> {
  const b = parseBody(await verifyBackend(req, env, 256 * 1024));
  const url = checkUrl(env, b.url);
  const creds = { hf_token: b.creds?.hf_token === true, vast_api_key: b.creds?.vast_api_key === true };
  await env.DB.prepare(
    `INSERT INTO backends (id,url,mode,gpu_json,creds_json,version,registered_at,last_heartbeat_at) VALUES ('default',?,?,?,?,?,?,?)
     ON CONFLICT(id) DO UPDATE SET url=excluded.url, mode=excluded.mode, gpu_json=excluded.gpu_json, creds_json=excluded.creds_json,
       version=excluded.version, registered_at=excluded.registered_at, last_heartbeat_at=excluded.last_heartbeat_at`,
  ).bind(url, String(b.mode || "unknown").slice(0, 16), JSON.stringify(b.gpu ?? null), JSON.stringify(creds),
         String(b.version || "").slice(0, 64), nowIso(), nowIso()).run();
  await reconcile(env, b.jobs);
  return json({ ok: true, url, heartbeat_s: HEARTBEAT_S, limits: await loadLimits(env) });
}

export async function heartbeat(req: Request, env: Env): Promise<Response> {
  const b = parseBody(await verifyBackend(req, env, 256 * 1024));
  const row = await env.DB.prepare("SELECT url FROM backends WHERE id='default'").first<{ url: string }>();
  if (!row) fail(409, "not_registered", "register first");
  const url = b.url ? checkUrl(env, b.url) : row!.url;
  await env.DB.prepare("UPDATE backends SET last_heartbeat_at=?, url=? WHERE id='default'").bind(nowIso(), url).run();
  if (b.jobs) await reconcile(env, b.jobs);
  return json({ ok: true, heartbeat_s: HEARTBEAT_S, limits: await loadLimits(env) });
}

export async function backendState(env: Env) {
  const r = await env.DB.prepare("SELECT * FROM backends WHERE id='default'").first<{ url: string; mode: string; gpu_json: string; version: string; last_heartbeat_at: string; registered_at: string }>();
  const after = intVar(env.BACKEND_OFFLINE_AFTER_S, 3 * HEARTBEAT_S);
  const age = r?.last_heartbeat_at ? (Date.now() - Date.parse(r.last_heartbeat_at)) / 1000 : Infinity;
  const online = !!r && age <= after;
  return {
    online, url: online ? r!.url : null, mode: r?.mode || null, gpu: r?.gpu_json ? JSON.parse(r.gpu_json) : null,
    version: r?.version || null, last_heartbeat_at: r?.last_heartbeat_at || null, registered_at: r?.registered_at || null,
    offline_after_s: after,
  };
}

export async function getBackend(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env);
  return json(await backendState(env));
}

export { STATUS as mapJobStatus };
