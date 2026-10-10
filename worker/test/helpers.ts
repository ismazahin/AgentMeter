import { SELF, env } from "cloudflare:test";
import { createUser } from "../src/admin";
import { hmacHex, sha256Hex, signToken } from "../src/crypto";

export const E = env as any;
export const PW = "correct-horse-battery";
let n = 0;
export const uname = (p = "u") => `${p}${Date.now().toString(36)}${(n++).toString(36)}`.slice(0, 30);

export async function api(path: string, opts: { method?: string; body?: unknown; token?: string; raw?: BodyInit; headers?: Record<string, string> } = {}) {
  const headers: Record<string, string> = { ...(opts.headers || {}) };
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`;
  let body: BodyInit | undefined = opts.raw;
  if (opts.body !== undefined) { body = JSON.stringify(opts.body); headers["content-type"] = "application/json"; }
  const r = await SELF.fetch(`http://w${path}`, { method: opts.method || (body ? "POST" : "GET"), headers, body });
  const text = await r.text();
  let data: any = text;
  try { data = text ? JSON.parse(text) : {}; } catch { /* raw */ }
  return { status: r.status, data, headers: r.headers, text };
}

export async function user(role: "admin" | "user" = "user", name = uname(role[0])) {
  await createUser(E, name, PW, role, false);
  const r = await api("/api/auth/login", { body: { username: name, password: PW } });
  if (r.status !== 200) throw new Error("login failed: " + JSON.stringify(r.data));
  return { name, token: r.data.access_token as string, refresh: r.data.refresh_token as string, id: r.data.user.id as string };
}

export async function backendCall(method: string, path: string, body: unknown = {}, opts: { raw?: Uint8Array | string; ts?: number; secret?: string } = {}) {
  const bytes = opts.raw !== undefined ? (typeof opts.raw === "string" ? new TextEncoder().encode(opts.raw) : opts.raw) : new TextEncoder().encode(JSON.stringify(body));
  const ts = String(opts.ts ?? Math.floor(Date.now() / 1000));
  const sig = await hmacHex(opts.secret ?? E.BACKEND_SECRET, `${method}\n${path}\n${ts}\n${await sha256Hex(bytes)}`);
  return api(path, { method, raw: bytes, headers: { "X-AM-Timestamp": ts, "X-AM-Signature": sig, "content-type": "application/octet-stream" } });
}

export const register = (extra: Record<string, unknown> = {}) =>
  backendCall("POST", "/api/backend/register", { url: "http://127.0.0.1:8766", mode: "mock", gpu: null, version: "test",
                                                creds: { hf_token: true, vast_api_key: false }, jobs: [], ...extra });

export const accessToken = (payload: Record<string, unknown>) => signToken(E.ACCESS_TOKEN_SECRET, { typ: "access", ...payload });
