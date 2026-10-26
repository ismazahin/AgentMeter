#!/usr/bin/env node
// One-time bootstrap of the FIRST admin (no public sign-up; there is no HTTP route for this).
// Hashes the password locally exactly like the Worker (PBKDF2-SHA256 over HMAC(pepper, pw)) and
// inserts the user with `wrangler d1 execute`, ONLY if the users table is empty.
//
//   PASSWORD_PEPPER=<same value as the Worker secret> node scripts/bootstrap-admin.mjs <username> [--local|--remote]
//   (the password is read from AGENTMETER_ADMIN_PASSWORD or prompted on stdin)
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline/promises";
import { webcrypto as crypto } from "node:crypto";

const enc = new TextEncoder();
const b64url = (b) => Buffer.from(b).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const [, , username, where = "--remote"] = process.argv;
if (!username || !/^[A-Za-z0-9_.-]{3,32}$/.test(username) || !["--local", "--remote"].includes(where)) {
  console.error("usage: PASSWORD_PEPPER=... node scripts/bootstrap-admin.mjs <username> [--local|--remote]");
  process.exit(2);
}
const pepper = process.env.PASSWORD_PEPPER;
if (!pepper || pepper.length < 16) { console.error("PASSWORD_PEPPER (the Worker secret) must be set, >= 16 chars"); process.exit(2); }
let password = process.env.AGENTMETER_ADMIN_PASSWORD;
if (!password) {
  const rl = createInterface({ input: process.stdin, output: process.stderr });
  password = (await rl.question("Admin password (10+ characters): ")).trim();
  rl.close();
}
if (password.length < 10) { console.error("password: at least 10 characters"); process.exit(2); }
const iterations = parseInt(process.env.PBKDF2_ITERATIONS || "40000", 10);
const hk = await crypto.subtle.importKey("raw", enc.encode(pepper), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
const peppered = new Uint8Array(await crypto.subtle.sign("HMAC", hk, enc.encode(password)));
const salt = crypto.getRandomValues(new Uint8Array(16));
const pk = await crypto.subtle.importKey("raw", peppered, "PBKDF2", false, ["deriveBits"]);
const hash = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations }, pk, 256);
const id = [...crypto.getRandomValues(new Uint8Array(16))].map((x) => x.toString(16).padStart(2, "0")).join("");
const now = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
const q = (s) => `'${String(s).replace(/'/g, "''")}'`;
const sql = `INSERT INTO users (id, username, role, pw_hash, pw_salt, pw_iter, disabled, must_change_pw, created_at, updated_at)
SELECT ${q(id)}, ${q(username.toLowerCase())}, 'admin', ${q(b64url(hash))}, ${q(b64url(salt))}, ${iterations}, 0, 0, ${q(now)}, ${q(now)}
WHERE NOT EXISTS (SELECT 1 FROM users);`;
const db = process.env.D1_DATABASE || "agentmeter";
const persist = where === "--local" && process.env.WRANGLER_PERSIST_TO ? ["--persist-to", process.env.WRANGLER_PERSIST_TO] : [];
// run the project's own wrangler with this Node (no npx / shell: works the same on Windows,
// where npx is npx.cmd and cannot be spawned directly)
const wrangler = fileURLToPath(new URL("../node_modules/wrangler/bin/wrangler.js", import.meta.url));
const d1 = (command) => JSON.parse(execFileSync(process.execPath, [wrangler, "d1", "execute", db, where, ...persist, "--json", "--command", command],
                                                { encoding: "utf8", stdio: ["ignore", "pipe", "inherit"] }));
d1(sql);
// local D1 does not report meta.changes: check that OUR row is there
const created = (d1(`SELECT COUNT(*) AS n FROM users WHERE id=${q(id)}`)?.[0]?.results?.[0]?.n ?? 0) > 0;
if (!created) { console.error("not created: the users table is not empty (bootstrap runs once). Ask an admin to create users."); process.exit(1); }
console.log(`admin "${username}" created (${where.slice(2)}). Log in from the app; create the other users under Admin.`);
