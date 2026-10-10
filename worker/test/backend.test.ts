import { describe, expect, it } from "vitest";
import { verifyToken } from "../src/crypto";
import { E, api, backendCall, register, user } from "./helpers";

const JOB = (n: number) => `job_20261010_0900${String(n).padStart(2, "0")}_${"a".repeat(31)}${n % 10}`;

describe("backend registration, heartbeat, offline", () => {
  it("rejects unsigned, wrongly signed, stale and replayed-with-other-body requests", async () => {
    expect((await api("/api/backend/register", { body: { url: "http://127.0.0.1:8766" } })).data.code).toBe("bad_timestamp");
    expect((await backendCall("POST", "/api/backend/register", { url: "http://127.0.0.1:8766" }, { secret: "wrong-secret-0123456789" })).data.code).toBe("bad_signature");
    expect((await backendCall("POST", "/api/backend/register", { url: "http://127.0.0.1:8766" }, { ts: Math.floor(Date.now() / 1000) - 600 })).data.code).toBe("bad_timestamp");
    expect((await register()).status).toBe(200);
  });

  it("registers a URL, users see it online; https required except the dev http://127.0.0.1", async () => {
    const u = await user();
    expect((await api("/api/backend", { token: u.token })).data.online).toBe(false);
    const r = await register({ url: "https://fluffy-cat.trycloudflare.com/", mode: "real", gpu: { name: "NVIDIA L4" } });
    expect(r.data.limits.sessions_per_hour).toBe(12);
    const st = (await api("/api/backend", { token: u.token })).data;
    expect(st).toMatchObject({ online: true, url: "https://fluffy-cat.trycloudflare.com", mode: "real", gpu: { name: "NVIDIA L4" } });
    expect((await register({ url: "http://evil.example.com" })).data.code).toBe("bad_url");
    expect((await register({ url: "https://agentmeter.example.org" })).status).toBe(200);    // a named tunnel on a domain: same path
    expect((await api("/api/backend")).status).toBe(401);                                    // users must be logged in
  });

  it("marks the backend offline when heartbeats stop", async () => {
    const u = await user();
    await register();
    expect((await backendCall("POST", "/api/backend/heartbeat", {})).status).toBe(200);
    await E.DB.prepare("UPDATE backends SET last_heartbeat_at=?").bind(new Date(Date.now() - 181_000).toISOString()).run();
    const st = (await api("/api/backend", { token: u.token })).data;
    expect(st.online).toBe(false);
    expect(st.url).toBeNull();
    expect((await backendCall("POST", "/api/backend/heartbeat", {})).status).toBe(200);
    expect((await api("/api/backend", { token: u.token })).data.online).toBe(true);
  });

  it("re-registration marks sessions the backend no longer knows as Interrupted", async () => {
    await register();
    await backendCall("POST", "/api/backend/jobs", { job_id: JOB(1), kind: "benchmark", status: "running", models: ["m/a"] });
    await backendCall("POST", "/api/backend/jobs", { job_id: JOB(2), kind: "benchmark", status: "running", models: ["m/a"] });
    await register({ jobs: [{ job_id: JOB(2), status: "running" }] });
    const rows = (await E.DB.prepare("SELECT id, status FROM sessions ORDER BY id").all()).results;
    expect(rows).toEqual([{ id: JOB(1), status: "Interrupted" }, { id: JOB(2), status: "Running" }]);
  });
});

describe("run tokens + per-user rate limit", () => {
  it("needs a logged-in user and an online backend", async () => {
    expect((await api("/api/runs/authorize", { body: { kind: "prepare" } })).status).toBe(401);
    const u = await user();
    expect((await api("/api/runs/authorize", { body: { kind: "prepare" }, token: u.token })).data.code).toBe("backend_offline");
  });

  it("issues a short-lived signed token naming the user, verifiable with the shared secret", async () => {
    const u = await user();
    await register();
    const r = await api("/api/runs/authorize", { body: { kind: "prepare" }, token: u.token });
    expect(r.status).toBe(200);
    const p = await verifyToken<any>(E.RUN_TOKEN_SECRET, r.data.token, "run");
    expect(p).toMatchObject({ kind: "prepare", sub: u.id, uname: u.name, aud: "agentmeter-backend" });
    expect(p.exp - p.iat).toBe(300);
    expect(p.jti).toMatch(/^[0-9a-f]{32}$/);
    expect(await verifyToken(E.ACCESS_TOKEN_SECRET, r.data.token, "run")).toBeNull();      // another secret: rejected
    expect((await api("/api/runs/authorize", { body: { kind: "rm -rf" }, token: u.token })).data.code).toBe("bad_kind");
    const files = await api("/api/runs/authorize", { body: { kind: "files", set: "csv_runs/x_1" }, token: u.token });
    expect((await verifyToken<any>(E.RUN_TOKEN_SECRET, files.data.token, "run"))!.set).toBe("csv_runs/x_1");
    expect((await api("/api/runs/authorize", { body: { kind: "files", set: "../etc" }, token: u.token })).status).toBe(400);
  });

  it("counts a wizard session (prepare + its benchmark) once; a second benchmark on it counts", async () => {
    const admin = await user("admin");
    await api("/api/admin/limits", { method: "PUT", token: admin.token, body: { sessions_per_hour: 2, max_queued_sessions: 10 } });
    const u = await user();
    await register();
    const auth = (body: Record<string, unknown>) => api("/api/runs/authorize", { body, token: u.token });
    for (let i = 0; i < 2; i++) {
      const p = await auth({ kind: "prepare" });
      expect(p.data.counted).toBe(true);
      const b = await auth({ kind: "benchmark", after_prepare: JOB(i), prepare_jti: p.data.jti });
      expect(b.data.counted).toBe(false);
      expect((await verifyToken<any>(E.RUN_TOKEN_SECRET, b.data.token, "run"))!.prepare_jti).toBe(p.data.jti);
    }
    const third = await auth({ kind: "prepare" });
    expect(third.status).toBe(429);
    expect(third.data.code).toBe("rate_limited");
    expect(Number(third.headers.get("Retry-After"))).toBeGreaterThan(3000);
    // the free benchmark is once per prepare grant: a second one on the same jti is counted (and refused here)
    const g = (await E.DB.prepare("SELECT jti FROM run_grants WHERE kind='prepare' LIMIT 1").first()).jti;
    expect((await auth({ kind: "benchmark", after_prepare: JOB(0), prepare_jti: g })).data.code).toBe("rate_limited");
    // another user's grant is not free for me
    const other = await user();
    const theirs = await api("/api/runs/authorize", { body: { kind: "benchmark", run: "csv_runs/a", prepare_jti: g }, token: other.token });
    expect(theirs.data.counted).toBe(true);
    // the limit is persistent, per user: the other user still has room
    expect((await api("/api/runs/authorize", { body: { kind: "prepare" }, token: other.token })).status).toBe(200);
  });

  it("refuses when the queue is full", async () => {
    const admin = await user("admin");
    await api("/api/admin/limits", { method: "PUT", token: admin.token, body: { max_queued_sessions: 1 } });
    await register();
    await backendCall("POST", "/api/backend/jobs", { job_id: JOB(5), kind: "benchmark", status: "queued", models: ["m/a"] });
    const r = await api("/api/runs/authorize", { body: { kind: "prepare" }, token: admin.token });
    expect(r.data.code).toBe("queue_full");
  });
});
