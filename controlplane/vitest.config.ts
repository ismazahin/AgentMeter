import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { setupFiles: ["./test/env.ts"], testTimeout: 30_000, hookTimeout: 60_000 },
});
