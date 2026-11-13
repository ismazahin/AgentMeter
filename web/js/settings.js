// AgentMeter web app — settings.js: Settings, Admin (users, model catalog, audit log, problems), login / logout, profile.
// One of the app's scripts (web/app.html loads them in order: core, home, wizard, session, analysis,
// compare, settings, boot); they share the page's global scope, so a function defined in one is used by the others.
"use strict";
// ---------- SETTINGS ----------
// Tabs (#/settings/<tab>); every control saves on change (PUT /api/settings merges a partial patch)
// and says "Saved" next to its section. The Account tab is what the Profile page was.
var TZS = (function () { try { return Intl.supportedValuesOf("timeZone"); } catch (e) { return ["UTC", "Asia/Kuala_Lumpur", "Europe/London", "America/New_York"]; } })();
var ST_TABS = ["notify", "defaults", "weights", "look", "account", "backend"];
var ST = { tab: "notify", link: null };
function stTab(t) {
  var tabs = ST_TABS.filter(function (x) { return !$("settab-" + x).hidden; });
  if (tabs.indexOf(t) < 0) t = tabs[0];
  ST.tab = t;
  ST_TABS.forEach(function (x) {                       // every panel, including those of hidden tabs
    var on = x === t;
    $("settab-" + x).setAttribute("aria-selected", String(on)); $("settab-" + x).tabIndex = on ? 0 : -1;
    $("setp-" + x).hidden = !on;
  });
  if (t === "account") loadProfile();
}
$("set-tabs").addEventListener("click", function (ev) {
  var b = ev.target.closest("[data-settab]"); if (!b) return;
  history.replaceState(null, "", "#/settings/" + b.getAttribute("data-settab")); stTab(b.getAttribute("data-settab"));
});
$("set-tabs").addEventListener("keydown", function (ev) {
  if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
  var tabs = ST_TABS.filter(function (x) { return !$("settab-" + x).hidden; }), i = tabs.indexOf(ST.tab);
  var n = tabs[(i + (ev.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
  history.replaceState(null, "", "#/settings/" + n); stTab(n); $("settab-" + n).focus(); ev.preventDefault();
});
// a section's save: the patch, then "Saved" (or the error) next to that section's heading
function stSave(where, patch) {
  var el = $("sv-" + where);
  if (el) { el.className = "saved"; el.textContent = "Saving…"; }
  return saveSettings(patch).then(function (r) {
    if (el) { el.className = "saved" + (r.ok ? "" : " err"); el.textContent = r.ok ? "Saved" : ((r.data || {}).error || "Not saved"); }
    return r;
  });
}
// default models: the study models (always known), plus extra models offered here; `chosen` stays ticked
function renderSetModels(chosen) {
  var models = (CONFIG && CONFIG.canonical_models && CONFIG.canonical_models.length ? CONFIG.canonical_models : STUDY_MODELS).slice();
  ((CATALOG && CATALOG.extra) || []).forEach(function (x) { if (x.available && models.indexOf(x.id) < 0) models.push(x.id); });
  chosen.forEach(function (m) { if (models.indexOf(m) < 0) models.push(m); });
  $("set-models").innerHTML = models.map(function (m, i) {
    return '<label class="model"><input type="checkbox" value="' + esc(m) + '" id="set-model-' + i + '"' + (chosen.indexOf(m) >= 0 ? " checked" : "") + '><span class="mono">' + esc(m) + '</span>' + extraTag(m) + '</label>';
  }).join("");
  var sync = function () {
    var on = $("set-models").querySelectorAll("input:checked").length;
    $("set-models").querySelectorAll("input").forEach(function (x) { x.disabled = !x.checked && on >= ((CONFIG && CONFIG.max_models) || 4); });
  };
  $("set-models").querySelectorAll("input").forEach(function (inp) { inp.addEventListener("change", function () { sync(); stSave("defaults", { default_models: pickedDefaults() }); }); });
  sync();
}
function pickedDefaults() { return Array.prototype.map.call($("set-models").querySelectorAll("input:checked"), function (i) { return i.value; }); }
function loadSettings(tab) {
  // the extra models arrive later: refresh ONLY the model list, keeping what is ticked
  if (ONLINE && !CATALOG) loadCatalog().then(function (c) {
    if (c && !$("view-settings").hidden && CP) renderSetModels(pickedDefaults());
  });
  var t = (SETTINGS && SETTINGS.theme) || themePref();
  ($("set-theme-" + t) || $("set-theme-system")).checked = true;
  var h = HEALTH || {};
  $("set-backend").innerHTML = '<dt>Address</dt><dd class="mono">' + esc(API || location.origin) + '</dd><dt>Mode</dt><dd>' +
    (!ONLINE ? "offline" : h.provider === "real" ? "Real GPU: " + esc((h.gpu || {}).name || "unknown") : "Demo (mock), no GPU") + '</dd>' +
    '<dt>Access</dt><dd>' + (h.auth_required ? "run tokens from a control plane" : "open (local development)") + '</dd>';
  // without a control plane (one GPU server, no accounts) only Appearance and Backend exist, kept in this browser
  ["notify", "defaults", "weights", "account"].forEach(function (x) { $("settab-" + x).hidden = !CP; });
  $("settab-backend").hidden = !!CP;
  $("set-intro").textContent = CP ? "Your preferences. Changes save as you make them." : "Saved in this browser only.";
  $("set-error").hidden = true;
  stTab(tab || (CP ? ST.tab : "look"));
  if (!CP) return;
  var S = SETTINGS || {};
  $("set-tg").value = S.telegram_chat_id || "";
  var nv = document.querySelector('#set-notify input[value="' + (S.notify_on || "none") + '"]'); if (nv) nv.checked = true;
  $("set-drift").checked = S.alert_on_drift !== false;
  $("set-flows").value = S.default_flows || 50; $("set-tz").value = S.timezone || "UTC";
  $("set-tz-list").innerHTML = TZS.map(function (z) { return '<option value="' + esc(z) + '">'; }).join("");
  renderSetModels(S.default_models || []);
  renderPresets(S.weight_presets || []);
  ["sv-notify", "sv-defaults", "sv-weights"].forEach(function (id) { $(id).textContent = ""; });
  $("set-tg-status").textContent = "";
  tgSteps();
  if (!ST.link) api("/api/settings/telegram-link").then(function (r) {   // the bot's Start link, with this user's one-time code
    if (r.ok) { ST.link = r.data; tgSteps(); }
  });
}
// ---- Telegram: three steps (open the bot, connect the chat, test) ----
function tgSteps() {
  var id = ((SETTINGS || {}).telegram_chat_id || "").trim(), connected = !!id, l = ST.link;
  $("set-tg-chip").className = "chip " + (connected ? "ok" : "warn");
  $("set-tg-chip").textContent = connected ? "Connected" : "Not set up";
  $("settab-notify-n").hidden = connected;
  var o = $("set-tg-open");
  if (l && l.url) { o.href = l.url; o.removeAttribute("aria-disabled"); } else { o.removeAttribute("href"); o.setAttribute("aria-disabled", "true"); }
  ["1", "2", "3"].forEach(function (n) { $("set-step-" + n).className = ""; });
  if (connected) { $("set-step-1").className = "done"; $("set-step-2").className = "done"; $("set-step-3").className = "now"; }
  else $("set-step-1").className = "now";
  $("set-tg-test").disabled = !connected;
}
$("set-tg-open").addEventListener("click", function (ev) {
  if (!ST.link || !ST.link.url) { ev.preventDefault(); $("set-tg-status").textContent = "The bot is not set up yet. Ask the admin to add the Telegram bot token."; return; }
  $("set-step-1").className = "done"; $("set-step-2").className = "now";
  $("set-tg-status").textContent = "In Telegram, press Start. Then come back and press Find my chat.";
});
$("set-tg-find").addEventListener("click", function () {
  var st = $("set-tg-status"), go = function () {
    st.textContent = "Looking for your chat…";
    api("/api/settings/telegram-link", { method: "POST" }).then(function (r) {
      if (!r.ok) { st.textContent = r.data.error; return; }
      var d = r.data;
      if (d.linked) {
        SETTINGS = SETTINGS || {}; SETTINGS.telegram_chat_id = d.chat_id; $("set-tg").value = d.chat_id;
        ST.link = null; tgSteps();
        st.textContent = "Connected" + (d.name ? " to " + d.name : "") + ". Now send a test message.";
        return;
      }
      st.textContent = d.reason === "expired" ? "That link has expired. Press Open in Telegram again, then Start."
        : d.reason === "not_found" ? "No Start found yet. Open the bot with the button above and press Start, then try again."
        : d.reason === "webhook" ? "The bot is connected to another service (a webhook), so AgentMeter cannot read its messages. Paste your chat ID instead."
        : "Telegram did not answer. Try again in a moment.";
      if (d.reason === "expired") api("/api/settings/telegram-link").then(function (x) { if (x.ok) { ST.link = x.data; tgSteps(); } });
    });
  };
  go();
});
$("set-tg").addEventListener("change", function () {
  var v = $("set-tg").value.trim();
  saveSettings({ telegram_chat_id: v }).then(function (r) {
    $("set-tg-status").textContent = r.ok ? (v ? "Chat ID saved." : "Telegram disconnected.") : r.data.error;
    tgSteps();
  });
});
$("set-tg-test").addEventListener("click", function () {
  $("set-tg-status").textContent = "Sending…";
  api("/api/settings/telegram-test", { method: "POST" }).then(function (t) {
    $("set-tg-status").textContent = t.ok ? (t.data.ok ? "Sent. Check Telegram." : "Telegram refused the message. Check that you pressed Start in the bot, and the chat ID.") : t.data.error;
    if (t.ok && t.data.ok) { $("set-step-3").className = "done"; }
  });
});
$("set-notify").addEventListener("change", function (ev) { if (ev.target.name === "notify_on") stSave("notify", { notify_on: ev.target.value }); });
$("set-drift").addEventListener("change", function () { stSave("notify", { alert_on_drift: $("set-drift").checked }); });
$("set-flows").addEventListener("change", function () {
  var n = parseInt($("set-flows").value, 10);
  if (!(n >= 1 && n <= 500)) { $("sv-defaults").className = "saved err"; $("sv-defaults").textContent = "Flows: a number from 1 to 500"; return; }
  stSave("defaults", { default_flows: n });
});
$("set-tz").addEventListener("change", function () { stSave("defaults", { timezone: $("set-tz").value.trim() || "UTC" }); });
// ---- Ranking weights (SAW presets) ----
function renderPresets(list) {
  $("set-presets").innerHTML = list.map(function (p, i) {
    var w = p.weights || {};
    return '<div class="preset" data-i="' + i + '"><div class="field name"><label for="pre-name-' + i + '">Name</label><input type="text" id="pre-name-' + i + '" data-k="name" value="' + esc(p.name || "") + '" maxlength="40"></div>' +
      [["latency", "Speed"], ["vram", "GPU memory"], ["tokens", "Tokens"]].map(function (k) {
        return '<div class="field"><label for="pre-' + k[0] + '-' + i + '">' + k[1] + '</label><input id="pre-' + k[0] + '-' + i + '" data-k="' + k[0] + '" type="number" min="0" max="1" step="0.05" value="' + esc(w[k[0]] != null ? w[k[0]] : 0.25) + '"></div>';
      }).join("") + '<button type="button" class="btn small-btn" data-rm="' + i + '">Remove</button></div>';
  }).join("") || '<p class="muted small">No weight mixes yet. Every page ranks with the default weights until you add one.</p>';
  $("set-presets").querySelectorAll("[data-rm]").forEach(function (b) { b.addEventListener("click", function () {
    var l = readPresets(); l.splice(+b.getAttribute("data-rm"), 1); renderPresets(l); savePresets();
  }); });
  $("set-presets").querySelectorAll("input").forEach(function (inp) { inp.addEventListener("change", savePresets); });
}
function readPresets() {
  return Array.prototype.map.call($("set-presets").querySelectorAll(".preset"), function (row) {
    var o = { name: "", weights: { accuracy: 0 } };     // sessions rank on efficiency only
    row.querySelectorAll("input").forEach(function (inp) { var k = inp.getAttribute("data-k"); if (k === "name") o.name = inp.value.trim(); else o.weights[k] = +inp.value || 0; });
    return o;
  });
}
function savePresets() { return stSave("weights", { weight_presets: readPresets() }); }
$("set-preset-add").addEventListener("click", function () {
  var l = readPresets();
  if (l.length >= 5) return;
  l.push({ name: "Mix " + (l.length + 1), weights: { accuracy: 0, latency: 0.35, vram: 0.35, tokens: 0.3 } });
  renderPresets(l); savePresets();
});
function saveSettings(patch) {
  return api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) }).then(function (r) {
    if (r.ok) SETTINGS = r.data.settings;
    return r;
  });
}
$("set-password").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var m = $("set-pw-msg");
  api("/api/me/password", { method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ current_password: $("set-pw-cur").value, new_password: $("set-pw-new").value }) }).then(function (r) {
    m.hidden = false; m.className = "notice " + (r.ok ? "" : "error");
    m.textContent = r.ok ? "Password changed. Your other devices were logged out." : r.data.error;
    if (r.ok) { $("set-pw-cur").value = ""; $("set-pw-new").value = ""; loadProfile(); }
  });
});

