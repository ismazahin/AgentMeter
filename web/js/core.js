// AgentMeter web app — core.js: globals, formatting, the backend and control-plane access (tokens, fetch), theme, tabs, popovers, routing, shared renderers.
// One of the app's scripts (web/app.html loads them in order: core, home, wizard, session, analysis,
// compare, settings, boot); they share the page's global scope, so a function defined in one is used by the others.
"use strict";
var CONFIG = null, API = "", HEALTH = null, ONLINE = false, offlineTimer = null;
var pollToken = 0;
var $ = function (id) { return document.getElementById(id); };
function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}
function enc(v) { return encodeURIComponent(v); }

// ---------- formatting ----------
function fmtTime(v) {
  if (v == null) return "—";
  return v < 1 ? (v * 1000).toFixed(v < 0.01 ? 2 : 1) + " ms" : v.toFixed(2) + " s";
}
function fmtNum(v, d) { return v == null ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d || 0, minimumFractionDigits: d || 0 }); }
function fmtPct(v) { return v == null ? "—" : (v * 100).toFixed(1) + "%"; }
function fmtMB(v) { return v == null ? "not measured" : fmtNum(v) + " MB"; }
function fmtSize(b) { return b >= 1048576 ? fmtNum(b / 1048576, 1) + " MB" : fmtNum(b / 1024, 1) + " KB"; }
function fmtUsd(v) { return v == null ? "—" : "$" + (v < 0.01 ? v.toFixed(5) : v.toFixed(4)); }
function fmtWh(v) { return v == null ? "—" : (v < 1 ? v.toFixed(4) : v.toFixed(2)) + " Wh"; }
function fmtDate(s) {
  if (!s) return "—";
  var tz = SETTINGS && SETTINGS.timezone, d = new Date(s);
  if (tz && tz !== "UTC" && !isNaN(d)) {                  // the user's time zone (Settings)
    try { return d.toLocaleString("sv-SE", { timeZone: tz }) + " " + tz; } catch (e) { /* unknown zone */ }
  }
  return String(s).replace("T", " ").replace("+00:00", " UTC").replace(/Z$/, " UTC");
}
function short(m) { return String(m || "").split("/").pop(); }
// the 5 study models (validated baseline) — mirrors configs/run_full_l4.yaml (tests/test_model_catalog.py checks it)
var STUDY_MODELS = ["mistralai/Mistral-7B-Instruct-v0.3", "meta-llama/Meta-Llama-3-8B-Instruct", "Qwen/Qwen2.5-7B-Instruct",
                    "microsoft/Phi-3-mini-4k-instruct", "google/gemma-2-9b-it"];
function isStudy(m) { return STUDY_MODELS.indexOf(m) >= 0; }
function extraTag(m) { return isStudy(m) ? "" : ' <span class="chip warn extra-tag" title="Extra model — not in the validated baseline. Quantized to 4-bit NF4 like the study models.">extra</span>'; }
// 128-bit job ids are long: show the date part + the first random hex digits (full id on hover)
function shortId(id) { id = String(id || ""); var m = /^(job_\d{8}_\d{6}_)([0-9a-f]{32})$/.exec(id); return m ? m[1] + m[2].slice(0, 8) + "…" : id; }
var METRIC_FMT = { s: fmtTime, MB: fmtMB, tokens: function (v) { return fmtNum(v); }, share: fmtPct,
                   "flows/s": function (v) { return v == null ? "—" : fmtNum(v, 2); }, USD: fmtUsd, Wh: fmtWh };

