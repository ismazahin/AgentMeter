// Model catalog state (admin): which extra models are offered, which download at start-up, the
// extra models an admin added, and each model's 4-bit verification result. Stored in the database
// (system_settings, key "model_catalog_state") because the GPU backend's disk is wiped on every
// rental; the backend receives it with every register/heartbeat answer (like the limits) and
// reports verification results back (HMAC-signed).
//
// AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product. This only
// decides which models users may pick; every model is quantized to 4-bit NF4 on the backend.
import { requireUser } from "./auth.ts";
import { verifyBackend } from "./backend.ts";
import { type Env, fail, json, nowIso, readJson } from "./util.ts";

export const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9._-]{1,96}$/;
const PREQUANT = /-(gptq|awq|gguf|exl2|4bit|8bit|int4|int8|fp8|bnb)$/i;
const KEY = "model_catalog_state";
const MAX_ADDED = 20;

export type Verified = { status: "ok" | "failed"; at: string; transformers: string | null; gpu: string | null;
                         weights_mb: number | null; seconds: number | null; share_4bit: number | null; error: string | null; by: string | null };
export type ModelState = { models: Record<string, { enabled?: boolean; download_at_start?: boolean; verified?: Verified }>;
                           added: { id: string; note: string }[] };

const empty = (): ModelState => ({ models: {}, added: [] });

export async function loadModelState(env: Env): Promise<ModelState> {
  const r = await env.DB.prepare("SELECT value FROM system_settings WHERE key=?").bind(KEY).first<{ value: string }>();
  if (!r) return empty();
  try {
    const s = JSON.parse(r.value);
    return { models: s.models && typeof s.models === "object" ? s.models : {}, added: Array.isArray(s.added) ? s.added : [] };
  } catch { return empty(); }
}

async function saveModelState(env: Env, st: ModelState, by: string): Promise<void> {
  await env.DB.prepare("INSERT INTO system_settings (key,value,updated_by,updated_at) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at")
    .bind(KEY, JSON.stringify(st), by, nowIso()).run();
}

function checkId(id: unknown): string {
  const s = String(id ?? "");
  if (!MODEL_ID.test(s)) fail(400, "bad_model", `model id must be "owner/name" (got ${JSON.stringify(s).slice(0, 80)})`);
  if (PREQUANT.test(s)) fail(400, "bad_model", `${s} looks pre-quantized — add the original-weights repo; AgentMeter quantizes it to 4-bit NF4 itself`);
  return s;
}

/** GET /api/admin/models — the stored state (the backend's GET /api/models/catalog adds the suitability). */
export async function getAdminModels(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env, "admin");
  return json({ state: await loadModelState(env) });
}

/** PUT /api/admin/models {set?: {id: {enabled?, download_at_start?}}, add?: [{id, note?}], remove?: [id]} */
export async function putAdminModels(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env, "admin");
  const b = await readJson<{ set?: Record<string, { enabled?: unknown; download_at_start?: unknown }>; add?: { id?: unknown; note?: unknown }[]; remove?: unknown[] }>(req);
  const st = await loadModelState(env);
  for (const x of b.add || []) {
    const id = checkId(x?.id);
    if (st.added.some((y) => y.id === id)) continue;
    if (st.added.length >= MAX_ADDED) fail(409, "too_many_models", `at most ${MAX_ADDED} added models`);
    st.added.push({ id, note: String(x?.note ?? "").slice(0, 200) });
    st.models[id] = { ...(st.models[id] || {}), enabled: false };          // added models start disabled
  }
  for (const id of b.remove || []) {
    const s = checkId(id);
    if (!st.added.some((y) => y.id === s)) fail(409, "not_added", `${s} is in configs/model_catalog.yaml — disable it instead`);
    st.added = st.added.filter((y) => y.id !== s);
    delete st.models[s];
  }
  for (const [id, v] of Object.entries(b.set || {})) {
    checkId(id);
    const cur = { ...(st.models[id] || {}) };
    for (const k of ["enabled", "download_at_start"] as const) {
      if (v?.[k] === undefined) continue;
      if (typeof v[k] !== "boolean") fail(400, "bad_request", `${id}.${k}: true | false`);
      cur[k] = v[k] as boolean;
    }
    st.models[id] = cur;
  }
  await saveModelState(env, st, a.user.username);
  return json({ state: st });
}

/** GET /api/backend/model-state (signed) — read at start-up by scripts/vast_models.py. */
export async function backendModelState(req: Request, env: Env): Promise<Response> {
  await verifyBackend(req, env, 0);
  return json({ state: await loadModelState(env) });
}

/** POST /api/backend/models/verified (signed) {model, result} — a 4-bit verification finished. */
export async function backendModelVerified(req: Request, env: Env): Promise<Response> {
  const raw = await verifyBackend(req, env, 16 * 1024);
  let b: any;
  try { b = JSON.parse(new TextDecoder().decode(raw)); } catch { fail(400, "bad_request", "body must be JSON"); }
  const id = checkId(b?.model);
  const r = b?.result || {};
  if (r.status !== "ok" && r.status !== "failed") fail(400, "bad_request", "result.status: ok | failed");
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const str = (v: unknown, n: number) => (v == null ? null : String(v).slice(0, n));
  const st = await loadModelState(env);
  st.models[id] = { ...(st.models[id] || {}), verified: {
    status: r.status, at: nowIso(), transformers: str(r.transformers, 32), gpu: str(r.gpu, 64),
    weights_mb: num(r.weights_mb), seconds: num(r.seconds), share_4bit: num(r.share_4bit), error: str(r.error, 400), by: str(r.by, 32) } };
  await saveModelState(env, st, "backend");
  return json({ ok: true });
}
