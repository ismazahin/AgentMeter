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

describe("Compare by setup and the Repeatability verdict (web/insights.js)", () => {
  const two = (i: number, extra: Record<string, unknown> = {}) => summary(i, 1, { models: ["m/fast", "m/slow"],
    metrics: { "m/fast": { mean_latency_s: 1 }, "m/slow": { mean_latency_s: 2 } }, run_name: "csv_runs/set", ...extra });

  it("groups sessions into setups and lists each model once, latest run first", () => {
    const ss = [two(0), two(1), summary(2, 1, { models: ["m/other"], metrics: { "m/other": {} } })];
    const { setups } = INSIGHTS.compareSetups(ss);
    expect(setups).toHaveLength(1);
    expect(setups[0].models.map((m: any) => m.model)).toEqual(["m/fast", "m/other", "m/slow"]);
    expect(setups[0].models[0].runs.map((r: any) => r.session_id)).toEqual([ss[1].session_id, ss[0].session_id]);
    expect(setups[0].models[0].latest).toBe(ss[1].session_id);
    expect(setups[0].name).toBe("csv_runs/set");
  });

  it("says why a session on another setup is left out", () => {
    const base = two(0);
    const otherGpu = two(1, { gpu: "NVIDIA A100", identity: { ...base.identity, gpu_label: "NVIDIA A100" } });
    const otherFlows = two(2, { identity: { ...base.identity, prepared_set_sha256: "b".repeat(64) } });
    const otherPipe = two(3, { pipeline: ["triage", "decide"], identity: { ...base.identity, settings_fingerprint: "e".repeat(16) } });
    const otherSettings = two(4, { identity: { ...base.identity, settings_fingerprint: "d".repeat(16) } });
    const old = two(5, { identity: {}, prepared_set_sha256: null, settings_fingerprint: null });
    const all = [base, otherGpu, otherFlows, otherPipe, otherSettings, old];
    const setup = INSIGHTS.compareSetups(all).setups.find((g: any) => g.key === INSIGHTS.setupKey(base));
    const why = Object.fromEntries(INSIGHTS.excludedFrom(setup, all).map((x: any) => [x.session.session_id, x.reasons.join("; ")]));
    expect(why[base.session_id]).toBeUndefined();
    expect(why[otherGpu.session_id]).toBe("different GPU (NVIDIA A100)");
    expect(why[otherFlows.session_id]).toBe("different flows");
    expect(why[otherPipe.session_id]).toBe("different pipeline (triage → decide)");
    expect(why[otherSettings.session_id]).toBe("different settings");
    expect(why[old.session_id]).toMatch(/no recorded setup/);
  });

  it("gives each model and setup a verdict: too few runs, stable, varies or moved", () => {
    const verdict = (vs: number[]) => INSIGHTS.repeatSummary(INSIGHTS.trends(vs.map((v, i) => summary(i, v)), {}))[0];
    expect(verdict([1, 1.02]).status).toBe("few");
    const st = verdict([1, 1.02, 0.98, 1.01]);
    expect(st.status).toBe("stable");
    expect(st.runs).toBe(4);
    expect(st.latency_spread).toBeCloseTo(0.025 / 1.005, 9);          // the furthest run from the median (1.005)
    expect(verdict([1, 1.3, 0.8, 1.25, 1.0]).status).toBe("varies");
    const mv = verdict([1, 1.02, 0.98, 1.0, 1.6]);
    expect(mv.status).toBe("moved");
    expect(mv.moved[0].metric).toBe("mean_latency_s");
  });
});

describe("Compare over every run, what changed, flow guidance (web/insights.js)", () => {
  it("mean ± 95% CI and Welch's t-test agree with SciPy", () => {
    const st = INSIGHTS.runStats([1, 2, 3, 4]);
    expect(st.mean).toBe(2.5);
    expect(st.ci).toBeCloseTo(3.182 * 1.2909944 / 2, 5);                                // t(3) × sd / √n
    const w = INSIGHTS.welch([10, 11, 12, 10.5], [13, 14, 13.5, 14.5]);
    expect(w.t).toBeCloseTo(-5.3716427147145085, 9);                                     // scipy.stats.ttest_ind(equal_var=False)
    expect(w.df).toBeCloseTo(5.584615384615386, 9);
    expect(w.p).toBeCloseTo(0.0021421139298311463, 9);
    expect(INSIGHTS.tTwoSided(2.228, 10)).toBeCloseTo(0.05, 3);
  });

  it("calls a difference real only with enough runs, p < 0.05 and a gap over 2%", () => {
    const r = INSIGHTS.compareRuns({ fast: [1.0, 1.02, 0.98], slow: [1.5, 1.52, 1.49], twin: [1.0, 1.01, 0.99], once: [2.0] }, true);
    const v = Object.fromEntries(r.rows.map((x: any) => [x.model, x.verdict]));
    expect(v).toEqual({ fast: "best", twin: "within noise", slow: "real difference", once: "too few runs" });
    expect(r.rows.find((x: any) => x.model === "slow").diff).toBeCloseTo(0.5033, 3);
  });

  it("lists the versions that changed in the latest run", () => {
    const pts = [{ env: { driver_version: "550.54", torch: "2.4.0" } }, { env: { driver_version: "550.54", torch: "2.4.0" } }];
    expect(INSIGHTS.envChanges({ env: { driver_version: "560.35", torch: "2.4.0" } }, pts).map(INSIGHTS.changeText)).toEqual(["NVIDIA driver 550.54 → 560.35"]);
    expect(INSIGHTS.envChanges({ env: { driver_version: "550.54", torch: "2.4.0" } }, pts)).toEqual([]);
    expect(INSIGHTS.envChanges({ env: null }, pts)).toEqual([]);
  });

  it("repeatability: a limit per measure, or one limit for all", () => {
    // latency 1.00 / 1.03 (±1.5% of the median), tokens 100 / 112 (±5.7%): tokens break their 5% limit
    const ss = [0, 1, 2, 3, 4].map((i) => summary(i, 1, { metrics: { "m/fast": { mean_latency_s: i % 2 ? 1.03 : 1, mean_tokens_per_flow: i % 2 ? 112 : 100 } } }));   // the last run is not a jump
    const t = INSIGHTS.trends(ss, {});
    const auto = INSIGHTS.repeatSummary(t)[0];
    expect(auto.status).toBe("varies");
    expect(auto.over).toEqual(["mean_tokens_per_flow"]);
    expect(auto.spread_by.mean_tokens_per_flow).toBeCloseTo(12 / 100, 9);                // median 100: the 112 runs are 12% away
    expect(INSIGHTS.repeatSummary(t, { within: 0.15 })[0].status).toBe("stable");       // one 15% limit for all
  });

  it("estimates the detectable difference before a run", () => {
    const hist = [{ status: "Done", demo: false, gpu: "L4", metrics: { a: { mean_latency_s: 2, median_latency_s: 1.9, p95_latency_s: 2.6 } } }];
    const g = INSIGHTS.flowGuidance(hist, { models: ["a"], n_flows: 50, gpu: "L4" });
    expect(g.cv).toBeCloseTo(0.7 / 1.645 / 2, 9);
    expect(g.detectable).toBeCloseTo((1.959964 + 0.841621) * g.cv * Math.sqrt(2 / 50), 9);
    expect(g.flows_for_5).toBe(Math.ceil(2 * ((1.959964 + 0.841621) * g.cv / 0.05) ** 2));
    expect(INSIGHTS.flowGuidance(hist, { models: ["a"], n_flows: 50, gpu: "A100" })).toBeNull();      // another GPU: no basis
  });
});
