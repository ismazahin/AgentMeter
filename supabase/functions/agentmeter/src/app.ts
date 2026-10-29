// AgentMeter control plane: every route, runtime-neutral (Supabase Edge Function: ../index.ts; tests
// and the local dev server: controlplane/). AgentMeter MEASURES LLM resource efficiency; it is not
// a threat-detection product. Routes and auth rules: docs/DEPLOY.md ("Control plane").
import { credentials, deleteUser, listUsers, patchUser, postUser } from "./admin.ts";
import { changePassword, errorResponse, listDevices, login, logout, me, refresh, revokeDevice } from "./auth.ts";
import { getBackend, heartbeat, register } from "./backend.ts";
import { authorize } from "./runs.ts";
import { backendGetFile, backendListSessions, backendSetIdentity, compareCandidatesRoute, compareMergedRoute, compareRoute, constraintsRoute, deleteSession, getFile, getResults, getSession, jobCreated, jobStatus, commitUpload, leaderboardRoute, listSessions, patchSession, putFile, putResults, putSummary, uploadUrl } from "./sessions.ts";
import { telegramTest } from "./notify.ts";
import { createPrepJob, getPrepFile, getPrepJob, prepEvents, startPrepJob } from "./prep.ts";
import { getLimits, getSettings, putLimits, putSettings } from "./settings.ts";
import { backendModelState, backendModelVerified, getAdminModels, putAdminModels } from "./models.ts";
import { type Env, corsHeaders, fail, json } from "./util.ts";

type Handler = (req: Request, env: Env, ...params: string[]) => Promise<Response>;
const ROUTES: [string, RegExp, Handler][] = [
  ["GET", /^\/api\/health$/, async () => json({ ok: true, service: "AgentMeter control plane — measures LLM resource efficiency (not a threat-detection product)" })],
  // auth
  ["POST", /^\/api\/auth\/login$/, login],
  ["POST", /^\/api\/auth\/refresh$/, refresh],
  ["POST", /^\/api\/auth\/logout$/, logout],
  ["GET", /^\/api\/me$/, me],
  ["GET", /^\/api\/me\/devices$/, listDevices],
  ["DELETE", /^\/api\/me\/devices\/([0-9a-f]{32})$/, revokeDevice],
  ["POST", /^\/api\/me\/password$/, changePassword],
  ["GET", /^\/api\/settings$/, getSettings],
  ["PUT", /^\/api\/settings$/, putSettings],
  ["POST", /^\/api\/settings\/telegram-test$/, telegramTest],
  // admin
  ["GET", /^\/api\/admin\/users$/, listUsers],
  ["POST", /^\/api\/admin\/users$/, postUser],
  ["PATCH", /^\/api\/admin\/users\/([0-9a-f]{32})$/, patchUser],
  ["DELETE", /^\/api\/admin\/users\/([0-9a-f]{32})$/, deleteUser],
  ["GET", /^\/api\/admin\/credentials$/, credentials],
  ["GET", /^\/api\/admin\/limits$/, getLimits],
  ["GET", /^\/api\/admin\/models$/, getAdminModels],
  ["PUT", /^\/api\/admin\/models$/, putAdminModels],
  ["PUT", /^\/api\/admin\/limits$/, putLimits],
  // backend state + runs
  ["GET", /^\/api\/backend$/, getBackend],
  ["POST", /^\/api\/runs\/authorize$/, authorize],
  // cloud data preparation while the GPU benchmarks (prep.ts; cloudprep/)
  ["POST", /^\/api\/prep\/jobs$/, createPrepJob],
  ["GET", /^\/api\/prep\/jobs\/([0-9a-f]{32})$/, getPrepJob],
  ["POST", /^\/api\/prep\/jobs\/([0-9a-f]{32})\/start$/, startPrepJob],
  ["GET", /^\/api\/prep\/jobs\/([0-9a-f]{32})\/files\/(manifest\.json|features\.csv|labels\.csv)$/, getPrepFile],
  ["POST", /^\/api\/prep\/jobs\/([0-9a-f]{32})\/events$/, prepEvents],
  // sessions (read: any user; delete: owner or admin)
  ["GET", /^\/api\/sessions$/, listSessions],
  ["GET", /^\/api\/sessions\/([^/]+)$/, getSession],
  ["DELETE", /^\/api\/sessions\/([^/]+)$/, deleteSession],
  ["PATCH", /^\/api\/sessions\/([^/]+)$/, patchSession],
  ["GET", /^\/api\/sessions\/([^/]+)\/results$/, getResults],
  ["GET", /^\/api\/sessions\/([^/]+)\/constraints$/, constraintsRoute],
  ["GET", /^\/api\/sessions\/([^/]+)\/files\/([a-z_.]+)$/, getFile],
  ["GET", /^\/api\/compare$/, compareRoute],
  ["GET", /^\/api\/compare\/candidates$/, compareCandidatesRoute],
  ["GET", /^\/api\/compare\/merged$/, compareMergedRoute],
  ["GET", /^\/api\/leaderboard$/, leaderboardRoute],
  // GPU backend (HMAC-signed with BACKEND_SECRET; user tokens are not accepted here)
  ["POST", /^\/api\/backend\/register$/, register],
  ["GET", /^\/api\/backend\/model-state$/, backendModelState],
  ["POST", /^\/api\/backend\/models\/verified$/, backendModelVerified],
  ["POST", /^\/api\/backend\/heartbeat$/, heartbeat],
  ["POST", /^\/api\/backend\/jobs$/, jobCreated],
  ["POST", /^\/api\/backend\/jobs\/([^/]+)\/status$/, jobStatus],
  ["PUT", /^\/api\/backend\/sessions\/([^/]+)\/results$/, putResults],
  ["PUT", /^\/api\/backend\/sessions\/([^/]+)\/summary$/, putSummary],
  ["PUT", /^\/api\/backend\/sessions\/([^/]+)\/files\/([a-z_.]+)$/, putFile],
  ["POST", /^\/api\/backend\/sessions\/([^/]+)\/upload-url$/, uploadUrl],
  ["POST", /^\/api\/backend\/sessions\/([^/]+)\/commit$/, commitUpload],
  // prepared-set hash migration (scripts/migrate_prepared_set_hash.py)
  ["GET", /^\/api\/backend\/sessions$/, backendListSessions],
  ["GET", /^\/api\/backend\/sessions\/([^/]+)\/files\/([a-z_.]+)$/, backendGetFile],
  ["POST", /^\/api\/backend\/sessions\/([^/]+)\/identity$/, backendSetIdentity],
];

