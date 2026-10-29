// Cloud data preparation (src/prep.ts): the control plane hands a user's dataset to the cloud
// preparation service (signed, single-use Storage URLs only), records its signed reports and
// serves the prepared files back to their owner.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { hmacHex, sha256Hex } from "../../supabase/functions/agentmeter/src/crypto.ts";
import { MemoryStore } from "../../supabase/functions/agentmeter/src/store.ts";
import { E, api, backendCall, user } from "./helpers.ts";

const SECRET = "test-cloud-prep-secret-0123456789";
const CLOUD = "https://prep.test";
const LIMITS = { max_csv_rows: 500000, max_pcap_packets: 100000, max_pcap_flows: 20000, pcap_windows: 10 };
const store = () => E.STORE as MemoryStore;

function spyCloud(status: number | number[] = 202) {
  const calls: { url: string; headers: Record<string, string>; body: any; raw: string }[] = [];
  const statuses = Array.isArray(status) ? [...status] : null;
  const real = globalThis.fetch;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input: any, init?: any) => {
    const url = typeof input === "string" ? input : input.url;
    if (url === `${CLOUD}/health`) return new Response('{"ok":true}', { status: 200 });
    if (url.startsWith(CLOUD)) {
      const raw = new TextDecoder().decode(init.body);
      calls.push({ url, headers: init.headers, body: JSON.parse(raw), raw });
      return new Response('{"accepted":true}', { status: statuses ? statuses.shift() ?? 202 : status as number });
    }
    return real(input, init);
  });
  return calls;
}
const report = (id: string, body: unknown, secret = SECRET) =>
  backendCall("POST", `/api/prep/jobs/${id}/events`, body, { secret });

async function uploaded(u: { token: string }, bytes = new TextEncoder().encode("a,b,Label\n1,2,BENIGN\n")) {
  const r = await api("/api/prep/jobs", { token: u.token, body: { filename: "flows.csv", size: bytes.length, max_flows: 20, limits: LIMITS } });
  expect(r.status).toBe(201);
  const token = r.data.upload_url.split("/upload/")[1];
  expect(await store().acceptUpload(token, bytes, "text/csv")).toBe(true);
  return r.data.job_id as string;
}

beforeEach(() => { E.CLOUD_PREP_URL = CLOUD; E.CLOUD_PREP_SECRET = SECRET; E.PUBLIC_BASE_URL = "https://cp.test/functions/v1/agentmeter"; });
afterEach(() => { vi.restoreAllMocks(); delete E.CLOUD_PREP_RETRY_MS; delete E.CLOUD_PREP_URL; delete E.CLOUD_PREP_SECRET; delete E.PUBLIC_BASE_URL; delete E.CLOUD_PREP_MAX_MB; });

