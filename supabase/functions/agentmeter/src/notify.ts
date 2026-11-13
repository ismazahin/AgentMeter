// Telegram notifications for finished sessions, sent by the control plane: the bot token is a
// control-plane secret (TELEGRAM_BOT_TOKEN, never returned to a browser), the chat id and "notify on"
// are each user's settings. Once per session (sessions.notified_status). Best effort: a
// Telegram failure never fails the backend's upload.
import { requireUser } from "./auth.ts";
import { loadSettings, storeSettings } from "./settings.ts";
import { randomHex } from "./crypto.ts";
import { type Moved, movedLines, movedRuns } from "./trends.ts";
import { type Env, fail, json } from "./util.ts";

const API = (env: Env) => env.TELEGRAM_API || "https://api.telegram.org";

export async function sendTelegram(env: Env, chatId: string, text: string): Promise<boolean> {
  if (!env.TELEGRAM_BOT_TOKEN || !chatId) return false;
  try {
    const r = await fetch(`${API(env)}/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true }),
    });
    return r.ok;
  } catch {
    return false;
  }
}

const WANTS: Record<string, string[]> = { done: ["Done", "Demo"], failed: ["Failed", "Interrupted", "Stopped"],
                                          both: ["Done", "Demo", "Failed", "Interrupted", "Stopped"], none: [] };

/** Call when a session reaches a final status; notifies its owner once, per their settings. */
export async function notifyFinal(env: Env, id: string, status: string, stoppedBy?: string): Promise<boolean> {
  if (!["Done", "Demo", "Failed", "Interrupted", "Stopped"].includes(status) || !env.TELEGRAM_BOT_TOKEN) return false;
  const row = await env.DB.prepare("SELECT owner_id, owner_username, more_efficient, models_json FROM sessions WHERE id=? AND notified_status IS NULL")
    .bind(id).first<{ owner_id: string | null; owner_username: string | null; more_efficient: string | null; models_json: string | null }>();
  if (!row?.owner_id) return false;
  const s = await loadSettings(env, row.owner_id);
  if (!s.telegram_chat_id) return false;
  // an alert (Repeatability rule): a finished run that got worse than its usual range on the same setup
  const moved = status === "Done" ? await movedFor(env, id) : [];
  const alert = s.alert_on_drift && moved.some((m) => m.worse);
  if (!(WANTS[s.notify_on] || []).includes(status) && !alert) return false;
  const claim = await env.DB.prepare("UPDATE sessions SET notified_status=? WHERE id=? AND notified_status IS NULL").bind(status, id).run();
  if (!claim.meta.changes) return false;                       // another request already sent it
  const models = row.models_json ? (JSON.parse(row.models_json) as string[]).join(" vs ") : "";
  const more = status === "Done" && row.more_efficient && row.more_efficient !== "—" ? `\nMore efficient (measured): ${row.more_efficient}` : "";
  const demo = status === "Demo" ? "\nDEMO (mock provider) — not a measurement." : "";
  const stop = status === "Stopped" ? `\nStopped${stoppedBy ? " by " + stoppedBy.slice(0, 32) : ""} before it finished — not scored.` : "";
  const drift = moved.length ? `\n\nAgainst earlier runs on the same setup:\n${movedLines(moved).join("\n")}` : "";
  return sendTelegram(env, s.telegram_chat_id,
    `${alert ? "AgentMeter ALERT: a result moved beyond its usual range" : "AgentMeter"}: session ${id} — ${status}\n${models}${more}${demo}${stop}${drift}` +
    `\nAgentMeter measures LLM efficiency; it is not a threat detector.`);
}

/** The session's results that moved beyond their usual spread against earlier runs on its setup. */
async function movedFor(env: Env, id: string): Promise<Moved[]> {
  const cur = await env.DB.prepare("SELECT summary_json, prepared_set_sha256, owner_id FROM sessions WHERE id=?").bind(id)
    .first<{ summary_json: string | null; prepared_set_sha256: string | null; owner_id: string | null }>();
  if (!cur?.summary_json || !cur.prepared_set_sha256 || !cur.owner_id) return [];
  // only the owner's own earlier runs: the alert never carries another user's results
  const rows = await env.DB.prepare("SELECT summary_json FROM sessions WHERE prepared_set_sha256=? AND owner_id=? AND status='Done' AND deleted_at IS NULL AND id<>? AND summary_json IS NOT NULL")
    .bind(cur.prepared_set_sha256, cur.owner_id, id).all<{ summary_json: string }>();
  try {
    return movedRuns(JSON.parse(cur.summary_json), rows.results.map((r) => JSON.parse(r.summary_json)));
  } catch {
    return [];
  }
}

/** POST /api/settings/telegram-test — a test message to the caller's own chat id. */
export async function telegramTest(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  const s = await loadSettings(env, a.user.id);
  if (!env.TELEGRAM_BOT_TOKEN) fail(409, "not_configured", "the admin has not set the Telegram bot token (control-plane secret TELEGRAM_BOT_TOKEN)");
  if (!s.telegram_chat_id) fail(400, "bad_setting", "set your Telegram chat ID first");
  const ok = await sendTelegram(env, s.telegram_chat_id, "AgentMeter: test notification — your Telegram alerts are working.");
  return json({ ok });
}

// ---- connecting a chat without outside tools -----------------------------------------------
// GET gives the caller a one-time link code and the bot's t.me link (https://t.me/<bot>?start=<code>):
// pressing Start there sends "/start <code>" to the bot. POST then reads the bot's recent updates
// and saves the private chat whose /start carried the caller's own code. The code is per user and
// expires, so a message from anyone else never matches.
const LINK_TTL_MS = 15 * 60 * 1000;
let BOT: { name: string | null; at: number } | null = null;

async function botUsername(env: Env): Promise<string | null> {
  if (BOT && Date.now() - BOT.at < 3600_000) return BOT.name;
  try {
    const r = await fetch(`${API(env)}/bot${env.TELEGRAM_BOT_TOKEN}/getMe`);
    const d = r.ok ? await r.json() as { result?: { username?: string } } : null;
    BOT = { name: d?.result?.username || null, at: Date.now() };
  } catch {
    BOT = { name: null, at: Date.now() };
  }
  return BOT.name;
}

/** GET /api/settings/telegram-link: a fresh one-time code and the bot's Start link. */
export async function telegramLinkStart(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  if (!env.TELEGRAM_BOT_TOKEN) fail(409, "not_configured", "the admin has not set the Telegram bot token (control-plane secret TELEGRAM_BOT_TOKEN)");
  const code = "am" + randomHex(10);
  const s = await loadSettings(env, a.user.id) as Record<string, unknown>;
  await storeSettings(env, a.user.id, { ...s, _tg_link: { code, exp: Date.now() + LINK_TTL_MS } });
  const bot = await botUsername(env);
  return json({ bot, code, url: bot ? `https://t.me/${bot}?start=${code}` : null, expires_in_s: LINK_TTL_MS / 1000 });
}

/** POST /api/settings/telegram-link: find the chat that pressed Start with the caller's code. */
export async function telegramLinkCheck(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  if (!env.TELEGRAM_BOT_TOKEN) fail(409, "not_configured", "the admin has not set the Telegram bot token (control-plane secret TELEGRAM_BOT_TOKEN)");
  const s = await loadSettings(env, a.user.id) as Record<string, unknown>;
  const link = s._tg_link as { code?: string; exp?: number } | undefined;
  if (!link?.code || !link.exp || link.exp < Date.now()) return json({ linked: false, reason: "expired" });
  let updates: { message?: { text?: string; chat?: { id?: number; type?: string; first_name?: string; username?: string } } }[] = [];
  try {
    const r = await fetch(`${API(env)}/bot${env.TELEGRAM_BOT_TOKEN}/getUpdates?allowed_updates=%5B%22message%22%5D&limit=100`);
    if (!r.ok) return json({ linked: false, reason: r.status === 409 ? "webhook" : "telegram_error" });
    updates = ((await r.json()) as { result?: typeof updates }).result || [];
  } catch {
    return json({ linked: false, reason: "telegram_error" });
  }
  const want = `/start ${link.code}`;
  const hit = updates.map((u) => u.message).reverse()
    .find((m) => m?.chat?.type === "private" && m.chat.id != null && (m.text || "").trim() === want);
  if (!hit) return json({ linked: false, reason: "not_found" });
  const chatId = String(hit.chat!.id);
  const { _tg_link: _drop, ...rest } = s;
  await storeSettings(env, a.user.id, { ...rest, telegram_chat_id: chatId });
  return json({ linked: true, chat_id: chatId, name: hit.chat!.first_name || hit.chat!.username || "" });
}
