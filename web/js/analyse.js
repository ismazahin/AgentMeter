// AgentMeter web app — analyse.js: New Benchmark steps 2-3 (#/new/prep/<job>): the upload prepared and
// scored by the IDS on the preparation service (no GPU), then "Your data" and Continue to the models.
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
// ---------- the preparation service (scripts/prep_local.py on the owner's PC) ----------
// Continue starts it when it is not running: the click opens agentmeter-prep://start, which
// `python scripts/prep_local.py install` registered on that PC (the browser asks once, "Open
// AgentMeter prep?"). The page then watches /api/backend until the service has registered and
// carries on with the upload by itself. The Stop link opens agentmeter-prep://stop the same way.
var AN_SVC = { waiting: null, until: 0, hintAt: 0, then: null };
var AN_SVC_HINT_MS = 20000, AN_SVC_WAIT_MS = 120000;
var AN_INSTALL_HINT = 'First time on this PC? Run <code>python scripts/prep_local.py install</code> once in the AgentMeter folder, then press Continue again.';
function anServiceNotice() {
  var un = $("an-unavailable"), svc = BACKEND && BACKEND.prep_service, w = AN_SVC.waiting;
  if ($("svc-upload-btn") && viaService()) $("svc-upload-btn").disabled = w === "start";
  if (!CP) { un.hidden = false; un.className = "notice warn"; un.innerHTML = "Analyse traffic needs the control plane on the hosted site. On a single GPU server, use New benchmark."; return; }
  if (anCloud()) {
    un.hidden = !(svc && svc.online);
    un.className = "notice small";
    un.innerHTML = 'Preparation service running on your computer. <a href="agentmeter-prep://stop" id="an-svc-stop">Stop it</a>' +
      (w === "stop" ? ' <span class="muted" id="an-svc-wait">Stopping…</span>' : "");
    return;
  }
  if (w === "start") {
    un.hidden = false; un.className = "notice small";
    un.innerHTML = '<span id="an-svc-wait" role="status">Starting the preparation service on your computer… Your browser may ask to open AgentMeter prep. Tick <i>Always allow</i>.</span>' +
      (Date.now() > AN_SVC.hintAt ? '<p class="small" id="an-svc-hint" style="margin:6px 0 0">' + AN_INSTALL_HINT + '</p>' : "");
    return;
  }
  if (w === "late") {
    un.hidden = false; un.className = "notice warn";
    un.innerHTML = '<p id="an-svc-late" role="alert" style="margin:0"><b>The preparation service did not start.</b> ' + AN_INSTALL_HINT +
      ' If it is installed, check <code>results/prep_local.log</code>, or run <code>python scripts/prep_local.py</code> in a terminal to see why.</p>';
    return;
  }
  un.hidden = true; un.innerHTML = "";              // not running: Continue starts it
}
// open agentmeter-prep://start from the Continue click (a user action, so the browser allows it)
function anLaunchService() {
  var a = document.createElement("a");
  a.href = "agentmeter-prep://start"; a.id = "an-svc-start"; a.hidden = true;
  document.body.appendChild(a); a.click(); a.remove();
}
function anWatchService(what, then) {
  AN_SVC.waiting = what; AN_SVC.until = Date.now() + AN_SVC_WAIT_MS; AN_SVC.hintAt = Date.now() + AN_SVC_HINT_MS; AN_SVC.then = then || null;
  anServiceNotice();
  var token = pollToken;
  (function tick() {
    if (token !== pollToken) { AN_SVC.waiting = null; AN_SVC.then = null; return; }
    cpFetch("/api/backend").then(function (r) {
      if (token !== pollToken) return;
      if (r.ok) BACKEND = r.data;
      var up = anCloud();
      if ((what === "start" && up) || (what === "stop" && !up)) {
        var then = AN_SVC.then;
        AN_SVC.waiting = null; AN_SVC.then = null; anServiceNotice();
        if (up) cpFetch("/api/prep/wake", { method: "POST" }).then(function () { /* best effort */ });
        if (then) then();
        return;
      }
      if (Date.now() > AN_SVC.until) { AN_SVC.waiting = what === "start" ? "late" : null; AN_SVC.then = null; anServiceNotice(); return; }
      anServiceNotice();                             // shows the install hint once 20 seconds have passed
      setTimeout(tick, 3000);
    });
  })();
}
document.addEventListener("click", function (ev) {
  var a = ev.target.closest && ev.target.closest("#an-svc-stop");
  if (a) anWatchService("stop");                     // the link itself opens (the browser asks)
});

