// The audit log: one row per sensitive action (who, what, on what, when). Writing is best effort —
// an audit failure never blocks the action itself — and never stores a password or a token.
import type { Env } from "./util.ts";
import { json, nowIso, intVar } from "./util.ts";
import { requireUser } from "./auth.ts";

export interface Actor { id?: string | null; username?: string | null }

export async function audit(env: Env, actor: Actor | null, action: string, target: string | null, detail?: Record<string, unknown>): Promise<void> {
  try {
    await env.DB.prepare("INSERT INTO audit_log (at, actor_id, actor_username, action, target, detail_json) VALUES (?,?,?,?,?,?)")
      .bind(nowIso(), actor?.id ?? null, actor?.username ?? null, action, target, detail ? JSON.stringify(detail).slice(0, 4000) : null).run();
  } catch (e) {
    console.warn("audit log write failed:", (e as Error).message);
  }
}

/** GET /api/admin/audit?limit=100&before=<id> — newest first (admins only). */
export async function getAudit(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env, "admin");
  const q = new URL(req.url).searchParams;
  const limit = Math.max(1, Math.min(500, intVar(q.get("limit") || "", 100)));
  const before = intVar(q.get("before") || "", 0);
  const r = await env.DB.prepare(
    `SELECT id, at, actor_username, action, target, detail_json FROM audit_log ${before > 0 ? "WHERE id < ?" : ""} ORDER BY id DESC LIMIT ?`,
  ).bind(...(before > 0 ? [before, limit] : [limit])).all<{ id: number; at: string; actor_username: string | null; action: string; target: string | null; detail_json: string | null }>();
  const rows = r.results.map((x) => ({ id: Number(x.id), at: x.at, actor: x.actor_username, action: x.action, target: x.target,
                                       detail: x.detail_json ? JSON.parse(x.detail_json) : null }));
  return json({ entries: rows, next_before: rows.length === limit ? rows[rows.length - 1].id : null });
}