function requireSecrets(env: Env) {
  for (const k of ["ACCESS_TOKEN_SECRET", "RUN_TOKEN_SECRET", "BACKEND_SECRET", "PASSWORD_PEPPER"] as const)
    if (!env[k] || env[k].length < 16) fail(500, "misconfigured", `control-plane secret ${k} is not set or shorter than 16 characters — run: supabase secrets set ${k}=...`);
}

/** One request, its URL path being the route ("/api/..."): a host that serves the API under a
 *  prefix (Supabase: /functions/v1/agentmeter) strips it first (../index.ts), so backend
 *  signatures, which cover the route path, verify the same everywhere. */
export async function handle(req: Request, env: Env): Promise<Response> {
  const cors = corsHeaders(env, req);
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
  let res: Response;
  try {
    requireSecrets(env);
    const path = new URL(req.url).pathname;
    let matched = false;
    res = json({ error: "not found", code: "not_found" }, 404);
    for (const [method, re, h] of ROUTES) {
      const m = re.exec(path);
      if (!m) continue;
      matched = true;
      if (method !== req.method) continue;
      res = await h(req, env, ...m.slice(1).map(decodeURIComponent));
      matched = false;
      break;
    }
    if (matched) res = json({ error: "method not allowed", code: "method_not_allowed" }, 405);
  } catch (e) {
    res = errorResponse(e);
  }
  const out = new Response(res.body, res);
  for (const [k, v] of Object.entries(cors)) out.headers.set(k, v);
  return out;
}
