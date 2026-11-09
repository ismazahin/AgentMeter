// AgentMeter web app — analyse.js: Analyse traffic (#/analyse): the IDS over a whole upload, no GPU.
// One of the app's scripts (web/app.html loads them in order: core, home, wizard, session, analysis,
// analyse, compare, settings, boot); they share the page's global scope.
//
// An analysis is a cloud preparation job (control plane: /api/prep/jobs) whose preparation service
// also scored every flow of the candidate pool with the stored IDS model. This page uploads the file,
// follows the job, and then works on its stored scores in the browser: the threshold, the alerts,
// what the IDS caught and missed (labelled CSV), and the LLM cost projected from earlier sessions.
// "Benchmark LLMs on these alerts" hands the prepared set (with its scores) to the GPU server.
"use strict";
var AN = { src: "file", job: null, rows: null, pool: null, ident: null, tau: null, page: 0, sessions: null, seen: 0 };
var AN_PAGE = 50;

function anCloud() { return !!(CP && BACKEND && BACKEND.cloud_prep); }
function loadAnalyse(p, q) {
  var id = p[1];
  $("an-start").hidden = !!id; $("an-detail").hidden = !id;
  if (id) { anDetail(id); return; }
  var un = $("an-unavailable");
  un.hidden = anCloud();
  un.innerHTML = !CP ? "Analyse traffic needs the control plane (the hosted site); on a single GPU server, use New benchmark."
    : "<b>The preparation service is not running.</b> Analyses run on it (no GPU). On the computer that hosts it, run " +
      "<code>python scripts/prep_local.py</code> and keep the window open, then reload this page.";
  $("an-go").disabled = !anCloud();
  if (BACKEND && BACKEND.cloud_prep_max_mb) $("an-limit").textContent = "Maximum upload: " + BACKEND.cloud_prep_max_mb + " MB — use Import from a link for larger files";
  if (anCloud()) cpFetch("/api/prep/wake", { method: "POST" }).then(function () { /* best effort: wakes a sleeping service */ });
  anList();
}
var anTabs = Array.prototype.slice.call(document.querySelectorAll('#an-start [role="tab"]'));
tablist(document.querySelector("#an-start .tabs"), function (t) {
  AN.src = t.id === "tab-an-url" ? "url" : "file";
  selectTab(anTabs, t);
  $("an-src-file").hidden = AN.src !== "file"; $("an-src-url").hidden = AN.src !== "url";
});

// ---------- the list of analyses ----------
function anList() {
  $("an-list").innerHTML = skel(3, true);
  cpFetch("/api/analyses").then(function (r) {
    if (!r.ok) { $("an-list").innerHTML = '<div class="notice error">' + esc((r.data || {}).error || "Could not load your analyses.") + '</div>'; return; }
    var a = r.data.analyses || [];
    if (!a.length) { $("an-list").innerHTML = '<div class="card empty" id="an-empty"><p>No analyses yet. Upload a file above: the IDS scores every flow in it.</p></div>'; return; }
    $("an-list").innerHTML = '<div class="card"><div class="table-wrap"><table id="an-table"><thead><tr><th>File</th><th class="num">Flows scored</th><th class="num">Alerts</th>' +
      '<th>Labels</th><th>Finished</th>' + (isAdmin() ? "<th>By</th>" : "") + '<th><span class="sr-only">Open</span></th></tr></thead><tbody>' +
      a.map(function (x) {
        var i = x.ids || {}, name = x.filename || (x.url ? x.url.split("/").pop() : (x.set || "").split("/").pop());
        return '<tr><td><b class="mono small">' + esc(name) + '</b></td><td class="num">' + fmtNum(i.n_flows_in) + '</td><td class="num">' + fmtNum(i.n_flagged) +
          '</td><td>' + (i.labelled ? '<span class="chip ok">labelled</span>' : '<span class="chip">no labels</span>') + '</td><td class="small">' + esc(fmtDate(x.finished_at)) +
          '</td>' + (isAdmin() ? '<td class="small">' + esc(x.owner || "—") + '</td>' : "") + '<td><a class="btn small-btn" href="#/analyse/' + enc(x.job_id) + '">Open</a></td></tr>';
      }).join("") + '</tbody></table></div></div>';
  });
}

