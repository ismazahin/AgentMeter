import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-plugin";
import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig(async () => {
  const migrations = await readD1Migrations(path.join(import.meta.dirname, "migrations"));
  return {
    plugins: [
      cloudflareTest({
        wrangler: { configPath: "./wrangler.toml" },
        miniflare: {
          bindings: {
            TEST_MIGRATIONS: migrations,
            ACCESS_TOKEN_SECRET: "test-access-secret-0123456789",
            RUN_TOKEN_SECRET: "test-run-secret-0123456789abcd",
            BACKEND_SECRET: "test-backend-secret-0123456789",
            PASSWORD_PEPPER: "test-pepper-0123456789abcdef",
            ALLOW_HTTP_BACKEND: "1",
            RESULTS_R2_THRESHOLD: "2000",
            ALLOWED_ORIGINS: "http://127.0.0.1:8080",
          },
        },
      }),
    ],
    test: { setupFiles: ["./test/apply-migrations.ts"] },
  };
});
