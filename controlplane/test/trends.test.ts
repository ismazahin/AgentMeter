// "Runs that moved" on the control plane (src/trends.ts) must flag exactly what the Trends page
// flags (web/insights.js), and a finished run that got worse alerts its owner on Telegram.
import { createRequire } from "node:module";
import { afterEach, describe, expect, it, vi } from "vitest";
import { drift, movedRuns, movedLines } from "../../supabase/functions/agentmeter/src/trends.ts";
import { api, backendCall, register, user } from "./helpers.ts";

const INSIGHTS = createRequire(import.meta.url)("../../web/insights.js");
const SET = "a".repeat(64);
const summary = (i: number, lat: number, extra: Record<string, unknown> = {}) => ({
  session_id: `job_20261101_10000${i}_${String(i).repeat(32).slice(0, 32)}`, status: "Done", finished_at: `2026-11-01T10:0${i}:00Z`,
  created_at: `2026-11-01T10:0${i}:00Z`, gpu: "NVIDIA L4", prepared_set_sha256: SET, settings_fingerprint: "f".repeat(16),
  identity: { prepared_set_sha256: SET, gpu_label: "NVIDIA L4", settings_fingerprint: "f".repeat(16) },
  models: ["m/fast"], metrics: { "m/fast": { mean_latency_s: lat, valid_rate: 0.9, saw_eff_composite_100: 80 } }, ...extra });

describe("runs that moved (parity with the Trends page)", () => {
  it("flags the same drifts as web/insights.js", () => {
    const cases = [[1, 1.02, 0.98, 1.0, 1.5], [1, 1.02, 0.98, 1.0, 1.05], [2, 2, 2, 1.2], [1, 1.1], [3, 1, 5, 2, 3, 9]];
    for (const vs of cases) {
      const ss = vs.map((v, i) => summary(i, v));
      const js = INSIGHTS.trends(ss, {});
      const flaggedJs = js.drifts.filter((d: any) => d.metric === "mean_latency_s").map((d: any) => [d.change, d.worse]);
      const ts = movedRuns(ss[ss.length - 1], ss.slice(0, -1)).filter((m) => m.metric === "mean_latency_s").map((m) => [m.change, m.worse]);
      expect(ts).toEqual(flaggedJs);
    }
    expect(drift([1, 1], true).enough).toBe(false);
  });

  it("compares only the same setup, and only earlier runs", () => {
    const ss = [summary(0, 1), summary(1, 1.01), summary(2, 0.99), summary(3, 2)];
    expect(movedRuns(ss[3], ss.slice(0, 3))).toHaveLength(1);
    const otherGpu = ss.slice(0, 3).map((s) => ({ ...s, identity: { ...s.identity, gpu_label: "A100" } }));
    expect(movedRuns(ss[3], otherGpu)).toHaveLength(0);
    const later = ss.slice(0, 3).map((s, i) => ({ ...s, finished_at: `2026-11-02T00:0${i}:00Z`, created_at: `2026-11-02T00:0${i}:00Z` }));
    expect(movedRuns(ss[3], later)).toHaveLength(0);
    const line = movedLines(movedRuns(ss[3], ss.slice(0, 3)))[0];
    expect(line).toMatch(/^⚠ Worse: fast mean latency per flow 1\.00 s → 2\.00 s \(\+100% vs the median of 3 earlier runs/);
  });
});

describe("Telegram alert when a finished run got worse", () => {
  afterEach(() => vi.restoreAllMocks());
  function spy() {
    const sent: any[] = [];
    const real = globalThis.fetch;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input: any, init?: any) => {
      const url = typeof input === "string" ? input : input.url;
      if (url.startsWith("https://telegram.test/")) { sent.push(JSON.parse(init.body)); return new Response('{"ok":true}'); }
      return real(input, init);
    });
    return sent;
  }
  async function finish(u: { token: string }, s: any) {
    const g = await api("/api/runs/authorize", { body: { kind: "benchmark", run: "csv_runs/x_1" }, token: u.token });
    await backendCall("POST", "/api/backend/jobs", { job_id: s.session_id, kind: "benchmark", status: "running", jti: g.data.jti, models: s.models });
    await backendCall("POST", `/api/backend/jobs/${s.session_id}/status`, { status: "done" });
    await backendCall("PUT", `/api/backend/sessions/${s.session_id}/summary`, { summary: s });
  }

  it("alerts even when 'notify me' is off; not when the run is within its usual range or the alert is off", async () => {
    const sent = spy();
    const u = await user();
    await register();
    await api("/api/settings", { method: "PUT", token: u.token, body: { telegram_chat_id: "42", notify_on: "none" } });
    for (const [i, v] of [[0, 1], [1, 1.01], [2, 0.99]] as const) await finish(u, summary(i, v));
    expect(sent).toHaveLength(0);                                            // no notifications asked for, nothing moved
    await finish(u, summary(3, 1.02));
    expect(sent).toHaveLength(0);                                            // within its usual range
    await finish(u, summary(4, 2));
    expect(sent).toHaveLength(1);
    expect(sent[0].text).toMatch(/AgentMeter ALERT: a result moved beyond its usual range/);
    expect(sent[0].text).toMatch(/⚠ Worse: fast mean latency per flow/);
    await api("/api/settings", { method: "PUT", token: u.token, body: { alert_on_drift: false } });
    await finish(u, summary(5, 4));
    expect(sent).toHaveLength(1);                                            // alerts turned off
    expect((await api("/api/settings", { method: "PUT", token: u.token, body: { alert_on_drift: "yes" } })).status).toBe(400);
  });
});