// ---------- upload -> cloud preparation (+ IDS) -> the analysis ----------
function anLog() {
  var log = $("an-log");
  return {
    start: function () { log.innerHTML = ""; log.hidden = false; $("an-error").hidden = true; $("an-go").disabled = true; },
    line: function (msg, cls, when) {
      var li = document.createElement("li"); li.className = "tel-line" + (cls ? " " + cls : "");
      li.textContent = (when ? new Date(when) : new Date()).toTimeString().slice(0, 8) + "  " + msg; log.appendChild(li); log.scrollTop = log.scrollHeight;
    },
    end: function () { $("an-go").disabled = !anCloud(); }
  };
}
$("an-form").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var file = $("an-file").files[0], url = $("an-url").value.trim(), ui = anLog(), err = $("an-error");
  if (AN.src === "file" && !file) { showError(err, { error: "Choose a file first." }); return; }
  if (AN.src === "url" && !/^https:\/\//i.test(url)) { showError(err, { error: "Give a direct https:// link." }); return; }
  var sample = Math.max(1, Math.min(500, parseInt($("an-sample").value || "50", 10) || 50));
  var body = { max_flows: sample, other_attack: false, limits: (CONFIG && CONFIG.limits) || {} };
  if (AN.src === "file") { body.filename = file.name; body.size = file.size; } else body.url = url;
  ui.start();
  ui.line("Sending " + (file ? file.name + " (" + fmtSize(file.size) + ")" : "the link") + " to the preparation service…");
  cpFetch("/api/prep/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(function (r) {
    if (!r.ok) { ui.end(); ui.line(r.data.error || "The analysis could not start.", "tel-failed"); showError(err, r.data); return; }
    var id = r.data.job_id;
    if (AN.src === "url") { location.hash = "#/analyse/" + enc(id); return; }
    ui.line("Uploading " + fmtSize(file.size) + "…");
    fetch(r.data.upload_url, { method: "PUT", body: file, headers: { "content-type": file.type || "application/octet-stream" } }).then(function (u) {
      if (!u.ok) throw new Error("storage answered HTTP " + u.status);
      ui.line("Uploaded. Waking the preparation service (about a minute if it was asleep)…");
      return cpFetch("/api/prep/jobs/" + enc(id) + "/start", { method: "POST" });
    }).then(function (s) {
      ui.end();
      if (!s.ok) { ui.line(s.data.error || "The preparation service did not start.", "tel-failed"); showError(err, s.data); return; }
      location.hash = "#/analyse/" + enc(id);
    }, function (e) { ui.end(); ui.line("Upload failed: " + e.message, "tel-failed"); showError(err, { error: "The upload failed (" + e.message + "). Try again." }); });
  });
});

