// The IDS gate's model (scripts/train_ids.py), kept in Storage so every newly rented GPU fetches it
// at start-up (scripts/vast_ids.py) instead of having it copied by hand. An admin uploads the three
// files from the site straight to Storage (single-use signed URLs) and commits them; the commit
// checks the metadata and that the .joblib's sha256 is the one the metadata names. The GPU backend
// (HMAC-signed) gets short-lived download URLs.
//
// AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product. The IDS only
// decides which flows the LLM agents triage.
import { requireUser } from "./auth.ts";
import { verifyBackend } from "./backend.ts";
import { sha256Hex } from "./crypto.ts";
import { audit } from "./audit.ts";
import { type Env, fail, json, readJson } from "./util.ts";

export const IDS_FILES = ["ids_rf.joblib", "ids_rf.json", "ids_rf.hashes.npy"] as const;
type IdsFile = typeof IDS_FILES[number];
const REQUIRED: IdsFile[] = ["ids_rf.joblib", "ids_rf.json"];
const CONTENT_TYPE: Record<IdsFile, string> = {
  "ids_rf.joblib": "application/octet-stream", "ids_rf.json": "application/json", "ids_rf.hashes.npy": "application/octet-stream" };
export const IDS_MAX_BYTES = 50 * 1024 * 1024;        // Supabase's free-plan limit per stored file
const DOWNLOAD_S = 900;
const key = (f: IdsFile) => `ids/${f}`;

function fileName(v: unknown): IdsFile {
  const s = String(v ?? "");
  if (!(IDS_FILES as readonly string[]).includes(s)) fail(400, "bad_request", `name: one of ${IDS_FILES.join(", ")}`);
  return s as IdsFile;
}

/** The stored model's metadata summary, or null when none is committed. */
async function storedMeta(env: Env): Promise<Record<string, any> | null> {
  const o = await env.STORE.get(key("ids_rf.json"));
  if (!o) return null;
  try { return await o.json<Record<string, any>>(); } catch { return null; }
}

function summary(m: Record<string, any>, sizes: Record<string, number | null>) {
  const h = (m.metrics || {}).holdout || {};
  return { name: m.name ?? null, version: m.version ?? null, demo: !!m.demo, created_at: m.created_at ?? null,
           sklearn: m.sklearn ?? null, threshold: m.threshold ?? null, threshold_rule: m.threshold_rule ?? null,
           sha256: m.sha256 ?? null, committed_by: m.committed_by ?? null, committed_at: m.committed_at ?? null,
           holdout: { recall: h.recall ?? null, precision: h.precision ?? null, false_positive_rate: h.false_positive_rate ?? null,
                      flagged_share: h.flagged_share ?? null, n: h.n ?? null }, sizes };
}

async function sizes(env: Env): Promise<Record<string, number | null>> {
  const out: Record<string, number | null> = {};
  for (const f of IDS_FILES) out[f] = await env.STORE.size(key(f));
  return out;
}

/** GET /api/admin/ids-model — what is stored (or {model: null}). */
export async function getIdsModel(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env, "admin");
  const m = await storedMeta(env);
  return json({ model: m ? summary(m, await sizes(env)) : null, max_bytes: IDS_MAX_BYTES });
}

/** POST /api/admin/ids-model/upload-url {name, bytes} — a single-use URL to PUT one file to. */
export async function idsUploadUrl(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env, "admin");
  const b = await readJson<{ name?: unknown; bytes?: unknown }>(req, 4096);
  const name = fileName(b.name);
  if (!Number.isInteger(b.bytes) || (b.bytes as number) <= 0 || (b.bytes as number) > IDS_MAX_BYTES)
    fail(413, "too_large", `bytes: 1 to ${IDS_MAX_BYTES} (Supabase's limit per file)`);
  const url = await env.STORE.signedUploadUrl(key(name));
  if (!url) fail(409, "direct_upload_unavailable", "this control plane cannot issue upload URLs");
  return json({ url, key: key(name), content_type: CONTENT_TYPE[name] });
}

/** POST /api/admin/ids-model/commit — check the uploaded files and make them the IDS for new GPUs. */
export async function idsCommit(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env, "admin");
  const sz = await sizes(env);
  for (const f of REQUIRED) if (!sz[f]) fail(409, "not_uploaded", `${f} is not in storage`);
  const m = await storedMeta(env);
  if (!m) fail(400, "bad_model", "ids_rf.json is not valid JSON");
  if (!Array.isArray(m!.features) || !m!.features.length) fail(400, "bad_model", "ids_rf.json: no feature list");
  if (typeof m!.threshold !== "number" || !(m!.threshold >= 0 && m!.threshold <= 1)) fail(400, "bad_model", "ids_rf.json: threshold must be 0..1");
  if (typeof m!.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(m!.sha256)) fail(400, "bad_model", "ids_rf.json: no sha256");
  const o = await env.STORE.get(key("ids_rf.joblib"));
  const bytes = new Uint8Array(await new Response(o!.body).arrayBuffer());
  if (await sha256Hex(bytes) !== m!.sha256)
    fail(409, "sha_mismatch", "ids_rf.joblib does not match ids_rf.json (upload the three files of the same training run)");
  m!.committed_by = a.user.username;
  m!.committed_at = new Date().toISOString();
  await env.STORE.put(key("ids_rf.json"), JSON.stringify(m), "application/json");
  await audit(env, a.user, "ids_model.commit", m!.version ?? null, { sha256: m!.sha256, threshold: m!.threshold, demo: !!m!.demo });
  return json({ model: summary(m!, sz) });
}

/** DELETE /api/admin/ids-model — new GPUs start without an IDS (the demo model only). */
export async function idsDelete(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env, "admin");
  const m = await storedMeta(env);
  await env.STORE.delete(IDS_FILES.map(key));
  await audit(env, a.user, "ids_model.delete", m?.version ?? null);
  return json({ ok: true });
}

/** GET /api/backend/ids-model (signed) — the stored model's metadata and short-lived download URLs. */
export async function backendIdsModel(req: Request, env: Env): Promise<Response> {
  await verifyBackend(req, env, 0);
  const m = await storedMeta(env);
  if (!m || !m.committed_at) fail(404, "no_ids_model", "no IDS model is stored (Admin › IDS model)");
  const files: Record<string, string> = {};
  for (const f of IDS_FILES) {
    if (await env.STORE.size(key(f)) === null) continue;
    const u = await env.STORE.signedDownloadUrl(key(f), DOWNLOAD_S);
    if (!u) fail(409, "direct_download_unavailable", "this control plane cannot issue download URLs");
    files[f] = u!;
  }
  return json({ meta: m, files });
}
