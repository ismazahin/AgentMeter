// Accounts and sessions: login (throttled), refresh rotation (reuse => revoke), logout, password.
import { hashPassword, randomHex, randomToken, sha256Hex, signToken, verifyPassword, verifyToken } from "./crypto";
import { Env, HttpError, clientIp, fail, intVar, json, nowIso, nowS, readJson } from "./util";

export const ACCESS_TTL_S = 15 * 60;
export const REFRESH_TTL_S = 30 * 24 * 3600;
export const LOGIN_MAX_FAILURES = 5;
export const LOGIN_WINDOW_S = 15 * 60;
export const MAX_USERS = 5;

export interface User {
  id: string;
  username: string;
  role: "admin" | "user";
  pw_hash: string;
  pw_salt: string;
  pw_iter: number;
  disabled: number;
  must_change_pw: number;
  created_at: string;
}

export interface Authed {
  user: User;
  sid: string;
}

export const iterations = (env: Env) => Math.max(10000, intVar(env.PBKDF2_ITERATIONS, 40000));
export const publicUser = (u: User) => ({
  id: u.id, username: u.username, role: u.role, disabled: !!u.disabled, must_change_pw: !!u.must_change_pw,
  created_at: u.created_at,
});

const USERNAME = /^[A-Za-z0-9_.-]{3,32}$/;
export function checkUsername(u: unknown): string {
  if (typeof u !== "string" || !USERNAME.test(u)) fail(400, "bad_username", "username: 3-32 letters, digits, _ . -");
  return u as string;
}
export function checkPassword(p: unknown): string {
  if (typeof p !== "string" || p.length < 10 || p.length > 200)
    fail(400, "weak_password", "password: 10 to 200 characters");
  return p as string;
}

// --- throttling (persistent in D1) --------------------------------------------------------
async function failures(env: Env, bucket: string, key: string): Promise<number> {
  const since = nowS() - LOGIN_WINDOW_S;
  const r = await env.DB.prepare("SELECT COALESCE(SUM(count),0) AS n FROM rate_limits WHERE bucket=? AND key=? AND window_start>=?")
    .bind(bucket, key, since).first<{ n: number }>();
  return r?.n || 0;
}
async function addFailure(env: Env, bucket: string, key: string) {
  const w = Math.floor(nowS() / 60) * 60;
  await env.DB.prepare(
    "INSERT INTO rate_limits (bucket,key,window_start,count) VALUES (?,?,?,1) ON CONFLICT(bucket,key,window_start) DO UPDATE SET count=count+1",
  ).bind(bucket, key, w).run();
}

// --- tokens ---------------------------------------------------------------------------------
async function issue(env: Env, user: User, sid?: string) {
  const refresh = randomToken(32);
  const now = nowS();
  if (!sid) {
    sid = randomHex(16);
    await env.DB.prepare(
      "INSERT INTO auth_sessions (id,user_id,refresh_hash,created_at,last_used_at,expires_at) VALUES (?,?,?,?,?,?)",
    ).bind(sid, user.id, await sha256Hex(refresh), nowIso(), nowIso(), nowIso((now + REFRESH_TTL_S) * 1000)).run();
  }
  const access = await signToken(env.ACCESS_TOKEN_SECRET, { typ: "access", sub: user.id, role: user.role, sid, iat: now, exp: now + ACCESS_TTL_S });
  return { access_token: access, refresh_token: refresh, expires_in: ACCESS_TTL_S, user: publicUser(user), sid };
}

export async function login(req: Request, env: Env): Promise<Response> {
  const body = await readJson<{ username?: string; password?: string }>(req);
  const username = String(body.username || "").slice(0, 64).toLowerCase();
  const ip = clientIp(req);
  if ((await failures(env, "login_user", username)) >= LOGIN_MAX_FAILURES ||
      (await failures(env, "login_ip", ip)) >= LOGIN_MAX_FAILURES * 2) {
    fail(429, "too_many_attempts", "too many failed logins — wait 15 minutes", { "Retry-After": String(LOGIN_WINDOW_S) });
  }
  const user = await env.DB.prepare("SELECT * FROM users WHERE username=?").bind(username).first<User>();
  let ok = false;
  if (user) ok = !user.disabled && (await verifyPassword(String(body.password || ""), env.PASSWORD_PEPPER, user));
  else await hashPassword(String(body.password || ""), env.PASSWORD_PEPPER, iterations(env));   // same cost: no username probing by timing
  if (!ok) {
    await addFailure(env, "login_user", username);
    await addFailure(env, "login_ip", ip);
    fail(401, "bad_credentials", "wrong username or password");
  }
  const t = await issue(env, user!);
  return json({ access_token: t.access_token, refresh_token: t.refresh_token, expires_in: t.expires_in, user: t.user });
}

