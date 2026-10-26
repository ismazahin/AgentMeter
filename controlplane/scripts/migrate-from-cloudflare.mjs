#!/usr/bin/env node
// Copy the control plane's data from Cloudflare (D1 + R2) to Supabase (Postgres + Storage), once.
// Runs on your laptop with your existing `npx wrangler login`; nothing is deleted on Cloudflare.
// Safe to re-run: rows already in Supabase are skipped (ON CONFLICT DO NOTHING), objects overwritten.
//
//   DATABASE_URL=<Supabase connection string> SUPABASE_URL=https://<ref>.supabase.co \
//   SUPABASE_SERVICE_ROLE_KEY=<service_role key> node scripts/migrate-from-cloudflare.mjs --dry-run
//   (then the same without --dry-run)
//
// Options: --d1 <name> (agentmeter)  --d1-id <database_id> (from the old wrangler.toml)
//          --r2 <bucket> (agentmeter-sessions)  --bucket <Supabase bucket> (agentmeter)
//          --no-users  copy sessions only (when the old PASSWORD_PEPPER is lost: passwords could
//                      not be checked anyway); owners keep their names, admins can edit everything
// WRANGLER overrides the wrangler command (default: npx --yes wrangler@4).
import { execSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import postgres from "postgres";

const argv = process.argv.slice(2);
const opt = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const DRY = argv.includes("--dry-run"), NO_USERS = argv.includes("--no-users");
const D1 = opt("--d1", "agentmeter"), D1_ID = opt("--d1-id", "e873f9c7-aaf0-40ab-9cac-4c9ac0ab98ac");
const R2 = opt("--r2", "agentmeter-sessions"), BUCKET = opt("--bucket", "agentmeter");
const { DATABASE_URL, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY } = process.env;
for (const [k, v] of Object.entries({ DATABASE_URL, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY }))
  if (!v) { console.error(`${k} must be set (see the header of this file)`); process.exit(2); }

// wrangler needs a config naming the D1 database; a throwaway one in a temp folder
const work = mkdtempSync(join(tmpdir(), "am-migrate-"));
writeFileSync(join(work, "wrangler.toml"),
  `name = "agentmeter-migrate"\ncompatibility_date = "2025-09-01"\n\n[[d1_databases]]\nbinding = "DB"\ndatabase_name = "${D1}"\ndatabase_id = "${D1_ID}"\n`);
const WR = process.env.WRANGLER || "npx --yes wrangler@4";
const sh = (args) => execSync(`${WR} ${args}`, { cwd: work, encoding: "utf8", maxBuffer: 512 * 1024 * 1024, stdio: ["ignore", "pipe", "inherit"] });
const q = (s) => `"${String(s).replace(/"/g, '\\"')}"`;
function d1(sqlText) {
  const out = JSON.parse(sh(`d1 execute ${D1} --remote --json --command ${q(sqlText)}`));
  return (Array.isArray(out) ? out[0] : out)?.results ?? [];
}
function r2(key) {
  const f = join(work, "obj");
  rmSync(f, { force: true });
  try { sh(`r2 object get ${q(`${R2}/${key}`)} --remote --file ${q(f)}`); } catch { return null; }
  try { return readFileSync(f); } catch { return null; }
}

// --- tables, in foreign-key order. rate_limits and backends are not copied (they refill by themselves).
const TABLES = NO_USERS ? ["system_settings", "sessions"]
  : ["users", "settings", "system_settings", "auth_sessions", "run_grants", "sessions"];
const PAGE = { sessions: 10 };              // a session row can hold up to 1 MB of results

const sql = postgres(DATABASE_URL, { prepare: false, onnotice: () => {} });
const TYPES = { "report.pdf": "application/pdf", "manifest.json": "application/json", "features.csv": "text/csv",
                "labels.csv": "text/csv", "flows.json": "application/json", "session_results.json": "application/json" };

async function putObject(key, bytes) {
  const name = key.split("/").pop();
  const r = await fetch(`${SUPABASE_URL.replace(/\/+$/, "")}/storage/v1/object/${BUCKET}/${key.split("/").map(encodeURIComponent).join("/")}`, {
    method: "POST", body: bytes,
    headers: { Authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`, apikey: SUPABASE_SERVICE_ROLE_KEY,
               "content-type": TYPES[name] || "application/octet-stream", "x-upsert": "true" } });
  if (!r.ok) throw new Error(`storage upload ${key}: HTTP ${r.status} ${(await r.text()).slice(0, 200)}`);
}

const keys = new Set();
const report = [];
try {
  for (const t of TABLES) {
    const total = d1(`SELECT COUNT(*) AS n FROM ${t}`)[0]?.n ?? 0;
    let copied = 0;
    const page = PAGE[t] || 200;
    for (let off = 0; off < total; off += page) {
      for (const row of d1(`SELECT * FROM ${t} ORDER BY rowid LIMIT ${page} OFFSET ${off}`)) {
        if (t === "sessions") {
          row.results_key = row.results_r2_key ?? null;
          delete row.results_r2_key;
          if (row.results_key) keys.add(row.results_key);
          for (const k of Object.values(row.files_json ? JSON.parse(row.files_json) : {})) keys.add(k);
          if (NO_USERS) row.owner_id = null;
        }
        if (!DRY) {
          const r = await sql`INSERT INTO ${sql(t)} ${sql(row)} ON CONFLICT DO NOTHING`;
          copied += r.count;
        }
      }
    }
    report.push(`${t}: ${total} in D1${DRY ? "" : `, ${copied} new in Supabase`}`);
    console.log(report[report.length - 1]);
  }
  let moved = 0, missing = 0;
  for (const k of keys) {
    if (DRY) continue;
    const bytes = r2(k);
    if (!bytes) { missing++; console.log(`  R2 object missing, skipped: ${k}`); continue; }
    await putObject(k, bytes);
    moved++;
  }
  console.log(`files: ${keys.size} referenced${DRY ? "" : `, ${moved} copied to Storage bucket "${BUCKET}", ${missing} missing in R2`}`);
  console.log(DRY ? "dry run: nothing was written. Run again without --dry-run." : "done. Cloudflare was not changed.");
} finally {
  await sql.end();
  rmSync(work, { recursive: true, force: true });
}
