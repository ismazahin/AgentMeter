// The Worker's Compare / Leaderboard / decision helper must return exactly what the Python
// reference returns (fixtures: python scripts/export_parity_fixtures.py) — both as pure
// functions and through the HTTP routes after the backend uploaded the summaries.
import { describe, expect, it } from "vitest";
import { compare, compareMany, comparable, evaluateFacts, leaderboard, leaderboardOverall, ConstraintError, pyFormat } from "../../supabase/functions/agentmeter/src/analytics.ts";
import FIX from "./fixtures/parity.json" with { type: "json" };
import { api, backendCall, register, user } from "./helpers.ts";

const F = FIX as any;
const byId = (id: string) => F.summaries.find((s: any) => s.session_id === id);

describe("parity with the Python reference (pure functions)", () => {
  it("compare", () => {
    for (const c of F.compare) expect(compare(byId(c.a), byId(c.b))).toEqual(c.out);
  });
  it("comparable sessions (same setup) and the merged compare", () => {
    const newest = [...F.summaries].reverse();
    for (const c of F.comparable) expect(comparable(byId(c.session), newest)).toEqual(c.out);
    for (const c of F.merged) expect(compareMany(c.sessions.map(byId))).toEqual(c.out);
  });
  it("leaderboard, every sort key", () => {
    const newest = [...F.summaries].reverse();
    for (const [sort, out] of Object.entries(F.leaderboard)) expect(leaderboard(newest, sort)).toEqual(out);
  });
  it("overall leaderboard (wins across setups)", () => {
    const newest = [...F.summaries].reverse();
    for (const c of F.leaderboard_overall) expect(leaderboardOverall(newest, c.by, c.demo)).toEqual(c.out);
  });
  it("decision helper, incl. its error messages", () => {
    for (const c of F.constraints) {
      const facts = byId(c.session).facts;
      if (c.error) {
        expect(() => evaluateFacts(facts, c.limits ?? undefined)).toThrow(ConstraintError);
        expect(() => evaluateFacts(facts, c.limits ?? undefined)).toThrow(c.error);
      } else expect(evaluateFacts(facts, c.limits ?? undefined)).toEqual(c.out);
    }
  });
  it("Python format() emulation", () => {
    const cases: [number, string, string][] = [[0.0003138062, ".4g", "0.0003138"], [5000, ",.0f", "5,000"], [1, "g", "1"],
      [1234567.891, ",.2f", "1,234,567.89"], [0.0000123, ".3g", "1.23e-05"], [123456789, ".3g", "1.23e+08"], [0.1, ".3f", "0.100"]];
    for (const [v, spec, want] of cases) expect(pyFormat(v, spec)).toBe(want);
  });
});

describe("parity through the HTTP routes", () => {
  it("summaries uploaded by the backend -> /api/compare, /api/leaderboard, /api/sessions/:id/constraints", async () => {
    const u = await user();
    await register();
    for (const s of F.summaries) {
      const r = await backendCall("PUT", `/api/backend/sessions/${s.session_id}/summary`, { summary: s });
      expect(r.status).toBe(200);
    }
    for (const c of F.compare) {
      const r = await api(`/api/compare?a=${c.a}&b=${c.b}`, { token: u.token });
      expect(r.data).toEqual(c.out);
    }
    for (const [sort, out] of Object.entries(F.leaderboard))
      expect((await api(`/api/leaderboard?sort=${sort}`, { token: u.token })).data).toEqual(out);
    for (const c of F.leaderboard_overall)
      expect((await api(`/api/leaderboard?view=overall&by=${c.by}${c.demo ? "&demo=1" : ""}`, { token: u.token })).data).toEqual(c.out);
    for (const c of F.comparable.filter((x: any) => /^job_\d{8}_\d{6}_[0-9a-f]{32}$/.test(x.session)))
      expect((await api(`/api/compare/candidates?session=${c.session}`, { token: u.token })).data).toEqual(c.out);
    for (const c of F.merged) {
      const r = await api(`/api/compare/merged?sessions=${c.sessions.join(",")}`, { token: u.token });
      if (c.out.error || new Set(c.sessions).size !== c.sessions.length) expect(r.status).toBe(400);
      else expect(r.data).toEqual(c.out);
    }
    for (const c of F.constraints.filter((x: any) => x.limits && Object.values(x.limits).every((v) => typeof v === "string"))) {
      const q = new URLSearchParams(c.limits).toString();
      const r = await api(`/api/sessions/${c.session}/constraints?${q}`, { token: u.token });
      if (c.error) expect([r.status, r.data.error]).toEqual([400, c.error]);
      else expect(r.data).toEqual(c.out);
    }
  });
});