export async function refresh(req: Request, env: Env): Promise<Response> {
  const { refresh_token } = await readJson<{ refresh_token?: string }>(req);
  if (!refresh_token) fail(400, "bad_request", "refresh_token missing");
  const h = await sha256Hex(String(refresh_token));
  const s = await env.DB.prepare(
    "SELECT a.*, u.disabled AS u_disabled FROM auth_sessions a JOIN users u ON u.id=a.user_id WHERE a.refresh_hash=? OR a.prev_hash=?",
  ).bind(h, h).first<{ id: string; user_id: string; refresh_hash: string; expires_at: string; revoked_at: string | null; u_disabled: number }>();
  if (!s || s.revoked_at || s.u_disabled || s.expires_at < nowIso()) fail(401, "session_expired", "please log in again");
  if (s!.refresh_hash !== h) {                    // an already-rotated token came back: likely stolen
    await env.DB.prepare("UPDATE auth_sessions SET revoked_at=? WHERE id=?").bind(nowIso(), s!.id).run();
    fail(401, "session_revoked", "refresh token reuse detected — the session was revoked; please log in again");
  }
  const user = await env.DB.prepare("SELECT * FROM users WHERE id=?").bind(s!.user_id).first<User>();
  const t = await issue(env, user!, s!.id);
  await env.DB.prepare("UPDATE auth_sessions SET prev_hash=refresh_hash, refresh_hash=?, last_used_at=? WHERE id=?")
    .bind(await sha256Hex(t.refresh_token), nowIso(), s!.id).run();
  return json({ access_token: t.access_token, refresh_token: t.refresh_token, expires_in: t.expires_in, user: t.user });
}

/** Bearer access token -> user (one D1 read: revoked session / disabled user end access at once). */
export async function requireUser(req: Request, env: Env, role?: "admin"): Promise<Authed> {
  const m = /^Bearer\s+(\S+)$/.exec(req.headers.get("Authorization") || "");
  if (!m) fail(401, "auth_required", "log in first");
  const p = await verifyToken<{ sub: string; sid: string }>(env.ACCESS_TOKEN_SECRET, m![1], "access");
  if (!p) fail(401, "token_expired", "access token invalid or expired");
  const row = await env.DB.prepare(
    "SELECT u.*, a.revoked_at AS a_revoked FROM auth_sessions a JOIN users u ON u.id=a.user_id WHERE a.id=? AND u.id=?",
  ).bind(p!.sid, p!.sub).first<User & { a_revoked: string | null }>();
  if (!row || row.a_revoked || row.disabled) fail(401, "session_revoked", "session ended — please log in again");
  if (role === "admin" && row!.role !== "admin") fail(403, "forbidden", "admin only");
  return { user: row!, sid: p!.sid };
}

export async function logout(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  await env.DB.prepare("UPDATE auth_sessions SET revoked_at=? WHERE id=?").bind(nowIso(), a.sid).run();
  return json({ ok: true });
}

export async function changePassword(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  const b = await readJson<{ current_password?: string; new_password?: string }>(req);
  if (!(await verifyPassword(String(b.current_password || ""), env.PASSWORD_PEPPER, a.user)))
    fail(403, "bad_credentials", "current password is wrong");
  const pw = checkPassword(b.new_password);
  const h = await hashPassword(pw, env.PASSWORD_PEPPER, iterations(env));
  await env.DB.batch([
    env.DB.prepare("UPDATE users SET pw_hash=?, pw_salt=?, pw_iter=?, must_change_pw=0, updated_at=? WHERE id=?")
      .bind(h.hash, h.salt, h.iterations, nowIso(), a.user.id),
    env.DB.prepare("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND id<>? AND revoked_at IS NULL")
      .bind(nowIso(), a.user.id, a.sid),                     // other devices are logged out
  ]);
  return json({ ok: true });
}

export function errorResponse(e: unknown): Response {
  if (e instanceof HttpError) return json({ error: e.message, code: e.code }, e.status, e.headers);
  console.error(e);
  return json({ error: "internal error", code: "internal" }, 500);
}
