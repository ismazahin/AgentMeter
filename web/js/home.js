// AgentMeter web app — home.js: Home, and search everything (Ctrl+K).
// One of the app's scripts (web/app.html loads them in order: core, home, wizard, session, analysis,
// compare, settings, boot); they share the page's global scope, so a function defined in one is used by the others.
"use strict";
// ---------- HOME ----------
function homeHeadline(row, models) {
  var w = row.more_efficient;
  if (models.length < 2) return short(models[0] || "") + ": single-model run";
  if (w && w !== "tie" && w !== "—" && w !== "mixed") return short(w) + " is more efficient";
  return w === "mixed" ? "Mixed result: each model wins somewhere" : "Tied on efficiency";
}
// first-visit guides: shown until "Got it" (per browser; nothing breaks without storage)
function guideSeen(k) { try { return localStorage.getItem("agentmeter.guide." + k) === "1"; } catch (e) { return false; } }
function guideHide(k) { try { localStorage.setItem("agentmeter.guide." + k, "1"); } catch (e) { /* shown again next time */ } $(k + "-guide").hidden = true; }
$("home-guide-hide").addEventListener("click", function () { guideHide("home"); });
$("help-guides-reset").addEventListener("click", function () {
  ["home", "session"].forEach(function (k) { try { localStorage.removeItem("agentmeter.guide." + k); } catch (e) { /* nothing stored */ } });
  $("help-guides-done").textContent = "Done: the tips show again on Home and on a session.";
});
// in-page links of the guide scroll instead of changing the route
$("view-help").addEventListener("click", function (e) {
  var a = e.target.closest('a[href^="#help-"]'); if (!a) return;
  e.preventDefault(); var t = $(a.getAttribute("href").slice(1)); if (t) { t.scrollIntoView({ behavior: "smooth", block: "start" }); t.setAttribute("tabindex", "-1"); t.focus({ preventScroll: true }); }
});
$("session-guide-hide").addEventListener("click", function () { guideHide("session"); });
function loadHome() {
  $("home-guide").hidden = guideSeen("home");
  api("/api/sessions?limit=8").then(function (r) {
    var rows = (r.ok && r.data.sessions) || [];
    $("home-empty").hidden = !(r.ok && !rows.length);
    $("home-recent").hidden = !rows.length;
    $("home-latest").hidden = true;
    if (!r.ok) { $("home-recent").hidden = false; $("home-sessions").innerHTML = '<p class="muted" style="padding:12px">' + esc(r.data.error) + '</p>'; return; }
    if (!rows.length) return;
    $("home-sessions").innerHTML = sessionsTable(rows, "home-sessions-table");
    var last = rows.filter(function (s) { return s.status === "Done" || s.status === "Demo"; })[0];
    if (!last || !last.metrics) return;
    var models = Object.keys(last.metrics);
    $("home-latest").hidden = false;
    $("home-latest").innerHTML =
      '<div class="row" style="margin-top:0;justify-content:space-between"><div><h2>Latest session</h2><p class="muted small" style="margin:2px 0 0">' +
      esc(last.input_type || "") + ' · ' + fmtNum(last.n_flows) + ' flows · ' + esc(last.gpu) + ' · ' + esc(fmtDate(last.finished_at)) + '</p></div>' +
      '<a class="btn" href="#/session/' + enc(last.session_id) + '" id="home-open-latest">Open session</a></div>' +
      '<p class="headline" id="home-headline">' + esc(homeHeadline(last, models)) + '</p>' +
      (last.verdict ? '<details class="info"><summary>Show full verdict</summary><p class="verdict" id="home-verdict">' + esc(last.verdict.text) + '</p></details>' : "") +
      '<div class="grid two">' +
      hbars("Mean latency per flow (lower is better)", models.map(function (m) { return { label: short(m), v: last.metrics[m].mean_latency_s }; }), fmtTime) +
      hbars("Mean working VRAM (excludes model weights; lower is better)", models.map(function (m) {
        var t = last.metrics[m].total_peak_mb;
        return { label: short(m), v: last.metrics[m].mean_peak_vram_mb, extra: t != null ? "total peak " + fmtMB(t) : null }; }), fmtMB,
            last.demo ? "not measured (demo)" : "not measured") + '</div>';
  });
}

