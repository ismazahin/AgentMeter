// Small HTTP helpers shared by every route.
export interface Env {
  DB: D1Database;
  R2: R2Bucket;
  ACCESS_TOKEN_SECRET: string;
  RUN_TOKEN_SECRET: string;
  BACKEND_SECRET: string;
  PASSWORD_PEPPER: string;
  TELEGRAM_BOT_TOKEN?: string;
  ALLOWED_ORIGINS?: string;
  PBKDF2_ITERATIONS?: string;
  BACKEND_OFFLINE_AFTER_S?: string;
  RESULTS_R2_THRESHOLD?: string;
  ALLOW_HTTP_BACKEND?: string;
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