// ---------- ADMIN: tabs (#/admin/<tab>) and the Overview (tiles + setup checklist + system) ----------
var AT_TABS = ["overview", "users", "models", "limits", "audit"];
var ADM = { tab: "overview", creds: null, ids: undefined, users: null, health: null };
function atTab(t) {
  if (AT_TABS.indexOf(t) < 0) t = "overview";
  ADM.tab = t;
  AT_TABS.forEach(function (x) {
    var on = x === t;
    $("atab-" + x).setAttribute("aria-selected", String(on)); $("atab-" + x).tabIndex = on ? 0 : -1;
    $("at-" + x).hidden = !on;
  });
}
$("adm-tabs").addEventListener("click", function (ev) {
  var b = ev.target.closest("[data-atab]"); if (!b) return;
  history.replaceState(null, "", "#/admin/" + b.getAttribute("data-atab")); atTab(b.getAttribute("data-atab"));
});
$("adm-tabs").addEventListener("keydown", function (ev) {
  if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
  var i = AT_TABS.indexOf(ADM.tab), n = AT_TABS[(i + (ev.key === "ArrowRight" ? 1 : AT_TABS.length - 1)) % AT_TABS.length];
  history.replaceState(null, "", "#/admin/" + n); atTab(n); $("atab-" + n).focus(); ev.preventDefault();
});
function admTiles() {
  var us = ADM.users, B = BACKEND || {}, h = ADM.health;
  var since = Date.now() - 7 * 864e5, week = ADM.week;
  var gpu = ONLINE ? ((HEALTH && HEALTH.gpu && HEALTH.gpu.name) || (HEALTH && HEALTH.provider === "real" ? "a GPU" : "demo mode")) : null;
  var tile = function (v, cls, label, href, link) { return '<div><div class="v' + (cls ? " " + cls : "") + '">' + v + '</div><div class="l">' + label + '</div><a href="' + href + '">' + link + '</a></div>'; };
  $("adm-tiles").innerHTML =
    tile(us ? fmtNum(us.length) : "…", "", us ? (us.filter(function (u) { return !u.disabled; }).length === us.length ? "users, all active" : "users, " + us.filter(function (u) { return u.disabled; }).length + " disabled") : "users", "#/admin/users", "Manage users") +
    tile(week == null ? "…" : fmtNum(week), "", "benchmarks in the last 7 days", "#/sessions", "See sessions") +
    tile(ONLINE ? "Online" : "Offline", ONLINE ? "ok" : "bad", ONLINE ? "GPU server, " + esc(gpu) : "GPU server" + (B.last_heartbeat_at ? ", last seen " + esc(fmtDate(B.last_heartbeat_at)) : ", never connected"), "#/help", "How to start it") +
    tile(h == null ? "…" : h.offline ? "–" : fmtNum(h.n), h && !h.offline && h.n ? "bad" : "", h && h.offline ? "problems (the GPU server is offline)" : "problems in 24 hours", "#/admin/overview", "See below");
  void since;
}
function admCheck() {
  var c = ADM.creds || {}, items = [], st = function (k) { return (c[k] || {}).status; };
  var tg = st("telegram_bot_token");
  items.push([tg === "set" ? "ok" : "no", "Telegram bot token", tg === "set" ? "Alerts can be sent." : "Not set. Add TELEGRAM_BOT_TOKEN as a Supabase secret.", "cred-telegram_bot_token"]);
  items.push(["ok", "Control plane", "Connected. Accounts, sessions and settings are stored.", "cred-control-plane"]);
  var B = BACKEND || {}, ps = B.prep_service;
  items.push([B.cloud_prep || (ps && ps.online) ? "ok" : "idk", "Preparation service", B.cloud_prep ? "Running." : ps && ps.online ? "Running on a computer." : "Not running now. Continue in New benchmark starts it.", "cred-prep"]);
  var ids = ADM.ids;
  items.push([ids === undefined ? "idk" : ids && !ids.demo ? "ok" : "no", "IDS model", ids === undefined ? "Checking…" : ids ? (ids.demo ? "A demo model is stored. Upload a real one for real results." : "Stored: " + (ids.version || ids.name) + ".") : "None stored. New GPUs use the small demo IDS.", "cred-ids", ids && !ids.demo ? "" : '<a class="btn small-btn" href="#/admin/models">Upload</a>']);
  ["hf_token", "vast_api_key"].forEach(function (k) {
    var s = st(k), name = k === "hf_token" ? "Hugging Face token" : "Vast API key";
    items.push([s === "set" ? "ok" : s === "not set" ? "no" : "idk", name,
      s === "set" ? "Set on the GPU server." : s === "not set" ? "Not set on the GPU server." : "Checked when the GPU server starts.", "cred-" + k]);
  });
  var ready = items.filter(function (x) { return x[0] === "ok"; }).length;
  $("adm-check-chip").className = "chip " + (ready === items.length ? "ok" : "warn");
  $("adm-check-chip").textContent = ready + " of " + items.length + " ready";
  $("adm-creds").innerHTML = items.map(function (x) {
    return '<li class="' + x[0] + '" id="' + x[3] + '"><span class="dot" aria-hidden="true">' + (x[0] === "ok" ? "✓" : x[0] === "no" ? "!" : "?") + '</span><div><b>' + esc(x[1]) + '</b><span class="d">' + esc(x[2]) + '</span></div>' + (x[4] || "<span></span>") + '</li>';
  }).join("");
}
function admSystem() {
  var B = BACKEND || {};
  $("adm-system").innerHTML = '<dt>Control plane</dt><dd class="mono">' + esc(CP || "none") + '</dd>' +
    '<dt>GPU server</dt><dd>' + (ONLINE ? '<span class="mono">' + esc(API || B.url || "") + '</span> (' + esc(B.mode || ((HEALTH || {}).provider === "real" ? "real GPU" : "demo")) + ')' : "none registered, offline") + '</dd>' +
    '<dt>Website</dt><dd class="mono">' + esc(location.origin) + '</dd>' +
    '<dt>You</dt><dd>' + esc(AUTH.user.username) + ' (admin)</dd>';
}

