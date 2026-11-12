// Session names and data-set names/removal (the Sessions and Data sets pages).
import { describe, expect, it } from "vitest";
import { E, api, backendCall, register, user } from "./helpers.ts";

const SID = "job_20261109_090000_" + "d".repeat(32);
const SHA = "e".repeat(64);

// a finished session (status Done) of user u on a prepared set: the data set's owner is whoever
// ran its first finished session
async function sessionOn(u: { token: string }, set = "csv_runs/x_1", sha: string | null = SHA, id = SID, finished = "2026-11-09T09:00:00Z") {
  const g = await api("/api/runs/authorize", { body: { kind: "benchmark", run: set }, token: u.token });
  await backendCall("POST", "/api/backend/jobs", { job_id: id, kind: "benchmark", status: "queued", jti: g.data.jti,
                                                   models: ["m/a", "m/b"], input_type: "csv", n_flows: 50, provider: "hf" });
  await E.DB.prepare("UPDATE sessions SET prepared_set=?, prepared_set_sha256=?, status='Done', finished_at=? WHERE id=?").bind(set, sha, finished, id).run();
}

describe("session names", () => {
  it("the owner or an admin names a session; it is one line, short, searchable and kept by a summary upload", async () => {
    const owner = await user(), other = await user(), admin = await user("admin");
    await register();
    await sessionOn(owner);
    const patch = (u: { token: string }, body: unknown) => api(`/api/sessions/${SID}`, { method: "PATCH", body, token: u.token });
    const r = await patch(owner, { title: "  Qwen vs Llama \n  baseline " });
    expect(r.status).toBe(200);
    expect(r.data.title).toBe("Qwen vs Llama baseline");
    expect((await patch(other, { title: "mine now" })).status).toBe(404);              // not theirs: not even there
    expect((await patch(owner, { title: "x".repeat(81) })).status).toBe(400);
    expect((await patch(owner, { title: 5 })).status).toBe(400);
    expect((await patch(admin, { notes: "checked" })).data).toMatchObject({ title: "Qwen vs Llama baseline", notes: "checked" });
    expect((await api("/api/sessions?q=baseline", { token: other.token })).data.sessions).toEqual([]);
    const list = await api("/api/sessions?q=baseline", { token: owner.token });
    expect(list.data.sessions.map((s: { session_id: string }) => s.session_id)).toEqual([SID]);
    expect(list.data.sessions[0].title).toBe("Qwen vs Llama baseline");
    await backendCall("PUT", `/api/backend/sessions/${SID}/summary`, { summary: { session_id: SID, status: "Done", models: ["m/a", "m/b"], run_name: "csv_runs/x_1", title: "from the backend" } });
    expect((await api(`/api/sessions/${SID}`, { token: owner.token })).data.title).toBe("Qwen vs Llama baseline");
    expect((await patch(owner, { title: "" })).data.title).toBe("");                     // "" removes the name
  });
});

