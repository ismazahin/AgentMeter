// Local control plane (no Supabase account needed): the same routes as the Edge Function on
// PGlite (Postgres in-process, saved under DATA_DIR) or, with DATABASE_URL, on a Postgres server;
// large files in memory, uploaded through single-use URLs this server serves itself, like Storage.
// Serves the API under /functions/v1/agentmeter (as Supabase does) and at the root.
//
//   node --experimental-transform-types dev-server.ts            (npm run dev)
//   env: PORT (8787), DATA_DIR (.dev-data), DATABASE_URL, AGENTMETER_ADMIN_USER + AGENTMETER_ADMIN_PASSWORD
//        (created when there are no users yet), and the control-plane secrets (dev defaults below).
import { createServer } from "node:http";
import { mkdirSync } from "node:fs";
import postgres from "postgres";
import { handle } from "../supabase/functions/agentmeter/src/app.ts";
import { createUser } from "../supabase/functions/agentmeter/src/admin.ts";
import { Db, NUMBER_TYPES, type Querier, pglite, postgresJs } from "../supabase/functions/agentmeter/src/db.ts";
import { MemoryStore } from "../supabase/functions/agentmeter/src/store.ts";
import { migrate, newPglite } from "./test/env-db.ts";

const P = process.env;
const port = Number(P.PORT || 8787);
const base = `http://127.0.0.1:${port}`;

let q: Querier;
if (P.DATABASE_URL) {
  const sql = postgres(P.DATABASE_URL, { prepare: false, onnotice: () => {}, types: { num: { to: 20, from: NUMBER_TYPES, parse: Number, serialize: String } } as any });
  q = postgresJs(sql);
  await migrate((s) => sql.unsafe(s));
} else {
  const dir = P.DATA_DIR || ".dev-data";
  mkdirSync(dir, { recursive: true });
  const pg = newPglite(`${dir}/pg`);
  q = pglite(pg);
  await migrate((s) => pg.exec(s));
}
const store = new MemoryStore(base);
const dev = (k: string, v: string) => P[k] || v;
const env = {
  ...(P as Record<string, string>),
  DB: new Db(q),
  STORE: store,
  ACCESS_TOKEN_SECRET: dev("ACCESS_TOKEN_SECRET", "dev-access-secret-0123456789"),
  RUN_TOKEN_SECRET: dev("RUN_TOKEN_SECRET", "dev-run-secret-0123456789abc"),
  BACKEND_SECRET: dev("BACKEND_SECRET", "dev-backend-secret-0123456789"),
  PASSWORD_PEPPER: dev("PASSWORD_PEPPER", "dev-pepper-0123456789abcdef"),
  ALLOW_HTTP_BACKEND: dev("ALLOW_HTTP_BACKEND", "1"),
  ALLOWED_ORIGINS: dev("ALLOWED_ORIGINS", "http://127.0.0.1:8080"),
  PUBLIC_BASE_URL: dev("PUBLIC_BASE_URL", `${base}/functions/v1/agentmeter`),   // where cloudprep reports
};

if (P.AGENTMETER_ADMIN_USER && P.AGENTMETER_ADMIN_PASSWORD) {
  const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM users").first<{ n: number }>();
  if (!n?.n) {
    await createUser(env as any, P.AGENTMETER_ADMIN_USER, P.AGENTMETER_ADMIN_PASSWORD, "admin", false);
    console.log(`admin "${P.AGENTMETER_ADMIN_USER}" created`);
  }
}

const PREFIX = /^(\/functions\/v1)?\/agentmeter(?=\/|$)/;

createServer(async (req, res) => {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  const body = Buffer.concat(chunks);
  const url = new URL(req.url || "/", base);
  try {
    const up = /^\/upload\/([0-9a-f-]{36})$/.exec(url.pathname);
    // like Storage: a browser may PUT to a signed upload URL from the app's origin (cloud preparation)
    const cors = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "PUT, OPTIONS", "Access-Control-Allow-Headers": "content-type, x-upsert" };
    if (up && req.method === "OPTIONS") { res.writeHead(204, cors).end(); return; }
    if (up && req.method === "PUT") {
      const ok = await store.acceptUpload(up[1], new Uint8Array(body), String(req.headers["content-type"] || ""));
      res.writeHead(ok ? 200 : 400, { "content-type": "application/json", ...cors }).end(JSON.stringify(ok ? { ok } : { error: "invalid upload token" }));
      return;
    }
    const down = /^\/download\/([0-9a-f-]{36})$/.exec(url.pathname);
    if (down && req.method === "GET") {
      const bytes = await store.serveDownload(down[1]);
      res.writeHead(bytes ? 200 : 404, { "content-type": "application/octet-stream" }).end(bytes ? Buffer.from(bytes) : undefined);
      return;
    }
    url.pathname = url.pathname.replace(PREFIX, "") || "/";
    const headers = new Headers();
    for (const [k, v] of Object.entries(req.headers)) if (typeof v === "string") headers.set(k, v);
    const r = await handle(new Request(url, { method: req.method, headers, body: ["GET", "HEAD"].includes(req.method || "") ? undefined : body }), env as any);
    const out: Record<string, string> = {};
    r.headers.forEach((v, k) => { out[k] = v; });
    res.writeHead(r.status, out).end(Buffer.from(await r.arrayBuffer()));
  } catch (e) {
    console.error(e);
    res.writeHead(500).end();
  }
}).listen(port, "127.0.0.1", () => console.log(`AgentMeter control plane (dev) on ${base}/functions/v1/agentmeter`));
