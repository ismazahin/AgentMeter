// The Worker's Compare / Leaderboard / decision helper must return exactly what the Python
// reference returns (fixtures: python scripts/export_parity_fixtures.py) — both as pure
// functions and through the HTTP routes after the backend uploaded the summaries.
import { describe, expect, it } from "vitest";
import { compare, evaluateFacts, leaderboard, ConstraintError, pyFormat } from "../src/analytics";
import FIX from "./fixtures/parity.json";
import { api, backendCall, register, user } from "./helpers";

const F = FIX as any;
const byId = (id: string) => F.summaries.find((s: any) => s.session_id === id);

describe("parity with the Python reference (pure functions)", () => {
  it("compare", () => {
    for (const c of F.compare) expect(compare(byId(c.a), byId(c.b))).toEqual(c.out);
  });
  it("leaderboard, every sort key", () => {
    const newest = [...F.summaries].reverse();
    for (const [sort, out] of Object.entries(F.leaderboard)) expect(leaderboard(newest, sort)).toEqual(out);
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
    for (const c of F.constraints.filter((x: any) => x.limits && Object.values(x.limits).every((v) => typeof v === "string"))) {
      const q = new URLSearchParams(c.limits).toString();
      const r = await api(`/api/sessions/${c.session}/constraints?${q}`, { token: u.token });
      if (c.error) expect([r.status, r.data.error]).toEqual([400, c.error]);
      else expect(r.data).toEqual(c.out);
    }
  });
});
