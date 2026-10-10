// Admin: users (max 5, enforced in SQL), credential STATUS ("set" / "not set" — never values).
import { MAX_USERS, User, checkPassword, checkUsername, iterations, publicUser, requireUser } from "./auth";
import { hashPassword, randomHex } from "./crypto";
import { Env, fail, json, nowIso, readJson } from "./util";

export async function createUser(env: Env, username: string, password: string, role: string, mustChange = true) {
  if (!["admin", "user"].includes(role)) fail(400, "bad_role", "role: admin | user");
  const h = await hashPassword(password, env.PASSWORD_PEPPER, iterations(env));
  const id = randomHex(16);
  const now = nowIso();
  // The max-5 rule lives in the INSERT itself, so concurrent creates cannot exceed it.
  const r = await env.DB.prepare(
    `INSERT INTO users (id, username, role, pw_hash, pw_salt, pw_iter, disabled, must_change_pw, created_at, updated_at)
     SELECT ?, ?, ?, ?, ?, ?, 0, ?, ?, ? WHERE (SELECT COUNT(*) FROM users) < ${MAX_USERS}
       AND NOT EXISTS (SELECT 1 FROM users WHERE username = ?)`,
  ).bind(id, username.toLowerCase(), role, h.hash, h.salt, h.iterations, mustChange ? 1 : 0, now, now, username.toLowerCase()).run();
  if (!r.meta.changes) {
    const exists = await env.DB.prepare("SELECT 1 FROM users WHERE username=?").bind(username.toLowerCase()).first();
    if (exists) fail(409, "user_exists", `user "${username}" already exists`);
    fail(409, "max_users", `at most ${MAX_USERS} users (disabled users count; delete one to free a slot)`);
  }
  return (await env.DB.prepare("SELECT * FROM users WHERE id=?").bind(id).first<User>())!;
}

export async function listUsers(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env, "admin");
  const r = await env.DB.prepare("SELECT * FROM users ORDER BY created_at").all<User>();
  return json({ users: r.results.map(publicUser), max_users: MAX_USERS });
}

export async function postUser(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env, "admin");
  const b = await readJson<{ username?: string; password?: string; role?: string }>(req);
  const u = await createUser(env, checkUsername(b.username), checkPassword(b.password), String(b.role || "user"), true);
  return json({ user: publicUser(u) }, 201);
}

async function activeAdmins(env: Env, excludeId?: string): Promise<number> {
  const r = await env.DB.prepare("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0 AND id<>?").bind(excludeId || "").first<{ n: number }>();
  return r?.n || 0;
}

export async function patchUser(req: Request, env: Env, id: string): Promise<Response> {
  const a = await requireUser(req, env, "admin");
  const target = await env.DB.prepare("SELECT * FROM users WHERE id=?").bind(id).first<User>();
  if (!target) fail(404, "not_found", "no such user");
  const b = await readJson<{ disabled?: boolean; role?: string; password?: string }>(req);
  const sets: string[] = [], vals: unknown[] = [];
  const losingAdmin = (b.disabled === true || (b.role !== undefined && b.role !== "admin")) && target!.role === "admin";
  if (losingAdmin && (await activeAdmins(env, id)) < 1) fail(409, "last_admin", "keep at least one active admin");
  if (b.disabled !== undefined) {
    if (typeof b.disabled !== "boolean") fail(400, "bad_request", "disabled: true | false");
    if (id === a.user.id && b.disabled) fail(409, "self", "you cannot disable yourself");
    sets.push("disabled=?"); vals.push(b.disabled ? 1 : 0);
  }
  if (b.role !== undefined) {
    if (!["admin", "user"].includes(b.role)) fail(400, "bad_role", "role: admin | user");
    sets.push("role=?"); vals.push(b.role);
  }
  if (b.password !== undefined) {                    // admin reset: the user must change it at next login
    const h = await hashPassword(checkPassword(b.password), env.PASSWORD_PEPPER, iterations(env));
    sets.push("pw_hash=?", "pw_salt=?", "pw_iter=?", "must_change_pw=1"); vals.push(h.hash, h.salt, h.iterations);
  }
  if (!sets.length) fail(400, "bad_request", "nothing to change (disabled, role, password)");
  sets.push("updated_at=?"); vals.push(nowIso());
  const stmts = [env.DB.prepare(`UPDATE users SET ${sets.join(", ")} WHERE id=?`).bind(...vals, id)];
  if (b.disabled === true || b.password !== undefined)              // end their sessions now
    stmts.push(env.DB.prepare("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL").bind(nowIso(), id));
  await env.DB.batch(stmts);
  const u = await env.DB.prepare("SELECT * FROM users WHERE id=?").bind(id).first<User>();
  return json({ user: publicUser(u!) });
}

export async function deleteUser(req: Request, env: Env, id: string): Promise<Response> {
  const a = await requireUser(req, env, "admin");
  const target = await env.DB.prepare("SELECT * FROM users WHERE id=?").bind(id).first<User>();
  if (!target) fail(404, "not_found", "no such user");
  if (!target!.disabled) fail(409, "not_disabled", "disable the user before deleting (frees a slot of the 5)");
  if (id === a.user.id) fail(409, "self", "you cannot delete yourself");
  await env.DB.batch([
    env.DB.prepare("DELETE FROM auth_sessions WHERE user_id=?").bind(id),
    env.DB.prepare("DELETE FROM settings WHERE user_id=?").bind(id),
    env.DB.prepare("UPDATE sessions SET owner_id=NULL WHERE owner_id=?").bind(id),   // their sessions stay, owner name kept
    env.DB.prepare("DELETE FROM run_grants WHERE user_id=?").bind(id),
    env.DB.prepare("DELETE FROM users WHERE id=?").bind(id),
  ]);
  return json({ ok: true });
}

/** "set" / "not set" only. HF token + Vast key live on the backend (reported as booleans in its
 *  heartbeat); the Telegram bot token is a Worker secret. Values never leave their secret store. */
export async function credentials(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env, "admin");
  const b = await env.DB.prepare("SELECT creds_json, last_heartbeat_at FROM backends WHERE id='default'").first<{ creds_json: string | null; last_heartbeat_at: string | null }>();
  const creds = b?.creds_json ? (JSON.parse(b.creds_json) as Record<string, unknown>) : null;
  const state = (v: unknown) => (creds === null ? "unknown (backend never registered)" : v === true ? "set" : "not set");
  return json({
    credentials: {
      hf_token: { status: state(creds?.hf_token), where: "GPU backend environment (HF_TOKEN)" },
      vast_api_key: { status: state(creds?.vast_api_key), where: "GPU backend environment (VAST_API_KEY)" },
      telegram_bot_token: { status: env.TELEGRAM_BOT_TOKEN ? "set" : "not set", where: "Worker secret (TELEGRAM_BOT_TOKEN)" },
    },
    reported_at: b?.last_heartbeat_at || null,
    note: "Credentials are infrastructure secrets (wrangler / backend env). They are never shown or edited here.",
  });
}
