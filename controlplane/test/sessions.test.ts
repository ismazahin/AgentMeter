import { describe, expect, it } from "vitest";
import FIX from "./fixtures/parity.json" with { type: "json" };
import { E, api, backendCall, register, user } from "./helpers.ts";

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
    expect((await backendCall("PUT", `/api/backend/sessions/${SID}/results`, null, { raw: small })).data.stored).toBe("db");
    expect((await api(`/api/sessions/${SID}/results`, { token: u.token })).text).toBe(small);
    const big = JSON.stringify({ schema: "x", pad: "y".repeat(5000) });            // test threshold: 2000 bytes
    const r = await backendCall("PUT", `/api/backend/sessions/${SID}/results`, null, { raw: big });
    expect(r.data).toMatchObject({ stored: "storage", bytes: big.length });
    expect((await E.DB.prepare("SELECT results_json, results_key FROM sessions WHERE id=?").bind(SID).first()))
      .toEqual({ results_json: null, results_key: `sessions/${SID}/session_results.json` });
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
    expect(await E.STORE.get(`sessions/${SID}/manifest.json`)).toBeNull();
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

describe("prepared-set hash migration (backend-signed)", () => {
  it("lists stored sessions, serves their stored copy, and replaces only the hash", async () => {
    const u = await user();
    await register();
    const s = { ...F.summaries[0], session_id: SID, job_id: SID, identity: { ...F.summaries[0].identity, prepared_set_sha256: "1".repeat(64) } };
    delete s.identity.prepared_set_hash_version;
    await backendCall("PUT", `/api/backend/sessions/${SID}/summary`, { summary: s });
    await backendCall("PUT", `/api/backend/sessions/${SID}/files/features.csv`, null, { raw: "flow_id,a\nf1,1\n" });
    expect((await api("/api/backend/sessions")).status).toBe(401);                          // signature required
    const list = (await backendCall("GET", "/api/backend/sessions", null, { raw: "" })).data.sessions;
    expect(list).toEqual([{ id: SID, prepared_set_sha256: "1".repeat(64), prepared_set_hash_version: 1, files: ["features.csv"] }]);
    expect((await backendCall("GET", `/api/backend/sessions/${SID}/files/features.csv`, null, { raw: "" })).text).toBe("flow_id,a\nf1,1\n");
    expect((await backendCall("POST", `/api/backend/sessions/${SID}/identity`, { prepared_set_sha256: "xyz", prepared_set_hash_version: 2 })).status).toBe(400);
    const r = await backendCall("POST", `/api/backend/sessions/${SID}/identity`, { prepared_set_sha256: "2".repeat(64), prepared_set_hash_version: 2 });
    expect(r.data).toMatchObject({ ok: true, before: "1".repeat(64), after: "2".repeat(64) });
    const got = await api(`/api/sessions/${SID}`, { token: u.token });
    expect(got.data.identity).toMatchObject({ prepared_set_sha256: "2".repeat(64), prepared_set_hash_version: 2 });
    expect(got.data.prepared_set_sha256).toBe("2".repeat(64));
    expect(got.data.status).toBe(s.status ?? "Done");                                       // nothing else changed
    const row = await E.DB.prepare("SELECT prepared_set_sha256 FROM sessions WHERE id=?").bind(SID).first();
    expect(row.prepared_set_sha256).toBe("2".repeat(64));
  });
});

describe("notes and tags (owner or admin edits, everyone searches)", () => {
  it("the owner edits; another user cannot; an admin can; bad input is refused; a summary upload keeps them", async () => {
    const owner = await user(), other = await user(), admin = await user("admin");
    await register();
    await benchmarkFor(owner);
    const patch = (u: { token: string }, body: unknown) => api(`/api/sessions/${SID}`, { method: "PATCH", body, token: u.token });
    const r = await patch(owner, { notes: "  rerun after the tokenizer fix ", tags: ["thesis ch4", "Thesis CH4", "rerun", " "] });
    expect(r.status).toBe(200);
    expect(r.data).toMatchObject({ notes: "rerun after the tokenizer fix", tags: ["thesis ch4", "rerun"] });   // trimmed, deduped
    expect((await patch(other, { notes: "x" })).status).toBe(403);
    expect((await patch(admin, { tags: ["checked"] })).data).toMatchObject({ notes: "rerun after the tokenizer fix", tags: ["checked"] });
    expect((await patch(owner, { notes: "y".repeat(2001) })).status).toBe(400);
    expect((await patch(owner, { tags: ["<script>"] })).status).toBe(400);
    expect((await patch(owner, { tags: Array.from({ length: 11 }, (_, i) => `t${i}`) })).status).toBe(400);
    expect((await patch(owner, { tags: "rerun" })).status).toBe(400);
    const s = { ...F.summaries[0], session_id: SID, job_id: SID };
    await backendCall("PUT", `/api/backend/sessions/${SID}/summary`, { summary: s });
    expect((await api(`/api/sessions/${SID}`, { token: other.token })).data).toMatchObject({ notes: "rerun after the tokenizer fix", tags: ["checked"] });
  });

  it("search matches every word across models, notes, tags and owner; tag filter is exact; wildcards are literal", async () => {
    const u = await user();
    await register();
    await benchmarkFor(u);
    await api(`/api/sessions/${SID}`, { method: "PATCH", body: { notes: "baseline before the 50% cut", tags: ["thesis ch4"] }, token: u.token });
    const ids = async (qs: string) => (await api(`/api/sessions?${qs}`, { token: u.token })).data.sessions.map((x: any) => x.session_id);
    expect(await ids("q=baseline")).toEqual([SID]);
    expect(await ids("q=" + encodeURIComponent("m/a BASELINE"))).toEqual([SID]);        // both words, any case
    expect(await ids("q=" + encodeURIComponent("baseline nowhere"))).toEqual([]);
    expect(await ids("q=" + encodeURIComponent(u.name))).toEqual([SID]);
    expect(await ids("q=" + encodeURIComponent("50%"))).toEqual([SID]);
    expect(await ids("q=" + encodeURIComponent("%"))).toEqual([SID]);                  // literal %, still in the notes
    expect(await ids("q=" + encodeURIComponent("_x_"))).toEqual([]);                   // _ is not a wildcard
    expect(await ids("tag=" + encodeURIComponent("Thesis CH4"))).toEqual([SID]);
    expect(await ids("tag=thesis")).toEqual([]);                                        // whole tag only
  });
});

describe("the session list says who may edit each row", () => {
  it("can_edit: true for the owner and an admin, false for other users", async () => {
    const owner = await user(), other = await user(), admin = await user("admin");
    await register();
    await benchmarkFor(owner);
    const row = async (u: { token: string }) => (await api("/api/sessions", { token: u.token })).data.sessions.find((x: any) => x.session_id === SID);
    expect((await row(owner)).can_edit).toBe(true);
    expect((await row(admin)).can_edit).toBe(true);
    expect((await row(other)).can_edit).toBe(false);
  });
});
