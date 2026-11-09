// AgentMeter web app — boot.js: start-up: config.json -> control plane / backend -> online or offline (runs last).
// One of the app's scripts (web/app.html loads them in order: core, home, wizard, session, analysis,
// compare, settings, boot); they share the page's global scope, so a function defined in one is used by the others.
"use strict";
// ---------- boot: config.json -> /health -> online | offline ----------
function badge(h) {
  var b = $("svc-backend-badge");
  if (!h) { b.className = "chip-gpu off"; b.textContent = "GPU offline · viewing"; b.title = "No GPU server connected: pages show stored results; new runs wait until it is back."; return; }
  if (h.provider === "real") {
    var g = h.gpu || {};
    b.className = "chip-gpu real";
    b.textContent = "Real GPU: " + (g.name || "unknown");
  } else { b.className = "chip-gpu demo"; b.textContent = "Demo mode (no GPU)"; }
  b.title = (h.provider === "real" ? "Runs use real models on this GPU" + ((h.gpu || {}).vram_total_mb ? " (" + fmtNum(h.gpu.vram_total_mb / 1024) + " GB)" : "")
                                   : "DEMO (mock): runs use a deterministic heuristic, not a language model") +
    (h.queue ? " · queue: " + (h.queue.running ? "1 running" : "idle") + ", " + (h.queue.queued || 0) + " queued" : "");
}
function runModeLine() {
  var h = HEALTH || {};
  $("svc-run-mode").innerHTML = h.provider === "real"
    ? '<span class="chip ok">Real GPU: ' + esc((h.gpu || {}).name || "unknown") + '</span> Runs the selected models in 4-bit NF4 on this GPU, one at a time.'
    : '<span class="chip warn">DEMO (mock)</span> No GPU: the run uses a deterministic heuristic, not a language model. Numbers are not model measurements.';
  if (typeof idsOptionSync === "function") idsOptionSync();
}
function health(timeoutMs) {
  var ctl = window.AbortController ? new AbortController() : null;
  var t = setTimeout(function () { if (ctl) ctl.abort(); }, timeoutMs || 6000);
  return fetch(API + "/health", { credentials: "omit", cache: "no-store", signal: ctl ? ctl.signal : undefined })
    .then(function (r) { clearTimeout(t); return r.ok ? r.json() : null; })
    .then(function (h) { return h && h.ok ? h : null; }, function () { clearTimeout(t); return null; });
}
// GPU offline = viewing mode: the menu and every page stay; pages show what is stored (with a
// control plane: every session, data set, ranking) and only starting a run waits for the GPU.
function goOffline() {
  var was = ONLINE;
  ONLINE = false; HEALTH = null; badge(null);
  $("svc-gpu-off").hidden = false; $("svc-demo").hidden = true; $("view-offline").hidden = true;
  document.documentElement.classList.remove("boot-login");
  clearTimeout(offlineTimer);
  offlineTimer = setTimeout(CP ? checkControlPlaneBackend : checkBackend, 30000);   // comes back by itself when the GPU is rented
  if (CP) { if (!$("view-new").hidden) routeWizard(parseHash().parts, parseHash().q); return; }
  if (was || !SHOWN) { SHOWN = true; route(); }
}
var SHOWN = false;
$("svc-gpu-retry").addEventListener("click", function () { this.disabled = true; var b = this; (CP ? checkControlPlaneBackend() : checkBackend()).then(function () { b.disabled = false; }); });
function gpuOffline() { if (CP) { API = ""; goOffline(); } }
function goOnline(h) {
  var first = !ONLINE;
  ONLINE = true; HEALTH = h; badge(h); runModeLine(); apiVersionCheck(h);
  $("view-offline").hidden = true; $("svc-gpu-off").hidden = true; SHOWN = true;
  if (CP) { clearTimeout(offlineTimer); offlineTimer = setTimeout(checkControlPlaneBackend, 30000); }
  if (!first) return Promise.resolve();
  return api("/api/service/config").then(function (r) {
    CONFIG = r.ok ? r.data : { canonical_models: [], max_models: 4, provider: "mock", base_config: null, demo_mode: true, note: "Service config unavailable." };
    $("svc-demo").hidden = !CONFIG.demo_mode;
    $("svc-demo").innerHTML = '<b>Demo mode:</b> no GPU, so results are simulated, not model measurements.' + infoI(CONFIG.note || "");
    $("svc-max-flows").value = (SETTINGS && SETTINGS.default_flows) || CONFIG.default_max_flows || 50;
    if (SETTINGS) $("svc-other-attack").checked = !!SETTINGS.default_other_attack;
    var L = CONFIG.limits;
    if (L) {
      $("svc-limits").innerHTML = "Maximum upload: " + esc(L.max_upload_label) + "; bigger files: Import from a link." +
        infoI("For larger files use Import from a link (up to " + L.max_url_download_label + "). Large inputs are sampled across the whole file.");
      $("svc-url-limits").textContent = "A direct download link, up to " + L.max_url_download_label + ".";
    }
    if (CP) { if (!$("view-new").hidden) routeWizard(parseHash().parts, parseHash().q); }
    else route();
  });
}
// the GPU backend must speak at least this API version (agentmeter/server/apiversion.py); an older
// one (the site was deployed, the backend not restarted) is named instead of failing quietly
var MIN_BACKEND = [1, 1];
function apiVersionCheck(h) {
  var v = String((h && h.api_version) || "1.0.0").split(".").map(Number), old = v[0] < MIN_BACKEND[0] || (v[0] === MIN_BACKEND[0] && (v[1] || 0) < MIN_BACKEND[1]);
  var box = $("svc-api-old");
  box.hidden = !old;
  if (old) box.innerHTML = '<b>The GPU backend is older than this website</b> (its API ' + esc((h && h.api_version) || "1.0") + '; this site needs ' + MIN_BACKEND.join(".") +
    '). Pull the latest code on the GPU server and restart it; until then GPU &amp; Queue and the newest features may not work.';
}
function checkBackend() { return health().then(function (h) { if (h) goOnline(h); else goOffline(); }); }
// control plane: where is the GPU backend, and is it up? (it registers + heartbeats itself)
function checkControlPlaneBackend() {
  if (!AUTH.user) return Promise.resolve();
  return api("/api/backend").then(function (r) {
    BACKEND = r.ok ? r.data : null;
    if (!BACKEND || !BACKEND.online || !BACKEND.url) { API = ""; return goOffline(); }
    if (API !== BACKEND.url) { API = BACKEND.url; ONLINE = false; }
    return health().then(function (h) { if (h) return goOnline(h); API = ""; goOffline(); });
  });
}
$("svc-retry").addEventListener("click", function () { $("svc-offline-detail").textContent = "Checking…"; checkBackend(); });
window.addEventListener("hashchange", function () { route(); });
if (window.matchMedia) {
  var mq = window.matchMedia("(prefers-color-scheme: dark)");
  var onScheme = function () { if (themePref() === "system") applyTheme("system", true); };
  if (mq.addEventListener) mq.addEventListener("change", onScheme);
}
// config.json: {"control_plane": "<Supabase function URL>"} (production) or {"api_base": "<backend>"} (development,
// also ?api=<url>, remembered; ?api=reset forgets it). ?cp=<url> / ?cp=reset does the same for the control plane.
function resolveConfig() {
  var qs = new URLSearchParams(location.search), qa = qs.get("api"), qc = qs.get("cp"), saved = {};
  try {
    if (qa === "reset") localStorage.removeItem("agentmeter.api"); else if (qa) localStorage.setItem("agentmeter.api", qa.replace(/\/+$/, ""));
    if (qc === "reset") localStorage.removeItem("agentmeter.cp"); else if (qc) localStorage.setItem("agentmeter.cp", qc.replace(/\/+$/, ""));
    saved = { api: localStorage.getItem("agentmeter.api"), cp: localStorage.getItem("agentmeter.cp") };
  } catch (e) { /* storage blocked: config.json only */ }
  return fetch("config.json", { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : {}; }, function () { return {}; })
    .then(function (c) {
      c = c || {};
      if (c.pyodide_base) PYODIDE_BASE = String(c.pyodide_base).replace(/\/*$/, "/");
      return { cp: String(saved.cp || c.control_plane || "").replace(/\/+$/, ""), api: String(saved.api || c.api_base || "").replace(/\/+$/, "") };
    });
}
resolveConfig().then(function (c) {
  CP = c.cp;
  if (!CP) { API = c.api; checkBackend(); return; }
  badge(null); $("svc-backend-badge").textContent = "GPU: checking…";
  doRefresh().then(function (ok) {
    if (!ok) { needLogin(); if (ok === null) showError($("login-error"), { error: "Cannot reach the control plane (" + CP + ")." }); return; }
    if (AUTH.user && AUTH.user.must_change_pw) { refreshSet(""); needLogin(); return; }   // log in again to set a new password
    afterLogin();
  });
});
