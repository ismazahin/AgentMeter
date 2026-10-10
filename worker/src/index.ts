// AgentMeter control plane (Cloudflare Worker). AgentMeter MEASURES LLM resource efficiency; it
// is not a threat-detection product. Routes and auth rules: docs/DEPLOY.md ("Control plane").
import { credentials, deleteUser, listUsers, patchUser, postUser } from "./admin";
import { changePassword, errorResponse, login, logout, publicUser, refresh, requireUser } from "./auth";
import { getBackend, heartbeat, register } from "./backend";
import { authorize } from "./runs";
import {
  backendGetFile, backendListSessions, backendSetIdentity, compareRoute, constraintsRoute, deleteSession, getFile, getResults, getSession, jobCreated, jobStatus,
  leaderboardRoute, listSessions, putFile, putResults, putSummary,
} from "./sessions";
import { telegramTest } from "./notify";
import { getLimits, getSettings, putLimits, putSettings } from "./settings";
import { Env, corsHeaders, fail, json } from "./util";

type Handler = (req: Request, env: Env, ...params: string[]) => Promise<Response>;
const ROUTES: [string, RegExp, Handler][] = [
  ["GET", /^\/api\/health$/, async () => json({ ok: true, service: "AgentMeter control plane — measures LLM resource efficiency (not a threat-detection product)" })],
  // auth
  ["POST", /^\/api\/auth\/login$/, login],
  ["POST", /^\/api\/auth\/refresh$/, refresh],
  ["POST", /^\/api\/auth\/logout$/, logout],
  ["GET", /^\/api\/me$/, async (req, env) => json({ user: publicUser((await requireUser(req, env)).user) })],
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
  ["PUT", /^\/api\/admin\/limits$/, putLimits],
  // backend state + runs
  ["GET", /^\/api\/backend$/, getBackend],
  ["POST", /^\/api\/runs\/authorize$/, authorize],
  // sessions (read: any user; delete: owner or admin)
  ["GET", /^\/api\/sessions$/, listSessions],
  ["GET", /^\/api\/sessions\/([^/]+)$/, getSession],
  ["DELETE", /^\/api\/sessions\/([^/]+)$/, deleteSession],
  ["GET", /^\/api\/sessions\/([^/]+)\/results$/, getResults],
  ["GET", /^\/api\/sessions\/([^/]+)\/constraints$/, constraintsRoute],
  ["GET", /^\/api\/sessions\/([^/]+)\/files\/([a-z_.]+)$/, getFile],
  ["GET", /^\/api\/compare$/, compareRoute],
  ["GET", /^\/api\/leaderboard$/, leaderboardRoute],
  // GPU backend (HMAC-signed with BACKEND_SECRET; user tokens are not accepted here)
  ["POST", /^\/api\/backend\/register$/, register],
  ["POST", /^\/api\/backend\/heartbeat$/, heartbeat],
  ["POST", /^\/api\/backend\/jobs$/, jobCreated],
  ["POST", /^\/api\/backend\/jobs\/([^/]+)\/status$/, jobStatus],
  ["PUT", /^\/api\/backend\/sessions\/([^/]+)\/results$/, putResults],
  ["PUT", /^\/api\/backend\/sessions\/([^/]+)\/summary$/, putSummary],
  ["PUT", /^\/api\/backend\/sessions\/([^/]+)\/files\/([a-z_.]+)$/, putFile],
  // prepared-set hash migration (scripts/migrate_prepared_set_hash.py)
  ["GET", /^\/api\/backend\/sessions$/, backendListSessions],
  ["GET", /^\/api\/backend\/sessions\/([^/]+)\/files\/([a-z_.]+)$/, backendGetFile],
  ["POST", /^\/api\/backend\/sessions\/([^/]+)\/identity$/, backendSetIdentity],
];

function requireSecrets(env: Env) {
  for (const k of ["ACCESS_TOKEN_SECRET", "RUN_TOKEN_SECRET", "BACKEND_SECRET", "PASSWORD_PEPPER"] as const)
    if (!env[k] || env[k].length < 16) fail(500, "misconfigured", `Worker secret ${k} is not set or shorter than 16 characters — run: npx wrangler secret put ${k} (inside the worker folder)`);
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
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
  },
};
