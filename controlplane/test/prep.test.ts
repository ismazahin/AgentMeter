// Data preparation with a waiting list (src/prep.ts). The preparation service runs on the GPU
// machine and PULLS work (signed /pending and /claim) only while no benchmark runs there; the
// control plane keeps uploads waiting, hands out single-use Storage URLs, records the signed
// reports, re-queues work when the machine goes away, and lets each upload start one benchmark.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { hmacHex, sha256Hex } from "../../supabase/functions/agentmeter/src/crypto.ts";
import { MemoryStore } from "../../supabase/functions/agentmeter/src/store.ts";
import { E, api, backendCall, register, user } from "./helpers.ts";

const SECRET = "test-cloud-prep-secret-0123456789";
const SVC = "https://prep.trycloudflare.com";
const LIMITS = { max_csv_rows: 500000, max_pcap_packets: 100000, max_pcap_flows: 20000, pcap_windows: 10 };
const store = () => E.STORE as MemoryStore;
const svc = (path: string, body: unknown = {}, secret = SECRET) => backendCall("POST", path, body, { secret });
const online = (url = SVC) => svc("/api/prep/register", { url, version: "test" });
const claim = async () => (await svc("/api/prep/claim")).data.job;
const report = (id: string, body: unknown, secret = SECRET) => svc(`/api/prep/jobs/${id}/events`, body, secret);

/** Record the control plane's nudges to the service and its Telegram messages; nothing else leaves. */
function spyOut() {
  const nudges: { url: string; headers: Record<string, string> }[] = [], telegram: any[] = [];
  const real = globalThis.fetch;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input: any, init?: any) => {
    const url = typeof input === "string" ? input : input.url;
    if (url.startsWith(SVC)) { nudges.push({ url, headers: init.headers }); return new Response('{"ok":true}', { status: 202 }); }
    if (url.startsWith("https://telegram.test/")) { telegram.push(JSON.parse(init.body)); return new Response("{}", { status: 200 }); }
    return real(input, init);
  });
  return { nudges, telegram };
}

async function uploaded(u: { token: string }, bytes = new TextEncoder().encode("a,b,Label\n1,2,BENIGN\n"), name = "flows.csv") {
  const r = await api("/api/prep/jobs", { token: u.token, body: { filename: name, size: bytes.length, max_flows: 20, limits: LIMITS } });
  expect(r.status, JSON.stringify(r.data)).toBe(201);
  expect(await store().acceptUpload(r.data.upload_url.split("/upload/")[1], bytes, "text/csv")).toBe(true);
  return r.data.job_id as string;
}
async function started(u: { token: string }, name?: string) {
  await new Promise((ok) => setTimeout(ok, 3));                    // a later queued_at: the order is clear
  const id = await uploaded(u, undefined, name);
  expect((await api(`/api/prep/jobs/${id}/start`, { method: "POST", token: u.token })).data.status).toBe("waiting");
  return id;
}
/** Run a claimed job to done: store its outputs, report done. */
async function finish(job: any, set = `csv_runs/flows_${job.job_id.slice(0, 8)}`) {
  const files = ["manifest.json", "features.csv", "labels.csv"];
  for (const n of files) expect(await store().acceptUpload(job.outputs[n].split("/upload/")[1], new TextEncoder().encode(`${n} bytes`), "text/plain")).toBe(true);
  expect((await report(job.job_id, { done: { set_name: set, files } })).status).toBe(200);
  return set;
}

beforeEach(() => { E.CLOUD_PREP_SECRET = SECRET; E.PUBLIC_BASE_URL = "https://cp.test/functions/v1/agentmeter"; });
afterEach(() => { vi.restoreAllMocks(); delete E.CLOUD_PREP_SECRET; delete E.PUBLIC_BASE_URL; delete E.CLOUD_PREP_MAX_MB; delete E.PREP_STORAGE_MAX_MB; });

