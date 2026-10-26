// The Postgres schema (supabase/migrations, in order) and a PGlite with the number parsers the
// routes expect; shared by the tests and the dev server.
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { PGlite } from "@electric-sql/pglite";
import { NUMBER_TYPES } from "../../supabase/functions/agentmeter/src/db.ts";

const MIGRATIONS = fileURLToPath(new URL("../../supabase/migrations/", import.meta.url));

export const migrationSql = () => readdirSync(MIGRATIONS).filter((x) => x.endsWith(".sql")).sort().map((f) => readFileSync(MIGRATIONS + f, "utf8"));

export async function migrate(exec: (sql: string) => Promise<unknown>) {
  for (const m of migrationSql()) await exec(m);
}

export function newPglite(dataDir?: string) {
  return new PGlite(dataDir, { parsers: Object.fromEntries(NUMBER_TYPES.map((t) => [t, Number])) });
}
