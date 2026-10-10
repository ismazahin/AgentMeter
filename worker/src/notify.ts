// Telegram notifications for finished sessions, sent by the control plane: the bot token is a
// Worker secret (TELEGRAM_BOT_TOKEN, never returned to a browser), the chat id and "notify on"
// are each user's settings. Once per session (sessions.notified_status). Best effort: a
// Telegram failure never fails the backend's upload.
import { requireUser } from "./auth";
import { loadSettings } from "./settings";
import { Env, fail, json } from "./util";

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

const WANTS: Record<string, string[]> = { done: ["Done", "Demo"], failed: ["Failed", "Interrupted"], both: ["Done", "Demo", "Failed", "Interrupted"], none: [] };

/** Call when a session reaches a final status; notifies its owner once, per their settings. */
export async function notifyFinal(env: Env, id: string, status: string): Promise<boolean> {
  if (!["Done", "Demo", "Failed", "Interrupted"].includes(status) || !env.TELEGRAM_BOT_TOKEN) return false;
  const row = await env.DB.prepare("SELECT owner_id, owner_username, more_efficient, models_json FROM sessions WHERE id=? AND notified_status IS NULL")
    .bind(id).first<{ owner_id: string | null; owner_username: string | null; more_efficient: string | null; models_json: string | null }>();
  if (!row?.owner_id) return false;
  const s = await loadSettings(env, row.owner_id);
  if (!(WANTS[s.notify_on] || []).includes(status) || !s.telegram_chat_id) return false;
  const claim = await env.DB.prepare("UPDATE sessions SET notified_status=? WHERE id=? AND notified_status IS NULL").bind(status, id).run();
  if (!claim.meta.changes) return false;                       // another request already sent it
  const models = row.models_json ? (JSON.parse(row.models_json) as string[]).join(" vs ") : "";
  const more = status === "Done" && row.more_efficient && row.more_efficient !== "—" ? `\nMore efficient (measured): ${row.more_efficient}` : "";
  const demo = status === "Demo" ? "\nDEMO (mock provider) — not a measurement." : "";
  return sendTelegram(env, s.telegram_chat_id,
    `AgentMeter: session ${id} — ${status}\n${models}${more}${demo}\nAgentMeter measures LLM efficiency; it is not a threat detector.`);
}

/** POST /api/settings/telegram-test — a test message to the caller's own chat id. */
export async function telegramTest(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  const s = await loadSettings(env, a.user.id);
  if (!env.TELEGRAM_BOT_TOKEN) fail(409, "not_configured", "the admin has not set the Telegram bot token (Worker secret TELEGRAM_BOT_TOKEN)");
  if (!s.telegram_chat_id) fail(400, "bad_setting", "set your Telegram chat ID first");
  const ok = await sendTelegram(env, s.telegram_chat_id, "AgentMeter: test notification — your Telegram alerts are working.");
  return json({ ok });
}