// ---------- the list of analyses ----------
function anList() {
  $("an-list").innerHTML = skel(3, true);
  cpFetch("/api/analyses").then(function (r) {
    if (!r.ok) { $("an-list").innerHTML = '<div class="notice error">' + esc((r.data || {}).error || "Could not load your analyses.") + '</div>'; return; }
    var a = r.data.analyses || [];
    if (!a.length) { $("an-list").innerHTML = '<div class="card empty" id="an-empty"><p>No analyses yet. Upload a file on the first tab. It is prepared and the IDS scores every flow in it.</p></div>'; return; }
    $("an-list").innerHTML = '<div class="card"><div class="table-wrap"><table id="an-table"><thead><tr><th>File</th><th class="num">Flows scored</th><th class="num">Alerts</th>' +
      '<th>Labels</th><th>Finished</th>' + (isAdmin() ? "<th>By</th>" : "") + '<th><span class="sr-only">Open</span></th></tr></thead><tbody>' +
      a.map(function (x) {
        var i = x.ids || {}, name = x.filename || (x.url ? x.url.split("/").pop() : (x.set || "").split("/").pop());
        return '<tr><td><b class="mono small">' + esc(name) + '</b></td><td class="num">' + fmtNum(i.n_flows_in) + '</td><td class="num">' + fmtNum(i.n_flagged) +
          '</td><td>' + (i.labelled ? '<span class="chip ok">labelled</span>' : '<span class="chip">no labels</span>') + '</td><td class="small">' + esc(fmtDate(x.finished_at)) +
          '</td>' + (isAdmin() ? '<td class="small">' + esc(x.owner || "—") + '</td>' : "") + '<td><a class="btn small-btn" href="#/new/prep/' + enc(x.job_id) + '">Open</a></td></tr>';
      }).join("") + '</tbody></table></div></div>';
  });
}

