import { afterEach, describe, expect, it, vi } from "vitest";
import FIX from "./fixtures/parity.json" with { type: "json" };
import { api, backendCall, register, user } from "./helpers.ts";

const F = FIX as any;
const SID = "job_20261010_100000_" + "e".repeat(32);

function spyTelegram() {
  const sent: { url: string; body: any }[] = [];
  const real = globalThis.fetch;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input: any, init?: any) => {
    const url = typeof input === "string" ? input : input.url;
    if (url.startsWith("https://telegram.test/")) {
      sent.push({ url, body: JSON.parse(init.body) });
      return new Response('{"ok":true}', { status: 200 });
    }
    return real(input, init);
  });
  return sent;
}
afterEach(() => vi.restoreAllMocks());

async function ownedSession(u: { token: string }) {
  await register();
  const g = await api("/api/runs/authorize", { body: { kind: "benchmark", run: "csv_runs/x_1" }, token: u.token });
  await backendCall("POST", "/api/backend/jobs", { job_id: SID, kind: "benchmark", status: "running", jti: g.data.jti, models: ["m/a", "m/b"] });
}

describe("Telegram notifications (sent by the control plane, per user settings)", () => {
  it("notifies the owner once when the session is done; the bot token never reaches a browser", async () => {
    const sent = spyTelegram();
    const u = await user();
    await api("/api/settings", { method: "PUT", token: u.token, body: { telegram_chat_id: "12345", notify_on: "done" } });
    await ownedSession(u);
    await backendCall("POST", `/api/backend/jobs/${SID}/status`, { status: "done" });
    expect(sent).toHaveLength(0);                                          // waits for the summary
    const s = { ...F.summaries[0], session_id: SID, job_id: SID };
    await backendCall("PUT", `/api/backend/sessions/${SID}/summary`, { summary: s });
    await backendCall("PUT", `/api/backend/sessions/${SID}/summary`, { summary: s });   // re-upload: no second message
    expect(sent).toHaveLength(1);
    expect(sent[0].url).toBe("https://telegram.test/bottest-bot-token-SECRET/sendMessage");
    expect(sent[0].body.chat_id).toBe("12345");
    expect(sent[0].body.text).toMatch(new RegExp(`${SID} — Done`));
    expect(sent[0].body.text).toMatch(/not a threat detector/);
    const me = await api("/api/settings", { token: u.token });
    expect(me.text).not.toMatch(/test-bot-token/);
  });

  it("respects notify_on: failed only / none", async () => {
    const sent = spyTelegram();
    const u = await user();
    await api("/api/settings", { method: "PUT", token: u.token, body: { telegram_chat_id: "777", notify_on: "failed" } });
    await ownedSession(u);
    await backendCall("POST", `/api/backend/jobs/${SID}/status`, { status: "failed" });
    expect(sent.map((x) => x.body.text)).toEqual([expect.stringMatching(/Failed/)]);
    const v = await user();
    await api("/api/settings", { method: "PUT", token: v.token, body: { telegram_chat_id: "888", notify_on: "none" } });
    const t = await api("/api/settings/telegram-test", { method: "POST", token: v.token });
    expect(t.data.ok).toBe(true);
    expect(sent.at(-1)!.body.chat_id).toBe("888");
    const w = await user();
    expect((await api("/api/settings/telegram-test", { method: "POST", token: w.token })).data.code).toBe("bad_setting");
  });

  it("a stopped session tells its owner who stopped it (notify on failed)", async () => {
    const sent = spyTelegram();
    const u = await user();
    await api("/api/settings", { method: "PUT", token: u.token, body: { telegram_chat_id: "999", notify_on: "failed" } });
    await ownedSession(u);
    await backendCall("POST", `/api/backend/jobs/${SID}/status`, { status: "stopped", stopped_by: "olivia" });
    expect(sent.map((x) => x.body.text)).toEqual([expect.stringMatching(/Stopped[\s\S]*Stopped by olivia[\s\S]*not scored/)]);
    const s = await api(`/api/sessions/${SID}`, { token: u.token });
    expect((s.data.session || s.data).status).toBe("Stopped");
  });

  it("connects a chat without outside tools: a one-time Start code, matched in the bot's updates", async () => {
    const updates: any[] = [];
    const real = globalThis.fetch;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input: any, init?: any) => {
      const url = typeof input === "string" ? input : input.url;
      if (url.startsWith("https://telegram.test/")) {
        if (url.includes("/getMe")) return new Response('{"ok":true,"result":{"username":"agentmeter_test_bot"}}');
        if (url.includes("/getUpdates")) return new Response(JSON.stringify({ ok: true, result: updates }));
        return new Response('{"ok":true}');
      }
      return real(input, init);
    });
    const u = await user(), v = await user();
    const g = await api("/api/settings/telegram-link", { token: u.token });
    expect(g.data.url).toBe(`https://t.me/agentmeter_test_bot?start=${g.data.code}`);
    expect((await api("/api/settings", { token: u.token })).text).not.toMatch(/_tg_link|am[0-9a-f]{20}/);   // the code stays internal
    // nothing pressed yet, then someone else's /start (no code / another code) never matches
    expect((await api("/api/settings/telegram-link", { method: "POST", token: u.token })).data).toEqual({ linked: false, reason: "not_found" });
    updates.push({ message: { text: "/start", chat: { id: 111, type: "private" } } },
                 { message: { text: "/start amdeadbeef00", chat: { id: 222, type: "private" } } });
    expect((await api("/api/settings/telegram-link", { method: "POST", token: u.token })).data.linked).toBe(false);
    // the caller presses Start with their own code
    updates.push({ message: { text: `/start ${g.data.code}`, chat: { id: 4242, type: "private", first_name: "Isma" } } });
    const c = await api("/api/settings/telegram-link", { method: "POST", token: u.token });
    expect(c.data).toEqual({ linked: true, chat_id: "4242", name: "Isma" });
    expect((await api("/api/settings", { token: u.token })).data.settings.telegram_chat_id).toBe("4242");
    // the code is used up, and another user's check never picks up this chat
    expect((await api("/api/settings/telegram-link", { method: "POST", token: u.token })).data.reason).toBe("expired");
    expect((await api("/api/settings/telegram-link", { method: "POST", token: v.token })).data.reason).toBe("expired");
    expect((await api("/api/settings", { token: v.token })).data.settings.telegram_chat_id).toBe("");
    // the internal key cannot be set through the normal settings route
    expect((await api("/api/settings", { method: "PUT", token: u.token, body: { _tg_link: { code: "x" } } })).data.code).toBe("unknown_setting");
  });
});