describe("cloud data preparation", () => {
  it("is off until the service URL and secret are set; /api/backend says which", async () => {
    const u = await user();
    expect((await api("/api/backend", { token: u.token })).data.cloud_prep).toBe(true);
    delete E.CLOUD_PREP_URL;
    expect((await api("/api/backend", { token: u.token })).data.cloud_prep).toBe(false);
    const r = await api("/api/prep/jobs", { token: u.token, body: { filename: "a.csv", size: 10, max_flows: 5, limits: LIMITS } });
    expect(r.status).toBe(409);
    expect(r.data.code).toBe("cloud_prep_unavailable");
    expect((await api("/api/prep/jobs", { body: {} })).status).toBe(401);
  });

  it("checks the request: file type, size, flows and limits", async () => {
    const u = await user();
    const bad = async (body: any) => (await api("/api/prep/jobs", { token: u.token, body })).status;
    expect(await bad({ filename: "a.docx", size: 10, max_flows: 5, limits: LIMITS })).toBe(400);
    expect(await bad({ filename: "a.csv", size: 10, max_flows: 0, limits: LIMITS })).toBe(400);
    expect(await bad({ filename: "a.csv", size: 10, max_flows: 5, limits: {} })).toBe(400);
    expect(await bad({ url: "http://example.org/a.csv", max_flows: 5, limits: LIMITS })).toBe(400);
    E.CLOUD_PREP_MAX_MB = "1";
    expect(await bad({ filename: "a.csv", size: 2 * 1024 * 1024, max_flows: 5, limits: LIMITS })).toBe(413);
  });

  it("runs a file job: signed dispatch with single-use URLs, signed reports, files back to the owner", async () => {
    const calls = spyCloud();
    const u = await user(), other = await user(), admin = await user("admin");
    const id = await uploaded(u);
    expect((await api(`/api/prep/jobs/${id}`, { token: other.token })).status).toBe(404);     // not theirs

    const s = await api(`/api/prep/jobs/${id}/start`, { method: "POST", token: u.token });
    expect(s.status).toBe(200);
    expect(calls).toHaveLength(1);
    const c = calls[0];
    expect(c.url).toBe(`${CLOUD}/prepare`);
    const expect_ = await hmacHex(SECRET, `POST\n/prepare\n${c.headers["X-AM-Timestamp"]}\n${await sha256Hex(new TextEncoder().encode(c.raw))}`);
    expect(c.headers["X-AM-Signature"]).toBe(expect_);                                          // signed by the control plane
    expect(c.body.source.kind).toBe("file");
    expect(c.body.source.url).toMatch(/\/download\//);
    expect(Object.keys(c.body.outputs).sort()).toEqual(["features.csv", "labels.csv", "manifest.json"]);
    expect(c.body.report_url).toBe(`https://cp.test/functions/v1/agentmeter/api/prep/jobs/${id}/events`);
    expect(c.raw).not.toMatch(/test-backend-secret|test-access-secret/);                       // no other key travels
    expect((await api(`/api/prep/jobs/${id}/start`, { method: "POST", token: u.token })).status).toBe(409);   // once

    expect((await report(id, { event: { phase: "validating", message: "x" } }, "wrong-secret-0123456789")).status).toBe(401);
    expect((await report(id, { event: { phase: "selecting", message: "Selecting flows" } })).status).toBe(200);
    let g = await api(`/api/prep/jobs/${id}`, { token: u.token });
    expect(g.data.status).toBe("running");
    expect(g.data.log.at(-1)).toMatchObject({ phase: "selecting", message: "Selecting flows" });

    const done = { done: { set_name: "csv_runs/flows_20261029_120000", files: ["manifest.json", "features.csv", "labels.csv"] } };
    expect((await report(id, done)).status).toBe(409);                                          // files not stored yet
    for (const n of done.done.files) {
      const t = c.body.outputs[n].split("/upload/")[1];
      expect(await store().acceptUpload(t, new TextEncoder().encode(`${n} bytes`), "text/plain")).toBe(true);
    }
    expect((await report(id, done)).status).toBe(200);
    g = await api(`/api/prep/jobs/${id}`, { token: u.token });
    expect(g.data).toMatchObject({ status: "done", set_name: "csv_runs/flows_20261029_120000", files: done.done.files });
    const f = await api(`/api/prep/jobs/${id}/files/features.csv`, { token: u.token });
    expect(f.status).toBe(200);
    expect(f.text).toBe("features.csv bytes");
    expect((await api(`/api/prep/jobs/${id}/files/features.csv`, { token: admin.token })).status).toBe(200);
    expect((await api(`/api/prep/jobs/${id}/files/features.csv`, { token: other.token })).status).toBe(404);
    expect([...store().objects.keys()].some((k) => k.includes("/input/"))).toBe(false);          // input removed
    expect((await report(id, { failed: { error: "late", code: "x" } })).data.ignored).toBe("done");  // late news ignored
  });

  it("needs the upload before it starts", async () => {
    spyCloud();
    const u = await user();
    const r = await api("/api/prep/jobs", { token: u.token, body: { filename: "a.csv", size: 99, max_flows: 5, limits: LIMITS } });
    const s = await api(`/api/prep/jobs/${r.data.job_id}/start`, { method: "POST", token: u.token });
    expect(s.status).toBe(409);
    expect(s.data.code).toBe("not_uploaded");
  });

  it("records a failure, and a refused or unreachable service fails the job", async () => {
    spyCloud();
    const u = await user();
    const id = await uploaded(u);
    await api(`/api/prep/jobs/${id}/start`, { method: "POST", token: u.token });
    await report(id, { failed: { error: "no ground-truth label column was found", code: "invalid_file" } });
    expect((await api(`/api/prep/jobs/${id}`, { token: u.token })).data).toMatchObject({ status: "failed", code: "invalid_file" });

    vi.restoreAllMocks();
    spyCloud(500);
    const r = await api("/api/prep/jobs", { token: u.token, body: { url: "https://example.org/capture.pcap", max_flows: 20, limits: LIMITS } });
    expect(r.status).toBe(502);
    const rows = await E.DB.prepare("SELECT status, error_code FROM prep_jobs WHERE source_kind='url'").all();
    expect(rows.results[0]).toMatchObject({ status: "failed", error_code: "cloud_prep_refused" });
  });

  it("waits for a sleeping service (502/503 while it wakes) and wakes it when New benchmark opens", async () => {
    E.CLOUD_PREP_RETRY_MS = "0";
    const calls = spyCloud([503, 502, 202]);
    const u = await user();
    const id = await uploaded(u);
    expect((await api(`/api/prep/jobs/${id}/start`, { method: "POST", token: u.token })).status).toBe(200);
    expect(calls).toHaveLength(3);
    expect((await api(`/api/prep/jobs/${id}`, { token: u.token })).data.status).toBe("running");
    expect((await api("/api/prep/wake", { method: "POST", token: u.token })).data).toMatchObject({ configured: true, up: true });
    expect((await api("/api/prep/wake", { method: "POST" })).status).toBe(401);
    delete E.CLOUD_PREP_URL;
    expect((await api("/api/prep/wake", { method: "POST", token: u.token })).data.configured).toBe(false);
  });

  it("limits unfinished jobs per user and fails a job that stopped answering", async () => {
    spyCloud();
    const u = await user();
    const a = await uploaded(u);
    await uploaded(u);
    const r = await api("/api/prep/jobs", { token: u.token, body: { filename: "c.csv", size: 5, max_flows: 5, limits: LIMITS } });
    expect(r.status).toBe(429);
    await E.DB.prepare("UPDATE prep_jobs SET updated_at='2020-01-01T00:00:00Z' WHERE id=?").bind(a).run();
    expect((await api(`/api/prep/jobs/${a}`, { token: u.token })).data).toMatchObject({ status: "failed", code: "cloud_prep_stalled" });
  });
});