// every session (paged: 200 at a time), cached for 5 s so pages opened one after another share one read
var ALL = { at: 0, p: null };
function pagedSessions(query) {                    // every page of a filtered list
  return (function page(offset, acc) {
    return api("/api/sessions?limit=200&offset=" + offset + (query ? "&" + query : "")).then(function (r) {
      if (!r.ok) return r;
      acc = acc.concat(r.data.sessions || []);
      if (r.data.next_offset != null && acc.length < 20000) return page(r.data.next_offset, acc);
      return { ok: true, status: 200, data: { sessions: acc, statuses: r.data.statuses } };
    });
  })(0, []);
}
function allSessions(fresh) {
  if (!fresh && ALL.p && Date.now() - ALL.at < 5000) return ALL.p;
  ALL.at = Date.now();
  ALL.p = (function page(offset, acc) {
    return api("/api/sessions?limit=200&offset=" + offset).then(function (r) {
      if (!r.ok) return r;
      acc = acc.concat(r.data.sessions || []);
      if (r.data.next_offset != null && acc.length < 20000) return page(r.data.next_offset, acc);
      return { ok: true, status: 200, data: { sessions: acc, statuses: r.data.statuses } };
    });
  })(0, []);
  ALL.p.then(function (r) { if (!r.ok) ALL.p = null; });
  return ALL.p;
}
// accessibility: a table wider than its box scrolls; make that box reachable and named for keyboard users
(function () {
  var t = null;
  function mark() {
    t = null;
    document.querySelectorAll(".table-wrap").forEach(function (w) {
      var scrolls = w.scrollWidth > w.clientWidth + 1;
      if (scrolls && !w.hasAttribute("tabindex")) {
        w.setAttribute("tabindex", "0"); w.setAttribute("role", "region");
        var h = w.closest(".card") && w.closest(".card").querySelector("h2, h3");
        w.setAttribute("aria-label", (h ? h.textContent.trim() + " — " : "") + "table, scrolls sideways");
      } else if (!scrolls && w.getAttribute("role") === "region") { w.removeAttribute("tabindex"); w.removeAttribute("role"); w.removeAttribute("aria-label"); }
    });
  }
  new MutationObserver(function () { if (!t) t = setTimeout(mark, 300); }).observe(document.body, { childList: true, subtree: true });
  window.addEventListener("resize", function () { if (!t) t = setTimeout(mark, 300); });
})();
// a loading placeholder in the shape of what is coming (no layout jump); screen readers hear "Loading"
function skel(lines, card) {
  var w = [92, 76, 84, 60, 88, 70, 80];
  var inner = '<div class="skel" role="status" aria-label="Loading">' + Array.apply(null, Array(lines)).map(function (_, i) { return '<span class="skel-line" style="width:' + w[i % w.length] + '%"></span>'; }).join("") + '<span class="sr-only">Loading…</span></div>';
  return card ? '<div class="card">' + inner + '</div>' : inner;
}
// ---------- search everything (Ctrl+K): pages, sessions, data sets, models ----------
var GS = { items: [], shown: [], sel: 0, last: null };
var GS_PAGES = [["Home", "#/", "start latest"], ["New benchmark", "#/new", "run upload prepare csv pcap"], ["Sessions", "#/sessions", "runs list history"],
  ["Data sets", "#/datasets", "prepared sets flows fingerprint"], ["Leaderboard — by setup", "#/leaderboard", "rank ranking"], ["Leaderboard — Compare models", "#/compare", "compare models side by side"],
  ["Repeatability", "#/trends", "trends history stable moved"], ["GPU & Queue", "#/gpu", "queue running waiting gpu now"], ["Settings", "#/settings", "theme telegram alerts limits"],
  ["How to use", "#/help", "help guide"], ["Methodology", "#/help", "saw formula statistics how measured"], ["Profile", "#/profile", "password devices"], ["Admin", "#/admin", "users audit log problems limits models"]];
