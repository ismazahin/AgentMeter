// Data sets: names and removals. A data set has no row of its own — the web app builds the Data sets
// page from the sessions and the GPU server's prepared sets — so the datasets table records only what
// users changed. Rename and remove: the set's owner (whoever ran the first session on it) or an admin.
// A set nobody has benchmarked yet has no session here to name its owner; the GPU server checks who
// prepared it before it deletes the files (POST /api/prepared/<kind>/<name>/delete, a delete_set token).
import { requireUser } from "./auth.ts";
import { audit } from "./audit.ts";
import { deletePrepOutputs, prepOwner } from "./prep.ts";
import { type Env, fail, json, nowIso, readJson } from "./util.ts";

const KEY = /^([0-9a-f]{64}|name:(csv_runs|pcap_runs)\/[A-Za-z0-9_.-]{1,80})$/;
const SET_ID = /^(csv_runs|pcap_runs)\/[A-Za-z0-9_.-]{1,80}$/;
export const DS_LABEL_MAX = 80;

interface Row { key: string; set_id: string | null; label: string | null; deleted_at: string | null }
type User = { id: string; username: string; role: string };

/** GET /api/datasets — every user: {datasets: [{key, set, label, deleted_at}]}. */
export async function listDatasets(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env);
  const r = await env.DB.prepare("SELECT key, set_id, label, deleted_at FROM datasets ORDER BY key").all<Row>();
  return json({ datasets: r.results.map((x) => ({ key: x.key, set: x.set_id, label: x.label ?? "", deleted_at: x.deleted_at })) });
}

async function readKey(req: Request): Promise<{ key: string; set: string | null; label?: unknown }> {
  const b = await readJson<{ key?: unknown; set?: unknown; label?: unknown }>(req, 4096);
  const key = String(b.key ?? ""), set = b.set == null || b.set === "" ? null : String(b.set);
  if (!KEY.test(key)) fail(400, "bad_request", "key: the data set's fingerprint (64 hex) or name:<kind>/<name>");
  if (set !== null && !SET_ID.test(set)) fail(400, "bad_request", "set: csv_runs/<name> or pcap_runs/<name>");
  return { key, set, label: b.label };
}

/** The owner of a data set: whoever ran its first finished session (Done or Demo — the sessions the
 *  Data sets page lists, oldest first by when they finished), found by the KEY alone: the fingerprint,
 *  or the set name of a name:<kind>/<name> key. Never by a caller-supplied set name, which would let
 *  anyone name a set of their own and so "own" another set. null: nobody has benchmarked it yet. */
export async function datasetOwner(env: Env, key: string): Promise<string | null> {
  const byName = key.startsWith("name:");
  const r = await env.DB.prepare(
    `SELECT owner_id FROM sessions WHERE owner_id IS NOT NULL AND deleted_at IS NULL AND status IN ('Done','Demo')
       AND ${byName ? "prepared_set" : "prepared_set_sha256"}=? ORDER BY COALESCE(finished_at, created_at) ASC, id ASC LIMIT 1`,
  ).bind(byName ? key.slice(5) : key).first<{ owner_id: string }>();
  // nobody has benchmarked it yet: the person who uploaded it for a traffic analysis (Analyse traffic)
  return r ? r.owner_id : await prepOwner(env, key);
}

/** The owner or an admin. No session yet: not checked here (see the header). */
async function checkOwner(env: Env, user: User, key: string, what: string): Promise<void> {
  if (user.role === "admin") return;
  const owner = await datasetOwner(env, key);
  if (owner && owner !== user.id) fail(403, "forbidden", `only the person who first benchmarked this data set (or an admin) can ${what} it`);
}

async function upsert(env: Env, key: string, set: string | null, user: User, label: string | null, deleted: boolean): Promise<void> {
  const now = nowIso();
  await env.DB.prepare(
    `INSERT INTO datasets (key, set_id, label, deleted_at, deleted_by, updated_at, updated_by) VALUES (?,?,?,?,?,?,?)
     ON CONFLICT (key) DO UPDATE SET set_id=COALESCE(EXCLUDED.set_id, datasets.set_id), label=COALESCE(EXCLUDED.label, datasets.label),
       deleted_at=COALESCE(EXCLUDED.deleted_at, datasets.deleted_at), deleted_by=COALESCE(EXCLUDED.deleted_by, datasets.deleted_by),
       updated_at=EXCLUDED.updated_at, updated_by=EXCLUDED.updated_by`,
  ).bind(key, set, label, deleted ? now : null, deleted ? user.username : null, now, user.username).run();
}

/** POST /api/datasets/rename {key, set?, label} — "" label = back to the prepared set's own name. */
export async function renameDataset(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  const b = await readKey(req);
  if (typeof b.label !== "string") fail(400, "bad_request", "label must be text");
  const label = (b.label as string).trim().replace(/\s+/g, " ");
  if (label.length > DS_LABEL_MAX) fail(400, "bad_request", `a data set name is limited to ${DS_LABEL_MAX} characters`);
  await checkOwner(env, a.user, b.key, "rename");
  await upsert(env, b.key, b.set, a.user, label, false);
  await audit(env, a.user, "dataset.rename", b.key, { set: b.set, label });
  return json({ key: b.key, set: b.set, label });
}

/** POST /api/datasets/delete {key, set?} — removes the set from Data sets; its sessions keep their results. */
export async function deleteDataset(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  const b = await readKey(req);
  await checkOwner(env, a.user, b.key, "delete");
  await upsert(env, b.key, b.set, a.user, null, true);
  // the traffic analysis's stored files go too, the owner-only address file included
  const removed = await deletePrepOutputs(env, b.key);
  await audit(env, a.user, "dataset.delete", b.key, { set: b.set, analysis_jobs: removed.jobs,
                                                       identity_file_deleted: removed.identity_file_deleted });
  return json({ ok: true, key: b.key });
}
