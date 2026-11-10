// AgentMeter web app — session.js: Sessions list and one session: its tabs and their cards (analysis expansions included).
// One of the app's scripts (web/app.html loads them in order: core, home, wizard, session, analysis,
// compare, settings, boot); they share the page's global scope, so a function defined in one is used by the others.
"use strict";
// ---------- SESSIONS ----------
var FLT_TAG = "";
function loadSessions() {
  ALL.p = null;                                    // the list page always reads fresh
  var q = [], st = $("flt-status").value, it = $("flt-input").value, qs = $("flt-q").value.trim();
  if (st) q.push("status=" + enc(st));
  if (it) q.push("input=" + enc(it));
  if (qs) q.push("q=" + enc(qs));
  if (FLT_TAG) q.push("tag=" + enc(FLT_TAG));
  $("flt-tag-row").hidden = !FLT_TAG; $("flt-tag").textContent = FLT_TAG + " ✕";
  var box = $("sessions-table-box");
  box.innerHTML = skel(6);
  pagedSessions(q.join("&")).then(function (r) {
    if (!r.ok) { box.innerHTML = '<p class="notice error" style="margin:12px">' + esc(r.data.error) + '</p>'; return; }
    var rows = r.data.sessions || [];
    var any = st || it || qs || FLT_TAG;
    box.innerHTML = rows.length ? sessionsTable(rows, "sessions-table", true)
      : '<div class="empty"><p>' + (any ? "No sessions match these filters." : "No sessions yet — start one with New benchmark.") + '</p>' +
        (any ? "" : '<a class="btn primary" href="#/new">New benchmark</a>') + '</div>';
    box.querySelectorAll("[data-tag]").forEach(function (b) { b.addEventListener("click", function () { FLT_TAG = b.getAttribute("data-tag"); loadSessions(); }); });
    wireRowActions(box, rows);
  });
}
["flt-status", "flt-input"].forEach(function (id) { $(id).addEventListener("change", loadSessions); });
// row actions (the "More actions" menu): rename (a dialog), a short note (inline editor under the row)
// and delete; the owner or an admin
function wireRowActions(box, rows) {
  var byId = {}; rows.forEach(function (s) { byId[s.session_id] = s; });
  var menuBtn = function (el) { var m = el.closest(".rmenu"); return (m && m.querySelector(".rmenu-btn")) || el; };
  box.querySelectorAll("[data-rename]").forEach(function (b) { b.addEventListener("click", function () {
    var id = b.getAttribute("data-rename"), s = byId[id];
    askName({ title: "Rename session", label: "Session name", value: s.title || "", placeholder: (s.models || []).map(short).join(" vs ") || shortId(id),
              hint: "Up to 80 characters, shown instead of the session id. Leave it empty to remove the name.",
              save: function (name) { return api("/api/sessions/" + enc(id), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: name }) }); }
    }).then(function (r) {
      if (!r) return;
      ALL.p = null;                                  // the Data sets / Home lists read fresh
      if (SESSION.id === id) SESSION = { id: null, res: null, job: null };
      loadSessions();
    });
  }); });
  box.querySelectorAll("[data-note]").forEach(function (b) { b.addEventListener("click", function () {
    var id = b.getAttribute("data-note"), tr = b.closest("tr"), open = tr.nextElementSibling && tr.nextElementSibling.classList.contains("note-row");
    box.querySelectorAll(".note-row").forEach(function (r) { r.remove(); });
    if (open) return;
    var row = document.createElement("tr"); row.className = "note-row";
    row.innerHTML = '<td colspan="' + tr.cells.length + '"><form class="note-form" novalidate><label for="row-note-text">Note for ' + esc(shortId(id)) + '</label>' +
      '<textarea id="row-note-text" rows="2" maxlength="2000" placeholder="e.g. rerun after the tokenizer fix">' + esc(byId[id].notes || "") + '</textarea>' +
      '<div class="actions"><button type="submit" class="btn primary small-btn">Save note</button><button type="button" class="btn small-btn" data-note-cancel>Cancel</button>' +
      '<span class="small muted" role="status" data-note-status></span></div></form></td>';
    tr.after(row);
    var f = row.querySelector("form"), ta = row.querySelector("textarea");
    ta.focus();
    row.querySelector("[data-note-cancel]").addEventListener("click", function () { row.remove(); menuBtn(b).focus(); });
    f.addEventListener("submit", function (ev) {
      ev.preventDefault();
      api("/api/sessions/" + enc(id), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ notes: ta.value }) }).then(function (r) {
        if (!r.ok) { row.querySelector("[data-note-status]").textContent = (r.data || {}).error || "Could not save."; return; }
        loadSessions();
      });
    });
  }); });
  box.querySelectorAll("[data-del]").forEach(function (b) { b.addEventListener("click", function () {
    var id = b.getAttribute("data-del"), s = byId[id];
    confirmAction({ title: "Delete session " + (s.title ? "\u201C" + s.title + "\u201D" : (s.models || []).map(short).join(" vs ")) + "?", danger: true, ok: "Delete session",
      body: "Session " + id + " is deleted for everyone: its results, PDF and stored prepared-set copy. This cannot be undone." }).then(function (yes) {
    if (!yes) return;
    var mb = menuBtn(b); mb.disabled = true;
    api("/api/sessions/" + enc(id), { method: "DELETE" }).then(function (r) {
      if (!r.ok) { mb.disabled = false; alert((r.data || {}).error || "Could not delete the session."); return; }
      if (SESSION.id === id) SESSION = { id: null, res: null, job: null };
      loadSessions();
    });
    });
  }); });
}
var fltTimer = null;
$("flt-q").addEventListener("input", function () { clearTimeout(fltTimer); fltTimer = setTimeout(loadSessions, 250); });
$("sessions-filter").addEventListener("submit", function (ev) { ev.preventDefault(); loadSessions(); });
$("flt-tag").addEventListener("click", function () { FLT_TAG = ""; loadSessions(); });

// ---------- ONE SESSION ----------
var SESSION = { id: null, res: null, job: null };
// Session sections (workflow order) and the sub-views of the two data-heavy ones. Each section
// and sub-view has its own link: #/session/<id>/<section>[/<view>].
var SECTIONS = {
  overview: {}, reliability: {}, agents: {}, cost: {}, data: {},
  ranking: { subs: [["scores", "Scores"], ["weighting", "Weighting"], ["robustness", "Robustness"], ["tradeoffs", "Trade-offs"], ["guide", "Model Guide"]] },
  performance: { subs: [["comparison", "Comparison"], ["latency", "Latency"], ["capacity", "Capacity"], ["drivers", "What drives it"], ["gpu", "GPU & Throughput"], ["statistics", "Statistics"]] },
  recommendations: { subs: [["limits", "Your limits"], ["context", "Model context"], ["guidance", "Deployment guidance"]] }
};
// sections of the embedded analysis page (web/analysis.html data-part) shown under a view
var EMBED_PARTS = {
  "ranking/scores": ["highlights", "saw"], "ranking/weighting": ["weights"],
  "ranking/guide": ["modelctx", "faq", "glossary"],
  "performance/comparison": ["perclass"], "performance/latency": ["cov", "prefill"],
  "performance/gpu": ["throughput"], "performance/statistics": ["stats"],
  "recommendations/guidance": ["ds-rec", "ds-optim"]
};
var LEGACY_TAB = { summary: "overview", detailed: "ranking", recommendation: "recommendations", downloads: "data", accuracy: "reliability" };
var sessionTabs = Array.prototype.slice.call(document.querySelectorAll("#session-tabs [role=tab]"));
var SUB_MEMORY = {};
tablist($("session-tabs"), function (t) {
  if (t.getAttribute("aria-disabled") === "true") return;
  var k = t.getAttribute("data-tab"), sub = SUB_MEMORY[k] || (SECTIONS[k].subs ? SECTIONS[k].subs[0][0] : null);
  location.hash = "#/session/" + enc(SESSION.id) + "/" + k + (sub ? "/" + sub : "");
});
function loadSession(id, tab) {
  if (CP) return loadSessionCP(id, tab);
  var token = ++pollToken;
  if (SESSION.id === id && SESSION.res) { showSessionTab(tab); return; }
  SESSION = { id: id, res: null, job: null };
  $("h-session").textContent = id;
  $("session-chips").innerHTML = "";
  $("session-body").hidden = true; $("session-watch").hidden = true;
  $("svc-dl-pdf").setAttribute("href", apiUrl("/api/jobs/" + enc(id) + "/report.pdf"));
  $("svc-dl-pdf").hidden = true;
  $("session-compare").setAttribute("href", "#/compare?a=" + enc(id)); $("session-again").hidden = true;
  (function tick() {
    if (token !== pollToken) return;
    api("/api/jobs/" + enc(id)).then(function (r) {
      if (token !== pollToken) return;
      var w = $("session-watch");
      if (!r.ok) { w.hidden = false; w.innerHTML = '<div class="notice error">' + esc(r.data.error) + ' <a href="#/sessions">All sessions</a></div>'; return; }
      var j = r.data;
      SESSION.job = j;
      if (j.kind === "prepare") { location.replace("#/new/models?prep=" + enc(id)); return; }
      if (j.status !== "done") {
        w.hidden = false;
        if (!$("watch-card")) {
          w.innerHTML = '<div class="card" id="watch-card"><div class="row" style="margin-top:0;justify-content:space-between"><h2 id="watch-phase">…</h2><span id="watch-status" class="status Running">…</span></div>' +
            '<p id="watch-models" class="muted small"></p><div class="bar" id="watch-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-label="Session progress"><span id="watch-fill"></span></div>' +
            '<p id="watch-text" aria-live="polite"></p><div id="watch-error" class="notice error" role="alert" hidden></div>' +
            '<div class="actions"><button type="button" class="btn danger" id="watch-stop" hidden>Stop benchmark</button><button type="button" class="btn" id="watch-resume" hidden>Resume</button></div></div><div id="watch-tel"></div>';
          startTelemetry($("watch-tel"), id);
          $("watch-resume").onclick = function () {
            api("/api/jobs/" + enc(id) + "/resume", { method: "POST" }).then(function (rr) { if (!rr.ok) showError($("watch-error"), rr.data); else { SESSION.id = null; loadSession(id, tab); } });
          };
        }
        renderProgress(j, { status: $("watch-status"), bar: $("watch-bar"), fill: $("watch-fill"), models: $("watch-models"),
                            text: $("watch-text"), phase: $("watch-phase"), error: $("watch-error"), resume: $("watch-resume"),
                            stop: $("watch-stop"), id: id, onStopped: function () { SESSION.id = null; loadSession(id, tab); } });
        if (j.status === "failed" || j.status === "interrupted" || j.status === "stopped") return;
        setTimeout(tick, 2000);
        return;
      }
      w.hidden = true; w.innerHTML = "";
      api("/api/jobs/" + enc(id) + "/result").then(function (rr) {
        if (token !== pollToken) return;
        if (!rr.ok) { w.hidden = false; w.innerHTML = '<div class="notice error">' + esc(rr.data.error) + '</div>'; return; }
        SESSION.res = rr.data;
        renderSession(rr.data, j);
        showSessionTab(tab);
      });
    });
  })();
}
// With a control plane: the stored session (Worker: D1/R2) — readable with the GPU off. A
// running session's live progress comes from the backend when it is online.
function loadSessionCP(id, tab) {
  var token = ++pollToken;
  if (SESSION.id === id && SESSION.res) { showSessionTab(tab); return; }
  SESSION = { id: id, res: null, job: null, stored: null };
  $("h-session").textContent = id;
  $("session-chips").innerHTML = "";
  $("session-body").hidden = true; $("session-watch").hidden = true;
  $("svc-dl-pdf").hidden = true;
  $("session-compare").setAttribute("href", "#/compare?a=" + enc(id)); $("session-again").hidden = true;
  var w = $("session-watch");
  function fromWorker(attempt) {
    api("/api/sessions/" + enc(id)).then(function (r) {
      if (token !== pollToken) return;
      if (!r.ok) {
        if (r.status === 404 && ONLINE && attempt === 0) return fromBackend();   // not stored yet: a brand-new job
        w.hidden = false; w.innerHTML = '<div class="notice error">' + esc(r.data.error) + ' <a href="#/sessions">All sessions</a></div>'; return;
      }
      SESSION.stored = r.data;
      if (r.data.has_results) {
        api("/api/sessions/" + enc(id) + "/results").then(function (rr) {
          if (token !== pollToken) return;
          if (!rr.ok) { w.hidden = false; w.innerHTML = '<div class="notice error">' + esc(rr.data.error) + '</div>'; return; }
          w.hidden = true; w.innerHTML = "";
          var st = SESSION.stored, job = { job_id: id, run_name: st.run_name, models: st.models, created_at: st.created_at,
                                          title: st.title || "", notes: st.notes || "", tags: st.tags || [], can_edit: !!st.can_delete,
                                          owner: st.owner, files: st.files || [], can_delete: st.can_delete, stored: true };
          SESSION.res = rr.data; SESSION.job = job;
          renderSession(rr.data, job);
          showSessionTab(tab);
        });
        return;
      }
      if (r.data.status === "Running" && ONLINE) return fromBackend();
      if (r.data.status === "Done" || r.data.status === "Demo") {           // finished; results still uploading
        if (attempt < 20) { setTimeout(function () { fromWorker(attempt + 1); }, 1500); return; }
      }
      w.hidden = false;
      w.innerHTML = '<div class="card" id="watch-card"><div class="row" style="margin-top:0;justify-content:space-between"><h2>' +
        (r.data.status === "Running" ? "Running on the GPU" : "Session " + esc(r.data.status).toLowerCase()) + '</h2>' + statusChip(r.data.status) + '</div>' +
        '<p class="muted small">' + (r.data.models || []).map(function (m) { return esc(m) + extraTag(m); }).join("  vs  ") + (r.data.owner ? " · started by " + esc(r.data.owner) : "") + '</p>' +
        (ONLINE ? "" : '<div class="notice warn" id="watch-offline">GPU offline — live progress and Resume need the GPU backend. Stored sessions stay browsable.</div>') +
        (r.data.status === "Stopped" ? '<p class="muted small">Stopped before it finished — not scored.</p>' +
          (r.data.run_name ? '<div class="actions"><a class="btn" id="watch-again" href="#/new/models?set=' + enc(r.data.run_name) + '">Benchmark these flows again</a></div>' : "") : "") + '</div>';
      if (ONLINE && (r.data.status === "Failed" || r.data.status === "Interrupted")) fromBackend();
    });
  }
  function fromBackend() {
    (function tick() {
      if (token !== pollToken) return;
      api("/api/jobs/" + enc(id)).then(function (r) {
        if (token !== pollToken) return;
        if (!r.ok) { w.hidden = false; w.innerHTML = '<div class="notice error">' + esc(r.data.error) + ' <a href="#/sessions">All sessions</a></div>'; return; }
        var j = r.data;
        SESSION.job = j;
        if (j.kind === "prepare") { location.replace("#/new/models?prep=" + enc(id)); return; }
        if (j.status === "done") { $("watch-card") && $("watch-card").remove(); return fromWorker(1); }
        w.hidden = false;
        if (!$("watch-progress")) {
          w.innerHTML = '<div class="card" id="watch-card"><div id="watch-progress"></div><div class="row" style="margin-top:0;justify-content:space-between"><h2 id="watch-phase">…</h2><span id="watch-status" class="status Running">…</span></div>' +
            '<p id="watch-models" class="muted small"></p><div class="bar" id="watch-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-label="Session progress"><span id="watch-fill"></span></div>' +
            '<p id="watch-text" aria-live="polite"></p><div id="watch-error" class="notice error" role="alert" hidden></div>' +
            '<div class="actions"><button type="button" class="btn danger" id="watch-stop" hidden>Stop benchmark</button><button type="button" class="btn" id="watch-resume" hidden>Resume</button></div></div><div id="watch-tel"></div>';
          startTelemetry($("watch-tel"), id);
          $("watch-resume").onclick = function () {
            api("/api/jobs/" + enc(id) + "/resume", { method: "POST" }).then(function (rr) { if (!rr.ok) showError($("watch-error"), rr.data); else { SESSION.id = null; loadSession(id, tab); } });
          };
        }
        renderProgress(j, { status: $("watch-status"), bar: $("watch-bar"), fill: $("watch-fill"), models: $("watch-models"),
                            text: $("watch-text"), phase: $("watch-phase"), error: $("watch-error"), resume: $("watch-resume"),
                            stop: $("watch-stop"), id: id, onStopped: function () { SESSION.id = null; loadSession(id, tab); } });
        if (j.status === "failed" || j.status === "interrupted" || j.status === "stopped") return;
        setTimeout(tick, 2000);
      });
    })();
  }
  fromWorker(0);
}
function rememberedTab() { try { return localStorage.getItem("agentmeter.sessionTab") || "overview"; } catch (e) { return "overview"; } }
function parseTab(tab) {
  var parts = String(tab || "overview").split("/"), k = LEGACY_TAB[parts[0]] || parts[0];
  if (!SECTIONS[k]) k = "overview";
  var subs = SECTIONS[k].subs, sub = null;
  if (subs) sub = subs.some(function (x) { return x[0] === parts[1]; }) ? parts[1] : (SUB_MEMORY[k] || subs[0][0]);
  return { tab: k, sub: sub };
}
function showSessionTab(tab) {
  var t = parseTab(tab), key = t.tab + (t.sub ? "/" + t.sub : ""), base = "#/session/" + enc(SESSION.id) + "/";
  if (t.sub) SUB_MEMORY[t.tab] = t.sub;
  try { localStorage.setItem("agentmeter.sessionTab", key); } catch (e) { /* not remembered */ }
  sessionTabs.forEach(function (x) { x.setAttribute("href", base + x.getAttribute("data-tab")); });
  selectTab(sessionTabs, $("stab-" + t.tab));
  var bar = $("session-subtabs"), subs = SECTIONS[t.tab].subs;
  bar.hidden = !subs;
  bar.innerHTML = subs ? subs.map(function (x) {
    return '<a role="tab" href="' + base + t.tab + "/" + x[0] + '" id="ssub-' + x[0] + '" data-sub="' + x[0] + '" aria-selected="' + (x[0] === t.sub) + '" tabindex="' + (x[0] === t.sub ? 0 : -1) + '">' + esc(x[1]) + '</a>';
  }).join("") : "";
  if (subs) tablist(bar, function (x) { location.hash = base + t.tab + "/" + x.getAttribute("data-sub"); });
  var panel = $("spanel-" + t.tab);
  panel.querySelectorAll("[data-pane]").forEach(function (pn) { pn.hidden = pn.getAttribute("data-pane") !== (t.sub || "main"); });
  showEmbed(EMBED_PARTS[key] || null, t.tab);
  if (t.tab === "agents") loadFlows();
  tocSpy(t.tab);
  var want = (parseHash().q || {}).sec;                    // a link to a section: open it there
  if (want) setTimeout(function () { tocGo(want, true); }, 60);
}
// ---------- report layout (2026-11-10): every subtab is a short report ----------
// After a tab renders, each card becomes a section: its heading the question it answers, a plain
// answer line when the results support one, then the card's own content. A contents list ("On this
// page") follows the reader and remembers the section in the link (?sec=).
var SECTION_Q = {
  "Can it keep up?": "How many flows can one GPU handle?", "Cold start": "How much does the first flow cost?",
  "Consistency of answers": "Are the answers consistent?", "Cost and energy": "What does it cost per 1,000 flows?",
  "Cost per valid answer": "What does each usable answer cost?", "Criteria profile": "How does each model score on each criterion?",
  "Deployment fit at a glance": "Which deployment setups does each model fit?", "Effect size and confidence": "How big is the difference?",
  "Efficiency frontier": "Which models are never beaten on two costs at once?", "Energy efficiency": "How energy-efficient is each model?",
  "Flow explorer": "Explore every flow", "GPU idle while running": "How much does an idle GPU cost?",
  "GPU memory": "How much GPU memory does it need?", "GPU over the run": "What did the GPU do during the run?",
  "Good to know": "Can these results be trusted?", "Hand-offs between agents": "What do agents pass to each other?",
  "Head-to-head": "Which model is faster, and by how much?", "IDS → LLM triage": "How did the IDS and the LLM split the work?",
  "Latency distribution": "How fast is each model?", "Latency over the run": "Does it slow down during the run?",
  "Measured verdict": "Which model should I deploy?", "Model context": "What is known about these models?",
  "Per model": "Each model at a glance", "Pipeline timeline for one flow": "One flow, step by step",
  "Project to your workload": "What would my volume cost?", "Prompt size and latency": "Do bigger prompts make it slower?",
  "SAW weights": "How much does each criterion count?", "Scorecard": "How do the models compare, criterion by criterion?",
  "Time per agent by attack class": "Which attack classes take longest, agent by agent?", "Token budgets": "Are the token budgets right?",
  "Triage": "What would an analyst have to do?", "Valid answers": "Did every flow get a usable answer?",
  "Where answers fail": "Where do answers fail?", "What makes a flow slow?": "Which flows are slow, and why?",
  "Which classes each model mixes up": "Which classes does each model mix up?", "What this would look like in a real SOC": "What would this look like in a real SOC?",
  "Prepared set": "Which flows were used?", "Report and results": "Download the report and results",
  "Since the last run on these flows": "What changed since the last run?", "Other pipelines on these flows": "How do other pipelines compare on these flows?",
  "Best model for my priority": "Which model is best for my priority?", "What the prompts are made of": "What are the prompts made of?"
};
var EMBED_TITLE = { "ranking/scores": "Score tables in detail", "ranking/weighting": "Weights in detail", "ranking/guide": "Model notes, questions and glossary",
  "performance/comparison": "Per attack class", "performance/latency": "How predictable is it, and where does the time go?",
  "performance/gpu": "Throughput", "performance/statistics": "Statistical tests", "recommendations/guidance": "What to do next, and how to optimise" };