function gsBuild() {
  var items = GS_PAGES.filter(function (x) { return (x[1] !== "#/admin" || (AUTH.user && AUTH.user.role === "admin")) && (x[1] !== "#/profile" || CP); })
    .map(function (x) { return { kind: "Page", label: x[0], sub: "", href: x[1], hay: (x[0] + " " + x[2]).toLowerCase(), rank: 0 }; });
  return allSessions().then(function (r) {
    var rows = r.ok ? r.data.sessions || [] : [], sets = {}, models = {};
    rows.forEach(function (s) {
      var ms = (s.models || []).map(short).join(", ");
      items.push({ kind: "Session", label: ms || shortId(s.session_id), sub: [s.status, s.run_name ? s.run_name.split("/").pop() : null, fmtDate(s.finished_at || s.created_at)].filter(Boolean).join(" · "),
                   href: "#/session/" + enc(s.session_id), rank: 2,
                   hay: [s.session_id, (s.models || []).join(" "), s.run_name, s.notes, (s.tags || []).join(" "), s.owner || s.owner_username, s.gpu, s.status].join(" ").toLowerCase() });
      if (s.run_name && !sets[s.run_name]) sets[s.run_name] = 1;
      (s.models || []).forEach(function (m) { models[m] = (models[m] || 0) + 1; });
    });
    Object.keys(sets).forEach(function (n) {
      items.push({ kind: "Data set", label: n.split("/").pop(), sub: n.split("/")[0] === "pcap_runs" ? "PCAP" : "CSV", href: "#/datasets", hay: n.toLowerCase(), rank: 1, ds: n });
    });
    Object.keys(models).forEach(function (m) {
      items.push({ kind: "Model", label: short(m), sub: models[m] + " session" + (models[m] === 1 ? "" : "s") + " — show them", href: "#/sessions", hay: m.toLowerCase(), rank: 1, model: m });
    });
    return items;
  });
}
function gsRender() {
  var q = $("gs-q").value.trim().toLowerCase(), words = q.split(/\s+/).filter(Boolean);
  GS.shown = GS.items.filter(function (x) { return words.every(function (w) { return x.hay.indexOf(w) >= 0; }); })
    .sort(function (a, b) {
      var pa = words.length && a.label.toLowerCase().indexOf(words[0]) === 0 ? 0 : 1, pb = words.length && b.label.toLowerCase().indexOf(words[0]) === 0 ? 0 : 1;
      return pa - pb || a.rank - b.rank;
    }).slice(0, words.length ? 40 : 14);
  GS.sel = Math.min(GS.sel, Math.max(0, GS.shown.length - 1));
  $("gs-list").innerHTML = GS.shown.length ? GS.shown.map(function (x, i) {
    return '<li role="option" id="gs-o' + i + '" data-i="' + i + '" aria-selected="' + (i === GS.sel) + '"><span class="gs-kind">' + esc(x.kind) + '</span><span>' + esc(x.label) + '</span><span class="gs-sub">' + esc(x.sub) + '</span></li>';
  }).join("") : '<li class="muted" aria-disabled="true">Nothing matches.</li>';
  $("gs-q").setAttribute("aria-activedescendant", GS.shown.length ? "gs-o" + GS.sel : "");
}
function gsOpen() {
  if (!$("gs").hidden) return;
  GS.last = document.activeElement;
  $("gs").hidden = false; $("gs-q").value = ""; GS.sel = 0; GS.items = GS.items.length ? GS.items : [];
  gsRender(); $("gs-q").focus();
  gsBuild().then(function (items) { GS.items = items; if (!$("gs").hidden) gsRender(); });
}
function gsClose() { $("gs").hidden = true; if (GS.last && GS.last.focus) GS.last.focus(); }
function gsGo(x) {
  if (!x) return;
  gsClose();
  if (x.model) { location.hash = "#/sessions"; setTimeout(function () { $("flt-q").value = x.model; loadSessions(); }, 0); return; }
  if (x.ds) { DS.open = (DS.rows || []).filter(function (g) { return g.names.indexOf(x.ds) >= 0; }).map(function (g) { return g.key; })[0] || null; }
  if (location.hash === x.href) route(); else location.hash = x.href;
  if (x.label === "Methodology") setTimeout(function () { var m = $("help-method"); if (m) m.scrollIntoView({ block: "start" }); }, 50);
}
$("gs-open").addEventListener("click", gsOpen);
$("gs-q").addEventListener("input", function () { GS.sel = 0; gsRender(); });
$("gs-q").addEventListener("keydown", function (ev) {
  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    ev.preventDefault();
    GS.sel = (GS.sel + (ev.key === "ArrowDown" ? 1 : -1) + Math.max(1, GS.shown.length)) % Math.max(1, GS.shown.length);
    gsRender(); var o = $("gs-o" + GS.sel); if (o) o.scrollIntoView({ block: "nearest" });
  } else if (ev.key === "Enter") { ev.preventDefault(); gsGo(GS.shown[GS.sel]); }
});
$("gs-list").addEventListener("click", function (ev) { var li = ev.target.closest("li[data-i]"); if (li) gsGo(GS.shown[+li.getAttribute("data-i")]); });
$("gs").addEventListener("click", function (ev) { if (ev.target.id === "gs") gsClose(); });
document.addEventListener("keydown", function (ev) {
  var typing = /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "") || (document.activeElement || {}).isContentEditable;
  if ((ev.key === "k" || ev.key === "K") && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); if ($("gs").hidden) gsOpen(); else gsClose(); return; }
  if (ev.key === "/" && !typing && $("gs").hidden && document.querySelector(".mainnav") && !document.querySelector(".mainnav").hidden) { ev.preventDefault(); gsOpen(); return; }
  if (ev.key === "Escape" && !$("gs").hidden) { ev.preventDefault(); gsClose(); }
  if (ev.key === "Tab" && !$("gs").hidden) { ev.preventDefault(); $("gs-q").focus(); }      // focus stays in the dialog
});
