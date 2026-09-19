/* AgentMeter dashboard — admin pull/eval endpoint configuration (PLACEHOLDER).
 *
 * The pull/eval server (scripts/pull_eval_server.py) prints a fresh public ngrok
 * URL every time it starts, so the base URL is NEVER hard-coded. Two ways to set
 * it, in priority order:
 *
 *   1. Type/paste it into the "ngrok base URL" field in the dashboard's admin
 *      panel at runtime (it is remembered in this browser via localStorage).
 *   2. Edit PULL_BASE_URL below to a default (leave "" to require step 1).
 *
 * Example: window.PULL_CONFIG = { PULL_BASE_URL: "https://abcd-1-2-3-4.ngrok-free.app" };
 */
window.PULL_CONFIG = {
  // Leave blank — paste the ngrok URL printed by pull_eval_server.py into the
  // admin panel field. Set a value here only if you want a baked-in default.
  PULL_BASE_URL: "",
  // How often the dashboard polls /status while a pull/eval runs (ms).
  POLL_INTERVAL_MS: 3000
};