// ---------- backend location + access ----------
// Phase 47. CP = the control plane (Supabase Edge Function) URL from config.json "control_plane".
// With CP: log in (access token in memory, refresh token in localStorage — the page is served
// with a strict CSP), sessions / Compare / Leaderboard / settings come from the Worker, and the
// GPU backend's URL comes from the Worker too (the backend registers itself). Every backend
// request that starts work or reads stored work carries a short-lived run token the Worker
// issued (POST /api/runs/authorize). Without CP (local development) the backend is called
// directly, without tokens.
var CP = "", AUTH = { access: null, exp: 0, user: null }, SETTINGS = null, BACKEND = null;
// "Prepare on this computer" loads Pyodide from here (config.json "pyodide_base" can point elsewhere, e.g. a self-hosted copy)
var PYODIDE_BASE = "https://cdn.jsdelivr.net/pyodide/v0.28.3/full/";
var WORKER_PATHS = /^\/api\/(sessions(\/|\?|$)|compare(\/|\?|$)|leaderboard(\?|$)|settings(\/|$)|me(\/|$)|admin\/|backend$|runs\/|auth\/)/;
var STRONG_JOB = /^job_\d{8}_\d{6}_[0-9a-f]{32}$/;
var READ_TOK = null, FILE_TOK = {}, GRANTS = {};          // cached read/files tokens; jti of my prepare/import
// "Remember me" on: the refresh token is kept in localStorage (stays after the browser closes);
// off: in sessionStorage, so closing the browser logs out and the next visit starts at the login page.
var REMEMBER = false, IDLE_MIN = 0, LAST_ACTIVE = Date.now();
function stores() { var o = []; try { o.push(localStorage); } catch (e) { /* blocked */ } try { o.push(sessionStorage); } catch (e) { /* blocked */ } return o; }
function refreshGet() {
  try { var l = localStorage.getItem("agentmeter.refresh"); if (l) { REMEMBER = true; return l; } } catch (e) { /* blocked */ }
  try { var t = sessionStorage.getItem("agentmeter.refresh"); if (t) { REMEMBER = false; return t; } } catch (e) { /* blocked */ }
  return "";
}
function refreshSet(v) {
  stores().forEach(function (st) {
    try {
      if (v && st === (REMEMBER ? localStorage : sessionStorage)) st.setItem("agentmeter.refresh", v); else st.removeItem("agentmeter.refresh");
    } catch (e) { /* storage blocked */ }
  });
}
function parse(r) {
  return r.text().then(function (t) {
    var data = null;
    try { data = t ? JSON.parse(t) : null; } catch (e) { data = { error: t || r.statusText }; }
    return { ok: r.ok, status: r.status, data: data || {}, headers: r.headers };
  });
}
function netErr(where) { return { ok: false, status: 0, data: { error: "Cannot reach the " + where + ". It may be offline.", code: "network" } }; }
var refreshing = null;
function doRefresh() {
  var rt = refreshGet();
  if (!rt) return Promise.resolve(false);
  refreshing = refreshing || fetch(CP + "/api/auth/refresh", { method: "POST", credentials: "omit", headers: { "Content-Type": "application/json" },
                                                               body: JSON.stringify({ refresh_token: rt }) }).then(parse, function () { return netErr("control plane"); })
    .then(function (r) {
      refreshing = null;
      if (r.ok) { setAuth(r.data); return true; }
      if (r.status === 401) { refreshSet(""); AUTH = { access: null, exp: 0, user: null };
        if (r.data && r.data.code === "session_idle") LOGOUT_REASON = idleReason(); }
      return r.status === 0 ? null : false;
    });
  return refreshing;
}
function setAuth(d) {
  AUTH = { access: d.access_token, exp: Date.now() + (d.expires_in - 30) * 1000, user: d.user };
  if (d.session) { REMEMBER = !!d.session.remember; IDLE_MIN = d.session.idle_minutes || 0; }
  if (d.refresh_token) refreshSet(d.refresh_token);
}
// Inactivity logout ("Remember me" off): no click, key, scroll or touch for IDLE_MIN minutes.
var LOGOUT_REASON = "";
function idleReason() { return "You were logged out after " + (IDLE_MIN || 30) + " minutes without activity."; }
function markActive() { LAST_ACTIVE = Date.now(); }
["click", "keydown", "scroll", "touchstart", "pointermove"].forEach(function (ev) { window.addEventListener(ev, markActive, { passive: true }); });
setInterval(function () {
  if (!CP || !AUTH.user || REMEMBER || !IDLE_MIN) return;
  if (Date.now() - LAST_ACTIVE >= IDLE_MIN * 60000) { LOGOUT_REASON = idleReason(); logout(); }
}, 15000);
function cpFetch(path, opts, retried) {
  opts = Object.assign({ credentials: "omit" }, opts || {});
  var go = function () {
    var h = Object.assign({}, opts.headers || {});
    if (AUTH.access) h.Authorization = "Bearer " + AUTH.access;
    return fetch(CP + path, Object.assign({}, opts, { headers: h })).then(opts.raw ? function (r) { return r; } : parse, function () { return netErr("control plane"); });
  };
  var pre = AUTH.access && Date.now() < AUTH.exp ? Promise.resolve(true) : doRefresh();
  return pre.then(function () {
    return go().then(function (r) {
      if (r.status === 401 && !retried && /token_expired|auth_required/.test(((r.data || {}).code) || "")) {
        return doRefresh().then(function (ok) { if (ok) return cpFetch(path, opts, true); needLogin(); return r; });
      }
      if (r.status === 401 && /session_revoked|session_expired/.test(((r.data || {}).code) || "")) needLogin();
      return r;
    });
  });
}
// which run token a backend request needs (mirrors agentmeter/server/access.py required())
function backendNeed(method, path) {
  var p = path.split("?")[0], m;
  if (method !== "GET") {
    if (p === "/api/prepare") return { kind: "prepare" };
    if (p === "/api/prepared/import") return { kind: "import" };
    if (p === "/api/jobs") return { kind: "benchmark" };
    if (p === "/api/models/verify") return { kind: "verify" };
    if (p === "/api/schedules" || /^\/api\/schedules\/[^/]+\/cancel$/.test(p)) return { kind: "schedule" };
    if ((m = /^\/api\/jobs\/([^/]+)\/(resume|stop)$/.exec(p))) return { kind: m[2], job_id: decodeURIComponent(m[1]) };
    return null;
  }
  if (/^\/api\/(jobs|sessions|compare|leaderboard|models\/catalog|schedules|gpu|logs\/recent)$/.test(p) || /^\/api\/sessions\/[^/]+$/.test(p)) return { kind: "read" };
  if ((m = /^\/api\/jobs\/([^/]+)(\/(result|report\.pdf|constraints|flows|telemetry))?$/.exec(p))) return m[2] || !STRONG_JOB.test(decodeURIComponent(m[1])) ? { kind: "read" } : null;
  if ((m = /^\/api\/prepared\/([^/]+)\/([^/]+)(\/[^/]+)?$/.exec(p)) && m[1] !== "import") return m[3] ? { kind: "files", set: decodeURIComponent(m[1]) + "/" + decodeURIComponent(m[2]) } : { kind: "read" };
  return null;
}
function authorize(body) {
  return cpFetch("/api/runs/authorize", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(function (r) {
    if (r.ok && r.data.backend && r.data.backend !== API) { API = r.data.backend; }
    if (!r.ok && r.data.code === "backend_offline") gpuOffline();
    return r;
  });
}
function runToken(need, opts) {
  if (need.kind === "read") {
    if (READ_TOK && READ_TOK.until > Date.now()) return Promise.resolve({ ok: true, data: { token: READ_TOK.token } });
    return authorize({ kind: "read" }).then(function (r) { if (r.ok) READ_TOK = { token: r.data.token, until: Date.now() + (r.data.expires_in - 60) * 1000 }; return r; });
  }
  if (need.kind === "files") {
    var c = FILE_TOK[need.set];
    if (c && c.until > Date.now()) return Promise.resolve({ ok: true, data: { token: c.token } });
    return authorize(need).then(function (r) { if (r.ok) FILE_TOK[need.set] = { token: r.data.token, until: Date.now() + (r.data.expires_in - 60) * 1000 }; return r; });
  }
  var body = { kind: need.kind };
  if (need.kind === "resume" || need.kind === "stop") body.job_id = need.job_id;
  if (need.kind === "schedule") { try { body.job_id = JSON.parse(opts.body || "{}").job_id; } catch (e) { /* not JSON */ } }
  if (need.kind === "verify") { try { body.model = JSON.parse(opts.body || "{}").model; } catch (e) { /* not JSON */ } }
  if (need.kind === "benchmark") {
    var b = {};
    try { b = JSON.parse(opts.body || "{}"); } catch (e) { b = {}; }
    if (b.after_prepare) body.after_prepare = b.after_prepare;
    if (b.run) body.run = b.run;
    var g = GRANTS[b.after_prepare || b.run || ""];
    if (g && !g.used) body.prepare_jti = g.jti;          // the wizard's own prepare/import: counted once
  }
  return authorize(body);
}
function backendFetch(path, opts) {
  opts = Object.assign({ credentials: "omit" }, opts || {});
  var method = (opts.method || "GET").toUpperCase(), need = CP ? backendNeed(method, path) : null;
  var pre = need ? runToken(need, opts) : Promise.resolve(null);
  return pre.then(function (t) {
    if (t && !t.ok) return t;                              // rate_limited, queue_full, backend_offline, ...
    if (CP && !API) return { ok: false, status: 503, data: { error: "GPU offline: the benchmark backend is not running.", code: "backend_offline" } };
    var h = Object.assign({}, opts.headers || {});
    if (t) h.Authorization = "Bearer " + t.data.token;
    return fetch(API + path, Object.assign({}, opts, { headers: h })).then(opts.raw ? function (r) { return r; } : parse,
      function () { return netErr("GPU backend (" + (API || location.origin) + ")"); }).then(function (r) {
      if (r.ok && t && (need.kind === "prepare" || need.kind === "import") && t.data.jti && r.data) {
        GRANTS[r.data.job_id || r.data.prepared_set || ""] = { jti: t.data.jti };
      }
      if (r.ok && t && need.kind === "benchmark") {
        try { var b = JSON.parse(opts.body || "{}"); var g = GRANTS[b.after_prepare || b.run || ""]; if (g) g.used = true; } catch (e) { /* not JSON */ }
      }
      if (r.status === 401 && need && need.kind === "read") READ_TOK = null;
      return r;
    });
  });
}
function api(path, opts) {
  if (CP && WORKER_PATHS.test(path)) return cpFetch(path, opts);
  return backendFetch(path, opts);
}
// downloads that need a token: fetch + blob (an <a href> cannot send Authorization)
function authDownload(path, name) {
  var p = (CP && WORKER_PATHS.test(path)) ? cpFetch(path, { raw: true }) : backendFetch(path, { raw: true });
  return p.then(function (r) {
    if (!r || r.ok === false || (r.status && r.status >= 400)) {
      return (r.text ? parse(r) : Promise.resolve(r)).then(function (e) { alert((e.data && e.data.error) || "Unable to download " + name + ". Check that the GPU server is online, then try again."); });
    }
    return r.blob().then(function (b) {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(b); a.download = name; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
    });
  });
}
document.addEventListener("click", function (ev) {
  var a = ev.target.closest && ev.target.closest("a[data-auth-dl]");
  if (!a || !CP) return;                                   // without CP the plain href works
  ev.preventDefault();
  authDownload(a.getAttribute("data-auth-dl"), a.getAttribute("data-name") || "download");
});
function dlLink(path, name, label, cls) {
  return '<a class="' + (cls || "btn") + '" data-auth-dl="' + esc(path) + '" data-name="' + esc(name) + '" href="' + esc(CP ? "#" : apiUrl(path)) + '" download="' + esc(name) + '">' + esc(label || name) + '</a>';
}
function apiUrl(path) { return API + path; }
function showError(el, data) {
  el.hidden = false;
  el.innerHTML = esc((data && data.error) || "Unable to reach the server. Check your connection and that the GPU server is online, then try again.") + (data && data.code ? ' <code>' + esc(data.code) + '</code>' : "");
}
function setPath(id) { return String(id).split("/").map(enc).join("/"); }

// ---------- theme ----------
function themePref() { try { return localStorage.getItem("agentmeter.theme") || "system"; } catch (e) { return "system"; } }
function resolvedTheme() {
  var t = themePref();
  if (t === "dark" || t === "light") return t;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function applyTheme(t, fromAccount) {
  try { localStorage.setItem("agentmeter.theme", t); } catch (e) { /* not persisted; still applied */ }
  if (CP && AUTH.user && !fromAccount && SETTINGS && SETTINGS.theme !== t) saveSettings({ theme: t });
  if (t === "dark" || t === "light") document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
  if (typeof syncThemeToggle === "function" && $("theme-toggle")) syncThemeToggle();
  var f = document.querySelector("iframe.embed");
  if (f) f.src = analysisSrc(f.getAttribute("data-session"));
}
document.querySelectorAll('input[name="theme"]').forEach(function (r) {
  r.addEventListener("change", function () { if (r.checked) applyTheme(r.value); });
});
// sidebar: collapse to an icon rail (remembered per browser); labels stay for screen readers
function syncSide() {
  var c = document.documentElement.classList.contains("side-collapsed"), b = $("side-collapse");
  b.setAttribute("aria-expanded", String(!c));
  b.setAttribute("aria-label", c ? "Expand the sidebar" : "Collapse the sidebar"); b.title = b.getAttribute("aria-label");
}
$("side-collapse").addEventListener("click", function () {
  var c = document.documentElement.classList.toggle("side-collapsed");
  try { localStorage.setItem("agentmeter.side", c ? "collapsed" : "open"); } catch (e) { /* not remembered */ }
  syncSide();
});
syncSide();
// header switch: flips between light and dark (Settings › Theme still offers System)
function syncThemeToggle() {
  var b = $("theme-toggle"), next = resolvedTheme() === "dark" ? "light" : "dark";
  if (!b) return;
  b.setAttribute("aria-label", "Switch to " + next + " theme"); b.title = "Switch to " + next + " theme";
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", "#102A5C");            // the navy header band, in both themes
}
$("theme-toggle").addEventListener("click", function () {
  var t = resolvedTheme() === "dark" ? "light" : "dark";
  applyTheme(t);
  var r = $("set-theme-" + t); if (r) r.checked = true;
  syncThemeToggle();
});
if (window.matchMedia) {
  var mqT = window.matchMedia("(prefers-color-scheme: dark)");
  if (mqT.addEventListener) mqT.addEventListener("change", syncThemeToggle);
}
syncThemeToggle();

// ---------- tabs (keyboard: arrows move, Enter/Space activate) ----------
function tablist(container, onSelect) {
  var tabs = Array.prototype.slice.call(container.querySelectorAll('[role="tab"]'));
  tabs.forEach(function (t, i) {
    t.addEventListener("keydown", function (ev) {
      var d = ev.key === "ArrowRight" ? 1 : ev.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      ev.preventDefault();
      var n = tabs[(i + d + tabs.length) % tabs.length];
      n.focus(); onSelect(n);
    });
    t.addEventListener("click", function (ev) { ev.preventDefault(); onSelect(t); });
  });
}
function selectTab(tabs, active) {
  tabs.forEach(function (t) {
    var on = t === active;
    t.setAttribute("aria-selected", on ? "true" : "false");
    t.tabIndex = on ? 0 : -1;
    var p = $(t.getAttribute("aria-controls"));
    if (p) p.hidden = !on;
  });
}

// ---------- ⓘ: a short label's full explanation, on hover (title) and on tap/click (popover) ----------
function infoI(text) {
  return text ? '<span class="info-i" tabindex="0" role="button" data-tip="' + esc(text) + '" aria-label="' + esc(text) + '">i</span>' : "";
}
var TIP = null;
function closeTip() { if (TIP) { TIP.remove(); TIP = null; } }
function openTip(el, text) {
  closeTip();
  text = text || el.getAttribute("data-tip") || el.getAttribute("title") || el.getAttribute("aria-label");
  if (!text) return;
  TIP = document.createElement("div");
  TIP.className = "tip-pop"; TIP.setAttribute("role", "tooltip"); TIP.textContent = text;
  document.body.appendChild(TIP);
  placeTip(el);
}
function placeTip(el) {
  var r = el.getBoundingClientRect(), w = TIP.offsetWidth;
  TIP.style.top = (window.scrollY + r.bottom + 6) + "px";
  TIP.style.left = Math.max(8, Math.min(window.scrollX + r.left - 12, window.scrollX + document.documentElement.clientWidth - w - 8)) + "px";
}
document.addEventListener("click", function (ev) {
  var el = ev.target.closest && ev.target.closest(".info-i");
  if (el) { ev.preventDefault(); ev.stopPropagation(); if (TIP && TIP._for === el) closeTip(); else { openTip(el); TIP._for = el; } return; }
  closeTip();
}, true);
document.addEventListener("keydown", function (ev) {
  var el = document.activeElement;
  if (ev.key === "Escape") closeTip();
  else if ((ev.key === "Enter" || ev.key === " ") && el && el.classList && el.classList.contains("info-i")) { ev.preventDefault(); openTip(el); TIP._for = el; }
});
// a scroll closes an ⓘ popover; a chart mark's hover value follows its mark
window.addEventListener("scroll", function () { if (TIP && TIP._hover && TIP._for && TIP._for.isConnected) placeTip(TIP._for); else closeTip(); }, { passive: true });
// chart marks: hover / focus shows their value (data-hover), the same popover as ⓘ
document.addEventListener("mouseover", function (ev) {
  var el = ev.target.closest && ev.target.closest("[data-hover]");
  if (el && (!TIP || TIP._for !== el)) hoverTip(el);
  else if (!el && TIP && TIP._hover) closeTip();
});
document.addEventListener("focusin", function (ev) {
  var el = ev.target.closest && ev.target.closest("[data-hover]");
  if (el) hoverTip(el);
});
function hoverTip(el) { openTip(el, el.getAttribute("data-hover")); if (TIP) { TIP._for = el; TIP._hover = true; } }

// ---------- routing ----------
var VIEWS = ["home", "new", "sessions", "session", "datasets", "gpu", "leaderboard", "trends", "settings", "profile", "admin", "help", "login"];
function parseHash() {
  var h = location.hash.replace(/^#\/?/, ""), q = {}, qi = h.indexOf("?");
  if (qi >= 0) { new URLSearchParams(h.slice(qi + 1)).forEach(function (v, k) { q[k] = v; }); h = h.slice(0, qi); }
  return { parts: h.split("/").filter(Boolean).map(decodeURIComponent), q: q };
}
// Confirm a consequential action in a <dialog> whose buttons name it (not the browser's OK / Cancel).
// opts: { title: "Delete session …?", body: "What is lost.", ok: "Delete session", cancel: "Cancel", danger: true } -> Promise<boolean>
function confirmAction(opts) {
  var d = $("confirm-dlg"), ok = $("confirm-ok"), back = document.activeElement;
  $("confirm-title").textContent = opts.title;
  $("confirm-body").textContent = opts.body || ""; $("confirm-body").hidden = !opts.body;
  ok.textContent = opts.ok; $("confirm-cancel").textContent = opts.cancel || "Cancel"; ok.className = "btn " + (opts.danger ? "danger" : "primary");
  d.returnValue = "cancel";
  return new Promise(function (resolve) {
    d.addEventListener("close", function () {
      if (back && back.focus && document.contains(back)) back.focus();
      resolve(d.returnValue === "ok");
    }, { once: true });
    d.showModal(); $("confirm-cancel").focus();
  });
}
var LAST_VIEW = null;
function show(view) {
  closeMenu();
  VIEWS.forEach(function (v) { $("view-" + v).hidden = v !== view; });
  $("view-offline").hidden = true;
  document.querySelector(".mainnav").hidden = view === "login";
  if (view === "login") $("svc-gpu-off").hidden = true;
  $("side-account").hidden = view === "login";
  var nav = view === "session" ? "sessions" : view;
  document.querySelectorAll("[data-nav]").forEach(function (a) {
    if (a.getAttribute("data-nav") === nav) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  // a new view: name the tab after it and move focus to it (screen readers hear the change); not on first load
  var h = document.querySelector("#view-" + view + " h1");
  document.title = (view === "home" || !h ? "" : (view === "session" ? "Session" : h.textContent.trim()) + " · ") + "AgentMeter";
  if (LAST_VIEW !== null && LAST_VIEW !== view) $("main").focus({ preventScroll: true });
  LAST_VIEW = view;
  document.documentElement.classList.remove("boot-login");      // the first-paint login screen (app.html <head>) has done its job
}
function route() {
  pollToken++;                                    // stops every running poll loop
  closeTip();                                     // a popover never outlives its view
  var r = parseHash(), p = r.parts, v = p[0] || "home";
  // links from before Phase 46
  if (v === "prepare" || v === "upload") { location.replace("#/new"); return; }
  if (v === "benchmark") { location.replace("#/new?src=reuse"); return; }
  if (v === "jobs") { location.replace("#/sessions"); return; }
  if ((v === "prepared" || v === "run") && p.length === 3) { location.replace("#/new/models?set=" + enc(p[1] + "/" + p[2])); return; }
  if (v === "job" && p[1]) { legacyJob(p[1]); return; }
  if (CP && !AUTH.user) { show("login"); showLogin(); return; }
  if (v === "compare") {                         // Compare lives in Leaderboard › Compare models
    show("leaderboard"); lbTab("compare", true); loadCompare(r.q); return;
  }
  if (VIEWS.indexOf(v) < 0 || v === "login" || (v === "admin" && !isAdmin()) || (v === "profile" && !CP)) v = "home";
  show(v);
  if (v === "home") loadHome();
  else if (v === "new") routeWizard(p, r.q);
  else if (v === "sessions") loadSessions();
  else if (v === "session" && p[1]) loadSession(p[1], p[2] ? p[2] + (p[3] ? "/" + p[3] : "") : rememberedTab());
  else if (v === "leaderboard") lbTab(LB.tab === "compare" ? "setup" : LB.tab);
  else if (v === "trends") loadTrends(r.q);
  else if (v === "datasets") loadDatasets(r.q);
  else if (v === "gpu") loadGpu();
  else if (v === "settings") loadSettings();
  else if (v === "profile") loadProfile();
  else if (v === "admin") loadAdmin();
  if (!/^session$/.test(v) || !p[2]) window.scrollTo(0, 0);
}
function legacyJob(id) {
  api("/api/jobs/" + enc(id)).then(function (r) {
    if (r.ok && r.data.kind === "prepare") location.replace("#/new/models?prep=" + enc(id));
    else location.replace("#/session/" + enc(id));
  });
}

// ---------- shared renderers ----------
function statusChip(s) { return '<span class="status ' + esc(s) + '">' + esc(s) + '</span>'; }
function sessionLink(row) {
  var running = row.status === "Running";
  return '<a href="#/session/' + enc(row.session_id) + '" data-open="' + esc(row.session_id) + '">' + (running ? "Watch" : "Open") + '</a>';
}
function sessionsTable(rows, id, actions) {
  return '<table id="' + id + '"><thead><tr><th>Session</th><th>Input</th><th class="num">Flows</th><th>GPU</th><th>More efficient</th><th>Status</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>' +
    rows.map(function (s) {
      var tags = (s.tags || []).map(function (t) { return '<button type="button" class="chip tag-chip" data-tag="' + esc(t) + '" title="Show sessions tagged ' + esc(t) + '">' + esc(t) + '</button>'; }).join("");
      return '<tr><td><span class="mono" title="' + esc(s.session_id) + '">' + esc(shortId(s.session_id)) + '</span><br><span class="muted small">' + esc((s.models || []).map(short).join(" vs ")) +
        '</span>' + (tags ? '<div class="tag-row">' + tags + '</div>' : "") + (s.notes ? '<div class="small note-snip" title="' + esc(s.notes) + '">' + esc(s.notes.length > 90 ? s.notes.slice(0, 88) + "…" : s.notes) + '</div>' : "") +
        '</td><td>' + esc(s.input_type || "—") + '</td><td class="num">' + fmtNum(s.n_flows) + '</td><td>' + esc(s.gpu || "—") +
        '</td><td>' + esc(s.more_efficient ? (s.more_efficient.indexOf("/") > 0 ? short(s.more_efficient) : s.more_efficient) : "—") +
        '</td><td>' + statusChip(s.status) + '</td><td class="row-acts">' + sessionLink(s) +
        (actions && s.can_edit ? '<button type="button" class="btn small-btn" data-note="' + esc(s.session_id) + '" aria-expanded="false">Note</button>' : "") +
        (actions && s.can_edit && CP && s.status !== "Running" ? '<button type="button" class="btn small-btn danger-lite" data-del="' + esc(s.session_id) + '">Delete</button>' : "") + '</td></tr>';
    }).join("") + '</tbody></table>';
}
function hbars(title, rows, fmt, unitNote) {
  var vals = rows.map(function (r) { return r.v; }).filter(function (v) { return v != null; });
  var max = vals.length ? Math.max.apply(null, vals) : 0;
  return '<div><h3 style="margin-bottom:8px">' + esc(title) + '</h3><div class="hbars">' + rows.map(function (r, i) {
    var w = r.v == null || !max ? 0 : Math.max(2, 100 * r.v / max);
    return '<div class="hbar"><span class="lbl">' + esc(r.label) + '</span><span class="track" aria-hidden="true"><span class="fill" style="display:block;width:' + w +
      '%;background:' + (rows.length > 2 ? "var(--mseries-" + (i % 4 + 1) : "var(--series-" + (i % 2 + 1)) + ')"></span></span><span class="val">' + (r.v == null ? (unitNote || "not measured") : fmt(r.v)) +
      (r.extra ? '<br><span class="small muted">' + esc(r.extra) + '</span>' : "") + '</span></div>';
  }).join("") + '</div></div>';
}
