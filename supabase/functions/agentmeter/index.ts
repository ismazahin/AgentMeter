// AgentMeter control plane — Supabase Edge Function (Deno). AgentMeter MEASURES LLM resource
// efficiency; it is not a threat-detection product. Every route is in src/app.ts; this file only
// connects it to Supabase: Postgres (SUPABASE_DB_URL), Storage (private bucket, service-role key,
// server side only) and the function's own secrets (supabase secrets set ...).
// Deploy: supabase functions deploy agentmeter --no-verify-jwt   (the app has its own logins)
import postgres from "npm:postgres@3.4.7";
import { handle } from "./src/app.ts";
import { Db, NUMBER_TYPES, postgresJs } from "./src/db.ts";
import { SupabaseStore } from "./src/store.ts";
import type { Env } from "./src/util.ts";

const E = Deno.env.toObject();
const sql = postgres(E.AGENTMETER_DB_URL || E.SUPABASE_DB_URL, {
  prepare: false,                     // works through Supabase's transaction pooler too
  max: 3,
  onnotice: () => {},
  types: { num: { to: 20, from: NUMBER_TYPES, parse: Number, serialize: String } } as any,
});

const env: Env = {
  ...(E as unknown as Env),
  DB: new Db(postgresJs(sql)),
  STORE: new SupabaseStore(E.SUPABASE_URL, E.SUPABASE_SERVICE_ROLE_KEY, E.STORAGE_BUCKET || "agentmeter"),
};

// Supabase serves the function under /agentmeter (/functions/v1/agentmeter from outside); the
// routes and the backend's signatures use the path after it.
const PREFIX = /^(\/functions\/v1)?\/agentmeter(?=\/|$)/;

Deno.serve((req) => {
  const u = new URL(req.url);
  const path = u.pathname.replace(PREFIX, "") || "/";
  if (path !== u.pathname) {
    u.pathname = path;
    req = new Request(u, req);
  }
  return handle(req, env);
});
