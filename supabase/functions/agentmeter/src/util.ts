// Small HTTP helpers shared by every route.
import type { Db } from "./db.ts";
import type { ObjectStore } from "./store.ts";

export interface Env {
  DB: Db;                          // PostgreSQL (db.ts)
  STORE: ObjectStore;              // Supabase Storage, private bucket (store.ts)
  ACCESS_TOKEN_SECRET: string;
  RUN_TOKEN_SECRET: string;
  BACKEND_SECRET: string;
  PASSWORD_PEPPER: string;
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_API?: string;
  ALLOWED_ORIGINS?: string;
  PBKDF2_ITERATIONS?: string;
  BACKEND_OFFLINE_AFTER_S?: string;
  RESULTS_INLINE_MAX?: string;    // bytes of session_results.json kept in the row (larger: Storage)
  ALLOW_HTTP_BACKEND?: string;
  CLOUD_PREP_URL?: string;         // the cloud preparation service (cloudprep/, Google Cloud Run)
  CLOUD_PREP_SECRET?: string;      // signs jobs to it and its reports back (the same value on both)
  CLOUD_PREP_MAX_MB?: string;      // largest upload prepared in the cloud (default 50: a Storage file)
  CLOUD_PREP_RETRY_MS?: string;    // wait between attempts while the service wakes (default 5000)
  CLOUD_PREP_MAX_URL_MB?: string;  // largest linked file the cloud downloads (default 2048: it is held in memory)
  PUBLIC_BASE_URL?: string;        // this API's public URL (default: SUPABASE_URL + /functions/v1/agentmeter)
  SUPABASE_URL?: string;
}

export class HttpError extends Error {
  constructor(public status: number, public code: string, message: string, public headers: Record<string, string> = {}) {
    super(message);
  }
}

export const fail = (status: number, code: string, message: string, headers: Record<string, string> = {}): never => {
  throw new HttpError(status, code, message, headers);
};

export function json(data: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers },
  });
}

export const nowIso = (ms = Date.now()) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
export const nowS = () => Math.floor(Date.now() / 1000);

export async function readJson<T = Record<string, unknown>>(req: Request, maxBytes = 64 * 1024): Promise<T> {
  const len = Number(req.headers.get("content-length") || "0");
  if (len > maxBytes) fail(413, "too_large", `request body over ${maxBytes} bytes`);
  const text = await req.text();
  if (text.length > maxBytes) fail(413, "too_large", `request body over ${maxBytes} bytes`);
  if (!text) return {} as T;
  try {
    const v = JSON.parse(text);
    if (v === null || typeof v !== "object" || Array.isArray(v)) fail(400, "bad_request", "body must be a JSON object");
    return v as T;
  } catch (e) {
    if (e instanceof HttpError) throw e;
    return fail(400, "bad_request", "body is not valid JSON");
  }
}

export function clientIp(req: Request): string {
  return req.headers.get("CF-Connecting-IP") || req.headers.get("X-Forwarded-For")?.split(",")[0].trim() || "local";
}

export function corsHeaders(env: Env, req: Request): Record<string, string> {
  const origin = req.headers.get("Origin");
  if (!origin) return {};
  const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim().replace(/\/+$/, "")).filter(Boolean);
  if (!allowed.includes(origin.replace(/\/+$/, ""))) return {};
  return {
    "Access-Control-Allow-Origin": origin,
    Vary: "Origin",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Expose-Headers": "Content-Disposition, Retry-After",
    "Access-Control-Max-Age": "600",
  };
}

export const intVar = (v: string | undefined, d: number) => {
  const n = parseInt(v || "", 10);
  return Number.isFinite(n) ? n : d;
};

/** Cloud data preparation is on when the service's URL and a long enough shared secret are set. */
export const cloudPrepConfigured = (env: Env) =>
  !!env.CLOUD_PREP_URL && /^https?:\/\//.test(env.CLOUD_PREP_URL) && (env.CLOUD_PREP_SECRET || "").length >= 16;
