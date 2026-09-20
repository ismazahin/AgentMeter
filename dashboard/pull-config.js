/* AgentMeter dashboard — admin pull/eval endpoint configuration (PLACEHOLDER).
 *
 * The pull/eval server (scripts/pull_eval_server.py) SERVES this dashboard and the
 * API from the SAME origin on a Vast.ai GPU instance. When you open the dashboard
 * from that server (http://<instance-ip>:<port>/), the API calls are same-origin,
 * so the base URL stays EMPTY and no CORS is involved.
 *
 * You only need a base URL when viewing this file directly over file:// (e.g. to
 * browse precomputed results locally) and want the admin panel to reach a remote
 * server. Two ways to set it, in priority order:
 *
 *   1. Type/paste it into the "Server base URL" field in the admin panel at
 *      runtime (remembered in this browser via localStorage).
 *   2. Edit PULL_BASE_URL below to a default (leave "" for same-origin).
 *
 * Example (file:// only): window.PULL_CONFIG = { PULL_BASE_URL: "http://203.0.113.7:8000" };
 */
window.PULL_CONFIG = {
  // Leave blank — same-origin when served by pull_eval_server.py. Set a value
  // here (or in the admin panel field) only for file:// viewing of a remote server.
  PULL_BASE_URL: "",
  // How often the dashboard polls /status while a pull/eval runs (ms).
  POLL_INTERVAL_MS: 3000
};