describe("data preparation on the GPU machine", () => {
  it("uploads need the service online (it registers itself, signed); /api/backend says so", async () => {
    const u = await user();
    expect((await api("/api/backend", { token: u.token })).data.cloud_prep).toBe(false);
    const r = await api("/api/prep/jobs", { token: u.token, body: { filename: "a.csv", size: 10, max_flows: 5, limits: LIMITS } });
    expect(r.status).toBe(409);
    expect(r.data.code).toBe("cloud_prep_unavailable");
    expect(r.data.error).toMatch(/GPU server is off/);
    expect((await api("/api/prep/jobs", { body: {} })).status).toBe(401);

    expect((await online()).status).toBe(200);
    expect((await svc("/api/prep/register", { url: SVC }, "wrong-secret-0123456789")).status).toBe(401);
    expect((await svc("/api/prep/register", { url: "http://pc.example.org" })).status).toBe(400);   // https only
    let b = (await api("/api/backend", { token: u.token })).data;
    expect(b.cloud_prep).toBe(true);
    expect(b.prep_service).toMatchObject({ online: true });
    // silence for 3 minutes: offline; stopping: offline at once
    await E.DB.prepare("UPDATE backends SET last_heartbeat_at='2020-01-01T00:00:00Z' WHERE id='prep'").run();
    expect((await api("/api/backend", { token: u.token })).data.cloud_prep).toBe(false);
    expect((await svc("/api/prep/heartbeat", { url: SVC })).status).toBe(200);
    expect((await api("/api/backend", { token: u.token })).data.cloud_prep).toBe(true);
    expect((await svc("/api/prep/register", { stopping: true })).data.stopped).toBe(true);
    b = (await api("/api/backend", { token: u.token })).data;
    expect(b.cloud_prep).toBe(false);
    delete E.CLOUD_PREP_SECRET;
    expect((await api("/api/backend", { token: u.token })).data.cloud_prep).toBe(false);
  });

  it("checks the request: file type, size, flows, limits and the Storage room", async () => {
    spyOut();
    await online();
    const u = await user();
    const bad = async (body: any) => (await api("/api/prep/jobs", { token: u.token, body })).status;
    expect(await bad({ filename: "a.docx", size: 10, max_flows: 5, limits: LIMITS })).toBe(400);
    expect(await bad({ filename: "a.csv", size: 10, max_flows: 0, limits: LIMITS })).toBe(400);
    expect(await bad({ url: "http://example.org/a.csv", max_flows: 5, limits: LIMITS })).toBe(400);
    E.CLOUD_PREP_MAX_MB = "1";
    const big = await api("/api/prep/jobs", { token: u.token, body: { filename: "a.csv", size: 2 * 1024 * 1024, max_flows: 5, limits: LIMITS } });
    expect(big.status).toBe(413);
    expect(big.data.error).toMatch(/Import from a link/);
    delete E.CLOUD_PREP_MAX_MB;
    E.PREP_STORAGE_MAX_MB = "1";
    const v = await user();
    expect((await api("/api/prep/jobs", { token: v.token, body: { filename: "a.csv", size: 700 * 1024, max_flows: 5, limits: LIMITS } })).status).toBe(201);
    const full = await api("/api/prep/jobs", { token: v.token, body: { filename: "b.csv", size: 700 * 1024, max_flows: 5, limits: LIMITS } });
    expect(full.status).toBe(507);
    expect(full.data.error).toBe("Too many uploads are waiting. Try again later.");
  });

  it("a file waits, the service is nudged (signed), claims it with fresh single-use URLs and reports back", async () => {
    const out = spyOut();
    await online();
    const u = await user(), other = await user(), admin = await user("admin");
    await api("/api/settings", { method: "PUT", token: u.token, body: { telegram_chat_id: "4242" } });
    const id = await uploaded(u);
    expect((await api(`/api/prep/jobs/${id}`, { token: other.token })).status).toBe(404);     // not theirs
    expect((await svc("/api/prep/pending", { state: "idle" })).data.pending).toBe(0);          // not uploaded yet

    expect((await api(`/api/prep/jobs/${id}/start`, { method: "POST", token: u.token })).data.status).toBe("waiting");
    expect(out.nudges).toHaveLength(1);
    const n = out.nudges[0];
    expect(n.url).toBe(`${SVC}/nudge`);
    expect(n.headers["X-AM-Signature"]).toBe(await hmacHex(SECRET, `POST\n/nudge\n${n.headers["X-AM-Timestamp"]}\n${await sha256Hex(new Uint8Array(0))}`));
    expect((await api(`/api/prep/jobs/${id}/start`, { method: "POST", token: u.token })).status).toBe(409);   // once
    let g = await api(`/api/prep/jobs/${id}`, { token: u.token });
    expect(g.data).toMatchObject({ status: "waiting", waiting: { reason: "next", ahead: 0 } });

    expect((await svc("/api/prep/pending", { state: "idle" })).data.pending).toBe(1);
    expect((await svc("/api/prep/claim", {}, "wrong-secret-0123456789")).status).toBe(401);
    const job = await claim();
    expect(job.job_id).toBe(id);
    expect(job.source.kind).toBe("file");
    expect(job.source.url).toMatch(/\/download\//);
    expect(Object.keys(job.outputs).sort()).toEqual(["features.csv", "labels.csv", "manifest.json"]);
    expect(job.report_url).toBe(`https://cp.test/functions/v1/agentmeter/api/prep/jobs/${id}/events`);
    expect(JSON.stringify(job)).not.toMatch(/test-backend-secret|test-access-secret|test-cloud-prep-secret/);   // no key travels
    expect(await claim()).toBeNull();                                                          // nothing else waits
    expect((await api(`/api/prep/jobs/${id}/cancel`, { method: "POST", token: u.token })).status).toBe(409);   // too late

    expect((await report(id, { event: { phase: "validating" } }, "wrong-secret-0123456789")).status).toBe(401);
    expect((await report(id, { event: { phase: "selecting", message: "Selecting flows" } })).status).toBe(200);
    g = await api(`/api/prep/jobs/${id}`, { token: u.token });
    expect(g.data.status).toBe("running");
    expect(g.data.log.at(-1)).toMatchObject({ phase: "selecting", message: "Selecting flows" });

    const done = { done: { set_name: "csv_runs/flows_20261122_120000", files: ["manifest.json", "features.csv", "labels.csv"] } };
    expect((await report(id, done)).status).toBe(409);                                          // files not stored yet
    await finish(job, done.done.set_name);
    g = await api(`/api/prep/jobs/${id}`, { token: u.token });
    expect(g.data).toMatchObject({ status: "done", set_name: done.done.set_name, used: false });
    expect(g.data.expires_at).toBeTruthy();
    expect((await api(`/api/prep/jobs/${id}/files/features.csv`, { token: u.token })).text).toBe("features.csv bytes");
    expect((await api(`/api/prep/jobs/${id}/files/features.csv`, { token: admin.token })).status).toBe(200);
    expect((await api(`/api/prep/jobs/${id}/files/features.csv`, { token: other.token })).status).toBe(404);
    expect([...store().objects.keys()].some((k) => k.includes("/input/"))).toBe(false);          // the upload is deleted
    expect(out.telegram).toHaveLength(1);
    expect(out.telegram[0]).toMatchObject({ chat_id: "4242" });
    expect(out.telegram[0].text).toMatch(/flows\.csv is prepared/);
    expect((await report(id, { failed: { error: "late", code: "x" } })).data.ignored).toBe("done");  // late news ignored
  });

  it("a link waits too; a job without limits gets config.yaml's defaults", async () => {
    spyOut();
    await online();
    const u = await user();
    const r = await api("/api/prep/jobs", { token: u.token, body: { url: "https://example.org/flows.csv", max_flows: 20 } });
    expect(r.status, JSON.stringify(r.data)).toBe(201);
    expect(r.data.status).toBe("waiting");
    const job = await claim();
    expect(job.source).toEqual({ kind: "url", url: "https://example.org/flows.csv" });
    expect(job.limits).toMatchObject({ max_csv_rows: 500000, max_pcap_packets: 100000, max_pcap_flows: 20000, pcap_windows: 10 });
  });

  it("says why a job waits: a benchmark holds the machine, the GPU server is off, or others are ahead", async () => {
    spyOut();
    await online();
    const u = await user(), v = await user();
    const a = await started(u), b = await started(v);
    await svc("/api/prep/pending", { state: "waiting_for_benchmark", holder: { kind: "benchmark", models: 3, started_at: "2026-11-22T10:00:00Z", job_id: "secret-ish" } });
    let g = (await api(`/api/prep/jobs/${a}`, { token: u.token })).data;
    expect(g.waiting).toEqual({ reason: "benchmark", holder: { kind: "benchmark", models: 3, started_at: "2026-11-22T10:00:00Z" }, ahead: 0 });
    expect((await api(`/api/prep/jobs/${b}`, { token: v.token })).data.waiting).toMatchObject({ reason: "benchmark", ahead: 1 });
    expect((await api("/api/backend", { token: u.token })).data.prep_service.state).toBe("waiting_for_benchmark");
    await svc("/api/prep/pending", { state: "idle" });
    expect((await api(`/api/prep/jobs/${b}`, { token: v.token })).data.waiting).toEqual({ reason: "queue", ahead: 1 });
    await svc("/api/prep/register", { stopping: true });
    g = (await api(`/api/prep/jobs/${a}`, { token: u.token })).data;
    expect(g).toMatchObject({ status: "waiting", waiting: { reason: "gpu_off" } });
    // the oldest goes first
    await online();
    expect((await claim()).job_id).toBe(a);
  });

  it("a machine that goes away puts its job back on the waiting list; three tries, then failed", async () => {
    spyOut();
    await online();
    const u = await user();
    const id = await started(u);
    await claim();
    await online("https://new-machine.trycloudflare.com");                         // a new GPU machine
    let g = (await api(`/api/prep/jobs/${id}`, { token: u.token })).data;
    expect(g).toMatchObject({ status: "waiting", attempts: 1 });
    expect(g.message).toMatch(/restarted/);
    expect(await svc("/api/prep/pending", {}).then((r) => r.data.pending)).toBe(1);
    await claim();
    // no news for 15 minutes: back to waiting (second try), then the third try fails for good
    await E.DB.prepare("UPDATE prep_jobs SET updated_at='2020-01-01T00:00:00Z' WHERE id=?").bind(id).run();
    expect((await api(`/api/prep/jobs/${id}`, { token: u.token })).data).toMatchObject({ status: "waiting", attempts: 2 });
    await claim();
    await E.DB.prepare("UPDATE prep_jobs SET updated_at='2020-01-01T00:00:00Z' WHERE id=?").bind(id).run();
    g = (await api(`/api/prep/jobs/${id}`, { token: u.token })).data;
    expect(g).toMatchObject({ status: "failed", code: "cloud_prep_stalled" });
  });

  it("a file that takes the machine down each time fails after three tries, also on restarts", async () => {
    spyOut();
    await online();
    const u = await user();
    const id = await started(u);
    for (let i = 1; i <= 3; i++) {
      expect((await claim()).job_id).toBe(id);
      await online(`https://machine-${i}.trycloudflare.com`);                       // the machine restarted
    }
    const g = (await api(`/api/prep/jobs/${id}`, { token: u.token })).data;
    expect(g).toMatchObject({ status: "failed", code: "cloud_prep_stalled", attempts: 3 });
    expect(await claim()).toBeNull();                                              // nothing left to hand out
  });

  it("a keep-alive during a long step keeps the job running, without a log entry", async () => {
    spyOut();
    await online();
    const u = await user();
    const id = await started(u);
    await claim();
    await E.DB.prepare("UPDATE prep_jobs SET updated_at='2020-01-01T00:00:00Z' WHERE id=?").bind(id).run();
    expect((await report(id, { keepalive: true })).status).toBe(200);
    const g = (await api(`/api/prep/jobs/${id}`, { token: u.token })).data;
    expect(g).toMatchObject({ status: "running", attempts: 1 });                    // not taken for a dead machine
    expect((g.log || []).length).toBe(0);
  });

  it("records a failure and tells the owner", async () => {
    const out = spyOut();
    await online();
    const u = await user();
    await api("/api/settings", { method: "PUT", token: u.token, body: { telegram_chat_id: "99" } });
    const id = await started(u);
    await claim();
    await report(id, { failed: { error: "no ground-truth label column was found", code: "invalid_file" } });
    expect((await api(`/api/prep/jobs/${id}`, { token: u.token })).data).toMatchObject({ status: "failed", code: "invalid_file" });
    expect(out.telegram[0].text).toMatch(/could not be prepared/);
  });

  it("needs the upload before it starts", async () => {
    spyOut();
    await online();
    const u = await user();
    const r = await api("/api/prep/jobs", { token: u.token, body: { filename: "a.csv", size: 99, max_flows: 5, limits: LIMITS } });
    const s = await api(`/api/prep/jobs/${r.data.job_id}/start`, { method: "POST", token: u.token });
    expect(s.status).toBe(409);
    expect(s.data.code).toBe("not_uploaded");
  });

  it("the owner can cancel a waiting upload (the file is deleted); nobody else can", async () => {
    spyOut();
    await online();
    const u = await user(), other = await user("admin");
    const id = await started(u);
    expect((await api(`/api/prep/jobs/${id}/cancel`, { method: "POST", token: other.token })).status).toBe(404);
    expect((await api(`/api/prep/jobs/${id}/cancel`, { method: "POST", token: u.token })).data.status).toBe("cancelled");
    expect([...store().objects.keys()].some((k) => k.includes("/input/"))).toBe(false);
    expect(await claim()).toBeNull();
  });

  it("limits unfinished uploads per user", async () => {
    spyOut();
    await online();
    const u = await user();
    for (let i = 0; i < 3; i++) await started(u);
    const r = await api("/api/prep/jobs", { token: u.token, body: { filename: "c.csv", size: 5, max_flows: 5, limits: LIMITS } });
    expect(r.status).toBe(429);
    expect(r.data.error).toMatch(/cancel one/);
  });

  it("Your uploads: the caller's uploads not yet benchmarked; they expire after 7 days", async () => {
    spyOut();
    await online();
    await register();
    const u = await user(), v = await user();
    const a = await started(u, "a.csv"), b = await started(u, "b.csv");
    await started(v);
    const job = await claim();
    expect(job.job_id).toBe(a);
    const set = await finish(job);
    let mine = (await api("/api/prep/mine", { token: u.token })).data;
    expect(mine.expire_days).toBe(7);
    expect(mine.uploads.map((x: any) => [x.filename, x.status])).toEqual([["b.csv", "waiting"], ["a.csv", "done"]]);
    // benchmarked: it leaves the list
    expect((await api("/api/runs/authorize", { token: u.token, body: { kind: "benchmark", run: set } })).status).toBe(200);
    mine = (await api("/api/prep/mine", { token: u.token })).data;
    expect(mine.uploads.map((x: any) => x.filename)).toEqual(["b.csv"]);
    // seven days later the waiting one is deleted
    await E.DB.prepare("UPDATE prep_jobs SET created_at='2020-01-01T00:00:00Z' WHERE id=?").bind(b).run();
    expect((await api("/api/prep/mine", { token: u.token })).data.uploads).toEqual([]);
    expect((await api(`/api/prep/jobs/${b}`, { token: u.token })).data.status).toBe("expired");
    expect([...store().objects.keys()].some((k) => k.includes(`prep/${b}/`))).toBe(false);
  });

  it("each upload starts one benchmark: a second one, someone else's set, or a set never uploaded is refused", async () => {
    spyOut();
    await online();
    await register();
    const u = await user(), v = await user();
    await started(u);
    const set = await finish(await claim());
    const auth = (who: { token: string }, run: string) => api("/api/runs/authorize", { token: who.token, body: { kind: "benchmark", run } });
    expect((await auth(v, set)).data.code).toBe("upload_again");                  // not theirs
    expect((await auth(u, "csv_runs/never_uploaded")).data.code).toBe("upload_again");
    expect((await auth(u, set)).status).toBe(200);
    const again = await auth(u, set);
    expect(again.status).toBe(409);
    expect(again.data.error).toBe("Each upload starts one benchmark. Upload the file again to run another.");
    // the first token expired and no session came of it: one retry
    await E.DB.prepare("UPDATE prep_jobs SET used_at='2020-01-01T00:00:00Z' WHERE set_name=?").bind(set).run();
    expect((await auth(u, set)).status).toBe(200);
    // a session exists for the set: never again
    await E.DB.prepare("UPDATE prep_jobs SET used_at='2020-01-01T00:00:00Z' WHERE set_name=?").bind(set).run();
    await E.DB.prepare("INSERT INTO sessions (id, status, created_at, updated_at, prepared_set) VALUES ('job_20261122_120000_aaaaaa','Running','x','x',?)").bind(set).run();
    expect((await auth(u, set)).data.code).toBe("upload_again");
    // an upload the GPU server prepared itself (after_prepare): one benchmark too
    const after = { kind: "benchmark", after_prepare: "job_20261122_120000_bbbbbb" };
    expect((await api("/api/runs/authorize", { token: u.token, body: after })).status).toBe(200);
    expect((await api("/api/runs/authorize", { token: u.token, body: after })).status).toBe(200);           // a retry right away
    await E.DB.prepare("UPDATE run_grants SET issued_at='2020-01-01T00:00:00Z' WHERE after_prepare=?").bind(after.after_prepare).run();
    expect((await api("/api/runs/authorize", { token: u.token, body: after })).data.code).toBe("upload_again");
  });

  it("hands a stored IDS model to the job, stores the analysis, and shows addresses to the owner only", async () => {
    const enc = new TextEncoder(), joblib = enc.encode("pretend-joblib");
    const admin = await user("admin");
    const put = async (name: string, bytes: Uint8Array) => {
      const r = await api("/api/admin/ids-model/upload-url", { token: admin.token, body: { name, bytes: bytes.length } });
      expect(await store().acceptUpload(r.data.url.split("/upload/")[1], bytes, r.data.content_type)).toBe(true);
    };
    await put("ids_rf.joblib", joblib);
    await put("ids_rf.json", enc.encode(JSON.stringify({ name: "ids_rf", version: "v1", sklearn: "1.9.1", threshold: 0.07,
      features: ["Flow Duration"], sha256: await sha256Hex(joblib) })));
    expect((await api("/api/admin/ids-model/commit", { token: admin.token, body: {} })).status).toBe(200);
    try {
      spyOut();
      await online();
      const u = await user();
      const id = await started(u);
      const c = await claim();
      expect(c.ids.sha256).toBe(await sha256Hex(joblib));
      expect(Object.keys(c.ids.files).sort()).toEqual(["ids_rf.joblib", "ids_rf.json"]);
      expect(c.ids.files["ids_rf.joblib"]).toMatch(/\/download\//);                             // single-use, no key
      expect(Object.keys(c.outputs).sort()).toEqual(["features.csv", "ids_flows_identity.csv", "ids_pool.json",
                                                     "ids_scores.csv", "labels.csv", "manifest.json"]);
      const files = ["manifest.json", "features.csv", "labels.csv", "ids_scores.csv", "ids_pool.json", "ids_flows_identity.csv"];
      for (const n of files)
        expect(await store().acceptUpload(c.outputs[n].split("/upload/")[1], enc.encode(`${n} bytes`), "text/plain")).toBe(true);
      expect((await report(id, { done: { set_name: "csv_runs/flows_x", files: [...files, "evil.sh"] } })).status).toBe(400);
      expect((await report(id, { done: { set_name: "csv_runs/flows_x", files } })).status).toBe(200);
      expect((await api(`/api/prep/jobs/${id}/files/ids_scores.csv`, { token: u.token })).text).toBe("ids_scores.csv bytes");
      expect((await api(`/api/prep/jobs/${id}/files/ids_scores.csv`, { token: admin.token })).status).toBe(200);
      expect((await api(`/api/prep/jobs/${id}/files/ids_flows_identity.csv`, { token: u.token })).status).toBe(200);
      const a = await api(`/api/prep/jobs/${id}/files/ids_flows_identity.csv`, { token: admin.token });
      expect(a.status).toBe(403);                                                                // not even an admin
      expect(a.data.code).toBe("owner_only");
    } finally {
      await api("/api/admin/ids-model", { method: "DELETE", token: admin.token });
    }
  });
});