function secSlug(s) { return "sec-" + String(s).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48); }
function reportify(tab, res) {
  var panel = $("spanel-" + tab), answers = window.INSIGHTS ? INSIGHTS.sectionAnswers(res) : {}, summary = panel.querySelector(".takeaway");
  var NAMES = {};                                     // a model's full or short name -> its full name
  (res.per_model || []).forEach(function (m) { NAMES[m.model] = m.model; NAMES[short(m.model)] = m.model; if (m.db_label) NAMES[m.db_label] = m.model; });
  if (summary) summary.id = "sec-summary-" + tab;
  panel.querySelectorAll("[data-pane]").forEach(function (pn) {
    var items = [], seen = {};
    Array.prototype.slice.call(pn.children).filter(function (c) { return c.classList.contains("card") && c.querySelector("h2"); }).forEach(function (card) {
      var h2 = card.querySelector("h2"), title = h2.textContent.trim(), q = SECTION_Q[title] || title, id = secSlug(q);
      while (seen[id]) id += "-2";
      seen[id] = true;
      h2.textContent = q; card.id = card.id || id; card.classList.add("rsec");
      var a = answers[q];
      if (a && !card.querySelector(".rsec-a")) {
        var p = document.createElement("p"); p.className = "rsec-a"; p.textContent = a;
        (h2.parentNode === card ? h2 : h2.parentNode).insertAdjacentElement("afterend", p);
      }
      card.querySelectorAll("details.info > summary").forEach(function (s) {
        if (/what each column means|what the columns mean|how this is (tested|measured)|how these are measured|definitions/i.test(s.textContent)) s.textContent = "How to read this";
      });
      // model names in tables and legends: the everyday name (full name on hover), so a narrow column stays readable
      card.querySelectorAll("td.mono, td .mono, span.mono, b.mono, .lg-name, .hb-l").forEach(function (el) {
        var full = NAMES[el.textContent.trim()];
        if (!full) return;
        var tn = null;                                // the name's text node (a legend may also hold a colour swatch)
        el.childNodes.forEach(function (n) { if (n.nodeType === 3 && n.nodeValue.trim()) tn = n; });
        if (!tn || el.querySelector("*:not(i)")) return;
        el.title = full; tn.nodeValue = nick(full); el.classList.add("model-name");
      });
      items.push({ id: card.id, q: q });
    });
    var key = tab + "/" + pn.getAttribute("data-pane");
    if (EMBED_PARTS[key]) items.push({ id: "embed-host", q: EMBED_TITLE[key] || "More detail" });
    if (items.length < 2) return;
    if (summary) items.unshift({ id: summary.id, q: "Summary" });
    var body = document.createElement("div"); body.className = "report-body";
    while (pn.firstChild) body.appendChild(pn.firstChild);
    var nav = document.createElement("nav"); nav.className = "report-toc"; nav.setAttribute("aria-label", "On this page");
    nav.innerHTML = '<p class="toc-k">On this page</p><ol>' + items.map(function (it) {
      return '<li><a href="#" data-toc="' + esc(it.id) + '">' + esc(it.q) + '</a></li>';
    }).join("") + '</ol>';
    pn.classList.add("report"); pn.appendChild(nav); pn.appendChild(body);
  });
}
// the section in view is marked in the visible pane's contents list
var TOC_SPY = null;
function tocSpy(tab) {
  if (TOC_SPY) TOC_SPY.disconnect();
  var pn = Array.prototype.filter.call($("spanel-" + tab).querySelectorAll("[data-pane].report"), function (x) { return !x.hidden; })[0];
  if (!pn || !window.IntersectionObserver) return;
  var links = pn.querySelectorAll("[data-toc]"), mark = function (id) {
    links.forEach(function (l) { if (l.getAttribute("data-toc") === id) l.setAttribute("aria-current", "true"); else l.removeAttribute("aria-current"); });
  };
  TOC_SPY = new IntersectionObserver(function (es) {
    es.forEach(function (e) { if (e.isIntersecting) mark(e.target.id); });
  }, { rootMargin: "-15% 0px -70% 0px" });
  links.forEach(function (l) { var el = document.getElementById(l.getAttribute("data-toc")); if (el) TOC_SPY.observe(el); });
  if (links[0]) mark(links[0].getAttribute("data-toc"));
}
function tocGo(id, instant) {
  var el = document.getElementById(id);
  if (!el) return;
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({ behavior: instant || reduce ? "auto" : "smooth", block: "start" });
  try { history.replaceState(null, "", location.hash.split("?")[0] + "?sec=" + encodeURIComponent(id)); } catch (e) { /* the link stays as it was */ }
}
document.addEventListener("click", function (ev) {
  var a = ev.target.closest && ev.target.closest("[data-toc]");
  if (!a) return;
  ev.preventDefault(); tocGo(a.getAttribute("data-toc"));
});
function pane(name, html) { return '<div data-pane="' + name + '"' + (name === "main" ? "" : " hidden") + '>' + html + '</div>'; }
// The takeaway at the top of a tab (A + C): the answer in large type, why it matters in a plain
// sentence, then each model ranked with a bar (the best one marked). Bars are relative to the largest.
function takeaway(t, key) {
  if (!t) return "";
  if (typeof t === "string") t = { h: t };
  var rows = t.chips || [], max = Math.max.apply(null, rows.map(function (c) { return c.n || 0; }).concat([0]));
  return '<div class="takeaway tab-summary" data-summary="' + esc(key || "") + '"><p class="tk-h">' + esc(t.h) + '</p>' +
    (t.s ? '<p class="tk-s">' + esc(t.s) + '</p>' : "") +
    (rows.length > 1 ? '<ol class="tk-list" aria-label="Per model">' + rows.map(function (c) {
      var w = max > 0 && c.n != null ? Math.max(2, 100 * c.n / max) : 0;
      return '<li class="tk-row' + (c.best ? " is-best" : "") + '"><span class="tk-name">' + esc(c.label) + '</span>' +
        '<span class="tk-bar" aria-hidden="true"><i style="width:' + w.toFixed(1) + '%"></i></span><span class="tk-v">' + esc(c.value) +
        (c.best ? '<span class="sr-only"> (best)</span>' : "") + '</span></li>';
    }).join("") + '</ol>' : "") + '</div>';
}
function renderSession(res, job) {
  var s = res.session, env = res.environment || {}, real = env.provider === "real";
  var vs = (res.per_model || []).map(function (m) { return nick(m.model); }).join(" vs ");
  $("h-session").textContent = job.title || vs || job.job_id;      // a session the owner named shows its name
  $("h-session").title = (res.per_model || []).map(function (m) { return m.model; }).join(" vs ");   // the full names on hover
  document.title = $("h-session").textContent + " · AgentMeter";
  $("session-chips").innerHTML = '<span class="chip ' + (real ? "ok" : "warn") + '">' + (real ? "GPU: " + esc(env.gpu_name || "unknown") : "Demo (no GPU)") + '</span>' +
    '<span class="chip">' + fmtNum(s.n_flows) + ' flows</span><span class="chip">' + esc((s.source_type || "").toUpperCase()) + '</span>' +
    '<span class="chip">' + (s.accuracy_available ? "labelled" : "no reference labels") + '</span>' +
    (job.title && vs ? '<span class="chip">' + esc(vs) + '</span>' : "") +
    '<span class="chip mono" title="Session id ' + esc(job.job_id) + '">' + esc(shortId(job.job_id)) + '</span>' +
    (job.owner ? '<span class="chip" title="Started by">' + esc(job.owner) + '</span>' : "");
  setDl($("svc-dl-pdf"), pdfPath(job), "agentmeter_report_" + job.job_id + ".pdf");
  $("svc-dl-pdf").hidden = false;
  if (job.run_name) {
    $("session-again").setAttribute("href", "#/new/models?set=" + enc(job.run_name) + "&models=" + (job.models || []).map(enc).join(",") + "&again=" + enc(job.job_id) +
      (CP && (job.stored || SESSION.stored) ? "&from=" + enc(job.job_id) : ""));
    $("session-again").hidden = false;
  }
  $("session-body").hidden = false;
  var base = "#/session/" + enc(job.job_id) + "/";
  if (window.INSIGHTS) setModelSlots(res);
  setAgentSlots(res);
  $("session-guide").hidden = guideSeen("session");
  var SUM = window.INSIGHTS ? INSIGHTS.tabSummaries(res) : {};
  var lead = function (k) { return takeaway(SUM[k], k); };
  LC = {};                                         // line charts of this session (wired after render)
  $("spanel-overview").innerHTML = pane("main", overviewTab(res) + '<div class="card" id="svc-rep-line" hidden></div>' + notesCard(job) + scheduleCard(job));
  $("spanel-ranking").innerHTML = lead("ranking") + pane("scores", '<div class="row" style="margin-top:0;justify-content:flex-end"><a class="btn primary" id="svc-evaluate" href="' + base +
      'recommendations/limits">Evaluate for my workload →</a></div>') + pane("weighting", weightsCard(res)) + pane("robustness", rankStabilityCard(res) + stabilityCard(res)) + pane("tradeoffs", frontierCard(res)) + pane("guide", "");
  $("spanel-reliability").innerHTML = lead("reliability") + pane("main", idsCard(res) + reliabilityCard(res) + accuracyStatsCard(res) + confusionCard(res) +
    triageCard(res) + socCard(res) + consistencyCard(res) + agreementCard(res) + failureCard(res));
  $("spanel-performance").innerHTML = lead("performance") + pane("comparison", h2hCard(res)) + pane("latency", latencyCard(res) + trendCard(res)) + pane("capacity", capacityCard(res) + coldStartCard(res)) + pane("drivers", driversCard(res) + scalingCard(res)) + pane("gpu", memoryCard(res) + loadCard(res)) + pane("statistics", effectsCard(res) + sampleSizeCard(res) + '<div id="svc-repeat-host"></div>');
  $("spanel-agents").innerHTML = lead("agents") + pane("main", agentsTab(res));
  $("spanel-cost").innerHTML = lead("cost") + pane("main", costCard(res) + energyEffCard(res) + gpuTimelineCard(res) + idleCard(res) + projectionCard(res) + priceCard(res) + correctCard(res));
  $("spanel-recommendations").innerHTML = lead("recommendations") + pane("limits", recLimitsTab(res)) + pane("context", recContextTab(res)) + pane("guidance", "");
  $("spanel-data").innerHTML = pane("main", downloadsTab(res, job));
  ["overview", "ranking", "reliability", "performance", "agents", "cost", "recommendations", "data"].forEach(function (t) { reportify(t, res); });
  resetEmbed();
  wireAgents(res); wireDecision(res); wireDownloads(res, job); wirePresets(res); wireOverview(res); wireProjection(res); wireLineCharts(); SAME_SETUP = {};       // fresh per render: a later session on these flows must show up
  wireRepeat(res, job); wireRepLine(job); wireNotes(job); wireSchedule(job); wireChanged(job); wireFrontier(res); wireCapacity(res); wirePipelineCompare(job); wirePrices(res); wirePicker(res);
  addExports($("session-body")); paintLimits($("session-body"));
}

function setDl(a, path, name) {
  a.setAttribute("data-auth-dl", path); a.setAttribute("data-name", name);
  a.setAttribute("href", CP ? "#" : apiUrl(path)); a.setAttribute("download", name);
}
// the stored PDF (Worker/R2, works with the GPU off) or the backend's
function pdfPath(job) {
  return CP && job.stored && (job.files || []).indexOf("report.pdf") >= 0 ? "/api/sessions/" + enc(job.job_id) + "/files/report.pdf"
    : "/api/jobs/" + enc(job.job_id) + "/report.pdf";
}
// SAW weights: the ones the stored scores used, and (view only) a user's preset
function weightsText(w) {
  return ["latency", "vram", "tokens"].filter(function (k) { return w && w[k] != null; })
    .map(function (k) { return k + " " + Number(w[k]).toFixed(2); }).join(" · ");
}
function presetRank(res, preset) {
  var p8 = res.phase8 || {}, w = Object.assign({}, preset.weights);
  w.accuracy = 0;                       // sessions rank on efficiency only; reference labels are a validity check
  var tot = ["accuracy", "latency", "vram", "tokens"].reduce(function (a, k) { return a + (+w[k] || 0); }, 0) || 1;
  Object.keys(w).forEach(function (k) { w[k] = (+w[k] || 0) / tot; });
  var rows = (p8.normalised || []).map(function (n) {
    var c = ["accuracy", "latency", "vram", "tokens"].reduce(function (a, k) { return a + w[k] * (+n[k] || 0); }, 0);
    return { model: String(n.model).replace(/^mock:/, ""), score: Math.round(c * 10000) / 100 };
  }).sort(function (a, b) { return b.score - a.score; });
  return { weights: w, rows: rows };
}
function weightsCard(res) {
  var p8 = res.phase8 || {}, presets = (SETTINGS && SETTINGS.weight_presets) || [], ew = (res.phase8_efficiency_only || {}).weights || p8.weights;
  return '<div class="card" id="svc-weights-card"><h2>SAW weights</h2><p class="small" id="svc-weights-used"><b>The score uses:</b> ' +
    esc(weightsText(ew) || "not recorded") + ' <span class="muted">(efficiency only: latency, VRAM and tokens; agreement with reference labels is a validity check under Output Reliability, not a criterion)</span></p>' +
    (presets.length ? '<div class="row"><div class="field"><label for="svc-preset">Re-rank with one of your presets (view only)</label><select id="svc-preset"><option value="">— stored weights —</option>' +
      presets.map(function (p, i) { return '<option value="' + i + '">' + esc(p.name) + '</option>'; }).join("") + '</select></div></div><div id="svc-preset-view"></div>'
      : '<p class="muted small">Add weight presets in Settings to re-rank here (view only).</p>') +
    '<p class="muted small">A preset never changes a stored score, rank or verdict; this view states which weights it uses.</p></div>';
}
function wirePresets(res) {
  var sel = $("svc-preset");
  if (!sel) return;
  sel.addEventListener("change", function () {
    var v = $("svc-preset-view"), p = (SETTINGS.weight_presets || [])[+sel.value];
    if (sel.value === "" || !p) { v.innerHTML = ""; return; }
    var r = presetRank(res, p);
    v.innerHTML = '<p class="small" id="svc-preset-weights"><b>This view uses preset “' + esc(p.name) + '”:</b> ' + esc(weightsText(r.weights)) + '</p>' +
      '<div class="table-wrap"><table id="svc-preset-table"><thead><tr><th class="num">View rank</th><th>Model</th><th class="num">Score with this preset</th></tr></thead><tbody>' +
      r.rows.map(function (x, i) { return '<tr><td class="num">' + (i + 1) + '</td><td class="mono">' + esc(x.model) + extraTag(x.model) + '</td><td class="num">' + x.score.toFixed(2) + '</td></tr>'; }).join("") +
      '</tbody></table></div>';
  });
}

