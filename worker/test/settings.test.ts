import { describe, expect, it } from "vitest";
import { DEFAULT_LIMITS, DEFAULT_SETTINGS } from "../src/settings";
import { E, api, register, user } from "./helpers";

describe("per-user settings", () => {
  it("defaults, update, persist, and are per user", async () => {
    const a = await user(), b = await user();
    expect((await api("/api/settings", { token: a.token })).data.settings).toEqual(DEFAULT_SETTINGS);
    const put = await api("/api/settings", { method: "PUT", token: a.token, body: {
      theme: "dark", telegram_chat_id: "-1001234567", notify_on: "both", default_models: ["Qwen/Qwen2.5-7B-Instruct"],
      default_flows: 25, default_other_attack: true, timezone: "Asia/Kuala_Lumpur",
      weight_presets: [{ name: "speed first", weights: { accuracy: 0.1, latency: 0.5, vram: 0.2, tokens: 0.2 } }] } });
    expect(put.status).toBe(200);
    const got = (await api("/api/settings", { token: a.token })).data.settings;
    expect(got.theme).toBe("dark");
    expect(got.weight_presets[0].name).toBe("speed first");
    expect(got.timezone).toBe("Asia/Kuala_Lumpur");
    expect((await api("/api/settings", { token: b.token })).data.settings.theme).toBe("system");
  });

  it("validates every field", async () => {
    const a = await user();
    const bad: Record<string, unknown>[] = [
      { theme: "neon" }, { notify_on: "sometimes" }, { telegram_chat_id: "abc" }, { default_models: ["a", "b", "c"] },
      { default_models: ["not a model"] }, { default_flows: 0 }, { default_flows: 501 }, { default_other_attack: "yes" },
      { timezone: "Mars/Olympus" }, { weight_presets: [{ name: "x", weights: { accuracy: 0, latency: 0, vram: 0, tokens: 0 } }] },
      { weight_presets: [{ name: "x", weights: { accuracy: 2, latency: 0, vram: 0, tokens: 0 } }] },
      { weight_presets: [{ name: "x", weights: { accuracy: 0.5, latency: 0.5, vram: 0, tokens: 0, cost: 1 } }] },
    ];
    for (const body of bad) {
      const r = await api("/api/settings", { method: "PUT", token: a.token, body });
      expect([r.status, JSON.stringify(body)]).toEqual([400, JSON.stringify(body)]);
    }
  });

  it("no setting can hide the DEMO banner, the caveats or the endorsement wording (allow-list)", async () => {
    const a = await user();
    for (const k of ["hide_demo_banner", "show_caveats", "hide_endorsement_note", "demo_banner", "caveats", "hide_disclaimer"]) {
      const r = await api("/api/settings", { method: "PUT", token: a.token, body: { [k]: false } });
      expect(r.status).toBe(400);
      expect(r.data.code).toBe("unknown_setting");
    }
    expect(Object.keys(DEFAULT_SETTINGS).sort()).toEqual(["default_flows", "default_models", "default_other_attack", "notify_on",
      "telegram_chat_id", "theme", "timezone", "weight_presets"]);
  });
});

describe("admin system limits", () => {
  it("admin reads and changes limits; users cannot; values are range-checked", async () => {
    const admin = await user("admin"), u = await user();
    expect((await api("/api/admin/limits", { token: admin.token })).data.limits).toEqual(DEFAULT_LIMITS);
    expect((await api("/api/admin/limits", { method: "PUT", token: u.token, body: { sessions_per_hour: 99 } })).status).toBe(403);
    const r = await api("/api/admin/limits", { method: "PUT", token: admin.token,
      body: { sessions_per_hour: 3, max_queued_sessions: 2, max_upload_mb: 50, idle_minutes: 20, keep_identification_columns_in_r2: true } });
    expect(r.data.limits).toEqual({ sessions_per_hour: 3, max_queued_sessions: 2, max_upload_mb: 50, idle_minutes: 20, keep_identification_columns_in_r2: true });
    for (const body of [{ max_upload_mb: 91 }, { sessions_per_hour: 0 }, { idle_minutes: -1 }, { keep_identification_columns_in_r2: "no" }, { gpu_price: 1 }])
      expect((await api("/api/admin/limits", { method: "PUT", token: admin.token, body })).status).toBe(400);
  });
});

describe("credential status never leaks values", () => {
  it("reports set / not set only — from the backend heartbeat and the Worker env", async () => {
    const admin = await user("admin");
    const before = (await api("/api/admin/credentials", { token: admin.token })).data;
    expect(before.credentials.hf_token.status).toMatch(/unknown/);
    await register({ creds: { hf_token: true, vast_api_key: false, secret_value: "hf_SHOULD_NOT_APPEAR" } });
    const r = await api("/api/admin/credentials", { token: admin.token });
    expect(r.data.credentials.hf_token.status).toBe("set");
    expect(r.data.credentials.vast_api_key.status).toBe("not set");
    expect(r.data.credentials.telegram_bot_token.status).toBe("set");
    expect(r.text).not.toMatch(/hf_SHOULD_NOT_APPEAR|test-backend-secret|test-run-secret|test-pepper|test-access-secret|test-bot-token/);
    const row = await E.DB.prepare("SELECT creds_json FROM backends").first();
    expect(JSON.parse(row.creds_json)).toEqual({ hf_token: true, vast_api_key: false });     // only booleans stored
  });
});
