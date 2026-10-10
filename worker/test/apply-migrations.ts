import { applyD1Migrations, env as testEnv } from "cloudflare:test";
import { beforeEach } from "vitest";

const env = testEnv as any;

await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);

// Each test starts from an empty control plane.
const TABLES = ["auth_sessions", "settings", "run_grants", "sessions", "rate_limits", "system_settings", "backends", "users"];
beforeEach(async () => {
  await env.DB.batch(TABLES.map((t) => env.DB.prepare(`DELETE FROM ${t}`)));
  const listed = await env.R2.list();
  if (listed.objects.length) await env.R2.delete(listed.objects.map((o: { key: string }) => o.key));
});