// ---------- ADMIN ----------
function isAdmin() { return !!(AUTH.user && AUTH.user.role === "admin"); }
// ---------- Admin: model catalog ----------
var VERIFYING = {};                       // model -> job id while a verification runs
function loadAdminModels() {
  $("adm-models-error").hidden = true;
  var state = api("/api/admin/models"), cat = ONLINE ? api("/api/models/catalog") : Promise.resolve(null);
  Promise.all([state, cat]).then(function (rs) {
    var st = rs[0], ct = rs[1];
    if (!st.ok) { showError($("adm-models-error"), st.data); return; }
    if (ct && ct.ok) CATALOG = ct.data;
    renderAdminModels(st.data.state, ct && ct.ok ? ct.data : null);
  });
}
function renderAdminModels(state, cat) {
  var real = cat && cat.mode === "real";
  $("adm-models-mode").textContent = !cat ? "GPU offline" : real ? "real GPU, verification required" : "demo, no verification";
  var rows = (cat ? cat.extra.slice() : []), cfg = state.models || {};
  (state.added || []).forEach(function (a) {             // added since the GPU's last heartbeat: listed now
    if (!rows.some(function (x) { return x.id === a.id; })) rows.push({ id: a.id, source: "admin", pending: !!cat });
  });
  if (!cat) $("adm-models-status").textContent = "The GPU is offline. Suitability and Verify need the GPU. You can still enable or disable models.";
  var study = (cat && cat.study) || ((CONFIG && CONFIG.canonical_models) || STUDY_MODELS).map(function (m) { return { id: m }; });
  var studyHtml = '<p class="small muted">Study models are the validated baseline and need no verification. They are downloaded the first time a benchmark uses them. Tick one to download it when a rental starts instead.</p>' +
    '<table id="adm-study-table"><thead><tr><th>Study model</th><th>On this server</th><th>Download at start-up</th></tr></thead><tbody>' +
    study.map(function (x) {
      var dl = (cfg[x.id] || {}).download_at_start != null ? cfg[x.id].download_at_start : !!x.download_at_start;
      return '<tr data-study="' + esc(x.id) + '"><td class="mono">' + esc(x.id) + '</td><td>' +
        (x.on_disk == null ? '<span class="muted">—</span>' : x.on_disk ? '<span class="chip ok">on disk</span>'
          : '<span class="small muted">first use downloads' + (x.download_gb != null ? " ~" + fmtNum(x.download_gb, 1) + " GB" : "") + '</span>') +
        '</td><td><input type="checkbox" data-dlstart="' + esc(x.id) + '" aria-label="Download ' + esc(x.id) + ' at start-up"' + (dl ? " checked" : "") + '></td></tr>';
    }).join("") + '</tbody></table><p class="small muted">Extra models:</p>';
  $("adm-models").innerHTML = studyHtml + '<table id="adm-models-table"><thead><tr><th>Model</th><th>Suitability</th><th>4-bit verification</th><th>Offered to users</th><th>Enabled</th><th>Download at start-up</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>' +
    rows.map(function (x) {
      var c = cfg[x.id] || {}, v = x.verification || (c.verified ? c.verified : { status: "not_verified" });
      // the control plane holds the admin's choices; the GPU's copy can be a heartbeat behind
      var enabled = c.enabled != null ? c.enabled : x.enabled != null ? x.enabled : x.source !== "admin";
      var dl = c.download_at_start != null ? c.download_at_start : !!x.download_at_start;
      var suit = !x.status ? '<span class="muted">—</span>' :
        '<span class="chip ' + ({ ok: "ok", warning: "warn", blocked: "bad", unknown: "" }[x.status] || "") + '">' + esc(x.status) + '</span>' +
        (x.licence_verification ? '<br><span class="chip warn">licence verification required</span>' : "") +
        (x.reasons || []).filter(function (r) { return r.rule !== "gated"; }).map(function (r) {
          return '<br><span class="small' + (r.outcome === "info" ? " muted" : "") + '">' + (r.outcome === "info" ? "ℹ " : "") + esc(r.note) + '</span>'; }).join("");
      var busy = !!VERIFYING[x.id];
      var ver = (v.status === "ok" ? '<span class="chip ok">✓ verified</span><br><span class="small muted">' + esc(fmtDate(v.at || "")) + (v.gpu ? ", " + esc(v.gpu) : "") +
                  (v.weights_mb != null ? ", weights " + fmtMB(v.weights_mb) : "") + (v.share_4bit != null ? ", " + fmtNum(v.share_4bit * 100, 1) + " % 4-bit" : "") + (v.transformers ? ", transformers " + esc(v.transformers) : "") + '</span>'
                : v.status === "failed" ? '<span class="chip bad">✕ failed</span><br><span class="small muted">' + esc(v.error || "") + '</span>'
                : '<span class="muted small">not verified</span>') +
        (real ? '<br><button type="button" class="btn small-btn" data-verify="' + esc(x.id) + '"' + (busy || x.suitable === false ? " disabled" : "") + '>' + (busy ? "Verifying…" : v.status === "ok" ? "Verify again" : "Verify") + '</button>' : "");
      var offered = x.pending ? '<span class="small muted">the GPU picks it up within a minute</span>' : x.available == null ? '<span class="muted">—</span>' : x.available ? '<span class="chip ok">offered</span>' : '<span class="small muted">' + esc(x.unavailable_reason || "no") + '</span>';
      return '<tr data-model="' + esc(x.id) + '"><td class="mono">' + esc(x.id) + (x.source === "admin" ? ' <span class="chip">added</span>' : "") +
        (x.params_b != null ? '<br><span class="small muted">' + fmtNum(x.params_b, 1) + ' B, about ' + fmtNum(x.vram_4bit_gb, 1) + ' GB at 4-bit</span>' : "") +
        (x.type ? '<br><span class="small muted">' + esc(x.type) + '</span>' : "") + '</td><td>' + suit + '</td><td>' + ver + '</td><td>' + offered +
        '</td><td><input type="checkbox" data-enable="' + esc(x.id) + '" aria-label="Enable ' + esc(x.id) + '"' + (enabled ? " checked" : "") + '></td>' +
        '<td><input type="checkbox" data-dlstart="' + esc(x.id) + '" aria-label="Download ' + esc(x.id) + ' at start-up"' + (dl ? " checked" : "") + '></td>' +
        '<td>' + (x.source === "admin" ? '<button type="button" class="btn small-btn danger" data-remove="' + esc(x.id) + '">Remove</button>' : "") + '</td></tr>';
    }).join("") + '</tbody></table>';
  var box = $("adm-models");
  box.querySelectorAll("[data-enable]").forEach(function (i) { i.addEventListener("change", function () { saveModel(i.dataset.enable, { enabled: i.checked }); }); });
  box.querySelectorAll("[data-dlstart]").forEach(function (i) { i.addEventListener("change", function () { saveModel(i.dataset.dlstart, { download_at_start: i.checked }); }); });
  box.querySelectorAll("[data-remove]").forEach(function (b) { b.addEventListener("click", function () {
    confirmAction({ title: "Remove " + b.dataset.remove + "?", body: "It leaves the model list. You can add it again later.", ok: "Remove model", danger: true }).then(function (yes) {
      if (yes) putModels({ remove: [b.dataset.remove] }, "Removed.");
    });
  }); });
  box.querySelectorAll("[data-verify]").forEach(function (b) { b.addEventListener("click", function () { verifyModel(b.dataset.verify); }); });
}
function putModels(body, done) {
  $("adm-models-error").hidden = true;
  return api("/api/admin/models", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(function (r) {
    if (!r.ok) { showError($("adm-models-error"), r.data); return r; }
    $("adm-models-status").textContent = done + " The GPU picks it up at its next heartbeat (within a minute).";
    CATALOG = null;
    setTimeout(loadAdminModels, ONLINE ? 1500 : 0);
    return r;
  });
}
function saveModel(id, patch) { var set = {}; set[id] = patch; return putModels({ set: set }, "Saved."); }
function verifyModel(id) {
  $("adm-models-error").hidden = true;
  api("/api/models/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: id }) }).then(function (r) {
    if (!r.ok) { showError($("adm-models-error"), r.data); return; }
    VERIFYING[id] = r.data.job_id;
    $("adm-models-status").textContent = "Verifying " + id + " on the GPU. It downloads first if needed and waits behind any running benchmark…";
    loadAdminModels();
    (function poll() {
      api("/api/jobs/" + enc(r.data.job_id)).then(function (j) {
        var d = j.ok ? j.data : null;
        if (d && (d.status === "queued" || d.status === "running")) {
          var dl = d.model_download;
          $("adm-models-status").textContent = "Verifying " + id + ": " + (dl && dl.phase === "downloading" && dl.total_bytes
            ? "downloading " + fmtNum(dl.done_bytes / 1073741824, 1) + " / " + fmtNum(dl.total_bytes / 1073741824, 1) + " GB" : d.message || d.status) + "…";
          setTimeout(poll, 2000); return;
        }
        delete VERIFYING[id];
        var v = d && d.verification;
        $("adm-models-status").textContent = v && v.status === "ok" ? id + " is verified. It loads and generates in 4-bit NF4." : id + " failed verification" + (v && v.error ? ". " + v.error : d && d.error ? ". " + d.error : "") + ".";
        CATALOG = null;
        setTimeout(loadAdminModels, 1500);     // the control plane has the result after its next delivery
      });
    })();
  });
}
$("adm-model-add").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var id = $("adm-model-id").value.trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9._-]{1,96}$/.test(id)) { showError($("adm-models-error"), { error: "Enter a Hugging Face model id like owner/name.", code: "bad_model" }); return; }
  putModels({ add: [{ id: id, note: $("adm-model-note").value.trim() }] }, "Added " + id + " (disabled until you enable it).").then(function (r) {
    if (r && r.ok) { $("adm-model-id").value = ""; $("adm-model-note").value = ""; }
  });
});

