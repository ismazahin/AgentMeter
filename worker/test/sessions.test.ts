import { describe, expect, it } from "vitest";
import FIX from "./fixtures/parity.json";
import { E, api, backendCall, register, user } from "./helpers";

const F = FIX as any;
const SID = "job_20261010_090000_" + "c".repeat(32);

async function benchmarkFor(u: { token: string }) {
  const g = await api("/api/runs/authorize", { body: { kind: "benchmark", run: "csv_runs/x_1" }, token: u.token });
  await backendCall("POST", "/api/backend/jobs", { job_id: SID, kind: "benchmark", status: "queued", jti: g.data.jti,
                                                   models: ["m/a", "m/b"], input_type: "csv", n_flows: 50, provider: "hf" });
  return g.data.jti as string;
}

describe("session lifecycle (backend writes, users read)", () => {
  it("a benchmark job becomes a Running session owned by the token's user; prepare jobs do not", async () => {
    const u = await user();
    await register();
    const jti = await benchmarkFor(u);
    expect((await E.DB.prepare("SELECT job_id FROM run_grants WHERE jti=?").bind(jti).first()).job_id).toBe(SID);
    await backendCall("POST", "/api/backend/jobs", { job_id: "job_20261010_090001_" + "d".repeat(32), kind: "prepare", status: "queued" });
    const list = (await api("/api/sessions", { token: u.token })).data.sessions;
    expect(list).toHaveLength(1);
    expect(list[0]).toMatchObject({ session_id: SID, status: "Running", owner: u.name, input_type: "CSV", has_results: false });
    expect((await api(`/api/sessions/${SID}/results`, { token: u.token })).data.code).toBe("not_ready");
    await backendCall("POST", `/api/backend/jobs/${SID}/status`, { status: "failed" });
    expect((await api(`/api/sessions/${SID}`, { token: u.token })).data.status).toBe("Failed");
  });

  it("small results live in D1, large ones in R2; both are served byte-for-byte; summary marks it done", async () => {
    const u = await user();
    await register();
    await benchmarkFor(u);
    const small = JSON.stringify({ schema: "x", per_model: [] });
    expect((await backendCall("PUT", `/api/backend/sessions/${SID}/results`, null, { raw: small })).data.stored).toBe("d1");
    expect((await api(`/api/sessions/${SID}/results`, { token: u.token })).text).toBe(small);
    const big = JSON.stringify({ schema: "x", pad: "y".repeat(5000) });            // test threshold: 2000 bytes
    const r = await backendCall("PUT", `/api/backend/sessions/${SID}/results`, null, { raw: big });
    expect(r.data).toMatchObject({ stored: "r2", bytes: big.length });
    expect((await E.DB.prepare("SELECT results_json, results_r2_key FROM sessions WHERE id=?").bind(SID).first()))
      .toEqual({ results_json: null, results_r2_key: `sessions/${SID}/session_results.json` });
    expect((await api(`/api/sessions/${SID}/results`, { token: u.token })).text).toBe(big);
    const s = { ...F.summaries[0], session_id: SID, job_id: SID };
    expect((await backendCall("PUT", `/api/backend/sessions/${SID}/summary`, { summary: s })).status).toBe(200);
    const got = (await api(`/api/sessions/${SID}`, { token: u.token })).data;
    expect(got).toMatchObject({ status: "Done", owner: u.name, has_results: true, can_delete: true });
    expect(got.weights_used.weights).toEqual(F.summaries[0].weights_used.weights);
    expect(got.facts).toBeUndefined();
    expect((await backendCall("PUT", `/api/backend/sessions/${SID}/summary`, { summary: F.summaries[0] })).status).toBe(400);
    const rows = (await E.DB.prepare("SELECT COUNT(*) AS n FROM sessions").first()).n;
    expect(rows).toBe(1);                                                            // one row per session, never per flow
  });

  it("files go to R2 and download with a login; unknown names are refused", async () => {
    const u = await user();
    await register();
    const pdf = "%PDF-1.4 fake";
    expect((await backendCall("PUT", `/api/backend/sessions/${SID}/files/report.pdf`, null, { raw: pdf })).status).toBe(200);
    expect((await backendCall("PUT", `/api/backend/sessions/${SID}/files/secrets.txt`, null, { raw: "x" })).status).toBe(400);
    const r = await api(`/api/sessions/${SID}/files/report.pdf`, { token: u.token });
    expect(r.text).toBe(pdf);
    expect(r.headers.get("content-type")).toBe("application/pdf");
    expect((await api(`/api/sessions/${SID}/files/report.pdf`)).status).toBe(401);
    expect((await api(`/api/sessions/${SID}/files/labels.csv`, { token: u.token })).status).toBe(404);
    expect((await api(`/api/sessions/${SID}`, { token: u.token })).data.files).toEqual(["report.pdf"]);
  });

  it("every user can view; only the owner or an admin deletes (R2 objects go too)", async () => {
    const owner = await user(), other = await user(), admin = await user("admin");
    await register();
    await benchmarkFor(owner);
    await backendCall("PUT", `/api/backend/sessions/${SID}/files/manifest.json`, null, { raw: "{}" });
    expect((await api(`/api/sessions/${SID}`, { token: other.token })).data.can_delete).toBe(false);
    expect((await api(`/api/sessions/${SID}`, { method: "DELETE", token: other.token })).status).toBe(403);
    expect((await api(`/api/sessions/${SID}`, { token: admin.token })).data.can_delete).toBe(true);
    expect((await api(`/api/sessions/${SID}`, { method: "DELETE", token: owner.token })).status).toBe(200);
    expect((await api(`/api/sessions/${SID}`, { token: owner.token })).status).toBe(404);
    expect(await E.R2.get(`sessions/${SID}/manifest.json`)).toBeNull();
    expect((await api("/api/sessions", { token: other.token })).data.sessions).toHaveLength(0);
  });

  it("reads need a login; ids are validated; backend writes need the signature", async () => {
    expect((await api("/api/sessions")).status).toBe(401);
    expect((await api("/api/leaderboard")).status).toBe(401);
    const u = await user();
    expect((await api("/api/sessions/../../etc", { token: u.token })).status).toBe(404);
    expect((await api(`/api/compare?a=${SID}&b=${SID}`, { token: u.token })).status).toBe(400);
    expect((await api("/api/leaderboard?sort=accuracy", { token: u.token })).status).toBe(400);   // efficiency sorts only
    expect((await api(`/api/backend/sessions/${SID}/results`, { method: "PUT", raw: "{}" })).status).toBe(401);
  });

  it("session browsing works with the backend offline (GPU off)", async () => {
    const u = await user();
    await register();
    for (const s of F.summaries.slice(0, 2)) await backendCall("PUT", `/api/backend/sessions/${s.session_id}/summary`, { summary: s });
    await E.DB.prepare("DELETE FROM backends").run();
    expect((await api("/api/backend", { token: u.token })).data.online).toBe(false);
    expect((await api("/api/sessions", { token: u.token })).data.sessions).toHaveLength(2);
    expect((await api(`/api/compare?a=${F.summaries[0].session_id}&b=${F.summaries[1].session_id}`, { token: u.token })).status).toBe(200);
    expect((await api("/api/leaderboard", { token: u.token })).data.groups.length).toBeGreaterThan(0);
    expect((await api("/api/runs/authorize", { body: { kind: "prepare" }, token: u.token })).data.code).toBe("backend_offline");
  });
});
