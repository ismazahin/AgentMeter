// Run authorization: the Worker issues short-lived signed run tokens; the GPU backend verifies
// them (agentmeter/server/runtoken.py) before Prepare / import / Benchmark / resume, and
// before serving prepared-set files. This replaces the shared passcode.
//
// Per-user rate limit, persistent in D1 (run_grants): sessions started in the last hour. A
// wizard session counts ONCE — its benchmark is free when it names the user's own prepare /
// import grant (prepare_jti) that has not yet given a free benchmark. The backend checks the
// link (the prepare job / imported set was created with that jti).
import { requireUser } from "./auth";
import { backendState } from "./backend";
import { randomHex, signToken } from "./crypto";
import { loadLimits } from "./settings";
import { Env, fail, json, nowIso, nowS, readJson } from "./util";

export const RUN_TTL_S = 300;
export const RUN_KINDS = ["prepare", "import", "benchmark", "resume"];
export const READ_KINDS = ["read", "files"];
const JOB_ID = /^job_\d{8}_\d{6}_([0-9a-f]{6}|[0-9a-f]{32})$/;
const SET_ID = /^(csv_runs|pcap_runs)\/[A-Za-z0-9_.-]{1,80}$/;

export async function authorize(req: Request, env: Env): Promise<Response> {
  const a = await requireUser(req, env);
  const b = await readJson<{ kind?: string; after_prepare?: string; prepare_jti?: string; run?: string; job_id?: string; set?: string }>(req);
  const kind = String(b.kind || "");
  if (![...RUN_KINDS, ...READ_KINDS].includes(kind)) fail(400, "bad_kind", `kind: ${[...RUN_KINDS, ...READ_KINDS].join(" | ")}`);
  const be = await backendState(env);
  if (!be.online) fail(503, "backend_offline", "GPU offline: the benchmark backend is not running");
  const now = nowS();
  const base = { typ: "run", jti: randomHex(16), sub: a.user.id, uname: a.user.username, kind, aud: "agentmeter-backend", iat: now, exp: now + RUN_TTL_S };

  if (kind === "read") return json({ token: await signToken(env.RUN_TOKEN_SECRET, base), expires_in: RUN_TTL_S, backend: be.url });
  if (kind === "files") {
    if (!SET_ID.test(String(b.set || ""))) fail(400, "bad_request", "set: csv_runs/<name> or pcap_runs/<name>");
    return json({ token: await signToken(env.RUN_TOKEN_SECRET, { ...base, set: b.set }), expires_in: RUN_TTL_S, backend: be.url });
  }

  const payload: Record<string, unknown> = { ...base };
  if (b.after_prepare !== undefined) {
    if (!JOB_ID.test(String(b.after_prepare))) fail(400, "bad_request", "after_prepare: a job id");
    payload.after_prepare = b.after_prepare;
  }
  if (b.run !== undefined) {
    if (!SET_ID.test(String(b.run))) fail(400, "bad_request", "run: csv_runs/<name> or pcap_runs/<name>");
    payload.run = b.run;
  }
  if (kind === "resume") {
    if (!JOB_ID.test(String(b.job_id || ""))) fail(400, "bad_request", "job_id: the job to resume");
    payload.job_id = b.job_id;
  }
  if (kind === "benchmark" && !payload.after_prepare && !payload.run) fail(400, "bad_request", "benchmark needs after_prepare or run");

  // free follow-up benchmark of the user's own wizard session?
  let counted = 1;
  if (kind === "benchmark" && b.prepare_jti) {
    const r = await env.DB.prepare(
      "UPDATE run_grants SET free_used=1 WHERE jti=? AND user_id=? AND kind IN ('prepare','import') AND free_used=0 AND issued_at>=?",
    ).bind(String(b.prepare_jti), a.user.id, nowIso((now - 6 * 3600) * 1000)).run();
    if (r.meta.changes) {
      counted = 0;
      payload.prepare_jti = b.prepare_jti;
    }
  }
  const limits = await loadLimits(env);
  if (counted) {
    const used = await env.DB.prepare("SELECT COUNT(*) AS n, MIN(issued_at) AS first FROM run_grants WHERE user_id=? AND counted=1 AND issued_at>=?")
      .bind(a.user.id, nowIso((now - 3600) * 1000)).first<{ n: number; first: string | null }>();
    if ((used?.n || 0) >= limits.sessions_per_hour) {
      const retry = used?.first ? Math.max(1, 3600 - Math.floor((Date.now() - Date.parse(used.first)) / 1000)) : 3600;
      fail(429, "rate_limited", `at most ${limits.sessions_per_hour} benchmark sessions per hour per user`, { "Retry-After": String(retry) });
    }
    const q = await env.DB.prepare("SELECT COUNT(*) AS n FROM sessions WHERE status='Running' AND deleted_at IS NULL").first<{ n: number }>();
    if ((q?.n || 0) >= limits.max_queued_sessions)
      fail(429, "queue_full", `the queue is full (${limits.max_queued_sessions} sessions waiting) — try again when one finishes`, { "Retry-After": "60" });
  }
  await env.DB.prepare(
    "INSERT INTO run_grants (jti,user_id,kind,after_prepare,prepare_jti,counted,issued_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
  ).bind(base.jti, a.user.id, kind, payload.after_prepare ?? null, payload.prepare_jti ?? null, counted, nowIso(now * 1000), nowIso(base.exp * 1000)).run();
  return json({ token: await signToken(env.RUN_TOKEN_SECRET, payload), jti: base.jti, counted: !!counted, expires_in: RUN_TTL_S, backend: be.url });
}