// the GPU server's recent warnings, errors and failed runs (GET /api/logs/recent, admins only)
function loadAdminHealth() {
  var host = $("adm-health");
  if (!host) return;
  if (CP && !ONLINE) { ADM.health = { offline: true }; admTiles(); host.innerHTML = '<h2>Problems in the last 24 hours</h2><p class="muted" style="margin-bottom:0">The GPU server is offline. Its logs are on it and can be read when it is back.</p>'; return; }
  api("/api/logs/recent?hours=24").then(function (r) {
    if (!r.ok) { host.innerHTML = '<h2>Problems in the last 24 hours</h2><p class="notice error small">' + esc(r.data.error || "Could not read the server's problems.") + '</p>'; return; }
    var d = r.data, c = d.counts, ok = !c.errors && !c.failed_jobs;
    ADM.health = { n: (c.errors || 0) + (c.failed_jobs || 0) }; admTiles();
    host.innerHTML = '<div class="row" style="margin-top:0;justify-content:space-between"><h2>Problems in the last 24 hours</h2><span class="chip ' + (ok ? (c.warnings ? "" : "ok") : "bad") + '" id="adm-health-chip">' +
      (ok && !c.warnings ? "all clear" : c.errors + " error" + (c.errors === 1 ? "" : "s") + ", " + c.warnings + " warning" + (c.warnings === 1 ? "" : "s") + ", " + c.failed_jobs + " failed run" + (c.failed_jobs === 1 ? "" : "s")) + '</span></div>' +
      '<p class="muted small" style="margin-top:0">From the GPU server, kept in its memory since it started (a restart clears them). Newest first.</p>' +
      (d.failed_jobs.length ? '<h3>Failed runs</h3><div class="table-wrap"><table id="adm-failed-table"><thead><tr><th>Run</th><th>What</th><th>When</th><th>Why</th></tr></thead><tbody>' +
        d.failed_jobs.map(function (j) {
          return '<tr><td><a class="mono" href="#/new/run/' + enc(j.job_id) + '">' + esc(shortId(j.job_id)) + '</a></td><td class="small">' + esc((j.kind === "prepare" ? "Data preparation" : "Benchmark") + ((j.models || []).length ? ": " + j.models.map(short).join(", ") : "")) +
            '</td><td class="small">' + esc(fmtDate(j.finished_at)) + '</td><td class="small">' + esc(j.error || "—") + (j.error_code ? ' <code>' + esc(j.error_code) + '</code>' : "") + '</td></tr>';
        }).join("") + '</tbody></table></div>' : "") +
      (d.problems.length ? '<details class="info"' + (c.errors ? " open" : "") + '><summary>Log messages (' + d.problems.length + ')</summary><div class="table-wrap"><table id="adm-log-table"><thead><tr><th>When</th><th>Level</th><th>Where</th><th>Message</th></tr></thead><tbody>' +
        d.problems.slice(0, 200).map(function (p) {
          return '<tr><td class="small">' + esc(fmtDate(p.ts)) + '</td><td><span class="chip ' + (p.level === "WARNING" ? "warn" : "bad") + '">' + esc(p.level.toLowerCase()) + '</span></td><td class="small mono">' + esc(p.logger) +
            '</td><td class="small">' + esc(p.msg) + (p.exc ? '<br><span class="muted">' + esc(p.exc) + '</span>' : "") + (p.job_id ? ' <a class="mono" href="#/new/run/' + enc(p.job_id) + '">' + esc(shortId(p.job_id)) + '</a>' : "") + '</td></tr>';
        }).join("") + '</tbody></table></div></details>' : '<p class="muted small" style="margin-bottom:0">No warnings or errors logged.</p>');
  });
}
var AUDIT_LABEL = { "user.create": "created user", "user.update": "changed user", "user.delete": "deleted user", "session.delete": "deleted session",
                   "limits.update": "changed system limits", "models.update": "changed the model catalog", "password.change": "changed their password",
                   "ids_model.commit": "stored the IDS model", "ids_model.delete": "removed the IDS model" };