// ---------- step 1 -> 2: the wizard's upload goes to the preparation service ----------
function startAnalysis(src) {
  var err = $("svc-upload-error"), btn = $("svc-upload-btn"), busy = $("svc-upload-busy");
  err.hidden = true;
  if (!anCloud()) {                                  // not running: start it, then carry on with this upload
    if (AN_SVC.waiting === "start") return;
    anLaunchService();
    anWatchService("start", function () { startAnalysis(src); });
    return;
  }
  var maxB = ((BACKEND && BACKEND.cloud_prep_max_mb) || 50) * 1024 * 1024;
  if (src.file && src.file.size > maxB) {
    showError(err, { error: "This file is " + fmtSize(src.file.size) + "; uploads are limited to " + fmtSize(maxB) + ". Use Import from a link for larger files.", code: "too_large" });
    return;
  }
  var body = { max_flows: Math.max(1, Math.min(500, parseInt($("svc-max-flows").value || "50", 10) || 50)),
               other_attack: false, limits: (CONFIG && CONFIG.limits) || {} };
  if (src.file) { body.filename = src.file.name; body.size = src.file.size; } else body.url = src.url;
  btn.disabled = true; busy.hidden = false; busy.textContent = "Sending to the preparation service…";
  var fail = function (d) { btn.disabled = false; busy.hidden = true; showError(err, d); };
  cpFetch("/api/prep/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(function (r) {
    if (!r.ok) { fail(r.data); return; }
    var id = r.data.job_id;
    if (!src.file) { busy.hidden = true; btn.disabled = false; location.hash = "#/new/prep/" + enc(id); return; }
    busy.textContent = "Uploading " + fmtSize(src.file.size) + "…";
    fetch(r.data.upload_url, { method: "PUT", body: src.file, headers: { "content-type": src.file.type || "application/octet-stream" } }).then(function (u) {
      if (!u.ok) throw new Error("storage answered HTTP " + u.status);
      busy.textContent = "Uploaded. Starting the preparation…";
      return cpFetch("/api/prep/jobs/" + enc(id) + "/start", { method: "POST" });
    }).then(function (st) {
      if (!st.ok) { fail(st.data); return; }
      btn.disabled = false; busy.hidden = true;
      location.hash = "#/new/prep/" + enc(id);
    }, function (e) { fail({ error: "The upload failed (" + e.message + "). Try again.", code: "upload_failed" }); });
  });
}

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
  box.innerHTML = '<p><a href="#/new">← Upload another file</a></p>' + skel(5, true);
  (function tick() {
    if (token !== pollToken) return;
    cpFetch("/api/prep/jobs/" + enc(id)).then(function (r) {
      if (token !== pollToken) return;
      if (!r.ok) { box.innerHTML = '<p><a href="#/new">← Upload another file</a></p><div class="notice error">' + esc((r.data || {}).error || "No such analysis.") + '</div>'; return; }
      var j = r.data;
      AN.job = j;
      if (j.status !== "done" && j.status !== "failed") { wizStep("prep"); anProgress(j); setTimeout(tick, 1500); return; }
      if (j.status === "failed") { wizStep("prep"); anProgress(j); return; }
      wizStep("review");
      if (j.files.indexOf("ids_scores.csv") < 0) {
        AN.pool = null; AN.rows = null;
        anFile(id, "manifest.json").then(function (m) {
          if (token !== pollToken) return;
          try { AN.manifest = m ? JSON.parse(m) : null; } catch (e) { AN.manifest = null; }
          box.innerHTML = '<p><a href="#/new">← Upload another file</a></p><h2>' + esc(anName(j)) + '</h2>' + anContents() +
            '<div class="notice warn">No IDS model was stored when this was prepared, so there are no IDS results. An admin can upload one in ' +
            '<a href="#/admin">Admin › IDS model</a>. You can still benchmark every flow of the sample.</div>' + anContinue(false);
          $("an-bench-go").addEventListener("click", anBenchmark);
        });
        return;
      }
      if (AN.rows) { anRender(); return; }
      Promise.all([anFile(id, "ids_scores.csv"), anFile(id, "ids_pool.json"), anFile(id, "ids_flows_identity.csv"), anFile(id, "manifest.json")]).then(function (fs) {
        if (token !== pollToken) return;
        if (!fs[0] || !fs[1]) { box.innerHTML = '<div class="notice error">The analysis files could not be read.</div>'; return; }
        AN.rows = anCsv(fs[0]).map(function (x) {
          return { flow_id: x.flow_id, score: +x.score, label: x.label === "" || x.label == null ? null : x.label, selected: /^true$/i.test(x.selected) };
        });
        AN.pool = JSON.parse(fs[1]);
        try { AN.manifest = fs[3] ? JSON.parse(fs[3]) : null; } catch (e) { AN.manifest = null; }
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
  $("an-detail").innerHTML = '<p><a href="#/new">← Upload another file</a></p><div class="card"><h2>' + esc(anName(j)) + '</h2>' +
    (j.status === "failed" ? '<div class="notice error" role="alert">' + esc(j.error || "The analysis failed.") + (j.code ? ' <code>' + esc(j.code) + '</code>' : "") + '</div>'
      : '<p class="muted">Preparing the data and scoring every flow with the IDS. The first job after a quiet spell waits about a minute while the preparation service wakes.</p><div class="bar indet"><span></span></div>') +
    '<ol class="tel-log mono" aria-label="Analysis progress">' + log + '</ol></div>';
}
function anRender() {
  var j = AN.job, p = AN.pool, m = p.model || {}, h = p.model_metrics_holdout || {};
  var head = '<p><a href="#/new">← Upload another file</a></p><div class="page-head" style="margin-top:0"><div><h2 style="margin:0">' + esc(anName(j)) + '</h2>' +
    '<p class="muted small">IDS <span class="mono">' + esc(m.version || m.name || "—") + '</span>' + (m.demo ? ' <span class="chip warn">Demo IDS</span>' : "") +
    ', threshold ' + fmtNum(p.threshold, 4) + (h.recall != null ? '. On its hold-out flows it has recall ' + fmtPct(h.recall) + ', false-positive rate ' + fmtPct(h.false_positive_rate) : "") +
    (p.training_overlap ? ' <span class="chip warn">' + fmtNum(p.training_overlap) + ' flows were in its training data</span>' : "") + '</p></div></div>';
  var bench = anContinue(true);
  $("an-detail").innerHTML = head + anContents() +
    '<div class="card"><div class="row" style="margin-top:0;gap:16px;flex-wrap:wrap;align-items:center"><label for="an-tau"><b>Threshold</b></label>' +
    '<input type="range" id="an-tau" min="0.001" max="1" step="0.001" value="' + AN.tau + '" style="flex:1 1 240px" aria-describedby="an-tau-hint">' +
    '<input type="number" id="an-tau-n" min="0.001" max="1" step="0.001" value="' + AN.tau + '" style="max-width:110px" aria-label="Threshold value">' +
    '<button type="button" class="btn small-btn" id="an-tau-reset">The model\'s own</button></div>' +
    '<p class="hint" id="an-tau-hint">A flow with an IDS score at or above the threshold is an alert. A lower threshold misses fewer attacks but raises more false alarms.</p>' +
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
// "What is in it": the prepared set's manifest (file, rows, flows kept, labels, class mix)
function anContents() {
  var ps = (AN.manifest || {}).prepared_set || {}, sel = ps.selection || {}, cc = ps.class_counts || {}, src = ps.source || {};
  var mix = cc.available ? cc.selected || {} : {}, keys = Object.keys(mix).sort(function (a, b) { return mix[b] - mix[a]; });
  var tot = keys.reduce(function (a, k) { return a + mix[k]; }, 0);
  return '<div class="card" id="an-contents"><h2>What is in it</h2><div class="grid2"><div><ul class="lk-list small">' +
    '<li>File: <b>' + esc(src.filename || anName(AN.job)) + '</b>' + (src.size_bytes ? " (" + fmtSize(src.size_bytes) + ")" : "") + '</li>' +
    '<li>Type: <b>' + esc(String(ps.input_type || "—").toUpperCase()) + '</b></li>' +
    '<li>Flows read: <b>' + fmtNum(sel.pool_size) + '</b>, benchmark sample <b>' + fmtNum(sel.selected) + '</b> flows</li>' +
    '<li>Labels: <b>' + (cc.available ? "yes, so accuracy can be checked" : "no, so efficiency only") + '</b></li>' +
    (ps.content_sha256 ? '<li>Fingerprint: <span class="mono">' + esc(ps.content_sha256.slice(0, 16)) + '…</span></li>' : "") + '</ul></div>' +
    '<div>' + (keys.length ? '<h3 style="margin-top:0">Benchmark sample by class</h3><div class="hbars">' + keys.map(function (k) {
      return '<div class="hbar"><span class="hb-l">' + esc(k) + '</span><span class="hb-t"><span class="hb-f" style="width:' + (100 * mix[k] / tot).toFixed(1) + '%"></span></span><span class="hb-v num">' + fmtNum(mix[k]) + '</span></div>';
    }).join("") + '</div>' : '<p class="muted small">No class labels (PCAP or unlabelled CSV).</p>') + '</div></div></div>';
}
// step 3 -> 4: needs the GPU server (the set goes to it; then models and settings)
function anContinue(ids) {
  return '<div class="card" id="an-bench"><h2>Next, choose models and settings</h2><p class="muted small">' +
    (ids ? 'The LLM agents triage the alerts among the ' + fmtNum((AN.pool || {}).n_selected) + '-flow benchmark sample, using these same IDS scores at your threshold.'
         : 'The LLM agents analyse the flows of the benchmark sample.') + '</p>' +
    '<div class="actions"><button type="button" class="btn primary" id="an-bench-go"' + (ONLINE ? "" : " disabled") + '>Continue</button>' +
    (ONLINE ? "" : '<span class="small" id="an-gpu-off">To run a benchmark, the GPU server must be running first.</span>') +
    '</div><div id="an-bench-msg" class="small" role="status"></div></div>';
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
    : (t.labelled ? "" : '<p class="small muted">This file has no labels (PCAP or unlabelled CSV). The alerts are shown, but what the IDS missed cannot be known.</p>');
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
    '<p class="small muted" style="margin-top:0">For the ' + fmtNum(alerts) + ' alerts at this threshold, from each model\'s latest benchmark on that GPU (time and cost per flow as measured there). It is a projection that assumes the same GPU, the same settings and similar flows.</p>' +
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
    location.hash = "#/new/models?set=" + enc(imp.data.prepared_set) + (AN.pool ? "&ids=" + enc(String(AN.tau)) : "");
  });
}
