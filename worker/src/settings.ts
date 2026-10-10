// Per-user settings and admin system limits. Both are ALLOW-LISTS: an unknown key is refused,
// so no setting can exist that hides the DEMO banner, the caveats or the "not an endorsement
// as a threat detector" wording (tests/settings.test.ts checks this).
import { requireUser } from "./auth";
import { Env, fail, json, nowIso, readJson } from "./util";

export const DEFAULT_SETTINGS = {
  theme: "system",
  telegram_chat_id: "",
  notify_on: "none",
  default_models: [] as string[],
  default_flows: 50,
  default_other_attack: false,
  weight_presets: [] as { name: string; weights: Record<string, number> }[],
  timezone: "UTC",
};
export type Settings = typeof DEFAULT_SETTINGS;

const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9._-]{1,96}$/;
const WEIGHT_KEYS = ["accuracy", "latency", "vram", "tokens"];

function validTimezone(tz: string): boolean {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}

/** Validate a partial update against the allow-list; returns the merged settings. */
export function mergeSettings(current: Settings, patch: Record<string, unknown>): Settings {
  const out = { ...current };
  for (const [k, v] of Object.entries(patch)) {
    switch (k) {
      case "theme":
        if (!["system", "dark", "light"].includes(v as string)) fail(400, "bad_setting", "theme: system | dark | light");
        out.theme = v as string;
        break;
      case "telegram_chat_id":
        if (typeof v !== "string" || !/^(-?\d{1,20})?$/.test(v)) fail(400, "bad_setting", "telegram_chat_id: digits (a leading - for groups) or empty");
        out.telegram_chat_id = v as string;
        break;
      case "notify_on":
        if (!["none", "done", "failed", "both"].includes(v as string)) fail(400, "bad_setting", "notify_on: none | done | failed | both");
        out.notify_on = v as string;
        break;
      case "default_models":
        if (!Array.isArray(v) || v.length > 2 || !v.every((m) => typeof m === "string" && MODEL_ID.test(m)))
          fail(400, "bad_setting", "default_models: up to 2 model ids (owner/name)");
        out.default_models = v as string[];
        break;
      case "default_flows":
        if (!Number.isInteger(v) || (v as number) < 1 || (v as number) > 500) fail(400, "bad_setting", "default_flows: 1-500");
        out.default_flows = v as number;
        break;
      case "default_other_attack":
        if (typeof v !== "boolean") fail(400, "bad_setting", "default_other_attack: true | false");
        out.default_other_attack = v as boolean;
        break;
      case "weight_presets": {
        if (!Array.isArray(v) || v.length > 10) fail(400, "bad_setting", "weight_presets: at most 10");
        const names = new Set<string>();
        for (const p of v as unknown[]) {
          const pr = p as { name?: unknown; weights?: Record<string, unknown> };
          if (!pr || typeof pr.name !== "string" || !pr.name.trim() || pr.name.length > 40) fail(400, "bad_setting", "preset name: 1-40 characters");
          if (names.has(pr.name as string)) fail(400, "bad_setting", "preset names must be unique");
          names.add(pr.name as string);
          const w = pr.weights || {};
          if (Object.keys(w).some((x) => !WEIGHT_KEYS.includes(x)) || !WEIGHT_KEYS.every((x) => typeof w[x] === "number" && (w[x] as number) >= 0 && (w[x] as number) <= 1))
            fail(400, "bad_setting", "preset weights: accuracy, latency, vram, tokens, each 0-1");
          if (WEIGHT_KEYS.reduce((s, x) => s + (w[x] as number), 0) <= 0) fail(400, "bad_setting", "preset weights must not all be 0");
        }
        out.weight_presets = (v as Settings["weight_presets"]).map((p) => ({ name: p.name.trim(), weights: Object.fromEntries(WEIGHT_KEYS.map((x) => [x, p.weights[x]])) }));
        break;
      }
      case "timezone":
        if (typeof v !== "string" || v.length > 64 || !validTimezone(v)) fail(400, "bad_setting", "timezone: an IANA name such as Asia/Kuala_Lumpur");
        out.timezone = v as string;
        break;
      default:
        fail(400, "unknown_setting", `unknown setting "${k}"`);
    }
  }
  return out;
}

export async function loadSettings(env: Env, userId: string): Promise<Settings> {
  const r = await env.DB.prepare("SELECT json FROM settings WHERE user_id=?").bind(userId).first<{ json: string }>();
  return { ...DEFAULT_SETTINGS, ...(r ? JSON.parse(r.json) : {}) };
}

export async function getSettings(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  return json({ settings: await loadSettings(env, a.user.id) });
}

export async function putSettings(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  const merged = mergeSettings(await loadSettings(env, a.user.id), await readJson(req));
  await env.DB.prepare("INSERT INTO settings (user_id,json,updated_at) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET json=excluded.json, updated_at=excluded.updated_at")
    .bind(a.user.id, JSON.stringify(merged), nowIso()).run();
  return json({ settings: merged });
}

// --- system limits (admin) ---------------------------------------------------------------
export const DEFAULT_LIMITS = {
  sessions_per_hour: 12,
  max_queued_sessions: 4,
  max_upload_mb: 90,            // hard cap 90: Cloudflare (tunnel + Workers) caps a request at 100 MB
  idle_minutes: 30,             // backend auto-shutdown after this long idle (0 = never)
  keep_identification_columns_in_r2: false,
};
export type Limits = typeof DEFAULT_LIMITS;
const LIMIT_RANGES: Record<string, [number, number]> = {
  sessions_per_hour: [1, 1000], max_queued_sessions: [1, 50], max_upload_mb: [1, 90], idle_minutes: [0, 1440],
};

export async function loadLimits(env: Env): Promise<Limits> {
  const rows = await env.DB.prepare("SELECT key, value FROM system_settings").all<{ key: string; value: string }>();
  const out: Record<string, unknown> = { ...DEFAULT_LIMITS };
  for (const r of rows.results) if (r.key in DEFAULT_LIMITS) out[r.key] = JSON.parse(r.value);
  return out as Limits;
}

export function validateLimits(patch: Record<string, unknown>): Partial<Limits> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(patch)) {
    if (k === "keep_identification_columns_in_r2") {
      if (typeof v !== "boolean") fail(400, "bad_limit", `${k}: true | false`);
      out[k] = v;
    } else if (k in LIMIT_RANGES) {
      const [lo, hi] = LIMIT_RANGES[k];
      if (!Number.isInteger(v) || (v as number) < lo || (v as number) > hi) fail(400, "bad_limit", `${k}: integer ${lo}-${hi}`);
      out[k] = v;
    } else fail(400, "unknown_limit", `unknown limit "${k}"`);
  }
  return out as Partial<Limits>;
}

export async function getLimits(req: Request, env: Env): Promise<Response> {
  await requireUser(req, env, "admin");
  return json({ limits: await loadLimits(env) });
}

export async function putLimits(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env, "admin");
  const patch = validateLimits(await readJson(req));
  const stmts = Object.entries(patch).map(([k, v]) =>
    env.DB.prepare("INSERT INTO system_settings (key,value,updated_by,updated_at) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at")
      .bind(k, JSON.stringify(v), a.user.username, nowIso()));
  if (stmts.length) await env.DB.batch(stmts);
  return json({ limits: await loadLimits(env) });
}