// Overview: headline, number tiles, per-model table, "Good to know" labels
function sawE(m) { return m.saw_efficiency_only || m.saw || {}; }
function gpuShort(n) { return String(n || "").replace(/^NVIDIA\s+/, "").replace(/^GeForce\s+/, ""); }
function headline(rel, pm) {
  if (!rel) return short((pm[0] || {}).model) + ": single-model run";
  var ev = rel.efficiency_verdict || {}, a = short(rel.models[0]), b = short(rel.models[1]), n = rel.models.length;
  if (n > 2) return ev.verdict === "more_efficient" ? short(ev.model) + " is the most efficient of " + n + " models"
    : ev.verdict === "mixed" ? "Mixed result: no model beats all " + (n - 1) + " others" : "All " + n + " models tie on efficiency";
  if (ev.verdict === "more_efficient") return short(ev.model) + " is more efficient than " + (ev.model === rel.models[0] ? b : a);
  if (ev.verdict === "mixed") return "Mixed result: each model wins somewhere";
  return a + " and " + b + " are tied on efficiency";
}
var TILE = { mean_latency_s: ["Latency", "faster"], mean_peak_vram_mb: ["VRAM", "less"], mean_tokens_per_flow: ["Tokens", "fewer"] };
function tiles(res) {
  var rel = res.relative_comparison, lab = !!(res.session || {}).accuracy_available;
  if (!rel) return "";
  var t = rel.metrics.filter(function (m) { return TILE[m.metric]; }).map(function (m) {
    var v = !m.available ? "—" : m.winner === "tie" ? "Tie" : Math.round(m.pct_better) + " % " + TILE[m.metric][1];
    var w = !m.available ? "not measured" : m.winner === "tie" ? "within the tie range" : short(m.winner) + (m.runner_up ? " vs " + short(m.runner_up) : "");
    return '<div class="tile" data-metric="' + esc(m.metric) + '"><div class="k">' + TILE[m.metric][0] + '</div><div class="v">' + esc(v) + '</div><div class="w">' + esc(w) + '</div></div>';
  });
  var orl = (res.output_reliability || {}).per_model || [];
  var low = orl.slice().sort(function (a, b) { return (a.valid_rate || 0) - (b.valid_rate || 0); })[0];
  t.push('<div class="tile" data-metric="valid"><div class="k">Valid answers</div><div class="v">' + (low ? fmtPct(low.valid_rate) : "—") + '</div><div class="w">' +
    esc(!low ? "not recorded" : orl.every(function (m) { return m.valid_rate === 1; }) ? "every model, every flow" : "lowest: " + short(low.model)) + '</div></div>');
  if (res.ids) t.push('<div class="tile" data-metric="ids"><div class="k">IDS gate</div><div class="v">' + fmtPct(res.ids.llm_calls_saved_share) + ' saved</div><div class="w">' +
    esc(fmtNum(res.ids.n_alerts) + " of " + fmtNum(res.ids.n_flows_in) + " flows sent to the LLM") + '</div></div>');
  var tg = ((res.triage || {}).per_model || []).filter(function (m) { return m.gate; });
  if (tg.length) {
    var pass = tg.filter(function (m) { return effGate(m).passed === true; }), fail = tg.filter(function (m) { return effGate(m).passed === false; });
    t.push('<div class="tile" data-metric="gate"><div class="k">Triage gate</div><div class="v">' + (pass.length + fail.length ? pass.length + " of " + tg.length + " pass" : "not checked") + '</div><div class="w">' +
      esc(fail.length ? "fails: " + fail.map(function (m) { return short(m.model); }).join(", ") : pass.length ? "every checked model" : "needs labelled input") + '</div></div>');
  }
  return '<div class="tiles" id="svc-tiles">' + t.join("") + '</div>';
}
// the stored caveat sentences, shown as short labels (each opens its full sentence)
var CAVEAT_LABEL = [
  [/^Relative view: all (\d) models/, function () { return "✓ Fair comparison"; }],
  [/^Relative view/, function () { return "✓ Fair A-vs-B"; }],
  [/^Measured on (.+?): absolute numbers/, function (m) { return (/^cpu$/i.test(m[1]) ? "CPU" : gpuShort(m[1])) + " ≠ study GPU (L4)"; }],
  [/^NON-VALIDATED/, function () { return "Exploratory run"; }],
  [/^Efficiency only/, function () { return "No labels → efficiency only"; }],
  [/^PCAP features are approximate/, function () { return "PCAP features approximate"; }],
  [/^Balanced sample/, function () { return "Balanced sample"; }],
  [/^6-class run/, function () { return "6 classes (Other Attack)"; }],
  [/^No VRAM readings/, function () { return "No VRAM readings"; }],
  [/^MOCK provider/, function () { return "Demo data (mock)"; }],
  [/^Small sample \((\d+) flows/, function (m) { return "Small sample (" + m[1] + " flows)"; }]
];
function caveatItems(cav) {
  var items = cav.map(function (c) {
    for (var i = 0; i < CAVEAT_LABEL.length; i++) { var m = CAVEAT_LABEL[i][0].exec(c); if (m) return { label: CAVEAT_LABEL[i][1](m), text: c }; }
    return { label: c.split(/[:.;—]/)[0].slice(0, 40), text: c };
  });
  // said once: the feature half of "Efficiency only: …; N of 78 features are not numerically comparable …"
  // goes with the PCAP label when there is one (both sentences stay readable, regrouped)
  var eff = items.filter(function (x) { return /^Efficiency only/.test(x.text); })[0];
  var pcap = items.filter(function (x) { return /^PCAP features are approximate/.test(x.text); })[0];
  if (eff && pcap) {
    var parts = eff.text.replace(/\.$/, "").split("; "), keep = parts.filter(function (x) { return !/features/.test(x); });
    var moved = parts.filter(function (x) { return /features/.test(x); });
    if (moved.length && keep.length) { eff.text = keep.join("; ") + "."; pcap.text = pcap.text + " (" + moved.join("; ") + ".)"; }
  }
  return items;
}
function overviewTab(res) {
  var s = res.session, pm = res.per_model, rel = res.relative_comparison, cmp = res.comparison, env = res.environment || {};
  var lab = !!s.accuracy_available, h = [];
  var ce = {}; ((res.cost_energy || {}).per_model || []).forEach(function (r) { ce[r.model] = r; });
  var mm = {}; ((res.memory || {}).per_model || []).forEach(function (r) { mm[r.model] = r; });
  var vr = {}; ((res.output_reliability || {}).per_model || []).forEach(function (r) { vr[r.model] = r; });
  var real = env.provider === "real";
  h.push('<div class="card" id="svc-verdict-card"><h2 class="headline" id="svc-headline">' + esc(headline(rel, pm)) + '</h2>' + tiles(res) +
    '<p id="svc-env" class="small" style="margin:6px 0 0">' + (real ? '<span class="chip ok">Real GPU · ' + esc(gpuShort(env.gpu_name) || "unknown") + '</span>'
      : '<span class="chip warn">Demo (mock): not a model measurement</span>') + (lab ? "" : ' <span class="chip">Efficiency only</span>') + '</p>' +
    '<details class="info" id="svc-full-verdict"><summary>Show full verdict</summary><p class="verdict" id="svc-verdict">' +
      esc(rel ? rel.verdict : "Single-model run — no head-to-head comparison.") + '</p></details>' +
    '<details class="info" id="svc-measured"><summary>ⓘ How this was measured</summary><p class="small muted">' +
      (real ? 'GPU ' + esc(env.gpu_name || "unknown") + ' · NVIDIA driver ' + esc(env.driver_version || "—") + ' · CUDA ' + esc(env.cuda_runtime_version || "—") + ' · 4-bit NF4' + (env.engine === "vllm" ? ', vLLM engine (continuous batching; per-agent working VRAM and time-to-first-token are not measured with it)' : env.engine ? ", " + esc(env.engine) + " engine" : "") + ', one model at a time.'
        : 'MOCK provider: a deterministic heuristic, not a language model. It shows the flow; the numbers are not model measurements.') + '</p>' +
      (rel && cmp ? '<p class="small muted" id="svc-absolute"><b>Absolute SAW</b> (against fixed L4-study targets): ' + esc(cmp.statement) + '</p>' : "") + '</details></div>');
  h.push(gpuHealthCard(res), findingsCard(res), scorecardCard(res), '<div id="svc-changed-host"></div>', profileCard(res));
  var tip = function (t) { return '<span class="info-i" tabindex="0" title="' + esc(t) + '" aria-label="' + esc(t) + '">i</span>'; };
  var rows = pm.map(function (m) {
    var e = m.efficiency.end_to_end, c = ce[m.model] || {};
    return '<tr><td class="mono">' + esc(m.model) + extraTag(m.model) + '</td><td class="num"' + limAttr("max_mean_latency_s", e.mean_latency_s) + '>' + fmtTime(e.mean_latency_s) + '</td><td class="num"' + limAttr("max_p95_latency_s", e.p95_latency_s) + '>' + fmtTime(e.p95_latency_s) +
      '</td><td class="num">' + fmtNum(e.mean_tokens_per_flow) + '</td><td class="num">' + fmtMB(e.mean_peak_vram_mb) +
      '</td><td class="num"' + limAttr("max_peak_vram_mb", (mm[m.model] || {}).total_peak_mb) + '>' + ((mm[m.model] || {}).total_peak_mb != null ? fmtMB(mm[m.model].total_peak_mb) : '<span class="muted">' + esc((mm[m.model] || {}).status || "—") + '</span>') +
      '</td><td class="num"' + limAttr("max_cost_per_1k_flows_usd", c.cost_per_1k_flows_usd) + '>' + (c.cost_per_1k_flows_usd != null ? fmtUsd(c.cost_per_1k_flows_usd) : '<span class="muted">' + esc(c.cost_status || "—") + '</span>') +
      '</td><td class="num">' + (c.wh_per_flow != null ? fmtWh(c.wh_per_flow) : '<span class="muted">' + esc(c.energy_status || "—") + '</span>') + '</td>' +
      '<td class="num">' + (vr[m.model] ? fmtPct(vr[m.model].valid_rate) : '<span class="muted">—</span>') + '</td>' +
      '<td class="num">' + esc(sawE(m).composite_100) + ' <span class="muted">' + esc(sawE(m).tier) + '</span></td></tr>';
  }).join("");
  h.push('<div class="card"><h2>Per model</h2><p class="muted small" id="lim-note" hidden style="margin-top:0">Coloured against your limits: <span class="over-limit">over</span> or <span class="in-limit">within</span> (set them under Recommendations → Does a model fit your limits?).</p><div class="table-wrap"><table id="svc-models-table"><thead><tr><th>Model</th><th class="num">Latency / flow</th><th class="num">p95</th><th class="num">Tokens / flow</th>' +
    '<th class="num">Working VRAM' + tip("Excludes the model weights: the memory the agent calls add") + '</th><th class="num">Total peak VRAM' + tip("Model weights + working memory") + '</th>' +
    '<th class="num">Cost / 1k flows</th><th class="num">Energy / flow</th>' +
    '<th class="num">Valid answers' + tip("Share of flows with a usable answer (see Output Reliability)") + '</th><th class="num">SAW (efficiency)</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
    '<p class="muted small">The SAW score uses latency, VRAM and tokens only.' + tip("AgentMeter measures how efficiently LLMs run the SOC workload; the reference labels only check that the output is valid.") + '</p></div>');
  var items = caveatItems((rel ? rel.caveats : s.caveats) || []);
  h.push('<div class="card" id="svc-caveats-card"><h2>Good to know</h2><div class="gtk" id="svc-caveats">' +
    items.map(function (x, i) { return '<button type="button" data-cav="' + i + '" aria-expanded="false" aria-controls="svc-cav-detail">' + esc(x.label) + '</button>'; }).join("") +
    '</div><p class="gtk-detail" id="svc-cav-detail" hidden></p><template id="svc-cav-texts">' +
    items.map(function (x) { return '<p>' + esc(x.text) + '</p>'; }).join("") + '</template></div>');
  return h.join("");
}
// Overview: key findings and the per-criterion profile (INSIGHTS, insights.js)
var CRIT_LABEL = { accuracy: "Accuracy", latency: "Latency", vram: "VRAM", tokens: "Tokens" };
// a model keeps one colour on every chart of a session (session model order, never rank)
// 1-2 models: --series-1/2; 3-4 models: --mseries-1..4 (validated for every pair, not only neighbours)
var MODEL_SLOT = {}, MODEL_N = 2;
function setModelSlots(res) { MODEL_SLOT = {}; MODEL_N = (res.per_model || []).length; (res.per_model || []).forEach(function (m, i) { MODEL_SLOT[INSIGHTS.bare(m.model)] = i; }); }
function slotColor(i) { return MODEL_N > 2 ? "var(--mseries-" + (i % 4 + 1) + ")" : "var(--series-" + (i % 2 + 1) + ")"; }
function modelColor(m) { var i = MODEL_SLOT[window.INSIGHTS ? INSIGHTS.bare(m) : m]; return slotColor(i == null ? 0 : i); }
function legendHtml(names) {
  return names.length < 2 ? "" : '<div class="ilegend">' + names.map(function (n) { return '<span><i style="--c:' + modelColor(n) + '"></i>' + esc(short(n)) + '</span>'; }).join("") + '</div>';
}
function findingsCard(res) {
  var f = window.INSIGHTS ? INSIGHTS.keyFindings(res) : [];
  if (!f.length) return "";
  return '<div class="card" id="svc-findings"><h2>Key findings</h2><ul class="findings">' +
    f.map(function (x) { return '<li data-finding="' + esc(x.key) + '">' + esc(x.text) + '</li>'; }).join("") +
    '</ul><p class="muted small">Each line restates one stored number of this session; nothing is re-measured.</p></div>';
}
function profileCard(res) {
  if (!window.INSIGHTS) return "";
  var pr = INSIGHTS.criteriaProfile(res), names = pr.rows.map(function (r) { return r.model; });
  if (!pr.rows.length) return "";
  var body = pr.criteria.map(function (k) {
    return '<div class="prof-row" data-crit="' + k + '"><span class="lbl">' + CRIT_LABEL[k] + (pr.missing[k] ? '<small>no readings</small>' : "") + '</span><div class="prof-bars">' +
      pr.rows.map(function (r, i) {
        var v = r.scores[k], na = pr.missing[k] || v == null;
        var txt = short(r.model) + " · " + CRIT_LABEL[k] + ": " + (na ? "not measured" : v.toFixed(2) + " of 1.00 (target)");
        return '<div class="ibar' + (na ? " na" : "") + '" tabindex="0" data-hover="' + esc(txt) + '" aria-label="' + esc(txt) + '"><span class="track"><span class="fill" style="--c:' + modelColor(r.model) + ';width:' + (na ? 0 : Math.min(100, 100 * v)) + '%"></span><span class="tgt" style="left:100%"></span></span><span class="val">' + (na ? "n/a" : v.toFixed(2)) + '</span></div>';
      }).join("") + '</div></div>';
  }).join("");
  return '<div class="card" id="svc-profile"><h2>Criteria profile</h2><p class="muted small" style="margin-top:0">Each criterion scored against the study target: 1.00 (dashed line) meets the target. These are the three inputs of the SAW score (efficiency only).</p>' +
    legendHtml(names) + '<div class="prof" role="group" aria-label="Criteria profile">' + body + '</div>' +
    '<details class="info"><summary>Show as table</summary><div class="table-wrap"><table id="svc-profile-table"><thead><tr><th>Model</th>' +
    pr.criteria.map(function (k) { return '<th class="num">' + CRIT_LABEL[k] + '</th>'; }).join("") + '</tr></thead><tbody>' +
    pr.rows.map(function (r) { return '<tr><td class="mono">' + esc(r.model) + '</td>' + pr.criteria.map(function (k) { return '<td class="num">' + (pr.missing[k] || r.scores[k] == null ? "—" : r.scores[k].toFixed(3)) + '</td>'; }).join("") + '</tr>'; }).join("") +
    '</tbody></table></div></details></div>';
}
// Ranking -> Robustness: the weight range in which the top model stays first
function stabilityCard(res) {
  if (!window.INSIGHTS || !res.phase8) return "";
  var st = INSIGHTS.weightStability(res);
  if (st.models.length < 2) return '<div class="card" id="svc-stability"><h2>How far can the weights move?</h2><p class="muted">Single-model run: there is no ranking to change.</p></div>';
  var r2 = function (v) { return v.toFixed(2); };
  var rows = st.rows.map(function (row) {
    var segs = row.segments.map(function (sg) {
      var lbl = (sg.leader ? short(sg.leader) + " first" : "tie") + " for weight " + r2(sg.from) + "–" + r2(sg.to);
      return '<span class="seg' + (sg.leader ? "" : " tie") + '" data-hover="' + esc(CRIT_LABEL[row.criterion] + ": " + lbl) + '" style="left:' + (100 * sg.from) + '%;width:' + Math.max(0.4, 100 * (sg.to - sg.from)) + '%;' +
        (sg.leader ? "--c:" + modelColor(sg.leader) : "") + '"></span>';
    }).join("");
    return '<div class="stab-row" data-crit="' + row.criterion + '"><span class="lbl">' + CRIT_LABEL[row.criterion] + '<small>stored ' + r2(row.stored) + '</small></span>' +
      '<div><div class="stab-track" role="img" aria-label="' + esc(INSIGHTS.stabilitySentence(row)) + '">' + segs +
      '<span class="stab-mark" style="left:' + (100 * row.stored) + '%" title="stored weight ' + r2(row.stored) + '"></span></div>' +
      '<p class="small stab-text">' + esc(INSIGHTS.stabilitySentence(row)) + '</p></div></div>';
  }).join("");
  return '<div class="card" id="svc-stability"><h2>How far can the weights move?</h2><p class="muted small" style="margin-top:0">Each strip runs one weight from 0 to 1 while the other three keep their stored proportions; ' +
    'the colour shows which model would rank first. The marker is the stored weight.</p>' + legendHtml(st.models) +
    '<div class="stab-axis"><span></span><div><span>0</span><span>0.5</span><span>1</span></div></div><div class="stab">' + rows + '</div>' +
    '<details class="info"><summary>Show as table</summary><div class="table-wrap"><table id="svc-stability-table"><thead><tr><th>Criterion</th><th class="num">Stored weight</th><th>First at stored weight</th><th>Stays first for weights</th></tr></thead><tbody>' +
    st.rows.map(function (row) {
      return '<tr><td>' + CRIT_LABEL[row.criterion] + '</td><td class="num">' + r2(row.stored) + '</td><td class="mono">' + esc(row.leader ? short(row.leader) : "tie") + '</td><td class="num">' +
        (row.no_data ? "any (no readings)" : row.stable ? r2(row.stable[0]) + " – " + r2(row.stable[1]) : "—") + '</td></tr>';
    }).join("") + '</tbody></table></div></details></div>';
}
// ---------- Analysis expansion, phase 1: scorecard, what changed, frontier, rank stability ----------
var GRADE = { best: ["best", "ok"], close: ["close", ""], behind: ["behind", "warn"], no_data: ["no data", ""] };
function fmtUnit(u, v) { return v == null ? "—" : u === "s" ? fmtTime(v) : u === "MB" ? fmtMB(v) : u === "USD" ? fmtUsd(v) : u === "share" ? fmtPct(v) : u === "Wh" ? fmtWh(v) : fmtNum(v); }
function scorecardCard(res) {
  if (!window.INSIGHTS || (res.per_model || []).length < 1) return "";
  var sc = INSIGHTS.scorecard(res);
  var head = '<tr><th>Model</th>' + sc.dims.map(function (d) { return '<th class="num">' + esc(d.label) + '</th>'; }).join("") + '</tr>';
  var body = sc.models.map(function (m, i) {
    return '<tr><td class="mono">' + esc(short(m)) + '</td>' + sc.dims.map(function (d) {
      var c = d.cells[i], g = GRADE[c.grade];
      var rel = c.grade === "no_data" || c.grade === "best" || sc.models.length < 2 ? "" :
        ' <span class="muted small">' + (d.key === "valid_rate" ? "−" + fmtNum(c.gap * 100, 1) + " pts" : "+" + fmtNum(c.gap * 100, 0) + "%") + '</span>';
      return '<td class="num" data-grade="' + c.grade + '">' + fmtUnit(d.unit, c.value) + rel + (sc.models.length > 1 && c.grade !== "no_data" ? ' <span class="chip ' + g[1] + '">' + g[0] + '</span>' : "") + '</td>';
    }).join("") + '</tr>';
  }).join("");
  return '<div class="card" id="svc-scorecard"><h2>Scorecard</h2><p class="muted small" style="margin-top:0">Each model against the best of this session: <b>best</b> = within ' + (sc.band * 100) +
    '% of the best, <b>close</b> = within 25% (validity: 5 points), <b>behind</b> = further. Lower is better except valid answers.</p>' +
    '<div class="table-wrap"><table id="svc-scorecard-table"><thead>' + head + '</thead><tbody>' + body + '</tbody></table></div></div>';
}
// the other sessions on the same flows, GPU and settings (shared by "What changed" and "Repeatability")
var SAME_SETUP = {};
function sameSetup(id) {
  if (!SAME_SETUP[id]) SAME_SETUP[id] = api("/api/compare/candidates?session=" + enc(id)).then(function (r) {
    var others = r.ok ? (r.data.sessions || []).slice(0, 19) : [];
    if (!r.ok || !others.length) return { ok: r.ok, error: (r.data || {}).error, others: [] };
    return api("/api/compare/merged?sessions=" + [id].concat(others.map(function (x) { return x.session_id; })).map(enc).join(",")).then(function (m) {
      return { ok: m.ok, error: (m.data || {}).error, others: others, merged: m.ok ? m.data : null };
    });
  });
  return SAME_SETUP[id];
}
function wireChanged(job) {
  var host = $("svc-changed-host"), id = job.job_id;
  if (!host || !window.INSIGHTS) return;
  sameSetup(id).then(function (d) {
    if (SESSION.id !== id) return;
    var wc = d.merged ? INSIGHTS.whatChanged(d.merged, id) : { models: [] };
    var any = wc.models.filter(function (m) { return m.previous; });
    host.innerHTML = '<div class="card" id="svc-changed"><h2>Since the last run on these flows</h2>' + (!any.length
      ? '<p class="muted small" style="margin-bottom:0">No earlier session ran these models on the same flows, GPU and settings. Run them again later to see what changes.</p>'
      : '<p class="muted small" style="margin-top:0">Each model against its most recent earlier session on the same flows, GPU and settings. Changes under ' + (wc.band * 100) + '% count as the same.</p>' +
        '<div class="table-wrap"><table id="svc-changed-table"><thead><tr><th>Model</th><th>Metric</th><th class="num">Before</th><th class="num">Now</th><th class="num">Change</th></tr></thead><tbody>' +
        any.map(function (m) {
          return m.changes.map(function (c, i) {
            var ch = c.direction === "same" ? '<span class="muted">about the same</span>' : (c.pct > 0 ? "+" : "−") + fmtNum(Math.abs(c.pct) * 100, 0) + "% " + (c.direction === "lower" ? "lower" : "higher");
            return '<tr>' + (i ? '<td></td>' : '<td class="mono">' + esc(short(m.model)) + '<br><a class="small" href="#/session/' + enc(m.previous) + '">' + esc(fmtDate(m.previous_at)) + '</a></td>') +
              '<td>' + esc(c.label) + '</td><td class="num">' + fmtUnit(c.unit, c.before) + '</td><td class="num">' + fmtUnit(c.unit, c.now) + '</td><td class="num" data-dir="' + c.direction + '">' + ch + '</td></tr>';
          }).join("");
        }).join("") + '</tbody></table></div>') + '</div>';
    addExports(host);
  });
}
// Model Ranking > Trade-offs: latency against a second cost; the frontier = not beaten on both
function frontierCard(res) {
  if (!window.INSIGHTS) return "";
  var fr = INSIGHTS.efficiencyFrontier(res);
  if (fr.points.length < 2) return '<div class="card" id="rank-frontier"><h2>Efficiency frontier</h2><p class="muted small">Needs two or more models with readings.</p></div>';
  return '<div class="card" id="rank-frontier"><h2>Efficiency frontier</h2><p class="muted small" style="margin-top:0">Each dot is a model: lower-left is better on both. A model on the frontier is not beaten on both at once; the others are.</p>' +
    '<div class="row" style="margin-top:0"><div class="field"><label for="frontier-y">Compare latency with</label><select id="frontier-y">' +
    fr.available.map(function (k) { return '<option value="' + k + '"' + (k === fr.y ? " selected" : "") + '>' + esc({ memory: "GPU memory", tokens: "Tokens per flow", cost: "Cost per 1,000 flows" }[k]) + '</option>'; }).join("") +
    '</select></div></div><div id="frontier-plot"></div></div>';
}
function drawFrontier(res) {
  var host = $("frontier-plot"), sel = $("frontier-y");
  if (!host) return;
  var fr = INSIGHTS.efficiencyFrontier(res, sel ? sel.value : null), W = 640, H = 300, L = 70, R = 20, T = 16, B = 44;
  var mx = Math.max.apply(null, fr.points.map(function (p) { return p.x; })) * 1.12 || 1, my = Math.max.apply(null, fr.points.map(function (p) { return p.y; })) * 1.12 || 1;
  var X = function (v) { return L + (W - L - R) * v / mx; }, Y = function (v) { return T + (H - T - B) * (1 - v / my); };
  var yu = { memory: "MB", tokens: "tokens", cost: "USD" }[fr.y];
  var front = fr.points.filter(function (p) { return p.frontier; }).sort(function (a, b) { return a.x - b.x; });
  var step = front.map(function (p, i) { return (i ? "L" + X(p.x) + " " + Y(front[i - 1].y) + " " : "M") + X(p.x) + " " + Y(p.y); }).join(" ");
  var ticks = [0, 0.5, 1].map(function (f) { return f; });
  host.innerHTML = legendHtml(fr.points.map(function (p) { return p.model; })) + '<svg viewBox="0 0 ' + W + ' ' + H + '" class="frontier" role="img" aria-label="' + esc("Latency against " + fr.y_label + ": " + fr.points.map(function (p) { return short(p.model) + (p.frontier ? " on the frontier" : " beaten"); }).join(", ")) + '">' +
    ticks.map(function (f) { return '<line class="grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(my * f) + '" y2="' + Y(my * f) + '"/><text class="ax" x="' + (L - 6) + '" y="' + (Y(my * f) + 4) + '" text-anchor="end">' + esc(fmtUnit(yu, my * f)) + '</text>' +
      '<text class="ax" x="' + X(mx * f) + '" y="' + (H - B + 16) + '" text-anchor="middle">' + esc(fmtTime(mx * f)) + '</text>'; }).join("") +
    '<text class="ax" x="' + ((W + L) / 2) + '" y="' + (H - 6) + '" text-anchor="middle">Latency per flow (lower is better)</text>' +
    '<text class="ax" transform="translate(14 ' + ((H - B + T) / 2) + ') rotate(-90)" text-anchor="middle">' + esc(fr.y_label) + '</text>' +
    (front.length > 1 ? '<path class="front" d="' + step + '"/>' : "") +
    labelled(fr.points, X, Y, W - 4).map(function (p) {
      var t = short(p.model) + ": " + fmtTime(p.x) + ", " + fmtUnit(yu, p.y) + (p.frontier ? " — on the frontier" : " — beaten on both by " + p.dominated_by.map(short).join(", "));
      return '<g tabindex="0" data-hover="' + esc(t) + '" aria-label="' + esc(t) + '"><circle cx="' + X(p.x) + '" cy="' + Y(p.y) + '" r="7" style="fill:' + modelColor(p.model) + '"' + (p.frontier ? "" : ' class="beaten"') + '/>' +
        (p.lx != null ? '<text class="lbl" x="' + p.lx + '" y="' + p.ly + '">' + esc(short(p.model)) + '</text>' : "") + '</g>';
    }).join("") + '</svg>' +
    '<div class="table-wrap"><table id="rank-frontier-table"><thead><tr><th>Model</th><th class="num">Latency / flow</th><th class="num">' + esc(fr.y_label) + '</th><th>On the frontier</th></tr></thead><tbody>' +
    fr.points.map(function (p) { return '<tr><td class="mono">' + esc(short(p.model)) + '</td><td class="num">' + fmtTime(p.x) + '</td><td class="num">' + fmtUnit(yu, p.y) + '</td><td>' + (p.frontier ? "yes" : "no — beaten by " + esc(p.dominated_by.map(short).join(", "))) + '</td></tr>'; }).join("") +
    '</tbody></table></div>';
}
// a direct label above-right of its dot, only where it collides with no other label or dot
// (the legend, the hover and the table name every model either way)
function labelled(points, X, Y, right) {
  var placed = [];
  return points.slice().sort(function (a, b) { return a.x - b.x; }).map(function (p) {
    var w = 7 * short(p.model).length, lx = X(p.x) + 11 + w > right ? X(p.x) - 11 - w : X(p.x) + 11, ly = Y(p.y) - 11;
    var hit = placed.some(function (q) { return lx < q.x + q.w + 6 && q.x < lx + w + 6 && Math.abs(q.y - ly) < 15; }) ||
      points.some(function (q) { return q !== p && X(q.x) > lx - 8 && X(q.x) < lx + w && Math.abs(Y(q.y) - ly) < 14; });
    if (hit) return Object.assign({}, p, { lx: null });
    placed.push({ x: lx, y: ly, w: w });
    return Object.assign({}, p, { lx: lx, ly: ly });
  });
}
function wireFrontier(res) {
  var sel = $("frontier-y");
  if (sel) sel.addEventListener("change", function () { drawFrontier(res); });
  drawFrontier(res);
}
// Model Ranking > Robustness: how sure is the order? (paired bootstrap of the flows)
function rankStabilityCard(res) {
  var rs = res.rank_stability;
  if (!rs) return "";
  if (rs.skipped) return '<div class="card" id="rank-boot"><h2>How sure is the ranking?</h2><p class="muted small">' + esc(rs.skipped) + '.</p></div>';
  var pm = rs.per_model.slice().sort(function (a, b) { return b.p_first - a.p_first; });
  var pl = function (v) { return v == null ? '<span class="muted">no readings</span>' : fmtPct(v); };
  return '<div class="card" id="rank-boot"><h2>How sure is the ranking?</h2><p class="muted small" style="margin-top:0">The flows were resampled ' + rs.n_boot + ' times; each time the efficiency score was recomputed. How often each model comes first' +
    (rs.share_top_tied > 0.2 ? ', and how often the top was a tie (' + fmtPct(rs.share_top_tied) + ')' : "") + ':</p><div class="hbars" id="rank-boot-bars">' +
    pm.map(function (m) {
      var t = short(m.model) + ": first in " + fmtPct(m.p_first) + " of resamples; rank " + m.rank_p5 + "–" + m.rank_p95 + " in 90% of them";
      return '<div class="hbar"><span class="lbl">' + esc(short(m.model)) + '</span><span class="track" tabindex="0" data-hover="' + esc(t) + '" aria-label="' + esc(t) + '"><span class="fill" style="display:block;width:' + (100 * m.p_first).toFixed(1) +
        '%;background:' + modelColor(m.model) + '"></span></span><span class="val">' + fmtPct(m.p_first) + '</span></div>';
    }).join("") + '</div><div class="table-wrap" style="margin-top:10px"><table id="rank-boot-table"><thead><tr><th>Model</th><th class="num">First</th><th class="num">Mean rank</th><th class="num">Rank (90% of resamples)</th>' +
    '<th class="num">Fastest</th><th class="num">Fewest tokens</th><th class="num">Least working VRAM</th></tr></thead><tbody>' +
    pm.map(function (m) { return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td class="num">' + fmtPct(m.p_first) + '</td><td class="num">' + fmtNum(m.mean_rank, 2) + '</td><td class="num">' + m.rank_p5 + '–' + m.rank_p95 +
      '</td><td class="num">' + pl(m.p_lowest.latency) + '</td><td class="num">' + pl(m.p_lowest.tokens) + '</td><td class="num">' + pl(m.p_lowest.vram) + '</td></tr>'; }).join("") +
    '</tbody></table></div><details class="info"><summary>Method</summary><p class="small muted">' + esc(rs.method) + ' ' + esc(rs.note) + '</p></details></div>';
}
// ---------- Analysis expansion, phase 3: capacity, cold start, what drives latency ----------
function capacityCard(res) {
  if (!window.INSIGHTS || !(res.latency_trend || {}).per_model) return '<div class="card"><p class="muted">This session predates the per-flow latency record.</p></div>';
  var means = (res.per_model || []).map(function (m) { return (((m.efficiency || {}).end_to_end) || {}).mean_latency_s; }).filter(function (x) { return x; });
  var def = means.length ? Math.max(1, Math.round(0.6 * 60 / Math.max.apply(null, means))) : 60;     // ~60% of the slowest model's capacity
  return '<form class="card" id="cap-card" novalidate><h2>Can it keep up?</h2><p class="muted small" style="margin-top:0">Enter how many flows arrive per minute. One GPU answers one flow at a time (as measured); the simulation shows whether a queue builds up and how long flows wait before their answer.</p>' +
    '<div class="row" style="margin-top:0"><div class="field"><label for="cap-rate">Flows arriving per minute</label><input id="cap-rate" type="number" min="0.1" step="any" value="' + def + '"></div></div>' +
    '<div id="cap-out"></div></form>';
}
function drawCapacity(res) {
  var out = $("cap-out"), inp = $("cap-rate");
  if (!out || !inp) return;
  var rate = +inp.value;
  if (!(rate > 0)) { out.innerHTML = '<p class="muted small">Enter a rate above 0.</p>'; return; }
  var q = INSIGHTS.queueSim(res, rate);
  out.innerHTML = '<div class="table-wrap"><table id="cap-table"><thead><tr><th>Model</th><th class="num">Capacity / min</th><th class="num">GPU busy</th><th>Keeps up?</th><th class="num">Mean wait</th><th class="num">p95 wait</th><th class="num">Longest queue</th></tr></thead><tbody>' +
    q.models.map(function (m) {
      if (!m.measured) return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td colspan="6" class="muted">no per-flow latencies</td></tr>';
      return '<tr data-keeps="' + m.keeps_up + '"><td class="mono">' + esc(short(m.model)) + '</td><td class="num">' + fmtNum(m.capacity_per_min, 1) + '</td><td class="num">' + fmtPct(Math.min(1, m.utilisation)) +
        '</td><td>' + (m.keeps_up ? '<span class="chip ok">✓ yes</span>' : '<span class="chip bad">✗ no</span> <span class="small">backlog grows by ' + fmtNum(m.backlog_per_hour) + ' flows/hour</span>') +
        '</td><td class="num">' + fmtTime(m.mean_wait_s) + '</td><td class="num">' + fmtTime(m.p95_wait_s) + '</td><td class="num">' + fmtNum(m.max_queue) + ' flows</td></tr>';
    }).join("") + '</tbody></table></div><p class="small muted">' + esc(q.method) + ' Above about 80% busy, waits grow quickly. A real deployment may batch flows (see load scaling under GPU &amp; Throughput).</p>';
}
function wireCapacity(res) {
  var inp = $("cap-rate");
  if (!inp) return;
  inp.addEventListener("input", function () { drawCapacity(res); });
  $("cap-card").addEventListener("submit", function (ev) { ev.preventDefault(); });
  drawCapacity(res);
}
function coldStartCard(res) {
  var cs = res.cold_start;
  if (!cs) return "";
  return '<div class="card" id="cold-start"><h2>Cold start</h2><p class="muted small" style="margin-top:0">How long loading each model took before its first flow, and whether that first flow was slower than the rest. It matters when the GPU restarts or models are swapped often.</p>' +
    '<div class="table-wrap"><table id="cold-start-table"><thead><tr><th>Model</th><th class="num">Load time</th><th class="num">First flow</th><th class="num">Median of the rest</th><th class="num">First-flow penalty</th></tr></thead><tbody>' +
    cs.per_model.map(function (m) {
      return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td class="num">' + (m.load_s == null ? '<span class="muted">not recorded</span>' : fmtTime(m.load_s)) + '</td><td class="num">' + fmtTime(m.first_flow_s) +
        '</td><td class="num">' + fmtTime(m.median_rest_s) + '</td><td class="num">' + (m.first_flow_penalty_s == null ? "—" : (m.first_flow_penalty_s >= 0 ? "+" : "−") + fmtTime(Math.abs(m.first_flow_penalty_s)) +
        (m.first_flow_ratio ? ' <span class="muted small">(' + fmtNum(m.first_flow_ratio, 1) + '×)</span>' : "")) + '</td></tr>';
    }).join("") + '</tbody></table></div><p class="small muted">' + esc(cs.note) + '</p></div>';
}
function driversCard(res) {
  var d = res.drivers;
  if (!d) return '<div class="card"><p class="muted">This session predates the driver analysis.</p></div>';
  var rho = function (v) {
    if (v == null) return '<span class="muted">—</span>';
    var w = Math.min(50, Math.abs(v) * 50);
    return '<span class="rho" title="' + esc(fmtNum(v, 2)) + '"><span class="rho-track"><span class="rho-fill" style="' + (v < 0 ? "right:50%" : "left:50%") + ';width:' + w.toFixed(1) + '%"></span></span><span class="val">' + (v > 0 ? "+" : v < 0 ? "−" : "") + fmtNum(Math.abs(v), 2) + '</span></span>';
  };
  var body = d.per_model.map(function (m) {
    if (!m.features || !m.features.length) return '<h3>' + esc(short(m.model)) + '</h3><p class="muted small">' + (d.has_features ? "Not enough flows or feature variation to correlate." : "The flow features of this session were not kept.") + '</p>';
    return '<h3><i class="sw" style="--c:' + modelColor(m.model) + '"></i>' + esc(short(m.model)) + ' <span class="muted small">' + m.n_flows + ' flows, ' + m.n_features_tested + ' features tested</span></h3>' +
      '<div class="table-wrap"><table class="drivers-table"><thead><tr><th>Flow feature</th><th>With latency</th><th>With tokens</th><th class="num">p (latency)</th></tr></thead><tbody>' +
      m.features.map(function (f) { return '<tr><td>' + esc(f.feature) + '</td><td>' + rho(f.rho_latency) + '</td><td>' + rho(f.rho_tokens) + '</td><td class="num">' + (f.p_latency == null ? "—" : (+f.p_latency).toPrecision(2)) + '</td></tr>'; }).join("") +
      '</tbody></table></div>';
  }).join("");
  return '<div class="card" id="drivers-card"><h2>What makes a flow slow?</h2><p class="muted small" style="margin-top:0">Which flow features go together with a longer answer time or more tokens (Spearman correlation: +1 = always more, −1 = always less, 0 = no link). Correlation, not cause.</p>' + body + '</div>';
}
function scalingCard(res) {
  var d = res.drivers;
  if (!d) return "";
  var ms = d.per_model.filter(function (m) { return m.input_scaling; });
  if (!ms.length) return '<div class="card" id="scaling-card"><h2>Prompt size and latency</h2><p class="muted small">The prompt size hardly varied between flows in this session, so there is no slope to measure.</p></div>';
  var W = 640, H = 280, L = 70, R = 20, T = 14, B = 42;
  // x: the measured range of prompt sizes (padded), not from 0 — the spread is the point
  var lo = Math.min.apply(null, ms.map(function (m) { return m.input_scaling.min_tokens; })), hi = Math.max.apply(null, ms.map(function (m) { return m.input_scaling.max_tokens; }));
  var pad = (hi - lo) * 0.06 || Math.max(1, hi * 0.02), x0d = Math.max(0, lo - pad), mx = hi + pad;
  var my = Math.max.apply(null, ms.map(function (m) { return Math.max.apply(null, m.input_scaling.points.map(function (p) { return p[1]; })); })) * 1.1 || 1;
  var X = function (v) { return L + (W - L - R) * (v - x0d) / (mx - x0d); }, Y = function (v) { return T + (H - T - B) * (1 - v / my); };
  var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" class="frontier" role="img" aria-label="Latency against input tokens per flow">' +
    [0, 0.5, 1].map(function (f) { return '<line class="grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(my * f) + '" y2="' + Y(my * f) + '"/><text class="ax" x="' + (L - 6) + '" y="' + (Y(my * f) + 4) + '" text-anchor="end">' + esc(fmtTime(my * f)) + '</text>' +
      '<text class="ax" x="' + X(x0d + (mx - x0d) * f) + '" y="' + (H - B + 16) + '" text-anchor="middle">' + fmtNum(x0d + (mx - x0d) * f) + '</text>'; }).join("") +
    '<text class="ax" x="' + ((W + L) / 2) + '" y="' + (H - 4) + '" text-anchor="middle">Input tokens per flow (all agents)</text>' +
    ms.map(function (m) {
      var s = m.input_scaling, c = modelColor(m.model);
      var x0 = s.min_tokens, x1 = s.max_tokens;
      return m.input_scaling.points.map(function (p) { return '<circle class="pt" cx="' + X(p[0]) + '" cy="' + Y(p[1]) + '" r="3" style="fill:' + c + '"/>'; }).join("") +
        '<line class="fit" x1="' + X(x0) + '" y1="' + Y(s.intercept_s + s.slope_s_per_1k_tokens * x0 / 1000) + '" x2="' + X(x1) + '" y2="' + Y(s.intercept_s + s.slope_s_per_1k_tokens * x1 / 1000) + '" style="stroke:' + c + '"/>';
    }).join("") + '</svg>';
  return '<div class="card" id="scaling-card"><h2>Prompt size and latency</h2><p class="muted small" style="margin-top:0">Each dot is a flow; the line is the best straight fit. Its slope says how much time 1,000 more input tokens add, which shows what longer feature lists or chattier hand-offs would cost.</p>' +
    legendHtml(ms.map(function (m) { return m.model; })) + svg +
    '<div class="table-wrap"><table id="scaling-table"><thead><tr><th>Model</th><th class="num">+ time per 1,000 input tokens</th><th class="num">Fit (r)</th><th class="num">Input tokens per flow</th></tr></thead><tbody>' +
    ms.map(function (m) { var s = m.input_scaling; return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td class="num">' + (s.slope_s_per_1k_tokens < 0 ? "−" + fmtTime(-s.slope_s_per_1k_tokens) : "+" + fmtTime(s.slope_s_per_1k_tokens)) + '</td><td class="num">' + fmtNum(s.r, 2) + '</td><td class="num">' + fmtNum(s.min_tokens) + '–' + fmtNum(s.max_tokens) + '</td></tr>'; }).join("") +
    '</tbody></table></div></div>';
}
// ---------- Analysis expansion, phase 4: agents ----------
function removeAgentCard(res) {
  if (!window.INSIGHTS || !res.agents) return "";
  var ra = INSIGHTS.removeAgent(res);
  return '<div class="card" id="agents-remove"><h2>What if one agent were removed?</h2><p class="muted small" style="margin-top:0">An estimate from this session\'s measurements: removing an agent saves its own call and the hand-off tokens later agents re-read. It says nothing about whether the answers stay as useful: re-run the pipeline without it (Build my own) to measure that.</p>' +
    ra.per_model.map(function (m) {
      return '<h3><i class="sw" style="--c:' + modelColor(m.model) + '"></i>' + esc(short(m.model)) + ' <span class="muted small">' + fmtTime(m.flow_s) + ' per flow</span></h3>' +
        '<div class="table-wrap"><table class="remove-table"><thead><tr><th>Remove</th><th class="num">Time saved / flow</th><th class="num">Share of flow time</th><th class="num">Tokens saved / flow</th><th>Who loses its answer</th></tr></thead><tbody>' +
        m.agents.map(function (a) {
          if (!a.removable) return '<tr><td><span class="legend"><span style="--c:' + agColor(a.agent) + '">' + esc(agName(a.agent)) + '</span></span></td><td colspan="4" class="muted">gives the pipeline\'s answer (label agent): cannot be removed</td></tr>';
          return '<tr><td><span class="legend"><span style="--c:' + agColor(a.agent) + '">' + esc(agName(a.agent)) + '</span></span></td><td class="num">' + fmtTime(a.saves_s) + (a.prefill_estimated ? ' <span class="muted small">(incl. est. re-reading)</span>' : "") +
            '</td><td class="num">' + fmtPct(a.saves_share) + '</td><td class="num">' + fmtNum(a.saves_tokens) + '</td><td>' + (a.readers.length ? esc(a.readers.map(agName).join(", ")) : '<span class="muted">nobody</span>') + '</td></tr>';
        }).join("") + '</tbody></table></div>';
    }).join("") + '</div>';
}
var ADVICE = { raise: ["raise it", "warn"], lower: ["can be lower", ""], fits: ["fits", "ok"], no_budget: ["no budget", ""], unknown: ["not recorded", ""] };
function budgetCard(res) {
  if (!window.INSIGHTS || !res.agents) return "";
  var bt = INSIGHTS.budgetTuning(res);
  return '<div class="card" id="agents-budget"><h2>Token budgets</h2><p class="muted small" style="margin-top:0">Each agent may write up to its output-token budget. An agent that often hits it gets cut off (raise it); one that never comes near it can have a tighter budget, which bounds its worst-case time without changing its usual answers.</p>' +
    '<div class="table-wrap"><table id="agents-budget-table"><thead><tr><th>Model</th><th>Agent</th><th class="num">Budget</th><th class="num">Hits it</th><th class="num">Writes (median · p95 · max)</th><th>Advice</th></tr></thead><tbody>' +
    bt.map(function (m) {
      return m.agents.map(function (a) {
        var ad = ADVICE[a.advice];
        return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td><span class="legend"><span style="--c:' + agColor(a.agent) + '">' + esc(agName(a.agent)) + '</span></span></td><td class="num">' + (a.budget == null ? "—" : fmtNum(a.budget)) +
          '</td><td class="num">' + (a.hit_rate == null ? "—" : fmtPct(a.hit_rate)) + '</td><td class="num">' + (a.max == null ? '<span class="muted">not recorded</span>' : fmtNum(a.p50) + ' · ' + fmtNum(a.p95) + ' · ' + fmtNum(a.max)) +
          '</td><td><span class="chip ' + ad[1] + '">' + ad[0] + '</span>' + (a.suggest ? ' <span class="small">→ ' + fmtNum(a.suggest) + ' tokens</span>' : "") + '</td></tr>';
      }).join("");
    }).join("") + '</tbody></table></div></div>';
}
var PART_COLORS = [["system_tokens", "System prompt", "var(--part-1)"], ["feature_tokens", "Flow features", "var(--part-2)"], ["handoff_tokens", "Hand-off", "var(--part-3)"], ["other_tokens", "Other (template, format lines)", "var(--part-4)"]];
function compositionCard(res) {
  var pc = res.prompt_composition;
  if (!pc) return "";
  if (!pc.measured) return '<div class="card" id="agents-composition"><h2>What the prompts are made of</h2><p class="muted small">' + esc(pc.reason || "Not measured in this session") + '.</p></div>';
  var legend = '<div class="ilegend">' + PART_COLORS.map(function (p) { return '<span><i style="--c:' + p[2] + '"></i>' + esc(p[1]) + '</span>'; }).join("") + '</div>';
  var mx = Math.max.apply(null, pc.per_model.map(function (m) { return Math.max.apply(null, m.agents.map(function (a) { return a.input_tokens; })); })) || 1;
  var groups = pc.per_model.map(function (m) {
    return '<div class="cat-group"><h3><i class="sw" style="--c:' + modelColor(m.model) + '"></i>' + esc(short(m.model)) + ' <span class="muted small">' + fmtNum(m.per_flow.input_tokens) + ' input tokens per flow · cacheable ' + fmtPct(m.per_flow.cacheable_share) + '</span></h3>' +
      m.agents.map(function (a) {
        var bar = PART_COLORS.map(function (p) {
          var v = a[p[0]] || 0, t = agName(a.agent) + " · " + p[1] + ": " + fmtNum(v) + " tokens";
          return v ? '<span tabindex="0" style="flex:' + v.toFixed(2) + ' 1 0;--c:' + p[2] + '" data-hover="' + esc(t) + '" aria-label="' + esc(t) + '"></span>' : "";
        }).join("");
        return '<div class="cat-row"><span class="lbl">' + esc(agName(a.agent)) + '</span><div class="cat-track"><div class="cat-bar" style="width:' + (100 * a.input_tokens / mx).toFixed(1) + '%">' + bar + '</div></div><span class="val">' + fmtNum(a.input_tokens) + '</span></div>';
      }).join("") + '</div>';
  }).join("");
  return '<div class="card" id="agents-composition"><h2>What the prompts are made of</h2><p class="muted small" style="margin-top:0">Input tokens per call, split into the agent\'s system prompt, the flow features, the earlier answers it re-reads, and the rest. <b>Cacheable</b>: the system prompt opens every call of an agent unchanged, so an engine with prefix caching (e.g. vLLM) could skip that share.</p>' +
    legend + groups +
    '<details class="info"><summary>Show as table</summary><div class="table-wrap"><table id="agents-composition-table"><thead><tr><th>Model</th><th>Agent</th><th class="num">Input</th><th class="num">System</th><th class="num">Features</th><th class="num">Hand-off</th><th class="num">Other</th><th class="num">Cacheable</th></tr></thead><tbody>' +
    pc.per_model.map(function (m) { return m.agents.map(function (a) { return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td>' + esc(agName(a.agent)) + '</td><td class="num">' + fmtNum(a.input_tokens) + '</td><td class="num">' + fmtNum(a.system_tokens) + '</td><td class="num">' + fmtNum(a.feature_tokens) +
      '</td><td class="num">' + fmtNum(a.handoff_tokens) + '</td><td class="num">' + fmtNum(a.other_tokens) + '</td><td class="num">' + fmtPct(a.cacheable_share) + '</td></tr>'; }).join(""); }).join("") +
    '</tbody></table></div></details><p class="small muted">' + esc(pc.note) + '</p></div>';
}
function wirePipelineCompare(job) {
  var host = $("agents-pipelines-host"), id = job.job_id;
  if (!host || !window.INSIGHTS) return;
  allSessions().then(function (r) {
    if (SESSION.id !== id || !r.ok) return;
    var pc = INSIGHTS.pipelineCompare(r.data.sessions || [], id), name = function (l) { return l.map(agName).join(" → "); };
    host.innerHTML = '<div class="card" id="agents-pipelines"><h2>Other pipelines on these flows</h2>' + (!pc.current ? '<p class="muted small">This session has no prepared-set fingerprint.</p>'
      : !pc.others.length ? '<p class="muted small" style="margin-bottom:0">No other session ran a different pipeline on these flows and this GPU. Use <b>Build my own</b> in a new benchmark on the same prepared set (for example drop or add an agent) to see whether more agents are worth their cost.</p>'
      : '<p class="muted small" style="margin-top:0">This session: <b>' + esc(name(pc.current.pipeline)) + '</b>. The same models on the same flows and GPU with another pipeline (change against this session):</p>' +
        '<div class="table-wrap"><table id="agents-pipelines-table"><thead><tr><th>Pipeline</th><th>Model</th><th class="num">Latency / flow</th><th class="num">Tokens / flow</th><th class="num">Valid answers</th></tr></thead><tbody>' +
        pc.others.map(function (o) {
          return o.models.map(function (m, i) {
            var d = function (v) { return v == null ? "" : ' <span class="muted small">(' + (v > 0 ? "+" : v < 0 ? "−" : "") + fmtNum(Math.abs(v) * 100, 0) + '%)</span>'; };
            return '<tr>' + (i ? '<td></td>' : '<td><a href="#/session/' + enc(o.session_id) + '/agents">' + esc(name(o.pipeline)) + '</a></td>') + '<td class="mono">' + esc(short(m.model)) + '</td><td class="num">' + fmtTime(m.latency_s) + d(m.latency_vs_this) +
              '</td><td class="num">' + fmtNum(m.tokens) + d(m.tokens_vs_this) + '</td><td class="num">' + fmtPct(m.valid_rate) + (m.valid_vs_this == null ? "" : ' <span class="muted small">(' + (m.valid_vs_this >= 0 ? "+" : "−") + fmtNum(Math.abs(m.valid_vs_this) * 100, 1) + ' pts)</span>') + '</td></tr>';
          }).join("");
        }).join("") + '</tbody></table></div>') + '</div>';
    addExports(host);
  });
}
// ---------- Analysis expansion, phase 5: energy, provider prices, idle cost, best-model picker ----------
function energyEffCard(res) {
  if (!window.INSIGHTS || !res.cost_energy) return "";
  var e = INSIGHTS.energyEfficiency(res);
  return '<div class="card" id="cost-energy-eff"><h2>Energy efficiency</h2><p class="muted small" style="margin-top:0">Work done per unit of energy: higher is better. Measured from the GPU board power during each model\'s flows.</p>' +
    '<div class="table-wrap"><table id="cost-energy-eff-table"><thead><tr><th>Model</th><th class="num">Energy / flow</th><th class="num">Flows per kWh</th><th class="num">Tokens per Wh</th></tr></thead><tbody>' +
    e.models.map(function (m) {
      return m.wh_per_flow == null ? '<tr><td class="mono">' + esc(short(m.model)) + '</td><td colspan="3" class="muted">' + esc(m.reason) + '</td></tr>'
        : '<tr><td class="mono">' + esc(short(m.model)) + '</td><td class="num">' + fmtWh(m.wh_per_flow) + '</td><td class="num">' + fmtNum(m.flows_per_kwh) + '</td><td class="num">' + fmtNum(m.tokens_per_wh, 1) + '</td></tr>';
    }).join("") + '</tbody></table></div></div>';
}
var OFFERS_KEY = "agentmeter.priceOffers";
function loadOffers() { try { var x = JSON.parse(localStorage.getItem(OFFERS_KEY) || "[]"); return Array.isArray(x) ? x : []; } catch (e) { return []; } }
function priceCard(res) {
  if (!window.INSIGHTS) return "";
  return '<form class="card" id="cost-prices" novalidate><h2>Where is it cheapest to run?</h2><p class="muted small" style="margin-top:0">The measured time per flow priced at other hourly rates for the <b>same GPU model</b>' +
    ((res.environment || {}).gpu_name ? " (" + esc(gpuShort(res.environment.gpu_name)) + ")" : "") + '. Enter the current prices you are quoted (cloud, rental, your own cluster); they are kept in this browser only. A different GPU model would change the time, so compare like for like.</p>' +
    '<div id="price-rows"></div><div class="row"><button type="button" class="btn small-btn" id="price-add">Add a price</button></div><div id="price-out" style="margin-top:12px"></div></form>';
}
function wirePrices(res) {
  var host = $("price-rows");
  if (!host) return;
  var offers = loadOffers();
  function save() { try { localStorage.setItem(OFFERS_KEY, JSON.stringify(offers)); } catch (e) { /* this view only */ } }
  function draw() {
    host.innerHTML = offers.map(function (o, i) {
      return '<div class="row price-row" data-i="' + i + '" style="margin-top:6px"><div class="field"><label for="po-n-' + i + '">Provider</label><input id="po-n-' + i + '" type="text" data-k="name" maxlength="40" value="' + esc(o.name || "") + '"></div>' +
        '<div class="field"><label for="po-p-' + i + '">USD per GPU-hour</label><input id="po-p-' + i + '" data-k="usd_per_hour" type="number" min="0" step="any" value="' + esc(o.usd_per_hour == null ? "" : o.usd_per_hour) + '"></div>' +
        '<button type="button" class="btn small-btn" data-rm="' + i + '" aria-label="Remove ' + esc(o.name || "this price") + '">Remove</button></div>';
    }).join("");
    out();
  }
  function out() {
    var pc = INSIGHTS.priceCompare(res, offers);
    $("price-out").innerHTML = !pc.offers.length ? '<p class="muted small">' + (pc.real ? "Add a price to compare." : "Demo session: the time is not a model measurement, but you can still try the comparison.") + '</p>'
      : '<div class="table-wrap"><table id="price-table"><thead><tr><th>Price</th><th class="num">USD / GPU-hour</th>' + pc.offers[0].per_model.map(function (m) { return '<th class="num">' + esc(short(m.model)) + ' / 1,000 flows</th>'; }).join("") + '</tr></thead><tbody>' +
        pc.offers.map(function (o) { return '<tr><td>' + esc(o.name) + '</td><td class="num">' + fmtUsd(o.usd_per_hour) + '</td>' + o.per_model.map(function (m) { return '<td class="num">' + fmtUsd(m.usd_per_1k_flows) + '</td>'; }).join("") + '</tr>'; }).join("") +
        '</tbody></table></div>';
  }
  $("price-add").addEventListener("click", function () { offers.push({ name: "", usd_per_hour: null }); save(); draw(); var x = host.querySelector('.price-row:last-child [data-k="name"]'); if (x) x.focus(); });
  host.addEventListener("input", function (e) {
    var row = e.target.closest(".price-row"); if (!row) return;
    var o = offers[+row.getAttribute("data-i")], k = e.target.getAttribute("data-k");
    o[k] = k === "usd_per_hour" ? (e.target.value === "" ? null : +e.target.value) : e.target.value; save(); out();
  });
  host.addEventListener("click", function (e) { var b = e.target.closest("[data-rm]"); if (!b) return; offers.splice(+b.getAttribute("data-rm"), 1); save(); draw(); });
  $("cost-prices").addEventListener("submit", function (ev) { ev.preventDefault(); });
  draw();
}
function idleCard(res) {
  if (!window.INSIGHTS) return "";
  var ic = INSIGHTS.idleCost(res);
  return '<div class="card" id="cost-idle"><h2>GPU idle while running</h2>' + (!ic.measured ? '<p class="muted small" style="margin-bottom:0">Not measured: ' + esc(ic.reason) + '.</p>'
    : '<p class="muted small" style="margin-top:0">Share of each model\'s run with the GPU under ' + ic.threshold_pct + '% busy (between agent calls, tokenising, Python), and the share of its energy drawn then. A high share means batching or overlapping agent calls would get more out of the same GPU.</p>' +
      '<div class="table-wrap"><table id="cost-idle-table"><thead><tr><th>Model</th><th class="num">Mean GPU busy</th><th class="num">Time nearly idle</th><th class="num">Energy while nearly idle</th></tr></thead><tbody>' +
      ic.models.map(function (m) { return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td class="num">' + (m.mean_util_pct == null ? "—" : fmtNum(m.mean_util_pct, 0) + "%") + '</td><td class="num">' + fmtPct(m.low_util_share) + '</td><td class="num">' + fmtPct(m.low_util_energy_share) + '</td></tr>'; }).join("") +
      '</tbody></table></div>') + '</div>';
}
var PICK_LABELS = [["speed", "Fastest per flow"], ["tail", "Shortest slow tail (p95)"], ["cost", "Cheapest to run"], ["memory", "Smallest GPU memory"], ["tokens", "Fewest tokens"], ["validity", "Most valid answers"]];
function pickerCard(res) {
  if (!window.INSIGHTS || (res.per_model || []).length < 2) return "";
  return '<form class="card" id="rec-picker" novalidate><h2>Best model for my priority</h2><p class="muted small" style="margin-top:0">Choose what matters most; optionally require a share of valid answers. The pick restates this session\'s measurements.</p>' +
    '<div class="row" style="margin-top:0"><div class="field"><label for="pick-priority">My priority</label><select id="pick-priority">' + PICK_LABELS.map(function (p) { return '<option value="' + p[0] + '">' + esc(p[1]) + '</option>'; }).join("") + '</select></div>' +
    '<div class="field"><label for="pick-valid">At least this share of valid answers <span class="hint">(%, optional)</span></label><input id="pick-valid" type="number" min="0" max="100" step="1" placeholder="e.g. 95"></div></div>' +
    '<p class="verdict" id="pick-out" aria-live="polite"></p></form>';
}
function wirePicker(res) {
  var sel = $("pick-priority");
  if (!sel) return;
  function draw() {
    var v = $("pick-valid").value, r = INSIGHTS.recommend(res, sel.value, v === "" ? null : +v / 100);
    $("pick-out").innerHTML = !r.best ? "No model meets that" + (r.min_valid != null ? " share of valid answers (" + fmtPct(r.min_valid) + ")" : "") + "."
      : r.tie ? 'Tie: <b>' + esc(short(r.best.model)) + '</b> and <b>' + esc(short(r.runner_up.model)) + '</b> are within 5% here (' + esc(fmtUnit(r.unit, r.best.value)) + ' and ' + esc(fmtUnit(r.unit, r.runner_up.value)) +
          '). Pick by your next priority.'
      : '<b>' + esc(short(r.best.model)) + '</b> ' + esc(r.label) + ': ' + esc(fmtUnit(r.unit, r.best.value)) +
        (r.runner_up ? ' — ' + (r.unit === "share" ? fmtNum(r.margin * 100, 1) + " points" : fmtNum(r.margin * 100, 0) + "%") + ' ahead of ' + esc(short(r.runner_up.model)) + ' (' + esc(fmtUnit(r.unit, r.runner_up.value)) + ')' : "") + '.' +
        (r.excluded.length ? ' <span class="muted">Left out for too few valid answers: ' + esc(r.excluded.map(short).join(", ")) + '.</span>' : "") +
        (r.no_data.length ? ' <span class="muted">No data for: ' + esc(r.no_data.map(short).join(", ")) + '.</span>' : "");
  }
  sel.addEventListener("change", draw); $("pick-valid").addEventListener("input", draw);
  $("rec-picker").addEventListener("submit", function (ev) { ev.preventDefault(); });
  draw();
}
// Output Reliability: is the pipeline's output usable? A validity check of the output, not a
// detection score — nothing here enters a score, rank or verdict.
function reliabilityCard(res) {
  var r = res.output_reliability;
  if (!r) return '<div class="card" id="rel-card"><h2>Valid answers</h2><p class="muted small">This session predates the output-reliability check; the per-agent failures below still apply.</p></div>';
  var pm = r.per_model.slice().sort(function (a, b) { return (MODEL_SLOT[INSIGHTS.bare(a.model)] || 0) - (MODEL_SLOT[INSIGHTS.bare(b.model)] || 0); });
  var cnt = function (k, m) { return m[k] ? '<b>' + fmtNum(m[k]) + '</b>' : '<span class="muted">0</span>'; };
  var bars = pm.map(function (m) {
    var t = short(m.model) + ": " + fmtPct(m.valid_rate) + " valid (" + m.valid + " of " + m.n_flows + " flows)";
    return '<div class="hbar"><span class="lbl">' + esc(short(m.model)) + '</span><span class="track" tabindex="0" data-hover="' + esc(t) + '" aria-label="' + esc(t) + '"><span class="fill" style="display:block;width:' +
      (100 * (m.valid_rate || 0)).toFixed(1) + '%;background:' + modelColor(m.model) + '"></span></span><span class="val">' + fmtPct(m.valid_rate) + ' <span class="muted small">(' + m.valid + '/' + m.n_flows + ')</span></span></div>';
  }).join("");
  var cons = function (m) {
    var c = m.verdict_consistency;
    return !r.verdict_agent ? '<span class="muted">no verdict agent</span>' : !c ? '<span class="muted">not recorded</span>' : fmtPct(c.rate) + ' <span class="muted small">(' + c.agree + '/' + c.checked + ')</span>';
  };
  return '<div class="card" id="rel-card"><h2>Valid answers</h2><p class="muted small" style="margin-top:0">Each flow should end with one usable answer: a label from the allowed list from the <b>' + esc(agName(r.label_agent)) +
    '</b> agent, and no agent returning nothing. This checks that the output can be used downstream; it is not a detection score and enters no score or ranking.</p>' +
    '<div class="hbars" id="rel-bars">' + bars + '</div>' +
    '<div class="table-wrap" style="margin-top:12px"><table id="rel-table"><thead><tr><th>Model</th><th class="num">Valid answers</th><th class="num">Not a label</th><th class="num">Empty answer</th><th class="num">Hit token budget</th><th>' +
      (r.verdict_agent ? esc(agName(r.verdict_agent)) + ' agrees with ' + esc(agName(r.label_agent)) : "Verdict consistency") + '</th>' + (r.labelled ? '<th class="num">Reference-label check</th>' : "") + '</tr></thead><tbody>' +
    pm.map(function (m) {
      return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td class="num">' + fmtPct(m.valid_rate) + '</td><td class="num">' + cnt("unparseable", m) + '</td><td class="num">' + cnt("empty", m) +
        '</td><td class="num">' + cnt("truncated", m) + '</td><td>' + cons(m) + '</td>' + (r.labelled ? '<td class="num">' + (m.reference_agreement ? fmtPct(m.reference_agreement.rate) : "—") + '</td>' : "") + '</tr>';
    }).join("") + '</tbody></table></div>' +
    '<details class="info"><summary>What each column means</summary><ul class="small list">' +
      '<li><b>Valid answers</b> — ' + esc(r.definitions.valid) + '.</li>' +
      '<li><b>Hit token budget</b> — ' + esc(r.definitions.truncated) + '. The answer can still be valid.</li>' +
      '<li><b>Agreement</b> — ' + esc(r.definitions.verdict_consistency) + '.</li>' +
      (r.labelled ? '<li><b>Reference-label check</b> — ' + esc(r.definitions.reference_agreement) + '. A fast pipeline that answers at random would not be a fair efficiency result; this number guards against that.</li>' : "") +
    '</ul></details></div>';
}
// The IDS -> LLM pipeline: the IDS detects, the LLM agents triage only its alerts (agentmeter/ids).
function idsCard(res) {
  var d = res.ids;
  if (!d) return "";
  var m = d.model || {}, i = d.ids || {}, lab = d.labelled;
  var frac = function (n, rate) { return n == null ? '<span class="muted">—</span>' : fmtNum(n) + ' <span class="muted small">(' + fmtPct(rate) + ')</span>'; };
  var per1k = function (p) { return (p.time_s == null ? "—" : fmtTime(p.time_s)) + (p.cost_usd != null ? ' · ' + fmtUsd(p.cost_usd) : "") + (p.energy_wh != null ? ' · ' + fmtWh(p.energy_wh) : ""); };
  var head = '<p class="muted small" style="margin-top:0">' + esc(d.notes.roles) + '</p>' +
    '<p class="small">' + (m.demo ? '<span class="chip warn">Demo IDS</span> ' : "") + 'IDS <span class="mono">' + esc(m.version || m.name || "—") + '</span> · threshold <b>' + fmtNum(d.threshold, 3) + '</b> <span class="muted">(' + esc(d.threshold_source || "") + ')</span> · ' +
      '<b>' + fmtNum(d.n_alerts) + '</b> of ' + fmtNum(d.n_flows_in) + ' flows flagged → <b>' + fmtPct(d.llm_calls_saved_share) + '</b> of LLM calls saved · scoring ' + fmtTime(d.scoring_s_per_flow) + ' per flow</p>' +
    (d.training_overlap ? '<p class="notice warn small">' + fmtNum(d.training_overlap) + ' selected flow(s) equal flows the IDS was trained on: its detection numbers are optimistic. ' + esc(d.notes.overlap) + '</p>' : "");
  var det = lab ? '<div class="table-wrap"><table id="ids-detect-table"><thead><tr><th>IDS</th><th class="num">Recall</th><th class="num">Precision</th><th class="num">False-positive rate</th><th class="num">Attacks missed</th><th class="num">False alarms</th></tr></thead><tbody>' +
      '<tr><td>Detection</td><td class="num">' + fmtPct(i.recall) + '</td><td class="num">' + fmtPct(i.precision) + '</td><td class="num">' + fmtPct(i.false_positive_rate) + '</td><td class="num">' + fmtNum(i.fn) + ' of ' + fmtNum(i.attacks) + '</td><td class="num">' + fmtNum(i.fp) + ' of ' + fmtNum(i.benign) + '</td></tr></tbody></table></div>' +
      '<p class="small muted">Recall by attack class: ' + Object.keys(i.recall_by_class || {}).map(function (c) { return esc(c) + ' ' + fmtPct(i.recall_by_class[c].recall); }).join(" · ") + '</p>'
    : '<p class="small muted">No labels: the IDS\'s recall and false alarms cannot be measured; only how many flows it flagged.</p>';
  var rows = d.per_model.map(function (r) {
    return '<tr><td class="mono">' + esc(short(r.model)) + '</td><td class="num">' + fmtNum(r.alerts) + '</td>' +
      (lab ? '<td class="num">' + frac(r.false_alarms_dismissed, r.false_alarms_dismissed_rate) + ' <span class="muted small">of ' + fmtNum(r.false_alarms) + '</span></td><td class="num">' + frac(r.attacks_dismissed_by_llm, r.attacks_dismissed_by_llm_rate) +
        '</td><td class="num">' + fmtNum(r.missed_total) + ' <span class="muted small">(' + fmtPct(r.missed_total_rate) + ')</span></td><td class="num">' + fmtNum(r.analyst_queue) + '</td>' : "") +
      '<td class="num">' + per1k(r.per_1k_incoming) + '</td></tr>';
  }).join("");
  var tab = '<div class="table-wrap"><table id="ids-models-table"><thead><tr><th>Model</th><th class="num">Alerts triaged</th>' +
    (lab ? '<th class="num">False alarms dismissed</th><th class="num">Attacks it dismissed</th><th class="num">Missed end to end</th><th class="num">Analyst queue</th>' : "") +
    '<th class="num">Per 1,000 incoming flows</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  var curve = d.ids_curve.length > 1 ? '<details class="info"><summary>Higher thresholds (τ curve)</summary><div class="table-wrap"><table id="ids-curve-table"><thead><tr><th class="num">Threshold</th>' +
      (lab ? '<th class="num">IDS recall</th>' : "") + '<th class="num">Flagged</th>' + d.per_model.map(function (r) { return '<th class="num">' + esc(short(r.model)) + (lab ? ': missed · ' : ": ") + 'time / 1,000</th>'; }).join("") + '</tr></thead><tbody>' +
      d.ids_curve.map(function (c, k) {
        return '<tr><td class="num">' + fmtNum(c.threshold, 2) + '</td>' + (lab ? '<td class="num">' + fmtPct(c.recall) + '</td>' : "") + '<td class="num">' + fmtPct(c.flagged_share) + '</td>' +
          d.per_model.map(function (r) { var t = r.tau_curve[k] || {}; return '<td class="num">' + (lab ? fmtPct(t.missed_total_rate) + ' · ' : "") + (t.per_1k_incoming && t.per_1k_incoming.time_s != null ? fmtTime(t.per_1k_incoming.time_s) : "—") + '</td>'; }).join("") + '</tr>';
      }).join("") + '</tbody></table></div><p class="small muted">' + esc(d.notes.tau_curve) + '</p></details>' : "";
  return '<div class="card" id="rel-ids"><h2>IDS → LLM triage</h2>' + head + det + tab + curve +
    '<details class="info"><summary>Definitions</summary><ul class="small list"><li>' + esc(d.notes.per_1k) + '</li><li>' + esc(d.notes.missed) + '</li><li>' + esc(d.notes.false_alarms) + '</li></ul></details></div>';
}
// The gate result, also for sessions stored before "not applicable" existed: a check with nothing
// to judge (no benign flow reached the model, e.g. the IDS let no false alarm through) does not
// hold the gate back (agentmeter/pipeline/triage.py effective_gate).
function effGate(m) {
  var g = m.gate;
  if (!g || g.passed !== null || (g.failed || []).length) return g;
  var na = g.not_applicable || [];
  if (!g.not_applicable) { if (m.n_attacks === 0) na.push("missed_attack_rate"); if (m.n_benign === 0) na.push("false_escalation_rate"); }
  var left = (g.not_checked || []).filter(function (c) { return na.indexOf(c) < 0; });
  return { passed: left.length ? null : true, failed: [], not_checked: left, not_applicable: na, checks: g.checks };
}
// Triage: the analyst queue each model's answers create, and the triage errors that matter in a
// SOC. The priority comes from the session's policy table, never from the model; context only.
function triageCard(res) {
  var t = res.triage;
  if (!t || !t.per_model || !t.per_model.length) return "";
  var P = t.priorities, pm = t.per_model;
  var q = function (m, p) { return fmtNum(m.queue[p]) + ' <span class="muted small">(' + fmtPct(m.queue_share[p]) + ')</span>'; };
  var frac = function (n, rate) { return n == null ? '<span class="muted">—</span>' : fmtNum(n) + ' <span class="muted small">(' + fmtPct(rate) + ')</span>'; };
  var G = { missed_attack_rate: "missed attacks", false_escalation_rate: "false escalations", valid_rate: "valid answers" };
  var gateCell = function (m) {
    var g = effGate(m);
    if (!g) return '<span class="muted">—</span>';
    if (g.passed === true) return '<span class="chip ok">✓ passes</span>' + ((g.not_applicable || []).length ? ' <span class="muted small" title="no such flow reached the model">(' + esc(g.not_applicable.map(function (k) { return G[k] || k; }).join(", ")) + ': n/a)</span>' : "");
    if (g.passed === false) return '<span class="chip bad">✗ ' + esc(g.failed.map(function (k) { return G[k] || k; }).join(", ")) + '</span>';
    return '<span class="chip">not checked</span>';
  };
  var head = '<th>Model</th><th>Triage gate</th>' + P.map(function (p) { return '<th class="num">' + esc(p) + '</th>'; }).join("") +
    (t.labelled ? '<th class="num">Missed attacks</th><th class="num">False escalations</th><th class="num">Priority right</th><th class="num">Cost per attack caught</th><th class="num">Energy per attack caught</th>' : "");
  var rows = pm.map(function (m) {
    return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td>' + gateCell(m) + '</td>' + P.map(function (p) { return '<td class="num">' + q(m, p) + '</td>'; }).join("") +
      (t.labelled ? '<td class="num">' + frac(m.missed_attacks, m.missed_attack_rate) + '</td><td class="num">' + frac(m.false_escalations, m.false_escalation_rate) +
        '</td><td class="num">' + fmtPct(m.priority_accuracy) + '</td><td class="num">' + fmtUsd(m.cost_per_attack_caught_usd) + '</td><td class="num">' + fmtWh(m.wh_per_attack_caught) + '</td>' : "") + '</tr>';
  }).join("");
  var sv = t.severity || {};
  var policy = Object.keys(t.policy).map(function (c) {
    var s = sv[c];
    return '<li>' + esc(c) + ' → <b>' + esc(t.policy[c]) + '</b>' + (s ? ' · severity <b>' + esc(s.level) + '</b> <span class="muted">(' + esc(s.classtype) + ', priority ' + s.priority + ')</span>' : "") + '</li>';
  }).join("");
  var gl = t.gate ? '<p class="small">Triage gate — recommended only if missed attacks ≤ ' + fmtPct(t.gate.max_missed_attack_rate) + ', false escalations ≤ ' + fmtPct(t.gate.max_false_escalation_rate) +
    ' and valid answers ≥ ' + fmtPct(t.gate.min_valid_rate) + '. <span class="muted">Basis: ' + esc(t.gate.basis) + '.</span></p>' : "";
  return '<div class="card" id="rel-triage"><h2>Triage</h2><p class="muted small" style="margin-top:0">What an analyst would have to do with each model\'s answers: escalate now, investigate, or dismiss. ' +
      'The priority comes from the policy table below, not from the model. Context for the efficiency results: it does not change the score or the ranking.</p>' +
    '<div class="table-wrap"><table id="triage-table"><thead><tr>' + head + '</tr></thead><tbody>' + rows + '</tbody></table></div>' + gl +
    (t.labelled ? "" : '<p class="small muted">This input has no labels, so only the queue is shown: missed attacks and false escalations need the true class.</p>') +
    '<details class="info"><summary>Policy and definitions</summary><ul class="small list">' + policy +
      '<li>Any other class, or an answer that could not be read → <b>' + esc(t.fallback) + '</b></li></ul><ul class="small list">' +
      '<li>' + esc(t.notes.missed) + '</li><li>' + esc(t.notes.false_escalation) + '</li><li>' + esc(t.notes.cost) + '</li><li>' + esc(t.notes.flows) + '</li>' +
      (t.notes.severity ? '<li>' + esc(t.notes.severity) + '</li>' : "") + (t.notes.gate ? '<li>' + esc(t.notes.gate) + '</li>' : "") + '</ul></details></div>';
}
// Output Reliability: is each model's accuracy, and the gap between two, more than chance?
function accuracyStatsCard(res) {
  var st = res.accuracy_stats;
  if (!st || !st.labelled || !(st.per_model || []).length) return "";
  var max = 1, bar = function (m) {
    var lo = m.ci95[0], hi = m.ci95[1], a = m.accuracy;
    if (lo == null) return "";
    return '<span class="ci-track" aria-hidden="true"><span class="ci-range" style="left:' + (100 * lo / max).toFixed(1) + '%;width:' + (100 * (hi - lo) / max).toFixed(1) +
      '%"></span><span class="ci-dot" style="left:' + (100 * a / max).toFixed(1) + '%"></span></span>';
  };
  var pairs = (st.pairs || []).map(function (p) {
    return '<tr><td class="mono">' + esc(short(p.a)) + ' vs ' + esc(short(p.b)) + '</td><td class="num">' + (p.difference == null ? "—" : (p.difference > 0 ? "+" : "") + fmtNum(100 * p.difference, 1) + " pts") +
      '</td><td class="num">' + fmtNum(p.only_a_right) + ' / ' + fmtNum(p.only_b_right) + '</td><td class="num">' + fmtNum(p.p_holm, 3) + '</td><td>' +
      (p.differ ? '<span class="chip ok">' + esc(p.verdict) + '</span>' : '<span class="chip">' + esc(p.verdict) + '</span>') + '</td></tr>';
  }).join("");
  return '<div class="card" id="rel-accuracy-stats"><h2>Are the accuracy differences real?</h2><p class="muted small" style="margin-top:0">Accuracy on these flows with its 95% range: ' +
      'with more flows the range narrows. Two models only differ beyond chance when the test says so. Context only: it does not change the score or the ranking.</p>' +
    '<div class="table-wrap"><table id="acc-ci-table"><thead><tr><th>Model</th><th class="num">Accuracy</th><th class="num">95% range</th><th style="min-width:180px">0% ─────── 100%</th></tr></thead><tbody>' +
    st.per_model.map(function (m) {
      return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td class="num">' + fmtPct(m.accuracy) + ' <span class="muted small">(' + m.correct + '/' + m.n + ')</span></td><td class="num">' +
        fmtPct(m.ci95[0]) + ' – ' + fmtPct(m.ci95[1]) + '</td><td>' + bar(m) + '</td></tr>';
    }).join("") + '</tbody></table></div>' +
    (pairs ? '<h3>Model against model</h3><div class="table-wrap"><table id="acc-pairs-table"><thead><tr><th>Pair</th><th class="num">Difference</th><th class="num">Only A right / only B right</th><th class="num">p (Holm)</th><th>Verdict</th></tr></thead><tbody>' + pairs + '</tbody></table></div>' : "") +
    (st.note ? '<p class="notice small">' + esc(st.note) + '</p>' : "") +
    '<details class="info"><summary>How this is tested</summary><p class="small">' + esc(st.method) + ' Significance level ' + st.alpha + '.</p></details></div>';
}
// Output Reliability: which classes each model mixes up (the stored confusion matrix)
function confusionCard(res) {
  var pms = (res.per_model || []).filter(function (pm) { return pm.accuracy && pm.accuracy.confusion; });
  if (!pms.length) return "";
  var grid = function (pm) {
    var cm = pm.accuracy.confusion, truths = Object.keys(cm), preds = [];
    truths.forEach(function (t) { Object.keys(cm[t]).forEach(function (p) { if (preds.indexOf(p) < 0) preds.push(p); }); });
    preds.sort(function (a, b) { return (truths.indexOf(a) < 0) - (truths.indexOf(b) < 0) || truths.indexOf(a) - truths.indexOf(b); });
    var worst = [];
    truths.forEach(function (t) { preds.forEach(function (p) { var v = cm[t][p] || 0; if (p !== t && v) worst.push([v, t, p]); }); });
    worst.sort(function (a, b) { return b[0] - a[0]; });
    var html = '<div class="table-wrap"><table class="cm-table"><thead><tr><th class="small">True ↓ / answered →</th>' + preds.map(function (p) { return '<th class="num small">' + esc(p) + '</th>'; }).join("") + '</tr></thead><tbody>' +
      truths.map(function (t) {
        var tot = preds.reduce(function (a, p) { return a + (cm[t][p] || 0); }, 0) || 1;
        return '<tr><th class="small">' + esc(t) + '</th>' + preds.map(function (p) {
          var v = cm[t][p] || 0, share = v / tot;
          return '<td class="num cm-cell' + (p === t ? " cm-diag" : v ? " cm-off" : "") + '" style="--share:' + (60 * share).toFixed(0) + '%" title="' + esc(t) + ' answered as ' + esc(p) + ': ' + v + ' (' + fmtPct(share) + ' of ' + esc(t) + ')">' + (v || '<span class="muted">·</span>') + '</td>';
        }).join("") + '</tr>';
      }).join("") + '</tbody></table></div>';
    return '<div class="cm-model"><h3 class="mono">' + esc(short(pm.model)) + '</h3>' + html +
      (worst.length ? '<p class="small muted">Most often confused: ' + worst.slice(0, 3).map(function (w) { return '<b>' + esc(w[1]) + '</b> answered as <b>' + esc(w[2]) + '</b> (' + w[0] + ')'; }).join("; ") + '.</p>'
                    : '<p class="small muted">No confusions on these flows.</p>') + '</div>';
  };
  return '<div class="card" id="rel-confusion"><h2>Which classes each model mixes up</h2><p class="muted small" style="margin-top:0">Rows are the true class, columns the answer. ' +
    'The diagonal is right; every other filled cell is a mistake, darker when it happens more often.</p><div class="cm-grid">' + pms.map(grid).join("") + '</div></div>';
}
// Output Reliability: the triage results re-projected to a realistic SOC traffic mix
var SOC = { share: 0.99 };
function socCard(res) {
  var t = res.triage;
  if (!t || !t.labelled || !(t.per_model || []).some(function (m) { return m.confusion && m.n_attacks && m.n_benign; })) return "";
  SOC.res = res;
  return '<div class="card" id="rel-soc"><h2>What this would look like in a real SOC</h2><p class="muted small" style="margin-top:0">These flows are a test mix. ' +
      'Real traffic is mostly benign, so a small false-escalation rate becomes most of an analyst\'s queue. The same per-class behaviour, re-projected to the mix you choose (per 10,000 flows).</p>' +
    '<div class="row" style="margin-top:0;gap:14px;align-items:center;flex-wrap:wrap"><label for="soc-share"><b>Benign share of traffic</b></label>' +
      '<input type="range" id="soc-share" min="0.5" max="0.999" step="0.001" value="' + SOC.share + '" style="flex:1 1 220px"><b id="soc-share-v" class="num">' + fmtPct(SOC.share) + '</b></div>' +
    '<div id="soc-body">' + socTable(res) + '</div>' +
    '<p class="small muted">Measured on this session: ' + esc(t.per_model.map(function (m) { return short(m.model) + " " + m.n_attacks + " attacks / " + m.n_benign + " benign"; }).join(", ")) +
    '. A projection: it assumes each model treats attacks and benign flows the same way in the new mix.</p></div>';
}
function socTable(res) {
  var rows = (res.triage.per_model || []).map(function (m) { return INSIGHTS.socProjection(m, SOC.share, 10000); }).filter(Boolean);
  return '<div class="table-wrap"><table id="soc-table"><thead><tr><th>Model</th><th class="num">Escalated</th><th class="num">… of which real attacks</th><th class="num">To investigate</th>' +
    '<th class="num">Analyst queue</th><th class="num">Missed attacks</th></tr></thead><tbody>' + rows.map(function (p) {
      return '<tr><td class="mono">' + esc(short(p.model)) + '</td><td class="num">' + fmtNum(p.escalations) + '</td><td class="num">' + fmtPct(p.escalation_precision) +
        '</td><td class="num">' + fmtNum(p.investigations) + '</td><td class="num"><b>' + fmtNum(p.analyst_queue) + '</b></td><td class="num">' + fmtNum(p.missed_attacks, 1) + ' <span class="muted small">of ' + fmtNum(p.attacks) + '</span></td></tr>';
    }).join("") + '</tbody></table></div>';
}
document.addEventListener("input", function (ev) {
  if (!ev.target || ev.target.id !== "soc-share" || !SOC.res) return;
  SOC.share = +ev.target.value; $("soc-share-v").textContent = fmtPct(SOC.share); $("soc-body").innerHTML = socTable(SOC.res);
});
// Output Reliability: does the verdict agent's explanation name the class the label agent chose?
function agreementCard(res) {
  var r = res.output_reliability;
  if (!r || !r.verdict_agent || !r.per_model.some(function (m) { return m.verdict_consistency; })) return "";
  var ex = [];
  r.per_model.forEach(function (m) { ((m.verdict_consistency || {}).examples || []).forEach(function (e) { ex.push([m.model, e]); }); });
  return '<div class="card" id="rel-agreement"><h2>Do the explanations match the verdict?</h2><p class="muted small" style="margin-top:0">The ' + esc(r.verdict_agent) +
      ' agent explains the answer the ' + esc(r.label_agent) + ' agent chose. When it names another class, the explanation contradicts the answer an analyst would act on.</p>' +
    '<div class="hbars" id="agree-bars">' + r.per_model.map(function (m) {
      var c = m.verdict_consistency;
      return '<div class="hbar"><span class="hb-l mono">' + esc(short(m.model)) + '</span><span class="hb-t"><span class="hb-f" style="width:' + (c ? (100 * c.rate).toFixed(1) : 0) + '%"></span></span>' +
        '<span class="hb-v num">' + (c ? fmtPct(c.rate) + ' <span class="muted small">(' + c.agree + '/' + c.checked + ')</span>' : "—") + '</span></div>';
    }).join("") + '</div>' +
    (ex.length ? '<details class="info"><summary>Where they disagree (' + ex.length + ' example' + (ex.length === 1 ? "" : "s") + ')</summary><div class="table-wrap"><table id="agree-examples"><thead><tr><th>Model</th><th>Flow</th><th>Answer</th><th>Explanation says</th><th>Explanation</th></tr></thead><tbody>' +
      ex.map(function (x) {
        var e = x[1];
        return '<tr><td class="mono small">' + esc(short(x[0])) + '</td><td class="mono small">' + esc(e.scenario_id) + '</td><td>' + esc(e.label) + '</td><td><b>' + esc(e.verdict_class) + '</b></td><td class="small">' + esc(e.verdict_text) + '</td></tr>';
      }).join("") + '</tbody></table></div></details>' : '<p class="small muted">Every checked explanation names the chosen class.</p>') + '</div>';
}
// Output Reliability: are the answers consistent (format, repeat, MITRE)?
function consistencyCard(res) {
  var r = res.output_reliability;
  if (!r || !r.per_model.some(function (m) { return m.format || m.mitre || m.answer_stability; })) return "";
  var pm = r.per_model, rep = pm.some(function (m) { return m.answer_stability; });
  var cell = function (x, rate, a, b) { return !x ? '<span class="muted">—</span>' : fmtPct(x[rate]) + ' <span class="muted small">(' + x[a] + '/' + x[b] + ')</span>'; };
  return '<div class="card" id="rel-consistency"><h2>Consistency of answers</h2><p class="muted small" style="margin-top:0">Does the pipeline answer in the expected form, give the same answer twice, and name the same ATT&amp;CK technique for the same label? Usable, repeatable output is what makes the timings meaningful.</p>' +
    '<div class="table-wrap"><table id="rel-consistency-table"><thead><tr><th>Model</th><th class="num">Reply is just a label</th><th class="num">Same answer when repeated</th><th class="num">Same technique per label</th><th class="num">Technique as configured</th></tr></thead><tbody>' +
    pm.map(function (m) {
      return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td class="num">' + cell(m.format, "exact_rate", "exact", "checked") + '</td><td class="num">' + cell(m.answer_stability, "rate", "same", "checked") +
        '</td><td class="num">' + cell(m.mitre, "consistent_rate", "consistent", "checked") + '</td><td class="num">' + cell(m.mitre, "match_rate", "matches_reference_map", "checked") + '</td></tr>';
    }).join("") + '</tbody></table></div>' +
    (rep ? "" : '<p class="small muted">“Same answer when repeated” needs the <b>repeat check</b> option when you start a benchmark (it answers the first 10 flows a second time).</p>') +
    '<details class="info"><summary>What each column means</summary><ul class="small list">' +
      '<li><b>Reply is just a label</b> — ' + esc(r.definitions.format) + '. Extra text costs tokens and needs cleaning before an analyst tool can use it.</li>' +
      '<li><b>Same answer when repeated</b> — ' + esc(r.definitions.answer_stability) + '.</li>' +
      '<li><b>Technique</b> — ' + esc(r.definitions.mitre) + '. “—” when there is no verdict agent or its text was not recorded.</li></ul></details></div>';
}
// per agent: where the pipeline's answers go wrong (from the per-agent breakdown)
function failureCard(res) {
  var ag = res.agents;
  if (!ag) return "";
  var rows = [];
  ag.per_model.forEach(function (m) {
    m.agents.forEach(function (a) {
      var f = a.failures || {};
      rows.push('<tr><td class="mono">' + esc(short(m.model)) + '</td><td><span class="legend"><span style="--c:' + agColor(a.agent) + '">' + esc(agName(a.agent)) + '</span></span></td><td class="num">' + fmtNum(f.empty_output) +
        '</td><td class="num">' + (f.hit_token_cap == null ? '<span class="muted">no budget</span>' : fmtNum(f.hit_token_cap) + ' <span class="muted small">(budget ' + f.token_cap + ')</span>') +
        '</td><td class="num">' + (f.unparseable_label == null ? '<span class="muted">—</span>' : fmtNum(f.unparseable_label)) + '</td><td class="num">' + fmtPct(a.failure_rate) + '</td></tr>');
    });
  });
  return '<div class="card" id="rel-failures"><h2>Where answers fail</h2><p class="muted small" style="margin-top:0">Per agent: empty answers, answers stopped at the agent\'s output-token budget, and label replies that match no allowed label. A high rate points at a prompt or budget to fix before trusting the timings.</p>' +
    '<div class="table-wrap"><table id="rel-failures-table"><thead><tr><th>Model</th><th>Agent</th><th class="num">Empty</th><th class="num">At token budget</th><th class="num">Not a label</th><th class="num">Failure rate</th></tr></thead><tbody>' +
    rows.join("") + '</tbody></table></div></div>';
}
function wireOverview(res) {
  var box = $("svc-caveats"), det = $("svc-cav-detail"), texts = $("svc-cav-texts");
  if (!box) return;
  box.querySelectorAll("[data-cav]").forEach(function (b) { b.addEventListener("click", function () {
    var open = b.getAttribute("aria-expanded") === "true";
    box.querySelectorAll("[data-cav]").forEach(function (x) { x.setAttribute("aria-expanded", "false"); });
    if (open) { det.hidden = true; return; }
    b.setAttribute("aria-expanded", "true");
    det.textContent = texts.content.children[+b.getAttribute("data-cav")].textContent;
    det.hidden = false;
  }); });
}
function h2hCard(res) {
  var rel = res.relative_comparison, pm = res.per_model, names = pm.map(function (m) { return m.model; });
  if (!rel) return '<div class="card"><h2>Head-to-head</h2><p class="muted">Single-model run: there is no second model to compare with.</p></div>';
  var multi = names.length > 2;
  var rr = rel.metrics.filter(function (m) { return m.metric !== "accuracy"; }).map(function (m) {
    var f = m.metric === "mean_latency_s" ? fmtTime : m.metric === "mean_peak_vram_mb" ? fmtMB : function (v) { return fmtNum(v); };
    var res2 = !m.available ? '<span class="muted">not available</span>' : m.winner === "tie" ? (multi ? "no clear best" : "tie") + ' <span class="muted">(' + esc(m.note || "") + ')</span>'
      : esc(short(m.winner)) + ' — ' + m.pct_better + "% better" + (m.runner_up ? " than " + esc(short(m.runner_up)) : "");
    return '<tr><td>' + esc(m.label) + (m.metric === "mean_peak_vram_mb" ? ' <span class="muted small">(excludes model weights)</span>' : "") + '</td>' +
      names.map(function (n) { return '<td class="num">' + f(m.values[n]) + '</td>'; }).join("") + '<td>' + res2 + '</td></tr>';
  }).join("");
  var pairs = !multi ? "" : '<h3 style="margin-top:16px">Every pair</h3><p class="muted small" style="margin-top:0">Each pair compared on its own (pairwise significance: Dunn post-hoc). The ranking counts pair wins minus losses.</p>' +
    '<div class="table-wrap"><table id="svc-pairs-table"><thead><tr><th>Pair</th><th>Result</th></tr></thead><tbody>' +
    rel.pairs.map(function (p) {
      var ev = p.efficiency_verdict;
      return '<tr><td>' + esc(short(p.models[0]) + " vs " + short(p.models[1])) + '</td><td>' + esc(ev.verdict === "more_efficient" ? short(ev.model) + " is more efficient" : ev.verdict === "mixed" ? "mixed: each wins a metric" : "tie") + '</td></tr>';
    }).join("") + '</tbody></table></div>' +
    '<ol class="small" id="svc-pair-ranking">' + rel.ranking.map(function (r) { return '<li><b>' + esc(short(r.model)) + '</b> — ' + r.pair_wins + ' pair win' + (r.pair_wins === 1 ? "" : "s") + ', ' + r.pair_losses + ' loss' + (r.pair_losses === 1 ? "" : "es") + '</li>'; }).join("") + '</ol>';
  return '<div class="card"><h2>' + (multi ? "All " + names.length + " models" : "Head-to-head") + '</h2><p class="muted small">Fair comparison: same flows, same GPU.' +
    '<span class="info-i" tabindex="0" title="Ties: within ' + (rel.thresholds.efficiency_tie_rel * 100) + '% (latency and VRAM also need p < ' + rel.thresholds.significance_alpha +
    ').">i</span></p>' +
    '<div class="table-wrap"><table id="svc-relative-table"><thead><tr><th>Metric</th>' + names.map(function (n) { return '<th class="num">' + esc(short(n)) + '</th>'; }).join("") + '<th>' + (multi ? "Best" : "Result") + '</th></tr></thead><tbody>' + rr + '</tbody></table></div>' + pairs + '</div>';
}
// was the GPU in a normal state during the run? (analyses.gpu_health_block)
function gpuHealthCard(res) {
  var g = res.gpu_health;
  if (!g || !g.available) return "";
  var per = g.per_model || [];
  if (g.status !== "check") {
    var cs = per.map(function (m) { return m.clock_share; }).filter(function (x) { return x != null; });
    var tm = per.map(function (m) { return m.max_temp_c; }).filter(function (x) { return x != null; });
    return '<p class="small" id="svc-gpu-health"><span class="chip ok">GPU state: normal</span> ' + (g.synthetic ? '<span class="chip warn">synthetic readings (test)</span> ' : "") +
      (cs.length ? 'clocks at ' + fmtNum(100 * Math.min.apply(null, cs), 0) + '%+ of maximum' : "") + (tm.length ? ', at most ' + fmtNum(Math.max.apply(null, tm), 0) + ' °C' : "") +
      ', no thermal or hardware slowdown, no other processes on the GPU.' + infoI(g.rule) + '</p>';
  }
  return '<div class="notice warn" id="svc-gpu-health" role="status"><b>Check the GPU: it was not in a normal state during this run.</b> The latency and energy of the models below may reflect the GPU, not the model.' +
    '<ul class="small" style="margin:6px 0 0">' + per.filter(function (m) { return m.status === "check"; }).map(function (m) {
      return '<li><b>' + esc(short(m.model)) + '</b>: ' + m.reasons.map(esc).join("; ") + '</li>';
    }).join("") + '</ul><p class="small" style="margin:6px 0 0">Run it again when the GPU is cool and not shared, then compare on <a href="#/trends">Repeatability</a>.' + infoI(g.rule) + '</p></div>';
}
function costCard(res) {
  var cen = res.cost_energy;
  if (!cen) return '<div class="card"><p class="muted">This session predates the cost and energy measurements.</p></div>';
  var pr = cen.price || {}, es = cen.energy_sampling || {};
  return '<div class="card" id="svc-cost-card"><h2>Cost and energy</h2><dl class="kv small">' +
    '<dt>GPU price</dt><dd>' + (pr.usd_per_hour != null ? "$" + pr.usd_per_hour.toFixed(3) + "/h · " + esc(pr.source) + " · fetched " + esc(fmtDate(pr.fetched_at)) : esc(pr.reason || "not applicable (no GPU)")) + '</dd>' +
    '<dt>Power sampling</dt><dd>' + (es.method ? esc(es.method) + " every " + esc(es.interval_s) + " s · " + fmtNum(es.n_samples) + " samples" : "not sampled (no GPU)") + '</dd></dl>' +
    '<div class="table-wrap" style="margin-top:10px"><table id="svc-cost-table"><thead><tr><th>Model</th><th class="num">Cost / 1,000 flows</th><th class="num">Energy / flow<br><span class="small">(whole board)</span></th><th class="num">Energy / flow<br><span class="small">(net of idle)</span></th><th class="num">Energy / 1,000 flows</th><th class="num">Mean GPU power</th><th class="num">Idle power</th></tr></thead><tbody>' +
    (cen.per_model || []).map(function (r) {
      return '<tr><td class="mono">' + esc(r.model) + '</td><td class="num">' + (r.cost_per_1k_flows_usd != null ? fmtUsd(r.cost_per_1k_flows_usd) : '<span class="muted">' + esc(r.cost_status) + '</span>') +
        '</td><td class="num">' + (r.wh_per_flow != null ? fmtWh(r.wh_per_flow) + (r.energy_uncertainty != null ? ' <span class="small muted" title="Uncertainty from the sampling interval and the 1 s timestamps at the edges of this model\'s window">±' + fmtNum(100 * r.energy_uncertainty, r.energy_uncertainty < 0.1 ? 1 : 0) + '%</span>' : "") : '<span class="muted">' + esc(r.energy_status) + '</span>') +
        '</td><td class="num">' + (r.wh_per_flow_net_idle != null ? fmtWh(r.wh_per_flow_net_idle) : '<span class="muted">' + (r.wh_per_flow != null ? "no idle sample" : "—") + '</span>') +
        '</td><td class="num">' + fmtWh(r.wh_per_1k_flows) + '</td><td class="num">' + (r.mean_power_w != null ? fmtNum(r.mean_power_w) + " W" : "—") +
        '</td><td class="num">' + (r.idle_power_w != null ? fmtNum(r.idle_power_w) + " W" : "—") + '</td></tr>';
    }).join("") + '</tbody></table></div>' +
    '<details class="info"><summary>How this is calculated</summary><p class="muted small" id="svc-energy-method"><b>Method.</b> ' + esc(((cen.per_model || [])[0] || {}).cost_reason || "") + ' ' + esc((cen.notes || {}).cost || "") + ' ' +
    esc((cen.notes || {}).energy || "") + ' ' + esc((cen.notes || {}).energy_net || "") + ' ' + esc((cen.notes || {}).energy_uncertainty || "") + '</p></details></div>';
}
// ---- a small SVG line chart with a crosshair: series of [x, y]; one y axis; optional bands ----
// spec: { id, w, h, xMax, yMax, xFmt, yFmt, yLabel, bands: [{from, to, cls, color, label}],
//         series: [{name, color, points, kind: "line" | "dots"}], rows: [{x, text}] (crosshair text, by x),
//         marks: [{x}] (thin vertical annotation lines), threshold: {y, label} (a limit to stay under),
//         group: charts sharing one crosshair and one zoom (GROUPS[group]: {text(x), zoom(x0, x1)}) }
var LC = {}, GROUPS = {};
function niceMax(v) { if (!(v > 0)) return 1; var e = Math.pow(10, Math.floor(Math.log10(v))), f = v / e; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * e; }
function lineChart(spec) {
  var W = spec.w || 720, H = spec.h || 150, L = 56, R = 10, T = 10, B = 24, pw = W - L - R, ph = H - T - B;
  var th = spec.threshold && spec.threshold.y > 0 ? spec.threshold : null;
  var ym = niceMax(Math.max(spec.yMax || 0, th ? th.y * 1.08 : 0)), x0 = spec.xMin || 0, xm = spec.xMax || 1;
  var X = function (x) { return L + pw * (x - x0) / ((xm - x0) || 1); }, Y = function (y) { return T + ph * (1 - y / ym); };
  LC[spec.id] = { spec: spec, X: X, L: L, pw: pw, x0: x0, xm: xm, group: spec.group || null };
  var grid = [0, 0.5, 1].map(function (f) {
    return '<line class="lc-grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(ym * f) + '" y2="' + Y(ym * f) + '"/><text class="lc-tick" x="' + (L - 6) + '" y="' + (Y(ym * f) + 4) + '" text-anchor="end">' + esc(f === 0 ? "0" : spec.yFmt(ym * f)) + '</text>';
  }).join("");
  var xt = [0, 0.5, 1].map(function (f) { var v = x0 + (xm - x0) * f; return '<text class="lc-tick" x="' + X(v) + '" y="' + (H - 6) + '" text-anchor="' + (f === 0 ? "start" : f === 1 ? "end" : "middle") + '">' + esc(spec.xFmt(v)) + '</text>'; }).join("");
  var clip = function (v) { return Math.max(x0, Math.min(xm, v)); };
  var bands = (spec.bands || []).filter(function (b) { return b.to >= x0 && b.from <= xm; }).map(function (b) {
    var f = clip(b.from), t = clip(b.to);
    return '<rect class="lc-band ' + (b.cls || "") + '" x="' + X(f) + '" y="' + T + '" width="' + Math.max(1, X(t) - X(f)) + '" height="' + ph + '"' + (b.color ? ' style="fill:' + b.color + '"' : "") + '/>' +
      (b.label ? '<text class="lc-band-l" x="' + (X(f) + 4) + '" y="' + (T + 12) + '">' + esc(b.label) + '</text>' : "");
  }).join("");
  var marks = (spec.marks || []).filter(function (m) { return m.x >= x0 && m.x <= xm; }).map(function (m) {
    return '<line class="lc-mark" x1="' + X(m.x).toFixed(1) + '" x2="' + X(m.x).toFixed(1) + '" y1="' + T + '" y2="' + (T + ph) + '"/>';
  }).join("");
  var thr = th ? '<line class="lc-thr" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(th.y) + '" y2="' + Y(th.y) + '"/><text class="lc-thr-l" x="' + (W - R - 4) + '" y="' + (Y(th.y) - 4) + '" text-anchor="end">' + esc(th.label || "limit") + '</text>' : "";
  var series = spec.series.map(function (sr) {
    var pts = sr.points.filter(function (p) { return p[1] != null; });
    if (sr.kind === "dots") return pts.map(function (p) { return '<circle class="lc-dot' + (sr.r ? " big" : "") + '" cx="' + X(p[0]).toFixed(1) + '" cy="' + Y(p[1]).toFixed(1) + '" r="' + (sr.r || 2.2) + '" style="fill:' + sr.color + '"/>'; }).join("");
    return '<polyline class="lc-line" style="stroke:' + sr.color + '" points="' + pts.map(function (p) { return X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1); }).join(" ") + '"/>';
  }).join("");
  var clipId = "clip-" + spec.id;        // a zoomed series never draws outside the plot
  return '<div class="lc' + (spec.group ? " lc-grouped" : "") + '" id="' + spec.id + '"><svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(spec.label || "") + '">' +
    '<defs><clipPath id="' + clipId + '"><rect x="' + (L - 3) + '" y="' + (T - 3) + '" width="' + (pw + 6) + '" height="' + (ph + 6) + '"/></clipPath></defs>' + bands + grid + xt + marks + thr +
    (spec.yLabel ? '<text class="lc-tick" x="4" y="' + (T + 4) + '">' + esc(spec.yLabel) + '</text>' : "") + '<g clip-path="url(#' + clipId + ')">' + series + '</g>' +
    '<rect class="lc-sel" x="0" y="' + T + '" width="0" height="' + ph + '" visibility="hidden"/>' +
    '<line class="lc-cross" x1="0" x2="0" y1="' + T + '" y2="' + (T + ph) + '" visibility="hidden"/><rect class="lc-hit" x="' + L + '" y="' + T + '" width="' + pw + '" height="' + ph + '"/></svg></div>';
}
// a lane of coloured segments on the same x scale (the agent taking its turn), for a chart group
function laneChart(spec) {
  var W = 720, H = spec.h || 40, L = 56, R = 10, T = 4, B = 4, pw = W - L - R, ph = H - T - B, x0 = spec.xMin || 0, xm = spec.xMax || 1;
  var X = function (x) { return L + pw * (x - x0) / ((xm - x0) || 1); };
  LC[spec.id] = { spec: spec, X: X, L: L, pw: pw, x0: x0, xm: xm, group: spec.group || null };
  var segs = (spec.segments || []).filter(function (g) { return g.to >= x0 && g.from <= xm; }).map(function (g) {
    var f = Math.max(x0, g.from), t = Math.min(xm, g.to);
    return '<rect class="lane-seg" x="' + X(f).toFixed(2) + '" y="' + T + '" width="' + Math.max(0.6, X(t) - X(f)).toFixed(2) + '" height="' + ph + '" style="fill:' + g.color + '"/>';
  }).join("");
  return '<div class="lc lc-grouped lane" id="' + spec.id + '"><svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(spec.label || "") + '">' +
    '<text class="lc-tick" x="' + (L - 6) + '" y="' + (T + ph / 2 + 4) + '" text-anchor="end">' + esc(spec.title || "") + '</text>' +
    '<rect class="lane-bg" x="' + L + '" y="' + T + '" width="' + pw + '" height="' + ph + '"/>' + segs +
    '<rect class="lc-sel" x="0" y="' + T + '" width="0" height="' + ph + '" visibility="hidden"/>' +
    '<line class="lc-cross" x1="0" x2="0" y1="' + T + '" y2="' + (T + ph) + '" visibility="hidden"/><rect class="lc-hit" x="' + L + '" y="' + T + '" width="' + pw + '" height="' + ph + '"/></svg></div>';
}
function lcX(c, svg, clientX) {
  var r = svg.getBoundingClientRect();
  return c.x0 + ((clientX - r.left) * svg.viewBox.baseVal.width / r.width - c.L) / c.pw * (c.xm - c.x0);
}
function wireLineCharts() {
  Object.keys(LC).forEach(function (id) {
    var el = $(id), c = LC[id];
    if (!el || el._wired) return;
    el._wired = true;
    var svg = el.querySelector("svg"), cross = el.querySelector(".lc-cross"), rows = c.spec.rows || [];
    var members = function () {   // this chart, or every chart of its group on the page
      return c.group ? Object.keys(LC).filter(function (k) { return LC[k].group === c.group && $(k); }) : [id];
    };
    var tipAt = function (text, clientX) {
      var r = svg.getBoundingClientRect();
      openTip(el, text); if (TIP) { TIP._for = el; TIP._hover = true; TIP.style.top = (window.scrollY + r.bottom - 18) + "px"; TIP.style.left = Math.max(8, Math.min(clientX + window.scrollX + 12, window.scrollX + document.documentElement.clientWidth - TIP.offsetWidth - 8)) + "px"; }
    };
    function at(clientX) {
      var x = lcX(c, svg, clientX), g = c.group && GROUPS[c.group];
      if (g) {           // one crosshair across the group, at the same moment on every chart
        if (x < c.x0 || x > c.xm) return;
        members().forEach(function (k) { var o = LC[k], cr = $(k).querySelector(".lc-cross"); cr.setAttribute("x1", o.X(x)); cr.setAttribute("x2", o.X(x)); cr.setAttribute("visibility", "visible"); });
        tipAt(g.text(x), clientX);
        return;
      }
      var best = null;
      rows.forEach(function (row) { if (!best || Math.abs(row.x - x) < Math.abs(best.x - x)) best = row; });
      if (!best) return;
      cross.setAttribute("x1", c.X(best.x)); cross.setAttribute("x2", c.X(best.x)); cross.setAttribute("visibility", "visible");
      tipAt(best.text, clientX);
    }
    var drag = null, sel = el.querySelector(".lc-sel");
    el.addEventListener("mousemove", function (ev) {
      at(ev.clientX);
      if (drag) {
        var x = lcX(c, svg, ev.clientX), a = Math.max(c.x0, Math.min(drag, x)), b = Math.min(c.xm, Math.max(drag, x));
        sel.setAttribute("x", c.X(a)); sel.setAttribute("width", Math.max(0, c.X(b) - c.X(a))); sel.setAttribute("visibility", "visible");
      }
    });
    el.addEventListener("mouseleave", function () {
      members().forEach(function (k) { $(k).querySelector(".lc-cross").setAttribute("visibility", "hidden"); });
      closeTip(); drag = null; sel.setAttribute("visibility", "hidden");
    });
    if (c.group && GROUPS[c.group] && GROUPS[c.group].zoom) {   // drag across a chart: every chart of the group zooms
      el.addEventListener("mousedown", function (ev) { if (ev.button === 0) { drag = lcX(c, svg, ev.clientX); ev.preventDefault(); } });
      el.addEventListener("mouseup", function (ev) {
        if (drag == null) return;
        var x = lcX(c, svg, ev.clientX), a = Math.max(c.x0, Math.min(drag, x)), b = Math.min(c.xm, Math.max(drag, x));
        drag = null; sel.setAttribute("visibility", "hidden");
        if (c.X(b) - c.X(a) > 6) { closeTip(); GROUPS[c.group].zoom(a, b); }
      });
      el.addEventListener("dblclick", function () { closeTip(); GROUPS[c.group].zoom(null, null); });
    }
  });
}
// Performance -> Latency: latency over the run, per model (shared y scale)
function trendCard(res) {
  var tr = window.INSIGHTS && INSIGHTS.latencyTrend(res);
  if (!tr) return "";
  var xMax = Math.max.apply(null, tr.models.map(function (m) { return m.n || 1; }));
  var panels = tr.models.map(function (m, i) {
    var d = m.drift, chip = d.tested ? (d.verdict === "stable" ? '<span class="chip ok">stable</span>' : '<span class="chip bad">' + (d.verdict === "slows_down" ? "slows down" : "speeds up") + '</span>') : '<span class="chip">not tested</span>';
    var p95 = userLimits().max_p95_latency_s;
    var chart = lineChart({ id: "trend-" + i, h: 140, xMin: 1, xMax: xMax, yMax: tr.ymax, xFmt: function (v) { return "flow " + Math.round(v); }, yFmt: fmtTime,
      label: "Latency per flow over the run for " + short(m.model), threshold: p95 ? { y: p95, label: "your p95 limit " + fmtTime(p95) } : null,
      series: [{ name: "flows", kind: "dots", color: modelColor(m.model), points: m.points.map(function (p) { return [p.flow, p.latency_s]; }) },
               { name: "rolling median", kind: "line", color: "var(--text)", points: m.points.map(function (p) { return [p.flow, p.median_s]; }) }],
      rows: m.points.map(function (p) { return { x: p.flow, text: short(m.model) + " · flow " + p.flow + ": " + fmtTime(p.latency_s) + " (rolling median " + fmtTime(p.median_s) + ")" }; }) });
    return '<div class="trend-panel" data-model="' + esc(m.model) + '"><div class="histo-head"><span class="mono small"><i class="sw" style="--c:' + modelColor(m.model) + '"></i>' + esc(short(m.model)) + '</span>' + chip + '</div>' + chart +
      '<p class="small trend-note">' + esc(d.note || "") + (m.thinned ? ' <span class="muted">(' + fmtNum(m.n) + ' flows; chart thinned, the test uses every flow)</span>' : "") + '</p></div>';
  }).join("");
  return '<div class="card" id="stats-trend"><h2>Latency over the run</h2><p class="muted small" style="margin-top:0">Each dot is one flow in the order it ran; the line is the rolling median over ' + (tr.models[0] ? tr.models[0].window : 3) + ' flows. A slow-down over the run points to heat, memory build-up or contention, not the model alone.</p>' +
    '<div class="ilegend"><span><i style="--c:var(--muted-2)"></i>one flow</span><span><i style="--c:var(--text);height:2px"></i>rolling median</span></div>' + panels +
    '<details class="info"><summary>Method</summary><p class="muted small">' + esc(tr.method || "") + '</p></details></div>';
}
// Cost & Energy: board power, utilisation and memory over the session, model windows shaded.
// Observability-style: one crosshair across the charts (power, utilisation, memory and the
// agent lane at the same moment), drag to zoom them all, flow starts and agent turns annotated.
var GPU_RES = null, GPUZ = null;           // the session's results and the zoomed range {from, to}
var MARKS_MAX = 80;                        // flow-start lines drawn only when this few are in view
function gpuTimelineCard(res) {
  var g = window.INSIGHTS && INSIGHTS.gpuTimeline(res);
  if (!g) return "";
  if (!g.sampled) return '<div class="card" id="gpu-timeline"><h2>GPU over the run</h2><p class="muted">Not sampled: ' + esc(g.reason) + '.</p></div>';
  if (GPU_RES !== res) { GPU_RES = res; GPUZ = null; }
  var x0 = GPUZ ? GPUZ.from : 0, x1 = GPUZ ? GPUZ.to : (g.t_max || 1), mk = g.markers;
  var bands = g.windows.map(function (w) { return { from: w.from_s, to: w.to_s, color: modelColor(w.model), cls: "win", label: short(w.model) }; })
    .concat(g.idle.map(function (w) { return { from: w.from_s, to: w.to_s, cls: "idle", label: "idle" }; }));
  var plain = bands.map(function (b) { return { from: b.from, to: b.to, color: b.color, cls: b.cls }; });   // names on the first chart only
  var inView = mk.flows.filter(function (f) { return f.end_s >= x0 && f.t_s <= x1; });
  var marks = inView.length <= MARKS_MAX ? inView.map(function (f) { return { x: f.t_s }; }) : [];
  var pts = g.points.filter(function (p, i) { var n = g.points[i + 1], b = g.points[i - 1]; return (p.t >= x0 && p.t <= x1) || (n && n.t >= x0 && p.t < x0) || (b && b.t <= x1 && p.t > x1); });
  var sec = function (v) { return fmtNum(v, x1 - x0 < 20 ? 1 : 0) + " s"; };
  var limits = userLimits();
  var memThr = limits.max_peak_vram_mb ? { y: limits.max_peak_vram_mb, label: "your limit " + fmtMB(limits.max_peak_vram_mb) } : null;
  GROUPS.gpu = {
    text: function (x) {
      var near = null;
      g.points.forEach(function (p) { if (!near || Math.abs(p.t - x) < Math.abs(near.t - x)) near = p; });
      var run = INSIGHTS.runningAt(mk, x), inw = g.windows.filter(function (w) { return x >= w.from_s && x <= w.to_s; })[0], idle = g.idle.filter(function (w) { return x >= w.from_s && x <= w.to_s; })[0];
      return fmtNum(x, 1) + " s" + (run ? " · " + short(run.model) + " · flow " + run.flow + (run.agent ? " · " + agName(run.agent) + " agent" : " · between agent calls") :
        inw ? " · " + short(inw.model) + " (between flows)" : idle ? " · idle baseline (" + short(idle.model) + ")" : "") +
        (near ? " — " + fmtNum(near.power_w, 0) + " W" + (near.util_pct != null ? " · " + fmtNum(near.util_pct, 0) + "% busy" : "") + (near.mem_mb != null ? " · " + fmtMB(near.mem_mb) : "") : "");
    },
    zoom: function (a, b) { GPUZ = a == null ? null : { from: a, to: b }; rerenderGpuTimeline(); }
  };
  var chart = function (id, key, yMax, yFmt, label, first, extra) {
    return lineChart(Object.assign({ id: id, h: 120, xMin: x0, xMax: x1, yMax: yMax, xFmt: sec, yFmt: yFmt, bands: first ? bands : plain, marks: marks, label: label, group: "gpu",
      series: [{ name: key, kind: "line", color: "var(--text)", points: pts.map(function (p) { return [p.t, p[key]]; }) }] }, extra || {}));
  };
  var segs = [];
  inView.forEach(function (f) { f.agents.forEach(function (a) { segs.push({ from: a.from_s, to: a.to_s, color: agColor(a.agent) }); }); });
  var lane = mk.flows.length ? '<h3 class="lc-h">Agent taking its turn</h3>' +
    laneChart({ id: "gpu-lane", xMin: x0, xMax: x1, group: "gpu", segments: segs, title: mk.thinned ? "(too many to draw)" : "", label: "Which agent was running, over the session" }) +
    '<div class="legend small" id="gpu-lane-legend">' + mk.agents.map(function (a) { return '<span style="--c:' + agColor(a) + '">' + esc(agName(a)) + '</span>'; }).join("") + '</div>' : "";
  var zoomBtns = '<div class="row gpu-zoom" role="group" aria-label="Zoom the GPU charts">' +
    (GPUZ ? '<button type="button" class="btn small-btn" data-gpu-zoom="reset">Show the whole run</button><span class="muted small">Showing ' + fmtNum(x0, 1) + '–' + fmtNum(x1, 1) + ' s</span>' :
      '<span class="muted small">Drag across any chart to zoom them all; double-click to zoom out.</span>') +
    g.windows.map(function (w, i) { return '<button type="button" class="btn small-btn" data-gpu-zoom="' + i + '">Zoom to ' + esc(short(w.model)) + '</button>'; }).join("") + '</div>';
  return '<div class="card" id="gpu-timeline"><h2>GPU over the run</h2>' +
    (g.synthetic ? '<div class="notice warn" id="gpu-synthetic">Synthetic test readings — not a measurement (test mode).</div>' : "") +
    '<p class="muted small" style="margin-top:0">Sampled every ' + esc(g.interval_s) + ' s (' + esc(g.method) + ', ' + fmtNum(g.n_samples) + ' samples) from outside the measured process. Shaded in the model\'s colour: its measured window; grey: its idle baseline (sampled just before it loads). Hover any chart to read every chart at that moment, with the flow and agent that were running.' +
    infoI(mk.method + (marks.length ? " Thin vertical lines: where each flow started." : mk.flows.length ? " Flow-start lines appear when you zoom in to " + MARKS_MAX + " flows or fewer." : "")) + '</p>' +
    legendHtml(g.windows.map(function (w) { return w.model; })) + zoomBtns +
    '<h3 class="lc-h">Board power (W)</h3>' + chart("gpu-power", "power_w", g.power_max, function (v) { return fmtNum(v, 0) + " W"; }, "GPU board power over the session", true) +
    (g.has_util ? '<h3 class="lc-h">GPU utilisation (%)</h3>' + chart("gpu-util", "util_pct", 100, function (v) { return fmtNum(v, 0) + "%"; }, "GPU utilisation over the session") : "") +
    (g.has_mem ? '<h3 class="lc-h">Device memory used</h3>' + chart("gpu-mem", "mem_mb", g.mem_max, function (v) { return fmtNum(v / 1024, 1) + " GB"; }, "Device memory over the session", false, { threshold: memThr }) : "") +
    lane +
    '<div class="table-wrap"><table id="gpu-timeline-table"><thead><tr><th>Model</th><th class="num">Samples</th><th class="num">Mean power</th><th class="num">Peak power</th><th class="num">Mean utilisation</th><th class="num">Peak memory</th></tr></thead><tbody>' +
    g.per_model.map(function (m) { return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td class="num">' + fmtNum(m.n_samples) + '</td><td class="num">' + (m.mean_power_w == null ? "—" : fmtNum(m.mean_power_w, 1) + " W") + '</td><td class="num">' + (m.peak_power_w == null ? "—" : fmtNum(m.peak_power_w, 0) + " W") +
      '</td><td class="num">' + (m.mean_util_pct == null ? "—" : fmtNum(m.mean_util_pct, 0) + "%") + '</td><td class="num">' + (m.peak_mem_mb == null ? "—" : fmtMB(m.peak_mem_mb)) + '</td></tr>'; }).join("") + '</tbody></table></div></div>';
}
function rerenderGpuTimeline() {
  var el = $("gpu-timeline");
  if (!el || !GPU_RES) return;
  var box = document.createElement("div");
  box.innerHTML = gpuTimelineCard(GPU_RES);
  var card = box.firstChild, parent = el.parentNode;
  el.replaceWith(card);
  wireLineCharts(); addExports(parent);
}
document.addEventListener("click", function (ev) {
  var b = ev.target.closest && ev.target.closest("[data-gpu-zoom]");
  if (!b || !GPU_RES) return;
  var v = b.getAttribute("data-gpu-zoom"), g = INSIGHTS.gpuTimeline(GPU_RES), w = g && g.windows[+v];
  GPUZ = v === "reset" || !w ? null : { from: Math.max(0, w.from_s - 1), to: w.to_s + 1 };
  rerenderGpuTimeline();
});
// ---- downloads: CSV of a card's table, PNG of its SVG chart (computed colours, any theme) ----
function addExports(root) {
  if (!root) return;
  root.querySelectorAll(".card").forEach(function (c) {
    if (c.querySelector(":scope > .exp-bar")) return;
    var hasT = !!c.querySelector("table"), hasS = !!c.querySelector(FIG_SEL);
    if (!hasT && !hasS) return;
    var bar = document.createElement("div");
    bar.className = "exp-bar";
    var hasL = !!c.querySelector(".lc");
    bar.innerHTML = (hasL ? '<button type="button" class="btn small-btn" data-exp="inspect" aria-expanded="false">Inspect data</button>' : "") +
      '<span class="muted small">Download</span>' + (hasT ? '<button type="button" class="btn small-btn" data-exp="csv">CSV</button>' : "") +
      (hasL ? '<button type="button" class="btn small-btn" data-exp="chartcsv">Chart data (CSV)</button>' : "") +
      (hasT ? '<button type="button" class="btn small-btn" data-exp="latex" title="A LaTeX table (booktabs) with this card\'s title as its caption">LaTeX</button>' : "") +
      (hasS ? '<button type="button" class="btn small-btn" data-exp="png" title="A picture with a caption">PNG</button><button type="button" class="btn small-btn" data-exp="svg" title="A vector figure with a caption: sharp at any size, for a thesis or paper">SVG</button>' : "");
    c.appendChild(bar);
  });
}
function expName(card, ext) {
  var h = ((card.querySelector("h2") || {}).textContent || "chart").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40);
  return "agentmeter_" + (SESSION.id && card.closest("#session-body") ? shortId(SESSION.id).replace(/…$/, "") + "_" : "") + h + "." + ext;
}
function tableCsv(t) {
  var q = function (v) { v = String(v).replace(/\s+/g, " ").trim(); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  return Array.prototype.map.call(t.rows, function (r) { return Array.prototype.map.call(r.cells, function (c) { return q(c.textContent); }).join(","); }).join("\n") + "\n";
}
// ---- thesis-ready figures: every chart as SVG (vector) or PNG, with a caption under it ----
var FIG_SEL = ".lc svg, .ho svg, svg.frontier, svg.trend, .hbars";
function figCaption(card) {
  var h = ((card.querySelector("h2") || {}).textContent || "AgentMeter chart").replace(/\s+/g, " ").trim(), sel = card.querySelector("select");
  if (sel && sel.selectedIndex >= 0 && card.querySelector(".hbars")) h += ": " + sel.options[sel.selectedIndex].textContent.trim();
  return h + " — AgentMeter" + (SESSION.id && card.closest("#session-body") ? ", session " + shortId(SESSION.id) : "") + ", " + new Date().toISOString().slice(0, 10);
}
// HTML bar lists (.hbars) become a small SVG so they export like every other chart
function barsSvg(box) {
  var rows = Array.prototype.map.call(box.querySelectorAll(".hbar"), function (r) {
    return { l: r.querySelector(".hb-l").textContent, w: parseFloat(r.querySelector(".hb-f").style.width) || 0, v: r.querySelector(".hb-v").textContent, lead: r.querySelector(".hb-f").classList.contains("lead") };
  });
  var W = 640, rh = 26, L = 210, R = 90, H = rows.length * rh + 8, ns = "http://www.w3.org/2000/svg";
  var svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  svg.innerHTML = rows.map(function (r, i) {
    var y = 4 + i * rh, bw = (W - L - R) * r.w / 100;
    return '<text x="0" y="' + (y + 16) + '" font-size="12">' + esc(r.l.length > 30 ? r.l.slice(0, 29) + "…" : r.l) + '</text>' +
      '<rect x="' + L + '" y="' + (y + 5) + '" width="' + (W - L - R) + '" height="13" rx="3" fill="#e2e2dc"/>' +
      '<rect x="' + L + '" y="' + (y + 5) + '" width="' + bw.toFixed(1) + '" height="13" rx="3" fill="' + (r.lead ? "#2e7d5b" : "#2455d6") + '"/>' +
      '<text x="' + W + '" y="' + (y + 16) + '" font-size="12" text-anchor="end">' + esc(r.v) + '</text>';
  }).join("");
  svg.style.position = "absolute"; svg.style.left = "-10000px"; svg.style.width = W + "px";
  document.body.appendChild(svg);
  return svg;
}
function figSvg(card) {
  var el = card.querySelector(FIG_SEL), tmp = null;
  if (el && el.classList.contains("hbars")) { tmp = barsSvg(el); el = tmp; }
  return { svg: el, done: function () { if (tmp) tmp.remove(); } };
}
// a standalone copy of a chart: computed styles inlined, a white background and a caption line
function svgStandalone(svg, caption) {
  var clone = svg.cloneNode(true), src = svg.querySelectorAll("*"), dst = clone.querySelectorAll("*");
  var PROPS = ["fill", "stroke", "stroke-width", "stroke-dasharray", "opacity", "font-size", "font-family", "font-weight", "visibility", "text-anchor"];
  Array.prototype.forEach.call(src, function (el, i) {
    var cs = getComputedStyle(el);
    dst[i].setAttribute("style", PROPS.map(function (p) { return p + ":" + cs.getPropertyValue(p); }).join(";"));
  });
  var vb = svg.viewBox.baseVal, w = (vb && vb.width) || svg.clientWidth, h = (vb && vb.height) || svg.clientHeight, x0 = (vb && vb.x) || 0, y0 = (vb && vb.y) || 0, capH = caption ? 30 : 0;
  var ns = "http://www.w3.org/2000/svg", g = document.createElementNS(ns, "g");
  while (clone.firstChild) g.appendChild(clone.firstChild);
  clone.appendChild(g);
  clone.setAttribute("xmlns", ns); clone.setAttribute("viewBox", x0 + " " + y0 + " " + w + " " + (h + capH)); clone.setAttribute("width", w); clone.setAttribute("height", h + capH);
  clone.removeAttribute("class"); clone.removeAttribute("style");
  var bg = document.createElementNS(ns, "rect");
  bg.setAttribute("x", x0); bg.setAttribute("y", y0); bg.setAttribute("width", w); bg.setAttribute("height", h + capH); bg.setAttribute("fill", getComputedStyle(document.body).backgroundColor || "#fff");
  clone.insertBefore(bg, g);
  if (caption) {
    var t = document.createElementNS(ns, "text");
    t.setAttribute("x", x0 + w / 2); t.setAttribute("y", y0 + h + 20); t.setAttribute("text-anchor", "middle");
    t.setAttribute("style", "font-size:12px;font-family:" + getComputedStyle(document.body).fontFamily + ";fill:" + getComputedStyle(document.body).color);
    t.textContent = caption; clone.appendChild(t);
  }
  return { xml: new XMLSerializer().serializeToString(clone), w: w, h: h + capH };
}
function latexTable(t, caption) {
  var esc2 = function (v) { return String(v).replace(/\s+/g, " ").trim().replace(/\\/g, "\\textbackslash{}").replace(/([&%$#_{}])/g, "\\$1").replace(/~/g, "\\textasciitilde{}").replace(/\^/g, "\\textasciicircum{}").replace(/—/g, "---").replace(/–/g, "--"); };
  var rows = Array.prototype.map.call(t.rows, function (r) { return Array.prototype.map.call(r.cells, function (c) { return esc2(c.textContent); }); });
  var n = Math.max.apply(null, rows.map(function (r) { return r.length; }));
  var head = rows[0], body = rows.slice(1);
  var label = "tab:" + caption.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 30);
  return "% AgentMeter export, " + new Date().toISOString().slice(0, 10) + " — needs \\usepackage{booktabs}\n" +
    "\\begin{table}[htbp]\n  \\centering\n  \\caption{" + esc2(caption) + "}\n  \\label{" + label + "}\n  \\begin{tabular}{l" + "r".repeat(Math.max(0, n - 1)) + "}\n    \\toprule\n" +
    "    " + head.join(" & ") + " \\\\\n    \\midrule\n" + body.map(function (r) { return "    " + r.join(" & ") + " \\\\"; }).join("\n") +
    "\n    \\bottomrule\n  \\end{tabular}\n\\end{table}\n";
}
function svgPng(svg, name, caption) {
  var st = svgStandalone(svg, caption), scale = 2, w = st.w, h = st.h;
  var url = URL.createObjectURL(new Blob([st.xml], { type: "image/svg+xml" }));
  var img = new Image();
  img.onload = function () {
    var cv = document.createElement("canvas"); cv.width = w * scale; cv.height = h * scale;
    var g = cv.getContext("2d"); g.fillStyle = getComputedStyle(document.body).backgroundColor; g.fillRect(0, 0, cv.width, cv.height);
    g.drawImage(img, 0, 0, cv.width, cv.height); URL.revokeObjectURL(url);
    cv.toBlob(function (b) { var a = document.createElement("a"); a.href = URL.createObjectURL(b); a.download = name; document.body.appendChild(a); a.click(); a.remove(); });
  };
  img.src = url;
}
document.addEventListener("click", function (ev) {
  var b = ev.target.closest && ev.target.closest("[data-exp]");
  if (!b) return;
  var card = b.closest(".card"), kind = b.getAttribute("data-exp");
  if (kind === "csv") download(expName(card, "csv"), tableCsv(card.querySelector("table")));
  else if (kind === "chartcsv") download(expName(card, "data.csv"), chartRows(card).map(function (r) { return r.map(csvCell).join(","); }).join("\n"));
  else if (kind === "inspect") inspectCard(card, b);
  else if (kind === "latex") download(expName(card, "tex"), latexTable(card.querySelector("table"), ((card.querySelector("h2") || {}).textContent || "AgentMeter table").trim()), "application/x-tex");
  else {
    var f = figSvg(card);
    if (!f.svg) return;
    if (kind === "svg") download(expName(card, "svg"), svgStandalone(f.svg, figCaption(card)).xml, "image/svg+xml");
    else svgPng(f.svg, expName(card, "png"), figCaption(card));
    f.done();
  }
});
// ---- Inspect: the exact numbers behind every chart of a card (observability-style) ----
function csvCell(v) { var t = v == null ? "" : String(v); return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t; }
function chartRows(card) {
  var rows = [["chart", "series", "x", "y"]];
  card.querySelectorAll(".lc[id]").forEach(function (el) {
    var c = LC[el.id];
    if (!c) return;
    var name = c.spec.label || el.id;
    (c.spec.series || []).forEach(function (sr) { sr.points.forEach(function (p) { if (p[1] != null) rows.push([name, sr.name, +(+p[0]).toFixed(4), +(+p[1]).toFixed(4)]); }); });
    (c.spec.segments || []).forEach(function (g) { rows.push([name, "turn", +(+g.from).toFixed(4), +(+g.to).toFixed(4)]); });
    if (c.spec.threshold) rows.push([name, "threshold", "", c.spec.threshold.y]);
  });
  return rows;
}
function inspectCard(card, btn) {
  var open = card.querySelector(":scope > .inspect");
  if (open) { open.remove(); btn.setAttribute("aria-expanded", "false"); return; }
  var rows = chartRows(card), body = rows.slice(1), MAX = 200;
  var box = document.createElement("div");
  box.className = "inspect";
  box.innerHTML = '<p class="muted small">' + fmtNum(body.length) + ' data points behind the chart' + (card.querySelectorAll(".lc[id]").length > 1 ? "s" : "") + ' of this card' +
    (body.length > MAX ? ' (the first ' + MAX + ' shown; Chart data (CSV) has them all)' : "") + '. x is the chart\'s horizontal value (seconds, flow number, ...).</p>' +
    '<div class="table-wrap"><table class="inspect-table"><thead><tr>' + rows[0].map(function (h) { return '<th>' + esc(h) + '</th>'; }).join("") + '</tr></thead><tbody>' +
    body.slice(0, MAX).map(function (r) { return '<tr><td class="small">' + esc(r[0]) + '</td><td class="mono small">' + esc(r[1]) + '</td><td class="num">' + esc(r[2]) + '</td><td class="num">' + esc(r[3]) + '</td></tr>'; }).join("") + '</tbody></table></div>';
  card.insertBefore(box, card.querySelector(":scope > .exp-bar"));
  btn.setAttribute("aria-expanded", "true");
}
// ---- Agent Analysis: flow explorer (flows_<run>.json; loaded when the tab opens) ----
var FLOWS = { id: null, data: null, filter: "all", q: "", page: 0, open: null };
function loadFlows() {
  var id = SESSION.id, job = SESSION.job || {};
  if (!$("flows-card") || FLOWS.id === id) return;
  FLOWS = { id: id, data: null, filter: "all", q: "", page: 0, open: null };
  var path = CP && job.stored ? "/api/sessions/" + enc(id) + "/files/flows.json" : "/api/jobs/" + enc(id) + "/flows";
  api(path).then(function (r) {
    if (SESSION.id !== id || !$("flows-status")) return;
    if (!r.ok) { $("flows-status").textContent = r.status === 404 ? "This session has no per-flow record: it was run before the flow explorer existed." : "Could not load the flows: " + ((r.data || {}).error || "error"); return; }
    FLOWS.data = r.data; drawFlows();
  });
}
function flowList() {
  var d = FLOWS.data, q = FLOWS.q.toLowerCase(), list = d.flows.slice();
  var lat = function (f) { return Math.max.apply(null, d.models.map(function (m) { return ((f.per_model[m] || {}).latency_s) || 0; })); };
  if (FLOWS.filter === "wrong") list = list.filter(function (f) { return d.models.some(function (m) { return f.per_model[m] && f.per_model[m].correct === false; }); });
  // a true attack that a model's answer would have dismissed (the costliest triage error)
  if (FLOWS.filter === "missed") list = list.filter(function (f) { return f.truth_priority && f.truth_priority !== "Dismiss" && d.models.some(function (m) { return f.per_model[m] && f.per_model[m].priority === "Dismiss"; }); });
  if (FLOWS.filter === "escalate") list = list.filter(function (f) { return d.models.some(function (m) { return f.per_model[m] && f.per_model[m].priority === "Escalate"; }); });
  if (FLOWS.filter === "disagree") list = list.filter(function (f) { var a = d.models.map(function (m) { return (f.per_model[m] || {}).predicted; }); return a.some(function (x) { return x !== a[0]; }); });
  if (FLOWS.filter === "slow") list = list.sort(function (a, b) { return lat(b) - lat(a); }).slice(0, 10);
  if (q) list = list.filter(function (f) { return (f.scenario_id + " " + (f.truth || "") + " " + d.models.map(function (m) { return (f.per_model[m] || {}).predicted || ""; }).join(" ")).toLowerCase().indexOf(q) >= 0; });
  return list;
}
function drawFlows() {
  var d = FLOWS.data, list = flowList(), per = 25, pages = Math.max(1, Math.ceil(list.length / per));
  FLOWS.page = Math.min(FLOWS.page, pages - 1);
  var labelled = d.labelled !== false && d.flows.some(function (f) { return f.truth; });
  $("flows-status").innerHTML = esc(d.flows.length + " flows × " + d.models.length + " model" + (d.models.length > 1 ? "s" : "") + ". ") +
    (d.has_text ? "Open a flow to read each agent's answer (up to " + fmtNum(d.text_chars) + " characters each)." : "<b>Agent answers were not recorded for this run</b> — times and tokens only.");
  var mark = function (pm) { return !pm ? "—" : (pm.correct === true ? '<span class="chip ok">✓ ' : pm.correct === false ? '<span class="chip bad">✗ ' : '<span class="chip">') + esc(pm.predicted || "—") + '</span>' + (pm.priority ? ' <span class="muted small">' + esc(pm.priority) + (pm.severity ? " · " + esc(pm.severity) : "") + '</span>' : ""); };
  var triaged = d.flows.some(function (f) { return d.models.some(function (m) { return f.per_model[m] && f.per_model[m].priority; }); });
  $("flows-body").innerHTML = '<div class="row" style="margin-top:0"><div class="field"><label for="flows-filter">Show</label><select id="flows-filter">' +
    [["all", "All flows"], ["wrong", "Wrong answer (any model)"], ["missed", "Attack dismissed (any model)"], ["escalate", "Escalated (any model)"], ["disagree", "Models disagree"], ["slow", "10 slowest"]]
      .filter(function (o) { return labelled || (o[0] !== "wrong" && o[0] !== "missed"); })
      .filter(function (o) { return triaged || (o[0] !== "missed" && o[0] !== "escalate"); })
      .filter(function (o) { return d.models.length > 1 || o[0] !== "disagree"; })
      .map(function (o) { return '<option value="' + o[0] + '"' + (FLOWS.filter === o[0] ? " selected" : "") + '>' + o[1] + '</option>'; }).join("") + '</select></div>' +
    '<div class="field" style="flex:1 1 220px"><label for="flows-q">Find</label><input type="search" id="flows-q" class="wide" placeholder="flow id, class or answer" value="' + esc(FLOWS.q) + '" autocomplete="off"></div></div>' +
    '<div class="table-wrap"><table id="flows-table"><thead><tr><th>Flow</th>' + (labelled ? "<th>True class</th>" : "") +
    d.models.map(function (m) { return '<th><i class="sw" style="--c:' + modelColor(m) + '"></i>' + esc(short(m)) + '</th><th class="num">Time</th>'; }).join("") + '<th><span class="sr-only">Actions</span></th></tr></thead><tbody>' +
    list.slice(FLOWS.page * per, FLOWS.page * per + per).map(function (f) {
      return '<tr' + (FLOWS.open === f.scenario_id ? ' class="changed"' : "") + '><td class="mono">' + esc(f.scenario_id) + '</td>' + (labelled ? '<td>' + esc(f.truth || "—") + '</td>' : "") +
        d.models.map(function (m) { var pm = f.per_model[m]; return '<td>' + mark(pm) + '</td><td class="num">' + (pm ? fmtTime(pm.latency_s) : "—") + '</td>'; }).join("") +
        '<td><button type="button" class="btn small-btn" data-flow="' + esc(f.scenario_id) + '">' + (FLOWS.open === f.scenario_id ? "Close" : "Open") + '</button></td></tr>';
    }).join("") + '</tbody></table></div>' +
    (list.length ? "" : '<p class="muted">No flow matches.</p>') +
    (pages > 1 ? '<div class="actions"><button type="button" class="btn small-btn" id="flows-prev"' + (FLOWS.page ? "" : " disabled") + '>Previous</button><span class="small muted">page ' + (FLOWS.page + 1) + ' of ' + pages + ' (' + list.length + ' flows)</span><button type="button" class="btn small-btn" id="flows-next"' + (FLOWS.page < pages - 1 ? "" : " disabled") + '>Next</button></div>' : "") +
    '<div id="flow-detail">' + flowDetail() + '</div>';
  $("flows-filter").addEventListener("change", function () { FLOWS.filter = this.value; FLOWS.page = 0; drawFlows(); });
  var t = null;
  $("flows-q").addEventListener("input", function () { var v = this.value; clearTimeout(t); t = setTimeout(function () { FLOWS.q = v; FLOWS.page = 0; drawFlows(); $("flows-q").focus(); }, 250); });
  $("flows-body").querySelectorAll("[data-flow]").forEach(function (b) { b.addEventListener("click", function () {
    var id = b.getAttribute("data-flow"); FLOWS.open = FLOWS.open === id ? null : id; drawFlows();
    if (FLOWS.open) $("flow-detail").scrollIntoView({ block: "nearest" });
  }); });
  if ($("flows-prev")) { $("flows-prev").addEventListener("click", function () { FLOWS.page--; drawFlows(); }); $("flows-next").addEventListener("click", function () { FLOWS.page++; drawFlows(); }); }
}
function flowDetail() {
  var d = FLOWS.data, f = FLOWS.open && d.flows.filter(function (x) { return x.scenario_id === FLOWS.open; })[0];
  if (!f) return "";
  return '<div class="flow-detail"><h3>Flow ' + esc(f.scenario_id) + (f.truth ? ' <span class="muted small">true class: ' + esc(f.truth) + '</span>' : "") +
    (f.ids_score != null ? ' <span class="muted small">IDS score ' + fmtNum(f.ids_score, 3) + '</span>' : "") + '</h3><div class="flow-cols">' +
    d.models.map(function (m) {
      var pm = f.per_model[m];
      if (!pm) return '<div class="flow-col"><h4>' + esc(short(m)) + '</h4><p class="muted">Not run.</p></div>';
      return '<div class="flow-col" data-model="' + esc(m) + '"><h4><i class="sw" style="--c:' + modelColor(m) + '"></i>' + esc(short(m)) + '</h4><p class="small">Answer <b>' + esc(pm.predicted || "—") + '</b>' + (pm.priority ? ' → <b>' + esc(pm.priority) + '</b>' + (pm.severity ? ' (severity ' + esc(pm.severity) + ')' : "") : "") +
        (pm.correct === true ? ' <span class="chip ok">✓ correct</span>' : pm.correct === false ? ' <span class="chip bad">✗ wrong</span>' : "") + ' · ' + fmtTime(pm.latency_s) + ' end to end</p>' +
        pm.agents.map(function (a) {
          return '<div class="flow-agent"><div class="small"><span class="legend"><span style="--c:' + agColor(a.agent) + '">' + esc(agName(a.agent)) + '</span></span> ' + fmtTime(a.wall_s) +
            ' · ' + fmtNum(a.input_tokens) + ' tokens in, ' + fmtNum(a.output_tokens) + ' out</div>' + (a.text != null ? '<pre class="flow-text">' + esc(a.text) + '</pre>' : (d.has_text ? '<p class="muted small">(empty answer)</p>' : "")) + '</div>';
        }).join("") + '</div>';
    }).join("") + '</div></div>';
}
