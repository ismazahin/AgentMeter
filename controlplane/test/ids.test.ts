// The IDS gate's model in Storage: admin upload + commit (checked), signed backend download.
import { describe, expect, it } from "vitest";
import { sha256Hex } from "../../supabase/functions/agentmeter/src/crypto.ts";
import { E, api, backendCall, user } from "./helpers.ts";

const enc = new TextEncoder();
const JOBLIB = enc.encode("pretend-joblib-bytes");

async function upload(token: string, name: string, bytes: Uint8Array) {
  const r = await api("/api/admin/ids-model/upload-url", { token, body: { name, bytes: bytes.length } });
  expect(r.status, JSON.stringify(r.data)).toBe(200);
  expect(await E.STORE.acceptUpload(r.data.url.split("/upload/")[1], bytes, r.data.content_type)).toBe(true);
}

async function meta(over: Record<string, unknown> = {}) {
  return enc.encode(JSON.stringify({ name: "ids_rf", version: "ids_rf-2026-11-04", sklearn: "1.9.1", threshold: 0.0708,
    features: ["Flow Duration"], sha256: await sha256Hex(JOBLIB),
    metrics: { holdout: { recall: 0.99997, precision: 0.9969, false_positive_rate: 0.001, flagged_share: 0.2464, n: 486443 } }, ...over }));
}

describe("IDS model storage", () => {
  it("is admin only", async () => {
    const u = await user();
    expect((await api("/api/admin/ids-model", { token: u.token })).status).toBe(403);
    expect((await api("/api/admin/ids-model/upload-url", { token: u.token, body: { name: "ids_rf.json", bytes: 10 } })).status).toBe(403);
    expect((await api("/api/admin/ids-model/commit", { token: u.token, body: {} })).status).toBe(403);
  });

  it("refuses unknown files, oversized files and a commit without the files", async () => {
    const a = await user("admin");
    await api("/api/admin/ids-model", { method: "DELETE", token: a.token });
    expect((await api("/api/admin/ids-model/upload-url", { token: a.token, body: { name: "evil.sh", bytes: 10 } })).status).toBe(400);
    expect((await api("/api/admin/ids-model/upload-url", { token: a.token, body: { name: "ids_rf.joblib", bytes: 60 * 1024 * 1024 } })).status).toBe(413);
    expect((await api("/api/admin/ids-model/commit", { token: a.token, body: {} })).data.code).toBe("not_uploaded");
    expect((await backendCall("GET", "/api/backend/ids-model", null, { raw: "" })).data.code).toBe("no_ids_model");
  });

  it("commit checks the joblib against the metadata's sha256", async () => {
    const a = await user("admin");
    await upload(a.token, "ids_rf.joblib", enc.encode("some other model"));
    await upload(a.token, "ids_rf.json", await meta());
    expect((await api("/api/admin/ids-model/commit", { token: a.token, body: {} })).data.code).toBe("sha_mismatch");
    await upload(a.token, "ids_rf.json", await meta({ threshold: 7 }));
    expect((await api("/api/admin/ids-model/commit", { token: a.token, body: {} })).data.code).toBe("bad_model");
  });

  it("a committed model is listed for admins and handed to the signed backend", async () => {
    const a = await user("admin");
    await upload(a.token, "ids_rf.joblib", JOBLIB);
    await upload(a.token, "ids_rf.json", await meta());
    await upload(a.token, "ids_rf.hashes.npy", enc.encode("hashes"));
    const c = await api("/api/admin/ids-model/commit", { token: a.token, body: {} });
    expect(c.status, JSON.stringify(c.data)).toBe(200);
    expect(c.data.model).toMatchObject({ version: "ids_rf-2026-11-04", threshold: 0.0708, committed_by: a.name });
    const g = await api("/api/admin/ids-model", { token: a.token });
    expect(g.data.model.sizes["ids_rf.joblib"]).toBe(JOBLIB.length);
    expect(g.data.model.holdout.recall).toBe(0.99997);
    expect((await api("/api/backend/ids-model")).status).toBe(401);                    // unsigned
    const b = await backendCall("GET", "/api/backend/ids-model", null, { raw: "" });
    expect(b.status).toBe(200);
    expect(Object.keys(b.data.files).sort()).toEqual(["ids_rf.hashes.npy", "ids_rf.joblib", "ids_rf.json"]);
    expect(b.data.meta.sha256).toBe(await sha256Hex(JOBLIB));
    const bytes = await E.STORE.serveDownload(b.data.files["ids_rf.joblib"].split("/download/")[1]);
    expect(new TextDecoder().decode(bytes)).toBe("pretend-joblib-bytes");
    const audit = await api("/api/admin/audit", { token: a.token });
    expect(JSON.stringify(audit.data)).toContain("ids_model.commit");
    expect((await api("/api/admin/ids-model", { method: "DELETE", token: a.token })).status).toBe(200);
    expect((await backendCall("GET", "/api/backend/ids-model", null, { raw: "" })).status).toBe(404);
  });
});