function auditDetail(e) {
  var d = e.detail || {};
  if (e.action === "user.update") return [d.role ? "role → " + d.role : null, d.disabled === true ? "disabled" : d.disabled === false ? "enabled" : null, d.password_reset ? "password reset" : null].filter(Boolean).join(", ");
  if (e.action === "user.create") return "role " + (d.role || "user");
  if (e.action === "limits.update") return Object.keys(d).map(function (k) { return k + " = " + JSON.stringify(d[k]); }).join(", ");
  if (e.action === "models.update") return [(d.added || []).length ? "added " + d.added.join(", ") : null, (d.removed || []).length ? "removed " + d.removed.join(", ") : null,
    Object.keys(d.set || {}).length ? "changed " + Object.keys(d.set).map(short).join(", ") : null].filter(Boolean).join("; ");
  if (e.action === "session.delete") return d.own ? "own session" : "owner: " + (d.owner || "—");
  return "";
}
function loadAudit(before) {
  var host = $("adm-audit");
  if (!host) return;
  api("/api/admin/audit?limit=50" + (before ? "&before=" + before : "")).then(function (r) {
    if (!r.ok) { host.innerHTML = '<h2>Audit log</h2><p class="notice error small">' + esc(r.data.error || "Could not read the audit log.") + '</p>'; return; }
    var rows = r.data.entries.map(function (e) {
      return '<tr><td class="small">' + esc(fmtDate(e.at)) + '</td><td>' + esc(e.actor || "—") + '</td><td>' + esc(AUDIT_LABEL[e.action] || e.action) + '</td><td class="mono small">' +
        esc(e.target ? (/^job_/.test(e.target) ? shortId(e.target) : e.target) : "—") + '</td><td class="small">' + esc(auditDetail(e)) + '</td></tr>';
    }).join("");
    if (before && $("adm-audit-table")) { $("adm-audit-table").querySelector("tbody").insertAdjacentHTML("beforeend", rows); }
    else host.innerHTML = '<h2>Audit log</h2><p class="muted small" style="margin-top:0">Who changed users, limits, the model catalog or deleted a session, and when. Passwords and tokens are never recorded.</p>' +
      (rows ? '<div class="table-wrap"><table id="adm-audit-table"><thead><tr><th>When</th><th>Who</th><th>What</th><th>On</th><th>Detail</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<p class="muted" style="margin-bottom:0">Nothing recorded yet.</p>');
    var more = host.querySelector("#adm-audit-more");
    if (more) more.remove();
    if (r.data.next_before) {
      host.insertAdjacentHTML("beforeend", '<div class="actions"><button type="button" class="btn small-btn" id="adm-audit-more">Show older</button></div>');
      $("adm-audit-more").addEventListener("click", function () { loadAudit(r.data.next_before); });
    }
  });
}
function loadAdmin(tab) {
  atTab(tab || ADM.tab);
  ADM.creds = null; ADM.users = null; ADM.health = null; ADM.week = null;
  admTiles(); admCheck(); admSystem();
  allSessions().then(function (r) {
    var since = Date.now() - 7 * 864e5;
    ADM.week = r.ok ? (r.data.sessions || []).filter(function (x) { return Date.parse(x.created_at || "") >= since; }).length : 0;
    admTiles();
  });
  if (CP) cpFetch("/api/backend").then(function (r) { if (r && r.ok) { BACKEND = r.data; admTiles(); admCheck(); admSystem(); } });
  loadAdminHealth(); loadAudit();
  loadAdminModels();
  loadAdminIds();
  $("adm-error").hidden = true; $("lim-error").hidden = true; $("lim-saved").textContent = "";
  api("/api/admin/users").then(function (r) {
    if (!r.ok) { showError($("adm-error"), r.data); return; }
    var us = r.data.users || [];
    ADM.users = us; admTiles();
    $("adm-count").textContent = us.length + " of " + r.data.max_users + " accounts";
    $("adm-new-go").disabled = us.length >= r.data.max_users;
    $("adm-users").innerHTML = '<table id="adm-users-table"><thead><tr><th>User</th><th>Role</th><th>Status</th><th>Created</th><th>Actions</th></tr></thead><tbody>' +
      us.map(function (u) {
        var me = u.id === AUTH.user.id;
        return '<tr data-user="' + esc(u.username) + '"><td class="mono">' + esc(u.username) + (me ? ' <span class="muted small">(you)</span>' : "") + '</td><td>' + esc(u.role) + '</td><td>' +
          (u.disabled ? '<span class="chip bad">disabled</span>' : '<span class="chip ok">active</span>') + (u.must_change_pw ? ' <span class="chip warn">must change password</span>' : "") +
          '</td><td class="small">' + esc(fmtDate(u.created_at)) + '</td><td class="actions" style="margin:0">' +
          (me ? "" : '<button type="button" class="btn small-btn" data-act="toggle" data-id="' + esc(u.id) + '" data-on="' + (u.disabled ? 0 : 1) + '">' + (u.disabled ? "Enable" : "Disable") + '</button>' +
            '<button type="button" class="btn small-btn" data-act="role" data-id="' + esc(u.id) + '" data-role="' + (u.role === "admin" ? "user" : "admin") + '">Make ' + (u.role === "admin" ? "user" : "admin") + '</button>') +
          '<button type="button" class="btn small-btn" data-act="reset" data-id="' + esc(u.id) + '">Reset password</button>' +
          (u.disabled ? '<button type="button" class="btn small-btn" data-act="delete" data-id="' + esc(u.id) + '">Delete</button>' : "") + '</td></tr>';
      }).join("") + '</tbody></table>';
    $("adm-users").querySelectorAll("[data-act]").forEach(function (b) { b.addEventListener("click", function () { adminAct(b); }); });
  });
  api("/api/admin/credentials").then(function (r) {
    if (!r.ok) return;
    ADM.creds = r.data.credentials || {}; admCheck();
  });
  api("/api/admin/limits").then(function (r) {
    if (!r.ok) return;
    var L = r.data.limits;
    $("lim-sph").value = L.sessions_per_hour; $("lim-mq").value = L.max_queued_sessions; $("lim-mb").value = L.max_upload_mb;
    $("lim-idle").value = L.idle_minutes; $("lim-sess-idle").value = L.session_idle_minutes; $("lim-ident").checked = !!L.keep_identification_columns_in_r2;
  });
}
// ---------- IDS MODEL (Admin) ----------
var IDS_NAMES = ["ids_rf.joblib", "ids_rf.json", "ids_rf.hashes.npy"];
function loadAdminIds() {
  api("/api/admin/ids-model").then(function (r) {
    if (!r.ok) { $("adm-ids-now").innerHTML = '<dt>Stored</dt><dd class="muted">' + esc((r.data || {}).error || "unavailable") + '</dd>'; return; }
    var m = r.data.model;
    ADM.ids = m || null; admCheck();
    $("adm-ids-del").hidden = !m;
    if (!m) { $("adm-ids-now").innerHTML = '<dt>Stored</dt><dd><span class="chip warn">none</span> <span class="muted">new GPUs offer the bundled demo IDS only</span></dd>'; return; }
    var h = m.holdout || {};
    $("adm-ids-now").innerHTML = '<dt>Stored</dt><dd><span class="chip ok">' + esc(m.version || m.name) + '</span>' + (m.demo ? ' <span class="chip warn">demo</span>' : "") + '</dd>' +
      '<dt>Threshold</dt><dd>' + fmtNum(m.threshold, 4) + ' <span class="muted">(' + esc(m.threshold_rule || "") + ')</span></dd>' +
      '<dt>Hold-out</dt><dd>recall ' + fmtPct(h.recall) + ', precision ' + fmtPct(h.precision) + ', false-positive rate ' + fmtPct(h.false_positive_rate) + ' <span class="muted">(' + fmtNum(h.n) + ' flows)</span></dd>' +
      '<dt>scikit-learn</dt><dd class="mono">' + esc(m.sklearn || "—") + '</dd>' +
      '<dt>Uploaded</dt><dd>' + esc(m.committed_at || "—") + ' by ' + esc(m.committed_by || "—") + '</dd>';
  });
}
$("adm-ids-form").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var err = $("adm-ids-error"), st = $("adm-ids-status"), files = Array.prototype.slice.call($("adm-ids-files").files || []);
  err.hidden = true;
  var byName = {};
  files.forEach(function (f) { byName[f.name] = f; });
  var bad = files.filter(function (f) { return IDS_NAMES.indexOf(f.name) < 0; });
  if (bad.length || !byName["ids_rf.joblib"] || !byName["ids_rf.json"]) {
    showError(err, { error: "Choose ids_rf.joblib and ids_rf.json (and ids_rf.hashes.npy) from models\\ids\\" + (bad.length ? ". Not expected " + bad.map(function (f) { return f.name; }).join(", ") : "") });
    return;
  }
  $("adm-ids-go").disabled = true;
  var names = IDS_NAMES.filter(function (n) { return byName[n]; });
  var step = function (i) {
    if (i >= names.length) return api("/api/admin/ids-model/commit", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    var f = byName[names[i]];
    st.textContent = "Uploading " + f.name + " (" + fmtNum(f.size / 1048576, 1) + " MB)…";
    return api("/api/admin/ids-model/upload-url", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: f.name, bytes: f.size }) }).then(function (r) {
      if (!r.ok) return r;
      return fetch(r.data.url, { method: "PUT", body: f, headers: { "content-type": r.data.content_type } }).then(function (u) {
        if (!u.ok) return { ok: false, data: { error: "Storage refused " + f.name + " (HTTP " + u.status + ")" } };
        return step(i + 1);
      });
    });
  };
  step(0).then(function (r) {
    $("adm-ids-go").disabled = false;
    if (!r || !r.ok) { st.textContent = ""; showError(err, (r || {}).data || { error: "Upload failed." }); return; }
    st.textContent = "Stored. A GPU rented from now on downloads it at start-up. A GPU that is already running keeps the model it started with.";
    $("adm-ids-files").value = ""; loadAdminIds();
  }).catch(function (e) { $("adm-ids-go").disabled = false; showError(err, { error: "Upload failed: " + e.message }); });
});
$("adm-ids-del").addEventListener("click", function () {
  confirmAction({ title: "Remove the stored IDS model?", body: "GPUs rented afterwards offer the demo IDS only.", ok: "Remove IDS model", danger: true }).then(function (yes) {
    if (!yes) return;
    api("/api/admin/ids-model", { method: "DELETE" }).then(function (r) { if (!r.ok) showError($("adm-ids-error"), r.data); else { $("adm-ids-status").textContent = "Removed."; loadAdminIds(); } });
  });
});
function adminAct(b) {
  var id = b.getAttribute("data-id"), act = b.getAttribute("data-act"), req;
  if (act === "toggle") req = api("/api/admin/users/" + id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ disabled: b.getAttribute("data-on") === "1" }) });
  else if (act === "role") req = api("/api/admin/users/" + id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role: b.getAttribute("data-role") }) });
  else if (act === "reset") {
    var pw = prompt("Temporary password for this user (10+ characters). They must change it at the next login.");
    if (!pw) return;
    req = api("/api/admin/users/" + id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: pw }) });
  } else if (act === "delete") {
    confirmAction({ title: "Delete this disabled account?", body: "Its sessions stay, under its name. This cannot be undone.", ok: "Delete account", danger: true }).then(function (yes) {
      if (yes) api("/api/admin/users/" + id, { method: "DELETE" }).then(function (r) { if (!r.ok) showError($("adm-error"), r.data); else loadAdmin(); });
    });
    return;
  }
  req.then(function (r) { if (!r.ok) showError($("adm-error"), r.data); else loadAdmin(); });
}
$("adm-new").addEventListener("submit", function (ev) {
  ev.preventDefault();
  $("adm-error").hidden = true;
  api("/api/admin/users", { method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ username: $("adm-new-user").value.trim(), password: $("adm-new-pass").value, role: $("adm-new-role").value }) }).then(function (r) {
    if (!r.ok) { showError($("adm-error"), r.data); return; }
    $("adm-new-user").value = ""; $("adm-new-pass").value = ""; loadAdmin();
  });
});
$("adm-limits").addEventListener("submit", function (ev) {
  ev.preventDefault();
  $("lim-error").hidden = true;
  api("/api/admin/limits", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
    sessions_per_hour: +$("lim-sph").value, max_queued_sessions: +$("lim-mq").value, max_upload_mb: +$("lim-mb").value,
    idle_minutes: +$("lim-idle").value, session_idle_minutes: +$("lim-sess-idle").value, keep_identification_columns_in_r2: $("lim-ident").checked }) }).then(function (r) {
    if (!r.ok) showError($("lim-error"), r.data); else $("lim-saved").textContent = "Saved. The GPU backend picks them up at its next heartbeat, within a minute.";
  });
});

