// The control plane under test: the real routes (supabase/functions/agentmeter/src/app.ts) on
// PGlite (real PostgreSQL, in-process) with the real migrations, and an in-memory object store.
// Each test file gets its own database; each test starts from empty tables.
// TEST_DATABASE_URL=postgres://... runs the same tests through postgres.js (the Edge Function's
// driver) on a real server instead, each test file in its own throwaway schema.
import postgres from "postgres";
import { afterAll, beforeEach } from "vitest";
import { handle } from "../../supabase/functions/agentmeter/src/app.ts";
import { Db, NUMBER_TYPES, type Querier, pglite, postgresJs } from "../../supabase/functions/agentmeter/src/db.ts";
import { MemoryStore } from "../../supabase/functions/agentmeter/src/store.ts";

import { migrate, newPglite } from "./env-db.ts";


let exec: (sql: string) => Promise<unknown>;
let querier: Querier;
const URL_ = process.env.TEST_DATABASE_URL;
if (URL_) {
  const schema = "t_" + Math.random().toString(36).slice(2, 10);
  const admin = postgres(URL_, { onnotice: () => {} });
  await admin.unsafe(`CREATE SCHEMA ${schema}`);
  const num = { to: 20, from: NUMBER_TYPES, parse: Number, serialize: String };
  const sql = postgres(URL_, { onnotice: () => {}, prepare: false, connection: { search_path: schema }, types: { num } as any });
  await migrate((m) => sql.unsafe(m));
  querier = postgresJs(sql);
  exec = (q) => sql.unsafe(q);
  afterAll(async () => { await sql.end(); await admin.unsafe(`DROP SCHEMA ${schema} CASCADE`); await admin.end(); });
} else {
  const pg = newPglite();
  await migrate((m) => pg.exec(m));
  querier = pglite(pg);
  exec = (q) => pg.exec(q);
}
const store = new MemoryStore("http://store.test");

export const env = {
  DB: new Db(querier),
  STORE: store,
  ACCESS_TOKEN_SECRET: "test-access-secret-0123456789",
  RUN_TOKEN_SECRET: "test-run-secret-0123456789abcd",
  BACKEND_SECRET: "test-backend-secret-0123456789",
  PASSWORD_PEPPER: "test-pepper-0123456789abcdef",
  ALLOW_HTTP_BACKEND: "1",
  RESULTS_INLINE_MAX: "2000",
  ALLOWED_ORIGINS: "http://127.0.0.1:8080",
  TELEGRAM_BOT_TOKEN: "test-bot-token-SECRET",
  TELEGRAM_API: "https://telegram.test",
};

export const fetchApi = (url: string, init?: RequestInit) => handle(new Request(url, init), env);

const TABLES = ["prep_jobs", "auth_sessions", "settings", "run_grants", "sessions", "rate_limits", "system_settings", "backends", "users"];
beforeEach(async () => {
  await exec(`TRUNCATE ${TABLES.join(", ")}`);
  store.objects.clear();
});
