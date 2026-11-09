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
    expect((await patch(other, { title: "mine now" })).status).toBe(403);
    expect((await patch(owner, { title: "x".repeat(81) })).status).toBe(400);
    expect((await patch(owner, { title: 5 })).status).toBe(400);
    expect((await patch(admin, { notes: "checked" })).data).toMatchObject({ title: "Qwen vs Llama baseline", notes: "checked" });
    const list = await api("/api/sessions?q=baseline", { token: other.token });
    expect(list.data.sessions.map((s: { session_id: string }) => s.session_id)).toEqual([SID]);
    expect(list.data.sessions[0].title).toBe("Qwen vs Llama baseline");
    await backendCall("PUT", `/api/backend/sessions/${SID}/summary`, { summary: { session_id: SID, status: "Done", models: ["m/a", "m/b"], run_name: "csv_runs/x_1", title: "from the backend" } });
    expect((await api(`/api/sessions/${SID}`, { token: owner.token })).data.title).toBe("Qwen vs Llama baseline");
    expect((await patch(owner, { title: "" })).data.title).toBe("");                     // "" removes the name
  });
});

describe("data sets: names and removal", () => {
  it("the first session's owner or an admin renames and removes; others cannot; everyone sees the result", async () => {
    const owner = await user(), other = await user(), admin = await user("admin");
    await register();
    await sessionOn(owner);
    const post = (u: { token: string }, what: string, body: unknown) => api(`/api/datasets/${what}`, { body, token: u.token });
    expect((await api("/api/datasets", { token: other.token })).data).toEqual({ datasets: [] });
    expect((await post(other, "rename", { key: SHA, set: "csv_runs/x_1", label: "theirs" })).status).toBe(403);
    let r = await post(owner, "rename", { key: SHA, set: "csv_runs/x_1", label: "  Monday   traffic " });
    expect(r.status).toBe(200);
    expect(r.data).toEqual({ key: SHA, set: "csv_runs/x_1", label: "Monday traffic" });
    // the owner is found by name too, before a fingerprint exists
    expect((await post(other, "rename", { key: "name:csv_runs/x_1", label: "nope" })).status).toBe(403);
    expect((await post(owner, "rename", { key: SHA, label: "x".repeat(81) })).status).toBe(400);
    expect((await post(owner, "rename", { key: "../etc", label: "x" })).status).toBe(400);
    expect((await post(owner, "rename", { key: SHA, set: "other_runs/x", label: "x" })).status).toBe(400);
    expect((await post(other, "delete", { key: SHA, set: "csv_runs/x_1" })).status).toBe(403);
    expect((await post(admin, "delete", { key: SHA, set: "csv_runs/x_1" })).status).toBe(200);
    const rows = (await api("/api/datasets", { token: other.token })).data.datasets;
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ key: SHA, set: "csv_runs/x_1", label: "Monday traffic" });   // removal keeps the name
    expect(rows[0].deleted_at).toBeTruthy();
    expect((await api(`/api/sessions/${SID}`, { token: other.token })).status).toBe(200);      // its session stays
    const audit = await E.DB.prepare("SELECT action FROM audit_log WHERE target=? ORDER BY id").bind(SHA).all();
    expect(audit.results.map((x: { action: string }) => x.action)).toEqual(["dataset.rename", "dataset.delete"]);
  });

  it("ownership comes from the key alone: naming a set of your own does not make you the owner of another", async () => {
    const alice = await user(), bob = await user();
    await register();
    await sessionOn(alice, "csv_runs/alices_1", SHA, SID, "2026-11-09T10:00:00Z");
    await sessionOn(bob, "csv_runs/bobs_old_1", "f".repeat(64), "job_20261109_080000_" + "a".repeat(32), "2026-11-01T08:00:00Z");
    for (const what of ["rename", "delete"]) {
      const r = await api(`/api/datasets/${what}`, { body: { key: SHA, set: "csv_runs/bobs_old_1", label: "mine" }, token: bob.token });
      expect(r.status).toBe(403);
    }
    expect((await api("/api/datasets", { token: bob.token })).data.datasets).toEqual([]);
    // a failed or running session does not make its user the owner (only finished ones count)
    await E.DB.prepare("UPDATE sessions SET status='Failed', finished_at='2026-01-01T00:00:00Z' WHERE id=?").bind("job_20261109_080000_" + "a".repeat(32)).run();
    await E.DB.prepare("UPDATE sessions SET prepared_set_sha256=? WHERE id=?").bind(SHA, "job_20261109_080000_" + "a".repeat(32)).run();
    expect((await api("/api/datasets/rename", { body: { key: SHA, label: "x" }, token: bob.token })).status).toBe(403);
    expect((await api("/api/datasets/rename", { body: { key: SHA, label: "Alice's" }, token: alice.token })).status).toBe(200);
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