// ---------- one analysis ----------
function anFile(id, name) {
  return cpFetch("/api/prep/jobs/" + enc(id) + "/files/" + name, { raw: true }).then(function (r) { return r && r.ok ? r.text() : null; }, function () { return null; });
}
// a small CSV reader (quoted fields allowed): [{column: value}]
function anCsv(text) {
  var rows = [], row = [], f = "", q = false, i, c;
  for (i = 0; i < text.length; i++) {
    c = text[i];
    if (q) { if (c === '"') { if (text[i + 1] === '"') { f += '"'; i++; } else q = false; } else f += c; }
    else if (c === '"') q = true;
    else if (c === ",") { row.push(f); f = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(f); rows.push(row); row = []; f = ""; }
    else f += c;
  }
  if (f !== "" || row.length) { row.push(f); rows.push(row); }
  var head = rows.shift() || [];
  return rows.filter(function (r) { return r.length > 1 || r[0] !== ""; }).map(function (r) {
    var o = {}; head.forEach(function (h, j) { o[h] = r[j]; }); return o;
  });
}
function anDetail(id) {
  var token = pollToken, box = $("an-detail");
  if (!AN.job || AN.job.job_id !== id) { AN = Object.assign(AN, { job: null, rows: null, pool: null, ident: null, tau: null, page: 0, seen: 0 }); }
  box.innerHTML = '<p><a href="#/analyse">← All analyses</a></p>' + skel(5, true);
  (function tick() {
    if (token !== pollToken) return;
    cpFetch("/api/prep/jobs/" + enc(id)).then(function (r) {
      if (token !== pollToken) return;
      if (!r.ok) { box.innerHTML = '<p><a href="#/analyse">← All analyses</a></p><div class="notice error">' + esc((r.data || {}).error || "No such analysis.") + '</div>'; return; }
      var j = r.data;
      AN.job = j;
      if (j.status !== "done" && j.status !== "failed") { anProgress(j); setTimeout(tick, 1500); return; }
      if (j.status === "failed") { anProgress(j); return; }
      if (j.files.indexOf("ids_scores.csv") < 0) {
        box.innerHTML = '<p><a href="#/analyse">← All analyses</a></p><div class="card"><h2>' + esc(anName(j)) + '</h2><div class="notice warn">The data was prepared, but ' +
          'no IDS model was stored when it ran, so there is no analysis. An admin can upload one in <a href="#/admin">Admin › IDS model</a>; then analyse the file again.</div></div>';
        return;
      }
      if (AN.rows) { anRender(); return; }
      Promise.all([anFile(id, "ids_scores.csv"), anFile(id, "ids_pool.json"), anFile(id, "ids_flows_identity.csv")]).then(function (fs) {
        if (token !== pollToken) return;
        if (!fs[0] || !fs[1]) { box.innerHTML = '<div class="notice error">The analysis files could not be read.</div>'; return; }
        AN.rows = anCsv(fs[0]).map(function (x) {
          return { flow_id: x.flow_id, score: +x.score, label: x.label === "" || x.label == null ? null : x.label, selected: /^true$/i.test(x.selected) };
        });
        AN.pool = JSON.parse(fs[1]);
        AN.ident = null;
        if (fs[2]) {                                                    // the owner only
          var any = false; AN.ident = {};
          anCsv(fs[2]).forEach(function (x) { AN.ident[x.flow_id] = x; any = any || !!(x.src_ip || x.dst_ip); });
          if (!any) AN.ident = null;                                    // the file had no addresses (e.g. CIC-IDS2017 ML CSVs)
        }
        AN.tau = anRound(AN.pool.threshold);
        anRender();
        allSessions().then(function (s) { AN.sessions = s.ok ? s.data.sessions || [] : []; if (token === pollToken) anProjection(); });
      });
    });
  })();
}
function anRound(v) { return Math.round(v * 10000) / 10000; }
function anName(j) { return j.filename || (j.url ? j.url.split("/").pop() : String(j.set_name || "").split("/").pop()); }
function anProgress(j) {
  var log = (j.log || []).map(function (ev) {
    return '<li class="tel-line">' + esc(new Date(ev.t).toTimeString().slice(0, 8)) + "  " + esc((PHASES[ev.phase] ? PHASES[ev.phase] + (ev.message ? ": " : "") : "") + (ev.message || "")) + '</li>';
  }).join("");
  $("an-detail").innerHTML = '<p><a href="#/analyse">← All analyses</a></p><div class="card"><h2>' + esc(anName(j)) + '</h2>' +
    (j.status === "failed" ? '<div class="notice error" role="alert">' + esc(j.error || "The analysis failed.") + (j.code ? ' <code>' + esc(j.code) + '</code>' : "") + '</div>'
      : '<p class="muted">Preparing the data and scoring every flow with the IDS. The first job after a quiet spell waits about a minute while the preparation service wakes.</p><div class="bar indet"><span></span></div>') +
    '<ol class="tel-log mono" aria-label="Analysis progress">' + log + '</ol></div>';
}
function anRender() {
  var j = AN.job, p = AN.pool, m = p.model || {}, h = p.model_metrics_holdout || {};
  var head = '<p><a href="#/analyse">← All analyses</a></p><div class="page-head" style="margin-top:0"><div><h2 style="margin:0">' + esc(anName(j)) + '</h2>' +
    '<p class="muted small">IDS <span class="mono">' + esc(m.version || m.name || "—") + '</span>' + (m.demo ? ' <span class="chip warn">Demo IDS</span>' : "") +
    ' · its threshold ' + fmtNum(p.threshold, 4) + (h.recall != null ? ' · on its hold-out flows: recall ' + fmtPct(h.recall) + ', false-positive rate ' + fmtPct(h.false_positive_rate) : "") +
    (p.training_overlap ? ' · <span class="chip warn">' + fmtNum(p.training_overlap) + ' flows were in its training data</span>' : "") + '</p></div></div>';
  var bench = '<div class="card" id="an-bench"><h2>Benchmark LLMs on these alerts</h2><p class="muted small">The LLM agents triage the alerts among the ' + fmtNum(p.n_selected) +
    '-flow benchmark sample, using these same scores at your threshold. It needs the GPU server.</p><div class="actions"><button type="button" class="btn primary" id="an-bench-go"' +
    (ONLINE ? "" : " disabled") + '>Benchmark these alerts</button>' + (ONLINE ? "" : '<span class="small muted">GPU offline: start the GPU server first.</span>') +
    '</div><div id="an-bench-msg" class="small" role="status"></div></div>';
  $("an-detail").innerHTML = head +
    '<div class="card"><div class="row" style="margin-top:0;gap:16px;flex-wrap:wrap;align-items:center"><label for="an-tau"><b>Threshold</b></label>' +
    '<input type="range" id="an-tau" min="0.001" max="1" step="0.001" value="' + AN.tau + '" style="flex:1 1 240px" aria-describedby="an-tau-hint">' +
    '<input type="number" id="an-tau-n" min="0.001" max="1" step="0.001" value="' + AN.tau + '" style="max-width:110px" aria-label="Threshold value">' +
    '<button type="button" class="btn small-btn" id="an-tau-reset">The model\'s own</button></div>' +
    '<p class="hint" id="an-tau-hint">A flow with an IDS score at or above the threshold is an alert. Lower: fewer missed attacks, more false alarms.</p>' +
    '<div id="an-figures"></div></div>' +
    '<div class="card"><h2>Projected LLM cost for these alerts</h2><div id="an-proj">' + skel(3) + '</div></div>' +
    bench + '<div class="card"><h2>Alerts</h2><div id="an-alerts"></div></div>';
  var set = function (v) {
    v = anRound(Math.min(1, Math.max(0.001, +v || p.threshold))); AN.tau = v; AN.page = 0;
    $("an-tau").value = v; $("an-tau-n").value = v; anFigures(); anProjection();
  };
  $("an-tau").addEventListener("input", function () { set(this.value); });
  $("an-tau-n").addEventListener("change", function () { set(this.value); });
  $("an-tau-reset").addEventListener("click", function () { set(p.threshold); });
  $("an-bench-go").addEventListener("click", anBenchmark);
  anFigures(); anProjection();
}
function anFigures() {
  var t = INSIGHTS.trafficAnalysis(AN.rows, AN.tau);
  AN.fig = t;
  var tile = function (k, v, w, id) { return '<div class="tile"' + (id ? ' id="' + id + '"' : "") + '><div class="k">' + k + '</div><div class="v">' + v + '</div>' + (w ? '<div class="w">' + w + '</div>' : "") + '</div>'; };
  var tiles = tile("Flows scored", fmtNum(t.n_flows), AN.pool.n_flows_in > t.n_flows ? "" : "every flow of the upload's pool") +
    tile("Alerts", fmtNum(t.flagged), fmtPct(t.flagged_share) + " of the flows", "an-t-alerts") + tile("Cleared", fmtNum(t.cleared), "no LLM call needed") +
    tile("In the benchmark sample", fmtNum(t.alerts_in_benchmark_sample), "alerts an LLM benchmark would triage");
  if (t.labelled) tiles += tile("Attacks caught (recall)", fmtPct(t.recall), fmtNum(t.missed_attacks) + " missed", "an-t-recall") +
    tile("Alerts that are attacks (precision)", fmtPct(t.precision), fmtNum(t.false_alarms) + " false alarms") +
    tile("False-positive rate", fmtPct(t.false_positive_rate), "benign flows flagged");
  var cls = t.labelled && t.per_class.length ? '<h3>Recall per attack class</h3><div class="table-wrap"><table id="an-classes"><thead><tr><th>Class</th><th class="num">Flows</th><th class="num">Caught</th></tr></thead><tbody>' +
    t.per_class.map(function (c) { return '<tr><td>' + esc(c.cls) + '</td><td class="num">' + fmtNum(c.n) + '</td><td class="num">' + fmtPct(c.recall) + '</td></tr>'; }).join("") + '</tbody></table></div>'
    : (t.labelled ? "" : '<p class="small muted">No labels in this file (PCAP or unlabelled CSV): the alerts are shown, but what the IDS missed cannot be known.</p>');
  $("an-figures").innerHTML = '<div class="tiles">' + tiles + '</div>' + cls;
  anAlerts();
}
function anAlerts() {
  var alerts = AN.rows.filter(function (r) { return r.score >= AN.tau; }).sort(function (a, b) { return b.score - a.score; });
  var from = AN.page * AN_PAGE, pageRows = alerts.slice(from, from + AN_PAGE), id = AN.ident, lab = AN.fig.labelled;
  var addr = function (r) {
    var x = id && id[r.flow_id];
    if (!x) return "—";
    return esc((x.src_ip || "?") + (x.src_port ? ":" + x.src_port : "") + " → " + (x.dst_ip || "?") + (x["Destination Port"] ? ":" + x["Destination Port"] : "")) +
      (x.protocol_name ? ' <span class="muted">' + esc(x.protocol_name) + '</span>' : "");
  };
  $("an-alerts").innerHTML = !alerts.length ? '<p class="muted">No flow reaches this threshold.</p>' :
    '<p class="small muted" style="margin-top:0">Highest score first. ' + (id ? "Addresses are shown to you only (you uploaded the file)." : "Source and destination addresses are shown only to the person who uploaded the file, when the file has them.") + '</p>' +
    '<div class="table-wrap"><table id="an-alerts-table"><thead><tr><th>Flow</th><th class="num">IDS score</th>' + (lab ? "<th>Label</th>" : "") +
    (id ? "<th>Source → destination</th>" : "") + '<th>In the benchmark sample</th></tr></thead><tbody>' +
    pageRows.map(function (r) {
      return '<tr><td class="mono small">' + esc(r.flow_id) + '</td><td class="num">' + fmtNum(r.score, 3) + '</td>' +
        (lab ? '<td>' + (r.label === "Benign" ? '<span class="chip warn">Benign (false alarm)</span>' : esc(r.label)) + '</td>' : "") +
        (id ? '<td class="mono small">' + addr(r) + '</td>' : "") + '<td>' + (r.selected ? "yes" : "") + '</td></tr>';
    }).join("") + '</tbody></table></div>' +
    (alerts.length > AN_PAGE ? '<div class="row" style="gap:10px;align-items:center"><button type="button" class="btn small-btn" id="an-prev"' + (AN.page ? "" : " disabled") + '>Previous</button>' +
      '<span class="small">' + fmtNum(from + 1) + "–" + fmtNum(Math.min(from + AN_PAGE, alerts.length)) + " of " + fmtNum(alerts.length) + '</span>' +
      '<button type="button" class="btn small-btn" id="an-next"' + (from + AN_PAGE < alerts.length ? "" : " disabled") + '>Next</button></div>' : "");
  if ($("an-prev")) $("an-prev").onclick = function () { AN.page--; anAlerts(); };
  if ($("an-next")) $("an-next").onclick = function () { AN.page++; anAlerts(); };
}
function anProjection() {
  if (!$("an-proj") || !AN.fig) return;
  if (!AN.sessions) return;
  var alerts = AN.fig.flagged, rows = INSIGHTS.projectCost(AN.sessions, { flows: alerts });
  $("an-proj").innerHTML = !rows.length ? '<p class="muted small" id="an-proj-none">No finished benchmark to project from yet. Once a model has been benchmarked on a GPU, ' +
      'this shows what triaging these ' + fmtNum(alerts) + ' alerts would cost with it.</p>' :
    '<p class="small muted" style="margin-top:0">For the ' + fmtNum(alerts) + ' alerts at this threshold, from each model\'s latest benchmark on that GPU (time and cost per flow as measured there). A projection: the same GPU, settings and similar flows.</p>' +
    '<div class="table-wrap"><table id="an-proj-table"><thead><tr><th>Model</th><th>GPU</th><th class="num">GPU time</th><th class="num">Cost</th><th class="num">Energy</th><th>Measured in</th></tr></thead><tbody>' +
    rows.map(function (x) {
      return '<tr><td>' + esc(short(x.model)) + '</td><td class="small">' + esc(x.gpu) + '</td><td class="num">' + anHours(x.gpu_hours) + '</td><td class="num">' +
        (x.cost_usd == null ? "—" : "$" + fmtNum(x.cost_usd, 2)) + '</td><td class="num">' + (x.energy_wh == null ? "—" : fmtNum(x.energy_wh / 1000, 2) + " kWh") +
        '</td><td class="small"><a class="mono" href="#/session/' + enc(x.source_session) + '">' + esc(shortId(x.source_session)) + '</a></td></tr>';
    }).join("") + '</tbody></table></div>' +
    '<p class="small"><a href="#/planner?flows=' + enc(String(alerts)) + '">Plan for a daily volume of alerts like this →</a></p>';
}
function anHours(h) { return h == null ? "—" : h < 1 ? fmtNum(h * 60, 1) + " min" : fmtNum(h, 1) + " h"; }
// the prepared set (with its scores, never the addresses) goes to the GPU server; then model choice
function anBenchmark() {
  var j = AN.job, msg = $("an-bench-msg"), btn = $("an-bench-go");
  btn.disabled = true; msg.textContent = "Sending the prepared flows and their IDS scores to the GPU server…";
  cloudImport(j).then(function (imp) {
    btn.disabled = false;
    if (!imp.ok) { msg.textContent = "The GPU server did not accept it: " + ((imp.data || {}).error || imp.status); return; }
    location.hash = "#/new/models?set=" + enc(imp.data.prepared_set) + "&ids=" + enc(String(AN.tau));
  });
}