// ---------- LOGIN / LOGOUT ----------
function showLogin() {
  $("login-form").hidden = false; $("pwchange-form").hidden = true; $("login-error").hidden = true;
  $("login-go").disabled = false;                                // disabled in the markup until the app is ready
  $("cp-profile").hidden = true; $("nav-admin").hidden = true;
  $("side-admin").hidden = true;
  if (LOGOUT_REASON) { $("login-info").hidden = false; $("login-info").textContent = LOGOUT_REASON; LOGOUT_REASON = ""; }
  setTimeout(function () { $("login-user").focus(); }, 0);
}
function needLogin() {
  if (!CP) return;
  AUTH = { access: null, exp: 0, user: null }; READ_TOK = null; FILE_TOK = {};
  pollToken++;
  show("login"); showLogin();
}
$("login-form").addEventListener("submit", function (ev) {
  ev.preventDefault();
  $("login-error").hidden = true; $("login-go").disabled = true; $("login-go").textContent = "Logging in\u2026";
  fetch(CP + "/api/auth/login", { method: "POST", credentials: "omit", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify({ username: $("login-user").value.trim(), password: $("login-pass").value,
                                                         remember: $("login-remember").checked }) })
    .then(parse, function () { return netErr("control plane"); }).then(function (r) {
      $("login-go").disabled = false; $("login-go").textContent = "Log in";
      if (!r.ok) { showError($("login-error"), r.data); return; }
      $("login-pass").value = ""; $("login-info").hidden = true;
      markActive();
      setAuth(r.data);
      if (r.data.user.must_change_pw) { $("login-form").hidden = true; $("pwchange-form").hidden = false; $("pwc-new").focus(); return; }
      afterLogin();
    });
});
$("pwchange-form").addEventListener("submit", function (ev) {
  ev.preventDefault();
  $("pwchange-error").hidden = true;
  api("/api/me/password", { method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ current_password: LAST_PW, new_password: $("pwc-new").value }) }).then(function (r) {
    if (!r.ok) { showError($("pwchange-error"), r.data); return; }
    LAST_PW = ""; AUTH.user.must_change_pw = false; afterLogin();
  });
});
var LAST_PW = "";
$("login-pass").addEventListener("input", function () { LAST_PW = $("login-pass").value; });
function logout() {
  closeMenu();
  return cpFetch("/api/auth/logout", { method: "POST" }).then(function () { refreshSet(""); needLogin(); });
}
$("cp-logout").addEventListener("click", logout);
// ---------- profile menu ----------
function closeMenu() { $("cp-menu").hidden = true; $("cp-profile-btn").setAttribute("aria-expanded", "false"); }
function openMenu() {
  $("cp-menu").hidden = false; $("cp-profile-btn").setAttribute("aria-expanded", "true");
  var first = $("cp-menu").querySelector("[role=menuitem]:not([hidden])"); if (first) first.focus();
}
$("cp-profile-btn").addEventListener("click", function (ev) { ev.stopPropagation(); if ($("cp-menu").hidden) openMenu(); else closeMenu(); });
document.addEventListener("click", function (ev) { if (!$("cp-profile").contains(ev.target)) closeMenu(); });
$("cp-menu").addEventListener("keydown", function (ev) {
  var items = Array.prototype.filter.call($("cp-menu").querySelectorAll("[role=menuitem]"), function (x) { return !x.hidden; });
  var i = items.indexOf(document.activeElement);
  if (ev.key === "Escape") { closeMenu(); $("cp-profile-btn").focus(); }
  else if (ev.key === "ArrowDown") { ev.preventDefault(); items[(i + 1) % items.length].focus(); }
  else if (ev.key === "ArrowUp") { ev.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
});
$("cp-menu").addEventListener("click", function (ev) { if (ev.target.closest("a")) closeMenu(); });
var ROLE_TEXT = { admin: "An admin manages accounts, credentials and system limits, and can delete any session.",
                  user: "A user runs benchmarks and manages their own sessions." };
function fillProfileMenu() {
  var u = AUTH.user, admin = isAdmin();
  $("cp-profile").hidden = false; $("nav-settings-icon").hidden = true;
  $("cp-user").textContent = u.username; $("cp-avatar").textContent = u.username.charAt(0);
  $("cp-role").textContent = admin ? "Admin" : "User"; $("cp-role").className = "role-chip" + (admin ? " admin" : "");
  $("cp-menu-name").textContent = u.username; $("cp-menu-role").textContent = admin ? "Admin" : "User";
  $("cp-profile-btn").setAttribute("aria-label", "Account menu: " + u.username + " (" + (admin ? "admin" : "user") + ")");
  $("nav-admin").hidden = !admin;
  $("side-admin").hidden = !admin;
}
// ---------- ACCOUNT (Settings › Account; was the Profile page) ----------
function deviceName(ua) {
  ua = ua || "";
  var b = /Edg\//.test(ua) ? "Edge" : /OPR\//.test(ua) ? "Opera" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "";
  var o = /Windows/.test(ua) ? "Windows" : /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iPhone / iPad" : /Mac OS X/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
  return (b && o) ? b + " on " + o : (b || o || "Unknown device");
}
function loadProfile() {
  $("prof-error").hidden = true; $("set-pw-msg").hidden = true;
  api("/api/me").then(function (r) {
    if (!r.ok) { showError($("prof-error"), r.data); return; }
    var u = r.data.user, s = r.data.session || {}, admin = u.role === "admin";
    $("prof-account").innerHTML = '<span class="av" aria-hidden="true">' + esc(u.username.charAt(0).toUpperCase()) + '</span><div>' +
      '<h2 id="prof-username" style="margin:0">' + esc(u.username) + '</h2>' +
      '<p class="st-sub" style="margin:4px 0 0"><span id="prof-role"><span class="role-chip' + (admin ? " admin" : "") + '">' + (admin ? "Admin" : "User") + '</span></span> ' +
      esc(ROLE_TEXT[u.role] || "") + '</p>' +
      '<p class="small muted" style="margin:6px 0 0">Member since ' + esc(fmtDate(u.created_at)) + '. This login ' + esc(fmtDate(s.logged_in_at)) + ', ' +
      (s.remember ? "remembered on this device" : "until the browser closes" + (s.idle_minutes ? " or " + s.idle_minutes + " min without activity" : "")) + '.' +
      (r.data.previous_login_at ? ' Previous login ' + esc(fmtDate(r.data.previous_login_at)) + '.' : "") + '</p></div>';
  });
  api("/api/me/devices").then(function (r) {
    if (!r.ok) { showError($("prof-error"), r.data); return; }
    var ds = r.data.devices || [], others = ds.filter(function (d) { return !d.current; });
    $("prof-revoke-others").hidden = !others.length;
    $("prof-devices").innerHTML = '<div id="prof-devices-table">' + ds.map(function (d) {
      return '<div class="st-dev" data-device="' + esc(d.id) + '"><div><b>' + esc(deviceName(d.user_agent)) + '</b>' + (d.current ? ' <span class="chip ok">this device</span>' : "") +
        '<div class="m">Logged in ' + esc(fmtDate(d.created_at)) + '. Last active ' + esc(fmtDate(d.last_used_at)) + '. ' + (d.remember ? "Remembered on this device." : "Kept until the browser closes.") + '</div></div>' +
        '<button type="button" class="btn small-btn" data-revoke="' + esc(d.id) + '"' + (d.current ? ' data-current="1"' : "") + '>Log out</button></div>';
    }).join("") + '</div>';
    $("prof-devices").querySelectorAll("[data-revoke]").forEach(function (b) { b.addEventListener("click", function () {
      if (b.getAttribute("data-current")) { logout(); return; }
      api("/api/me/devices/" + b.getAttribute("data-revoke"), { method: "DELETE" }).then(function (x) {
        if (!x.ok) showError($("prof-error"), x.data); else loadProfile();
      });
    }); });
    $("prof-revoke-others").onclick = function () {
      Promise.all(others.map(function (d) { return api("/api/me/devices/" + d.id, { method: "DELETE" }); })).then(function (xs) {
        var bad = xs.filter(function (x) { return !x.ok; })[0];
        if (bad) showError($("prof-error"), bad.data);
        loadProfile();
      });
    };
  });
}
function afterLogin() {
  fillProfileMenu();
  return api("/api/settings").then(function (r) {
    SETTINGS = r.ok ? r.data.settings : null;
    if (SETTINGS && SETTINGS.theme && SETTINGS.theme !== themePref()) applyTheme(SETTINGS.theme, true);
    return checkControlPlaneBackend();
  }).then(function () { if (location.hash === "#/login") location.hash = "#/"; route(); });
}