describe("data sets: names and removal", () => {
  it("each user names and removes a data set in their own view only; nobody else's view changes", async () => {
    const owner = await user(), other = await user(), admin = await user("admin");
    await register();
    await sessionOn(owner);
    const post = (u: { token: string }, what: string, body: unknown) => api(`/api/datasets/${what}`, { body, token: u.token });
    const rows = async (u: { token: string }) => (await api("/api/datasets", { token: u.token })).data.datasets;
    expect(await rows(other)).toEqual([]);
    let r = await post(owner, "rename", { key: SHA, set: "csv_runs/x_1", label: "  Monday   traffic " });
    expect(r.status).toBe(200);
    expect(r.data).toEqual({ key: SHA, set: "csv_runs/x_1", label: "Monday traffic" });
    expect((await post(owner, "rename", { key: SHA, label: "x".repeat(81) })).status).toBe(400);
    expect((await post(owner, "rename", { key: "../etc", label: "x" })).status).toBe(400);
    expect((await post(owner, "rename", { key: SHA, set: "other_runs/x", label: "x" })).status).toBe(400);
    // another user (who uploaded the same flows) names it for themselves; the owner's name stays
    expect((await post(other, "rename", { key: SHA, set: "csv_runs/x_1", label: "Their copy" })).status).toBe(200);
    expect((await rows(owner))[0]).toMatchObject({ key: SHA, label: "Monday traffic", deleted_at: null });
    expect((await rows(other))[0]).toMatchObject({ key: SHA, label: "Their copy" });
    // an admin's removal hides it from the admin's view only
    expect((await post(admin, "delete", { key: SHA, set: "csv_runs/x_1" })).status).toBe(200);
    expect((await rows(admin))[0].deleted_at).toBeTruthy();
    expect((await rows(owner))[0].deleted_at).toBeNull();
    expect((await post(owner, "delete", { key: SHA, set: "csv_runs/x_1" })).status).toBe(200);
    expect((await rows(owner))[0]).toMatchObject({ label: "Monday traffic" });               // removal keeps the name
    expect((await rows(owner))[0].deleted_at).toBeTruthy();
    expect((await api(`/api/sessions/${SID}`, { token: owner.token })).status).toBe(200);    // its session stays
    const audit = await E.DB.prepare("SELECT action FROM audit_log WHERE target=? ORDER BY id").bind(SHA).all();
    expect(audit.results.map((x: { action: string }) => x.action)).toEqual(["dataset.rename", "dataset.rename", "dataset.delete", "dataset.delete"]);
  });

  it("the GPU files of a set: only its owner (the first finished session's user) or an admin gets a delete token", async () => {
    const alice = await user(), bob = await user();
    await register();
    await sessionOn(alice, "csv_runs/alices_1", SHA, SID, "2026-11-09T10:00:00Z");
    await sessionOn(bob, "csv_runs/bobs_old_1", "f".repeat(64), "job_20261109_080000_" + "a".repeat(32), "2026-11-01T08:00:00Z");
    const ask = (u: { token: string }, set: string) => api("/api/runs/authorize", { body: { kind: "delete_set", set }, token: u.token });
    expect((await ask(bob, "csv_runs/alices_1")).status).toBe(403);
    expect((await ask(alice, "csv_runs/alices_1")).status).toBe(200);
  });

  it("only the owner (or an admin) gets a token to delete a benchmarked set's files", async () => {
    const owner = await user(), other = await user(), admin = await user("admin");
    await register();
    await sessionOn(owner);
    const ask = (u: { token: string }) => api("/api/runs/authorize", { body: { kind: "delete_set", set: "csv_runs/x_1" }, token: u.token });
    const payload = (tok: string) => JSON.parse(Buffer.from(tok.split(".")[1], "base64url").toString());
    expect((await ask(other)).status).toBe(403);
    expect(payload((await ask(owner)).data.token)).toMatchObject({ kind: "delete_set", set: "csv_runs/x_1", owner: true, admin: false });
    expect(payload((await ask(admin)).data.token)).toMatchObject({ admin: true, owner: false });
  });

  it("a set nobody has benchmarked has no owner here: the GPU server checks who prepared it", async () => {
    const u = await user();
    const r = await api("/api/datasets/rename", { body: { key: "name:csv_runs/fresh_1", set: "csv_runs/fresh_1", label: "Fresh" }, token: u.token });
    expect(r.status).toBe(200);
    expect((await api("/api/datasets", { token: u.token })).status).toBe(200);
    expect((await api("/api/datasets")).status).toBe(401);                                    // login required
  });

  it("issues a delete_set token for one set (owner check on the GPU server; admin flag carried)", async () => {
    const u = await user(), admin = await user("admin");
    await register();
    expect((await api("/api/runs/authorize", { body: { kind: "delete_set", set: "nope" }, token: u.token })).status).toBe(400);
    const t = await api("/api/runs/authorize", { body: { kind: "delete_set", set: "csv_runs/x_1" }, token: u.token });
    expect(t.status).toBe(200);
    const payload = (tok: string) => JSON.parse(Buffer.from(tok.split(".")[1], "base64url").toString());
    expect(payload(t.data.token)).toMatchObject({ kind: "delete_set", set: "csv_runs/x_1", admin: false });
    const ta = await api("/api/runs/authorize", { body: { kind: "delete_set", set: "csv_runs/x_1" }, token: admin.token });
    expect(payload(ta.data.token).admin).toBe(true);
  });
});

// An analysis (Analyse traffic) is a finished cloud preparation job whose service scored the pool:
// it is listed before anyone benchmarks it, its uploader owns it, and deleting the data set removes
// its stored files, the owner-only address file included.
describe("traffic analyses", () => {
  const FILES = ["manifest.json", "features.csv", "ids_scores.csv", "ids_pool.json", "ids_flows_identity.csv"];
  async function analysisBy(u: { id: string }, set: string, sha: string, id = "a".repeat(31) + "1") {
    const t = "2026-11-10T09:00:00Z";
    await E.DB.prepare(`INSERT INTO prep_jobs (id, owner_id, status, source_kind, filename, options_json, set_name, files_json,
                          prepared_sha256, ids_json, created_at, updated_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`)
      .bind(id, u.id, "done", "file", "flows.csv", "{}", set, JSON.stringify(FILES), sha,
            JSON.stringify({ threshold: 0.07, n_flows_in: 40, n_flagged: 12, n_selected: 10, labelled: true }), t, t, t).run();
    for (const f of FILES) await E.STORE.put(`prep/${id}/out/${f}`, `${f} bytes`, "text/plain");
    return id;
  }

  it("lists the uploader's analyses (an admin sees all), with the data set key", async () => {
    const u = await user(), other = await user(), admin = await user("admin");
    const sha = "1".repeat(64);
    const id = await analysisBy(u, "csv_runs/flows_a1", sha);
    const mine = await api("/api/analyses", { token: u.token });
    expect(mine.data.analyses.find((x: any) => x.job_id === id)).toMatchObject({ mine: true, set: "csv_runs/flows_a1", key: sha,
                                                                               ids: { n_flagged: 12 } });
    expect((await api("/api/analyses", { token: other.token })).data.analyses.some((x: any) => x.job_id === id)).toBe(false);
    expect((await api("/api/analyses", { token: admin.token })).data.analyses.some((x: any) => x.job_id === id)).toBe(true);
  });

  it("removing it removes only the remover's own analysis files, and is audited", async () => {
    const u = await user(), other = await user(), admin = await user("admin");
    const sha = "2".repeat(64);
    const id = await analysisBy(u, "csv_runs/flows_a2", sha, "b".repeat(31) + "2");
    // someone else removing the same flows from their view leaves the uploader's files alone
    expect((await api("/api/datasets/delete", { body: { key: sha }, token: other.token })).status).toBe(200);
    for (const f of FILES) expect(await E.STORE.get(`prep/${id}/out/${f}`)).not.toBeNull();
    expect((await api("/api/datasets/delete", { body: { key: sha }, token: admin.token })).status).toBe(200);
    for (const f of FILES) expect(await E.STORE.get(`prep/${id}/out/${f}`)).not.toBeNull();
    expect((await api("/api/datasets/rename", { body: { key: sha, label: "Tuesday capture" }, token: u.token })).status).toBe(200);
    expect((await api("/api/datasets/delete", { body: { key: sha, set: "csv_runs/flows_a2" }, token: u.token })).status).toBe(200);
    for (const f of FILES) expect(await E.STORE.get(`prep/${id}/out/${f}`)).toBeNull();
    expect((await api("/api/analyses", { token: u.token })).data.analyses.some((x: any) => x.job_id === id)).toBe(false);
    const log = await api("/api/admin/audit?action=dataset.delete", { token: admin.token });
    const entry = (log.data.entries || log.data.audit || []).find((e: any) => e.target === sha);
    expect(entry?.detail ?? JSON.parse(entry?.detail_json ?? "{}")).toMatchObject({ analysis_jobs: 1, identity_file_deleted: true });
  });
});
