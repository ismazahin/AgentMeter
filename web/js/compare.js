// AgentMeter web app — compare.js: Leaderboard › Compare models, Data sets, GPU & Queue, Leaderboard.
// One of the app's scripts (web/app.html loads them in order: core, home, wizard, session, analysis,
// compare, settings, boot); they share the page's global scope, so a function defined in one is used by the others.
"use strict";
// ---------- COMPARE ----------
// Compare: one session + the sessions on the SAME setup (same flows hash, GPU, settings) —
// only those are offered. The merged view shows every run side by side (stored values).
var CMP = { rows: [], data: null, tab: "overview", setups: [], key: null, given: [], want: [], pick: {}, old: null };
function sessionLabel(s) {
  return (s.models || []).map(short).join(" vs ") + " · " + (s.input_type || "") + " · " + fmtNum(s.n_flows) + " flows · " +
    (s.gpu || "") + " · " + fmtDate(s.finished_at || s.created_at);
}
// ---- variables (as on an observability dashboard): one Model and one Pipeline filter, shared
// by Compare and Trends; "All" shows everything. Kept in this browser.
var VARS = (function () { try { return JSON.parse(localStorage.getItem("agentmeter.vars") || "{}") || {}; } catch (e) { return {}; } })();
function pipeName(s) { return s.pipeline && s.pipeline.length ? s.pipeline.join(" → ") : "Default (4 agents)"; }
function varsMatch(s, model) {
  return (!VARS.model || (model ? model === VARS.model : (s.models || []).indexOf(VARS.model) >= 0)) && (!VARS.pipeline || pipeName(s) === VARS.pipeline);
}
function varsBar(host, sessions, onChange) {
  var models = [], pipes = [];
  sessions.forEach(function (s) {
    (s.models || []).forEach(function (m) { if (models.indexOf(m) < 0) models.push(m); });
    var p = pipeName(s); if (pipes.indexOf(p) < 0) pipes.push(p);
  });
  if (VARS.model && models.indexOf(VARS.model) < 0) VARS.model = "";
  if (VARS.pipeline && pipes.indexOf(VARS.pipeline) < 0) VARS.pipeline = "";
  var sel = function (id, label, vals, cur, fmt) {
    return '<div class="field"><label for="' + id + '">' + label + '</label><select id="' + id + '"><option value="">All</option>' +
      vals.sort().map(function (v) { return '<option value="' + esc(v) + '"' + (v === cur ? " selected" : "") + '>' + esc(fmt ? fmt(v) : v) + '</option>'; }).join("") + '</select></div>';
  };
  host.innerHTML = '<div class="card row vars-row">' + sel(host.id + "-model", "Model", models, VARS.model, short) + sel(host.id + "-pipeline", "Pipeline", pipes, VARS.pipeline) + '</div>';
  [["model", host.id + "-model"], ["pipeline", host.id + "-pipeline"]].forEach(function (k) {
    $(k[1]).addEventListener("change", function () {
      VARS[k[0]] = this.value;
      try { localStorage.setItem("agentmeter.vars", JSON.stringify(VARS)); } catch (e) { /* not kept */ }
      onChange();
    });
  });
}
// Compare: setup first (one prepared set, one GPU, one group of settings), then the models
// measured on it. Only the model differs; a model measured several times is one column (its
// latest run, or the run the link names) — how its runs vary is the Repeatability page's job.
function cmpRow(id) { return CMP.rows.filter(function (s) { return s.session_id === id; })[0] || null; }
function cmpSetup() { return (CMP.setups || []).filter(function (g) { return g.key === CMP.key; })[0] || null; }
function setupName(g) {
  var st = g.setup;
  return (g.name || "prepared set " + String(st.prepared_set_sha256).slice(0, 8)) + " · " + (st.input_type || "") + " · " + fmtNum(st.n_flows) + " flows · " + (st.gpu || "GPU") + " · " + g.pipeline;
}
function repLink(model, key) { return "#/trends?model=" + enc(model) + "&setup=" + enc(key); }
function loadCompare(q) {
  $("cmp-error").hidden = true;
  if (q.a && q.b && !q.sessions) { location.replace("#/compare?sessions=" + enc(q.a) + "," + enc(q.b)); return; }   // old links
  var ids = (q.sessions || "").split(",").filter(Boolean), first = ids[0] || q.a || "";
  var want = (q.models || "").split(",").filter(Boolean);
  (function () {
    var want = ids.length ? ids : first ? [first] : [];
    return allSessions().then(function (r) {           // a session named in the link but not in a cached list: read again
      var miss = r.ok && want.some(function (id) { return !(r.data.sessions || []).some(function (x) { return x.session_id === id; }); });
      return miss ? allSessions(true) : r;
    });
  })().then(function (r) {
    if (!r.ok) { showError($("cmp-error"), r.data); return; }
    CMP.rows = (r.data.sessions || []).filter(function (s) { return s.status === "Done" || s.status === "Demo"; });
    CMP.setups = window.INSIGHTS ? INSIGHTS.compareSetups(CMP.rows).setups : [];
    CMP.given = ids.length ? ids : first ? [first] : [];
    CMP.want = want;
    CMP.old = null;
    $("cmp-result").innerHTML = ""; $("cmp-excluded").innerHTML = "";
    var target = first ? cmpRow(first) : null;
    if (first) {
      CMP.key = target ? INSIGHTS.setupKey(target) : null;
      if (target && !CMP.key) CMP.old = target;
      // no models= in the link: every model of the setup (Compare is for ranking many models at once)
      if (!CMP.want.length && CMP.key) CMP.want = ((CMP.setups.filter(function (g) { return g.key === CMP.key; })[0] || {}).models || []).map(function (m) { return m.model; });
    }
    var off = ids.filter(function (id) { var s = cmpRow(id); return !s || !CMP.key || INSIGHTS.setupKey(s) !== CMP.key; });
    varsBar($("cmp-vars"), CMP.rows, fillSetups);
    fillSetups();
    if (!CMP.rows.length) { $("cmp-result").innerHTML = '<div class="card empty"><p>No finished sessions yet.</p><a class="btn primary" href="#/new">New benchmark</a></div>'; return; }
    if (off.length) {
      showError($("cmp-error"), { error: "These sessions do not share the same flows, GPU and settings, so they are not compared (" + off.map(shortId).join(", ") +
        "). Choose a setup below and tick the models measured on it." });
      return;
    }
    if (ids.length && CMP.want.length >= 2) runPicked();
  });
}
function fillSetups() {
  var all = CMP.setups || [], shown = all.filter(function (g) { return g.key === CMP.key || g.sessions.some(function (s) { return varsMatch(s); }); });
  $("cmp-setup").innerHTML = '<option value="">Choose a setup… (' + shown.length + (shown.length === all.length ? "" : " of " + all.length) + ')</option>' + shown.map(function (g) {
    return '<option value="' + esc(g.key) + '"' + (g.key === CMP.key ? " selected" : "") + '>' + esc(setupName(g) + " — " + g.models.length + " model" + (g.models.length === 1 ? "" : "s")) + '</option>';
  }).join("");
  renderSetup();
}
$("cmp-setup").addEventListener("change", function () {
  CMP.key = this.value || null; CMP.given = []; CMP.want = []; CMP.old = null;
  $("cmp-result").innerHTML = ""; $("cmp-error").hidden = true;
  renderSetup();
});
function renderSetup() {
  var g = cmpSetup(), box = $("cmp-cands");
  $("cmp-go").disabled = true;
  if (CMP.old) {
    $("cmp-locked").innerHTML = ""; $("cmp-excluded").innerHTML = "";
    box.innerHTML = '<p class="notice warn small" id="cmp-none">This session predates the setup fingerprint, so it cannot be compared.</p>';
    return;
  }
  if (!g) { $("cmp-locked").innerHTML = ""; box.innerHTML = ""; $("cmp-excluded").innerHTML = ""; return; }
  var st = g.setup, ok = '<span class="lk-ok" aria-hidden="true">✓</span> ';
  $("cmp-locked").innerHTML = '<div class="validity ok locked" id="cmp-locked-box" role="status"><strong>Locked conditions</strong>' +
    '<ul class="lk-list">' +
    '<li>' + ok + 'Data set: <b>' + esc(g.name || "—") + '</b> (' + esc(st.input_type || "") + ')</li>' +
    '<li>' + ok + 'Flows: <b>' + fmtNum(st.n_flows) + '</b>, the same flows for every model (set <span class="mono">' + esc(String(st.prepared_set_sha256).slice(0, 12)) + '</span>)</li>' +
    '<li>' + ok + 'GPU: <b>' + esc(st.gpu || "—") + '</b></li>' +
    '<li>' + ok + 'Pipeline: <b>' + esc(g.pipeline) + '</b></li>' +
    '<li>' + ok + 'Settings: <b>4-bit NF4</b>, the same token caps and engine (settings <span class="mono">' + esc(String(st.settings_fingerprint).slice(0, 8)) + '</span>)</li>' +
    '</ul><p class="small" style="margin:6px 0 0"><b>Only the LLM model differs.</b></p></div>';
  var one = g.models.length < 2;
  if (one) {
    var last = g.sessions[0];
    box.innerHTML = '<div class="notice" id="cmp-none"><b>Only one model (' + esc(short(g.models[0].model)) + ') has been measured on this setup.</b> Run these flows again with other models to compare them.' +
      (g.models[0].runs.length > 1 ? ' Its ' + g.models[0].runs.length + ' runs are on <a href="' + repLink(g.models[0].model, g.key) + '">Repeatability</a>.' : "") +
      (last.run_name ? '<div class="actions"><a class="btn" href="#/new/models?set=' + enc(last.run_name) + '">Benchmark these flows again</a></div>' : "") + '</div>';
  } else {
    var want = CMP.want.filter(function (m) { return g.models.some(function (x) { return x.model === m; }); });
    if (!want.length) want = g.models.map(function (x) { return x.model; });              // every model, by default
    box.innerHTML = '<fieldset style="border:0;padding:0;margin:12px 0 0"><legend class="small" style="font-weight:600"><span class="step-n">2</span> Models measured on this setup (' + g.models.length + ') — tick 2 or more</legend>' +
      '<div class="row" style="margin:4px 0 8px;gap:8px"><button type="button" class="btn small-btn" id="cmp-all">Select all</button><button type="button" class="btn small-btn" id="cmp-clear">Clear</button></div>' +
      '<div class="models" id="cmp-list">' + g.models.map(function (m) {
        var on = want.indexOf(m.model) >= 0;
        return '<label class="model"><input type="checkbox" value="' + esc(m.model) + '"' + (on ? " checked" : "") + '><span><b>' + esc(short(m.model)) + '</b>' + extraTag(m.model) +
          '<br><span class="muted small">' + (m.runs.length === 1 ? "1 run · " + esc(fmtDate(m.runs[0].at)) : m.runs.length + ' runs · latest ' + esc(fmtDate(m.runs[0].at)) +
            ' · <a href="' + repLink(m.model, g.key) + '" data-rep-link>how they vary</a>') + '</span></span></label>';
      }).join("") + '</div></fieldset>' +
      '<p class="small muted" id="cmp-pick-note" style="margin:6px 0 0">Each model is one row, from its latest run on this setup. Whether a model gives the same result every time is on <a href="#/trends">Repeatability</a>.</p>';
    var sync = function () {
      var on = box.querySelectorAll("input:checked").length;
      $("cmp-go").disabled = on < 2;
      $("cmp-go").textContent = on >= 2 ? "Compare " + on + " models" : "Compare";
    };
    box.querySelectorAll("input").forEach(function (x) { x.addEventListener("change", sync); });
    $("cmp-all").addEventListener("click", function () { box.querySelectorAll("input").forEach(function (x) { x.checked = true; }); sync(); });
    $("cmp-clear").addEventListener("click", function () { box.querySelectorAll("input").forEach(function (x) { x.checked = false; }); sync(); });
    sync();
  }
  var ex = INSIGHTS.excludedFrom(g, CMP.rows);
  $("cmp-excluded").innerHTML = !ex.length ? "" : '<details class="card info" id="cmp-excluded-card"><summary>Not on this setup (' + ex.length + ' session' + (ex.length === 1 ? "" : "s") + ') — left out of Compare</summary>' +
    '<div class="table-wrap"><table id="cmp-excluded-table"><thead><tr><th>Session</th><th>Models</th><th>Data set</th><th>GPU</th><th>Why it is left out</th></tr></thead><tbody>' +
    ex.slice(0, 100).map(function (x) {
      var s = x.session;
      return '<tr><td><a class="mono" href="#/session/' + enc(s.session_id) + '">' + esc(shortId(s.session_id)) + '</a></td><td class="small">' + esc((s.models || []).map(short).join(", ")) +
        '</td><td class="small">' + esc((s.run_name || "—") + " · " + fmtNum(s.n_flows) + " flows") + '</td><td class="small">' + esc(s.gpu || "—") + '</td><td>' + esc(x.reasons.join("; ")) + '</td></tr>';
    }).join("") + '</tbody></table></div>' + (ex.length > 100 ? '<p class="small muted">The first 100 are listed.</p>' : "") + '</details>';
}
// the session that stands for each model: the latest one the link names, else its latest run
function cmpPick(g, models) {
  var pick = {};
  models.forEach(function (m) {
    var row = g.models.filter(function (x) { return x.model === m; })[0];
    if (!row) return;
    var named = row.runs.filter(function (r) { return CMP.given.indexOf(r.session_id) >= 0; })[0];
    pick[m] = (named || row.runs[0]).session_id;
  });
  return pick;
}
function runPicked() {
  var g = cmpSetup();
  if (!g) return;
  var pick = cmpPick(g, CMP.want), ids = [];
  Object.keys(pick).forEach(function (m) { if (ids.indexOf(pick[m]) < 0) ids.push(pick[m]); });
  if (Object.keys(pick).length < 2) { showError($("cmp-error"), { error: "Tick at least two models measured on this setup." }); return; }
  if (ids.length === 1) {                    // every model came from one session: merged needs two, so add another of this setup
    var other = g.sessions.filter(function (s) { return s.session_id !== ids[0]; })[0];
    if (!other) {
      $("cmp-result").innerHTML = '<div class="notice" id="cmp-one-session">These models were measured together in one session, which already compares them: <a href="#/session/' + enc(ids[0]) + '">open it</a>.</div>';
      return;
    }
    ids.push(other.session_id);
  }
  if (ids.length > 20) { showError($("cmp-error"), { error: "These models come from " + ids.length + " sessions; up to 20 sessions can be merged at once. Untick a few models." }); return; }
  runMerged(ids, pick);
}
$("cmp-form").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var g = cmpSetup(), models = Array.prototype.map.call($("cmp-cands").querySelectorAll("input:checked"), function (x) { return x.value; });
  if (!g || models.length < 2) { showError($("cmp-error"), { error: "Choose a setup and tick at least two models measured on it." }); return; }
  var pick = cmpPick(g, models), ids = [];
  models.forEach(function (m) { if (ids.indexOf(pick[m]) < 0) ids.push(pick[m]); });
  var h = "#/compare?sessions=" + ids.map(enc).join(",") + "&models=" + models.map(enc).join(",");
  if (location.hash === h) loadCompare(parseHash().q); else location.hash = h;
});
var CMP_TABS = [["overview", "Overview"], ["performance", "Performance"], ["cost", "Cost & Energy"]];
var CMP_GROUPS = {
  performance: ["mean_latency_s", "median_latency_s", "p95_latency_s", "p99_latency_s", "mean_peak_vram_mb", "max_peak_vram_mb", "total_peak_mb",
                "weights_mb", "mean_tokens_per_flow", "mean_input_tokens_per_flow", "mean_output_tokens_per_flow", "handoff_share_of_total", "throughput_flows_per_s"],
  cost: ["cost_per_1k_flows_usd", "wh_per_flow", "wh_per_1k_flows", "wh_per_flow_net_idle"]
};
function runMerged(ids, pick) {
  $("cmp-result").innerHTML = skel(6, true);
  api("/api/compare/merged?sessions=" + ids.map(enc).join(",")).then(function (r) {
    if (!r.ok) { $("cmp-result").innerHTML = ""; showError($("cmp-error"), r.data); return; }
    CMP.data = r.data; CMP.tab = "overview"; CMP.pick = pick || {};
    renderMerged();
  });
}
// one column per model: the run of the session picked for it (model against model only)
function cmpCols() {
  var d = CMP.data, seen = {}, cols = [];
  d.runs.forEach(function (run, i) {
    if (CMP.pick[run.model] !== run.session || seen[run.model]) return;
    seen[run.model] = true;
    var g = cmpSetup(), mr = g && g.models.filter(function (x) { return x.model === run.model; })[0], n = mr ? mr.runs.length : 1;
    var latest = function (k) { var row = d.rows.filter(function (x) { return x.metric === k; })[0]; return row ? row.values[i] : null; };
    var all = CMP.basis === "all" && n > 1;
    cols.push({ head: short(run.model), model: run.model, runs: n, session: run.session,
                sub: all ? "mean of " + n + " runs" : fmtDate(run.created_at).slice(0, 16) + (n > 1 ? " · latest of " + n + " runs" : ""),
                stats: function (k) { return INSIGHTS.runStats(cmpRunValues(run.model, k)); },
                val: all ? function (k) { var st = INSIGHTS.runStats(cmpRunValues(run.model, k)); return st.n ? st.mean : latest(k); } : latest });
  });
  return cols;
}
function bestIdx(vals, lower) {
  var p = vals.filter(function (v) { return v != null; });
  if (p.length < 2) return [];
  var b = lower ? Math.min.apply(null, p) : Math.max.apply(null, p);
  if (p.every(function (v) { return v === b; })) return [];               // all equal: nothing stands out
  return vals.map(function (v, i) { return v === b ? i : -1; }).filter(function (i) { return i >= 0; });
}
function cmpTable(id, keys) {
  var d = CMP.data, cols = cmpCols();
  var defs = keys.map(function (k) { return d.rows.filter(function (x) { return x.metric === k; })[0]; }).filter(Boolean);
  return '<div class="table-wrap"><table id="' + id + '"><thead><tr><th>Metric</th>' + cols.map(function (c) {
    return '<th class="num">' + esc(short(c.model)) + extraTag(c.model) + '<br><span class="small muted" style="font-weight:400">' + esc(c.sub) + '</span></th>';
  }).join("") + '</tr></thead><tbody>' + defs.map(function (row) {
    var f = row.unit === "score" ? function (x) { return fmtNum(x, 2); } : (METRIC_FMT[row.unit] || function (x) { return fmtNum(x, 2); });
    var vals = cols.map(function (c) { return c.val(row.metric); });
    var best = row.context_only ? [] : bestIdx(vals, row.lower_is_better);
    return '<tr><td>' + esc(row.label) + (row.context_only && !/context/.test(row.label) ? ' <span class="muted small">(context)</span>' : "") + '</td>' + vals.map(function (x, i) {
      return '<td class="num' + (best.indexOf(i) >= 0 ? " best" : "") + '">' + (x == null ? '<span class="muted">—</span>' : f(x)) + '</td>';
    }).join("") + '</tr>';
  }).join("") + '</tbody></table></div>';
}
function cmpTiles() {
  var cols = cmpCols(), pick = function (k, lower) {
    var vals = cols.map(function (c) { return c.val(k); }), b = bestIdx(vals, lower);
    return b.length === 1 ? cols[b[0]].head : vals.filter(function (v) { return v != null; }).length > 1 ? "Tie" : "—";
  };
  var t = [["Fastest", pick("mean_latency_s", true)], ["Least working VRAM", pick("mean_peak_vram_mb", true)],
           ["Fewest tokens", pick("mean_tokens_per_flow", true)], ["Top SAW score (efficiency)", pick("saw_eff_composite_100", false)]];
  return '<div class="tiles" id="cmp-tiles">' + t.map(function (x) { return '<div class="tile"><div class="k">' + x[0] + '</div><div class="v" style="font-size:18px">' + esc(x[1]) + '</div></div>'; }).join("") + '</div>';
}
// many models at once: one row per model, ranked by the efficiency SAW score
var CMP_RANK = [["saw_eff_composite_100", "SAW score (efficiency)"], ["mean_latency_s", "Latency / flow"], ["p95_latency_s", "p95 latency"], ["mean_peak_vram_mb", "Working VRAM"],
                ["mean_tokens_per_flow", "Tokens / flow"], ["cost_per_1k_flows_usd", "Cost / 1k flows"], ["wh_per_flow", "Energy / flow"]];
// every run of a model on the chosen setup: its stored value of one measure, per session
function cmpRunValues(model, k) {
  var g = cmpSetup(), mr = g && g.models.filter(function (x) { return x.model === model; })[0];
  return mr ? mr.runs.map(function (r) { var row = cmpRow(r.session_id); return row && row.metrics && row.metrics[model] ? row.metrics[model][k] : null; })
    .filter(function (v) { return v != null; }) : [];
}
// the comparison in one plain sentence (what a reader should take away), from the shown values
function cmpPlain() {
  var cols = cmpCols(), pick = function (k, lower) {
    var vs = cols.map(function (c) { return { c: c, v: c.val(k) }; }).filter(function (x) { return x.v != null; });
    if (vs.length < 2) return null;
    vs.sort(function (a, b) { return lower ? a.v - b.v : b.v - a.v; });
    return { best: vs[0], worst: vs[vs.length - 1], tie: vs[0].v === vs[1].v };
  };
  var bits = [], lat = pick("mean_latency_s", true), vr = pick("mean_peak_vram_mb", true), tok = pick("mean_tokens_per_flow", true), saw = pick("saw_eff_composite_100", false);
  if (lat && !lat.tie) bits.push("<b>" + esc(lat.best.c.head) + "</b> is the fastest (" + esc(fmtTime(lat.best.v)) + " per flow" +
    (lat.worst.v ? ", " + fmtNum(100 * (1 - lat.best.v / lat.worst.v), 0) + "% quicker than " + esc(lat.worst.c.head) : "") + ")");
  if (vr && !vr.tie) bits.push("<b>" + esc(vr.best.c.head) + "</b> needs the least working memory (" + esc(fmtMB(vr.best.v)) + ")");
  if (tok && !tok.tie) bits.push("<b>" + esc(tok.best.c.head) + "</b> uses the fewest tokens");
  if (saw && !saw.tie) bits.push("the best balance (SAW score) is <b>" + esc(saw.best.c.head) + "</b>'s");
  if (!bits.length) return '<p class="tab-summary" id="cmp-plain">These models are level on every measure shown here.</p>';
  return '<p class="tab-summary" id="cmp-plain">In short: ' + bits.join("; ") + "." + (CMP.basis === "all" ? " (Means over every run.)" : cols.some(function (c) { return c.runs > 1; }) ? " (Latest runs; switch to Every run to see whether the gaps hold.)" : "") + '</p>';
}
function cmpDiffCard() {
  var row = cmpDef(CMP.bar || "mean_latency_s"), f = cmpFmt(row), byModel = {};
  cmpCols().forEach(function (c) { byModel[c.model] = cmpRunValues(c.model, row.metric); });
  var r = INSIGHTS.compareRuns(byModel, row.lower_is_better);
  if (r.rows.length < 2) return "";
  var best = short(r.best);
  return '<div class="card" id="cmp-diff-card"><h2>Are the differences real? — ' + esc(row.label) + '</h2><p class="muted small" style="margin-top:0">Over every run on this setup, against the best model (' + esc(best) + ').' + infoI(r.rule) + '</p>' +
    '<div class="table-wrap"><table id="cmp-diff-table"><thead><tr><th>Model</th><th class="num">Mean ± 95% CI</th><th class="num">Runs</th><th class="num">vs ' + esc(best) + '</th><th class="num">p</th><th>Verdict</th></tr></thead><tbody>' +
    r.rows.map(function (x) {
      var chip = x.verdict === "best" ? '<span class="chip ok">best</span>' : x.verdict === "real difference" ? '<span class="chip warn">real difference</span>' : x.verdict === "within noise" ? '<span class="chip">within noise</span>' : '<span class="chip">too few runs</span>';
      return '<tr><td><b>' + esc(short(x.model)) + '</b></td><td class="num">' + f(x.mean) + (x.ci != null ? ' <span class="small muted">± ' + f(x.ci) + '</span>' : "") + '</td><td class="num">' + x.n +
        '</td><td class="num">' + (x.diff == null ? "—" : (x.diff > 0 ? "+" : "") + fmtNum(100 * x.diff, 1) + "%") + '</td><td class="num">' + (x.p == null ? "—" : x.p < 0.001 ? "&lt; 0.001" : fmtNum(x.p, 3)) + '</td><td>' + chip + '</td></tr>';
    }).join("") + '</tbody></table></div>' +
    (r.rows.some(function (x) { return x.verdict === "too few runs"; }) ? '<p class="small muted" style="margin-bottom:0">Run a model on this setup at least twice (Run again on its session) to judge whether its difference is real.</p>' : "") + '</div>';
}
function cmpDef(k) { return CMP.data.rows.filter(function (x) { return x.metric === k; })[0]; }
function cmpFmt(row) { return row.unit === "score" ? function (x) { return fmtNum(x, 1); } : (METRIC_FMT[row.unit] || function (x) { return fmtNum(x, 2); }); }
function cmpRanking() {
  var cols = cmpCols(), defs = CMP_RANK.map(function (r) { return [r, cmpDef(r[0])]; }).filter(function (x) { return x[1]; });
  var saw = function (c) { var v = c.val("saw_eff_composite_100"); return v == null ? -Infinity : v; };
  var lat = function (c) { var v = c.val("mean_latency_s"); return v == null ? Infinity : v; };
  var order = cols.slice().sort(function (a, b) { return saw(b) - saw(a) || lat(a) - lat(b) || a.head.localeCompare(b.head); });   // a tie on SAW: the faster first
  var best = {};
  defs.forEach(function (x) { var vals = order.map(function (c) { return c.val(x[0][0]); }); best[x[0][0]] = bestIdx(vals, x[1].lower_is_better); });
  return '<div class="card"><h2>Ranking (' + cols.length + ' models)</h2><p class="muted small" style="margin-top:0">Ordered by the SAW score (efficiency), then by latency on a tie; the best value in each column is marked.</p>' +
    '<div class="table-wrap"><table id="cmp-rank-table"><thead><tr><th class="num">#</th><th>Model</th>' + defs.map(function (x) { return '<th class="num">' + esc(x[0][1]) + '</th>'; }).join("") + '</tr></thead><tbody>' +
    order.map(function (c, i) {
      return '<tr><td class="num">' + (saw(c) === -Infinity ? "—" : i + 1) + '</td><td><b>' + esc(c.head) + '</b>' + extraTag(c.model) + '<br><span class="small muted"><a href="#/session/' + enc(c.session) + '" title="' + esc(c.session) + '">' +
        esc(shortId(c.session)) + '</a>' + (c.runs > 1 ? (CMP.basis === "all" ? ' · mean of ' + c.runs + ' runs' : ' · latest of ' + c.runs + ' runs') : "") + '</span></td>' + defs.map(function (x) {
          var v = c.val(x[0][0]), st = CMP.basis === "all" && c.runs > 1 ? c.stats(x[0][0]) : null;
          return '<td class="num' + (best[x[0][0]].indexOf(i) >= 0 ? " best" : "") + '">' + (v == null ? '<span class="muted">—</span>' : cmpFmt(x[1])(v) +
            (st && st.ci != null ? '<br><span class="small muted">± ' + cmpFmt(x[1])(st.ci) + '</span>' : "")) + '</td>';
        }).join("") + '</tr>';
    }).join("") + '</tbody></table></div></div>';
}
function cmpBars() {
  var defs = CMP_RANK.filter(function (r) { return cmpDef(r[0]); });
  if (!defs.some(function (r) { return r[0] === CMP.bar; })) CMP.bar = "mean_latency_s";
  var row = cmpDef(CMP.bar), f = cmpFmt(row);
  var items = cmpCols().map(function (c) { return { c: c, v: c.val(CMP.bar) }; }).filter(function (x) { return x.v != null; })
    .sort(function (a, b) { return row.lower_is_better ? a.v - b.v : b.v - a.v; });
  var max = Math.max.apply(null, items.map(function (x) { return Math.abs(x.v); }).concat([0])) || 1;
  return '<div class="card" id="cmp-bars-card"><div class="row" style="margin-top:0;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap"><h2 style="margin:0">Side by side</h2>' +
    '<div class="field" style="margin:0"><label for="cmp-bar-metric">Measure</label><select id="cmp-bar-metric">' + defs.map(function (r) {
      return '<option value="' + r[0] + '"' + (r[0] === CMP.bar ? " selected" : "") + '>' + esc(r[1]) + '</option>'; }).join("") + '</select></div></div>' +
    '<p class="muted small">' + (row.lower_is_better ? "Lower is better" : "Higher is better") + '; best first.</p>' +
    (items.length ? '<div id="cmp-bars" class="hbars">' + items.map(function (x, i) {
      return '<div class="hbar"><span class="hb-l">' + esc(x.c.head) + '</span><span class="hb-t"><span class="hb-f' + (i === 0 && items.length > 1 ? " lead" : "") + '" style="width:' +
        Math.max(1, 100 * Math.abs(x.v) / max).toFixed(1) + '%"></span></span><span class="hb-v num">' + esc(f(x.v)) + '</span></div>';
    }).join("") + '</div>' : '<p class="muted" id="cmp-bars-none">Not measured for these models (e.g. demo runs have no GPU energy or price).</p>') + '</div>';
}
function renderMerged() {
  var d = CMP.data, st = d.setup, g = cmpSetup(), cols = cmpCols();
  var tabs = CMP_TABS;
  if (!tabs.some(function (t) { return t[0] === CMP.tab; })) CMP.tab = "overview";
  var body;
  if (CMP.tab === "overview") {
    body = cmpPlain() + cmpTiles() + cmpRanking() + cmpBars() + cmpDiffCard();
  } else {
    body = '<div class="card">' + cmpTable("cmp-" + CMP.tab + "-table", CMP_GROUPS[CMP.tab]) + '</div>';
  }
  $("cmp-result").innerHTML =
    '<div class="validity ok" id="cmp-validity" role="status"><strong>Like-for-like comparison</strong> · ' + cols.length + ' models · same flows (' +
      esc(String(st.input_type || "").toUpperCase()) + ', ' + fmtNum(st.n_flows) + ' flows, set <span class="mono">' + esc(String(st.prepared_set_sha256).slice(0, 12)) + '</span>), same GPU (' + esc(st.gpu) + '), same settings · only the model differs' +
      infoI("Every model answered the same flows on the same GPU with the same settings, so the differences are the models'. Each model is one entry, from its latest run on this setup. Values are the stored ones; the best is marked.") + '</div>' +
    (cols.some(function (c) { return c.runs > 1; }) ? '<div class="seg-opts" id="cmp-basis" role="radiogroup" aria-label="Which runs" style="margin:0 0 12px">' +
      '<label><input type="radio" name="cmp-basis" value="latest"' + (CMP.basis !== "all" ? " checked" : "") + '> Latest run of each model</label>' +
      '<label><input type="radio" name="cmp-basis" value="all"' + (CMP.basis === "all" ? " checked" : "") + '> Every run (mean ± 95% CI)</label></div>' : "") +
    '<div class="tabs" role="tablist" aria-label="Comparison sections" id="cmp-tabs">' + tabs.map(function (t) {
      return '<a role="tab" href="#" data-ctab="' + t[0] + '" id="ctab-' + t[0] + '" aria-selected="' + (t[0] === CMP.tab) + '">' + esc(t[1]) + '</a>';
    }).join("") + '</div>' + body +
    '<details class="info"><summary>All metrics</summary>' + cmpTable("cmp-table", d.rows.map(function (x) { return x.metric; })) + '</details>' +
    '<p class="muted small">View only: stored scores never change.</p>';
  $("cmp-result").querySelectorAll("[data-ctab]").forEach(function (a) { a.addEventListener("click", function (ev) { ev.preventDefault(); CMP.tab = a.getAttribute("data-ctab"); renderMerged(); }); });
  if ($("cmp-bar-metric")) $("cmp-bar-metric").addEventListener("change", function () { CMP.bar = this.value; renderMerged(); });
  $("cmp-result").querySelectorAll('input[name="cmp-basis"]').forEach(function (x) { x.addEventListener("change", function () { CMP.basis = x.value; renderMerged(); }); });
  addExports($("cmp-result"));
}

// ---------- DATA SETS: every prepared set, what is in it and who used it ----------
// One row per prepared set, keyed by its content hash (the like-for-like key), built from the
// finished sessions (always available) plus the sets the GPU server prepared but nobody has
// benchmarked yet (while it is online). Details come from the set's own summary.
var DS = { rows: null, open: null, offline: false };
function finishedRow(s) { return s.status === "Done" || s.status === "Demo"; }
function datasetRows(sessions, jobs) {
  var by = {}, byName = {};
  var at = function (x) { return String(x.finished_at || x.created_at || ""); };
  sessions.filter(finishedRow).forEach(function (s) {
    var sha = (s.identity || {}).prepared_set_sha256 || s.prepared_set_sha256 || null;
    if (!sha && !s.run_name) return;
    var k = sha || "name:" + s.run_name;
    var g = by[k] || (by[k] = { key: k, sha: sha, names: [], type: s.input_type || null, n_flows: s.n_flows, labelled: s.labelled, sessions: [], models: [], gpus: [], prepared_at: null, source: null, last: "" });
    if (s.run_name && g.names.indexOf(s.run_name) < 0) g.names.push(s.run_name);
    g.sessions.push(s);
    (s.models || []).forEach(function (m) { if (g.models.indexOf(m) < 0) g.models.push(m); });
    if (s.gpu && g.gpus.indexOf(s.gpu) < 0) g.gpus.push(s.gpu);
    if (at(s) > g.last) g.last = at(s);
    if (s.run_name) byName[s.run_name] = k;
  });
  jobs.filter(function (j) { return j.kind === "prepare" && j.status === "done" && j.prepared; }).forEach(function (j) {
    var k = byName[j.prepared] || "name:" + j.prepared;
    var g = by[k] || (by[k] = { key: k, sha: null, names: [j.prepared], type: /^pcap/.test(j.prepared) ? "PCAP" : "CSV", n_flows: null, labelled: null, sessions: [], models: [], gpus: [], prepared_at: null, source: null, last: "" });
    if (!g.prepared_at || String(j.finished_at) < g.prepared_at) { g.prepared_at = j.finished_at; g.source = j.run_name; }
    if (String(j.finished_at || "") > g.last) g.last = String(j.finished_at || "");
  });
  return Object.keys(by).map(function (k) {
    var g = by[k];
    g.sessions.sort(function (a, b) { return at(b).localeCompare(at(a)); });
    g.name = g.names[0] || null;
    return g;
  }).sort(function (a, b) { return b.last.localeCompare(a.last); });
}
function loadDatasets(q) {
  q = q || {};
  if (q.open) DS.open = q.open;
  $("ds-body").innerHTML = skel(5, true);
  var gpuJobs = CP && !ONLINE ? Promise.resolve({ ok: false }) : api("/api/jobs?limit=200");
  Promise.all([allSessions(), gpuJobs]).then(function (rs) {
    if (!rs[0].ok) { $("ds-body").innerHTML = '<div class="notice error">' + esc(rs[0].data.error || "Could not load the sessions.") + '</div>'; return; }
    DS.offline = !rs[1].ok;
    DS.rows = datasetRows(rs[0].data.sessions || [], rs[1].ok ? rs[1].data.jobs || [] : []);
    renderDatasets();
  });
}
function dsMatch(g) {
  var t = $("ds-type").value, words = $("ds-q").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (t && g.type !== t) return false;
  var hay = (g.names.join(" ") + " " + (g.sha || "") + " " + g.models.join(" ") + " " + (g.source || "")).toLowerCase();
  return words.every(function (w) { return hay.indexOf(w) >= 0; });
}
function renderDatasets() {
  if (!DS.rows) return;
  var rows = DS.rows.filter(dsMatch), off = DS.offline && CP;
  if (!DS.rows.length) {
    $("ds-body").innerHTML = '<div class="card empty" id="ds-empty"><h2>No data sets yet</h2><p>Upload a CSV or PCAP file on <a href="#/new">New benchmark</a>: it is prepared into a set of flows that every model then answers.</p></div>';
    return;
  }
  $("ds-body").innerHTML = (off ? '<p class="notice small" id="ds-offline">The GPU server is offline: sets that have been benchmarked are listed; sets prepared but not yet benchmarked appear when it is back.</p>' : "") +
    '<div class="card"><p class="muted small" style="margin-top:0">' + rows.length + ' of ' + DS.rows.length + ' set' + (DS.rows.length === 1 ? "" : "s") +
    '. The fingerprint is the hash of the prepared flows: two sessions with the same fingerprint answered exactly the same flows.</p>' +
    '<div class="table-wrap"><table id="ds-table"><thead><tr><th>Data set</th><th class="num">Flows</th><th>Fingerprint</th><th class="num">Sessions</th><th class="num">Models</th><th>Last used</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>' +
    rows.map(function (g) {
      var open = DS.open === g.key, nm = g.name ? g.name.split("/").pop() : "(unnamed set)";
      return '<tr data-ds="' + esc(g.key) + '"><td><b class="mono small">' + esc(nm) + '</b><br><span class="chip">' + esc(g.type || "—") + '</span>' +
        (g.labelled === true ? ' <span class="chip ok">labelled</span>' : g.labelled === false ? ' <span class="chip">no labels</span>' : "") +
        (g.sessions.length ? "" : ' <span class="chip warn">not benchmarked yet</span>') + '</td><td class="num">' + (g.n_flows == null ? "—" : fmtNum(g.n_flows)) +
        '</td><td class="mono small">' + (g.sha ? esc(g.sha.slice(0, 12)) : "—") + '</td><td class="num">' + g.sessions.length + '</td><td class="num">' + g.models.length +
        '</td><td class="small">' + esc(g.last ? fmtDate(g.last) : "—") + '</td><td class="ds-actions"><button type="button" class="btn small-btn" data-ds-open="' + esc(g.key) + '" aria-expanded="' + open + '">' + (open ? "Hide" : "Details") + '</button>' +
        (g.name ? ' <a class="btn small-btn" href="#/new/models?set=' + enc(g.name) + '">Benchmark</a>' : "") +
        (g.models.length > 1 ? ' <a class="btn small-btn" href="#/compare?a=' + enc(g.sessions[0].session_id) + '">Compare models</a>' : "") + '</td></tr>' +
        (open ? '<tr class="ds-detail"><td colspan="7"><div id="ds-detail">' + dsDetail(g, null) + '</div></td></tr>' : "");
    }).join("") + '</tbody></table></div>' + (rows.length ? "" : '<p class="muted" id="ds-none">No set matches.</p>') + '</div>';
  addExports($("ds-body"));
  var g = DS.rows.filter(function (x) { return x.key === DS.open; })[0];
  if (g && g.name && !(CP && !ONLINE)) {
    api("/api/prepared/" + setPath(g.name)).then(function (r) {
      if (DS.open === g.key && $("ds-detail")) $("ds-detail").innerHTML = dsDetail(g, r);
    });
  }
}
function dsDetail(g, r) {
  var sum = r && r.ok ? r.data : null, ccs = sum ? sum.class_counts || {} : {};
  var cc = sum ? sum.label_distribution_selected || ccs.selected || {} : {}, inFile = ccs.in_file || {};   // flows kept per class (and how many the file had)
  var classes = Object.keys(cc).sort(function (a, b) { return cc[b] - cc[a]; });
  var tot = classes.reduce(function (a, k) { return a + cc[k]; }, 0);
  var files = sum ? (sum.downloads || []).map(function (f) { return dlLink("/api/prepared/" + setPath(g.name) + "/" + enc(f), g.name.split("/").pop() + "_" + f, f, "btn small-btn"); }).join(" ") : "";
  var about = !g.name ? "" : !r ? '<p class="muted small" id="ds-files">' + (CP && !ONLINE ? "The set's files are on the GPU server, which is offline." : "Loading the set's details…") + '</p>'
    : !sum ? '<p class="muted small" id="ds-files">' + esc((r.data && r.data.error) || "The set's details are not on this GPU server.") + '</p>'
    : '<div class="grid2"><div><h3>What is in it</h3><ul class="lk-list small">' +
        '<li>Source file: <b>' + esc((sum.source || {}).filename || sum.source_file || g.source || "—") + '</b></li>' +
        '<li>Rows read: <b>' + (sum.rows_total == null ? "—" : fmtNum(sum.rows_total)) + '</b>, flows kept: <b>' + (sum.rows_selected == null ? fmtNum(g.n_flows) : fmtNum(sum.rows_selected)) + '</b></li>' +
        '<li>Labels: <b>' + (sum.accuracy_available ? "yes — the output check can compare with them" : "no — efficiency only") + '</b></li>' +
        (g.prepared_at ? '<li>Prepared: ' + esc(fmtDate(g.prepared_at)) + '</li>' : "") +
        (g.sha ? '<li>Fingerprint: <span class="mono">' + esc(g.sha) + '</span></li>' : "") + '</ul></div>' +
      '<div><h3>Flow mix</h3>' + (classes.length ? '<div class="hbars" id="ds-mix">' + classes.map(function (k) {
        return '<div class="hbar"><span class="hb-l">' + esc(k) + '</span><span class="hb-t"><span class="hb-f" style="width:' + (100 * cc[k] / tot).toFixed(1) + '%"></span></span><span class="hb-v num" title="' + (inFile[k] == null ? "" : fmtNum(inFile[k]) + ' in the file') + '">' + fmtNum(cc[k]) + '</span></div>';
      }).join("") + '</div><p class="small muted" style="margin:6px 0 0">Flows kept per class' + (Object.keys(inFile).length ? ' (hover a number for how many the file had)' : "") + '.</p>'
        : '<p class="muted small">No class labels in this set (PCAP traffic has none): every model answers the same flows, measured for efficiency.</p>') + '</div></div>' +
      (files ? '<p class="small" id="ds-files" style="margin-bottom:0">Download: ' + files + '</p>' : "");
  var used = g.sessions.length ? '<h3>Used by</h3><div class="table-wrap"><table class="ds-used"><thead><tr><th>Session</th><th>Models</th><th>GPU</th><th>Finished</th></tr></thead><tbody>' +
    g.sessions.slice(0, 50).map(function (s) {
      return '<tr><td><a class="mono" href="#/session/' + enc(s.session_id) + '">' + esc(shortId(s.session_id)) + '</a></td><td class="small">' + esc((s.models || []).map(short).join(", ")) +
        '</td><td class="small">' + esc(s.gpu || "—") + '</td><td class="small">' + esc(fmtDate(s.finished_at || s.created_at)) + '</td></tr>';
    }).join("") + '</tbody></table></div>' : '<p class="muted small">No session has used this set yet.</p>';
  return about + used;
}
$("ds-q").addEventListener("input", renderDatasets);
$("ds-type").addEventListener("change", renderDatasets);
document.addEventListener("click", function (ev) {
  var b = ev.target.closest && ev.target.closest("[data-ds-open]");
  if (!b) return;
  var k = b.getAttribute("data-ds-open");
  DS.open = DS.open === k ? null : k;
  renderDatasets();
});
// ---------- GPU & QUEUE: the one GPU server right now (polled while the page is open) ----------
var GQ = { hist: [], last: null };
var KIND_LABEL = { benchmark: "Benchmark", prepare: "Data preparation" };
function gqSpark(vals, max, color) {
  vals = vals.filter(function (v) { return v != null; });
  if (vals.length < 2) return "";
  var W = 120, H = 28, m = max || Math.max.apply(null, vals) || 1;
  var pts = vals.map(function (x, i) { return (i * W / (vals.length - 1)).toFixed(1) + "," + (H - 2 - (H - 4) * Math.min(1, x / m)).toFixed(1); }).join(" ");
  return '<svg class="tel-spark" viewBox="0 0 ' + W + ' ' + H + '" aria-hidden="true"><polyline points="' + pts + '" style="stroke:' + color + '"/></svg>';
}
function secs(a, b) { var x = Date.parse(a), y = b ? Date.parse(b) : Date.now(); return isFinite(x) && isFinite(y) ? Math.max(0, (y - x) / 1000) : null; }
function gqDur(s) {
  if (s == null) return "—";
  s = Math.round(s);
  return s < 60 ? s + " s" : s < 3600 ? Math.floor(s / 60) + " min " + (s % 60) + " s" : Math.floor(s / 3600) + " h " + Math.round((s % 3600) / 60) + " min";
}
function loadGpu() {
  var token = pollToken;
  GQ.hist = []; GQ.last = null;
  $("gq-status").innerHTML = skel(3, true);
  ["gq-gpu", "gq-prep", "gq-running", "gq-queued", "gq-upcoming", "gq-recent"].forEach(function (id) { $(id).innerHTML = ""; });
  var tick = function () {
    if (token !== pollToken) return;                        // left the page
    var off = CP && !ONLINE;
    (off ? Promise.resolve(null) : api("/api/gpu")).then(function (r) {
      if (token !== pollToken) return;
      renderGpu(r);
      setTimeout(tick, off || !r || !r.ok ? 10000 : 3000);
    });
  };
  tick();
  if (!(CP && !ONLINE)) backendFetch("/api/schedules").then(function (r) { if (token === pollToken) renderUpcoming(r); });
}
function renderGpu(r) {
  var off = !r || !r.ok, d = off ? null : r.data;
  if (off) {
    var why = !r ? "The GPU server is not connected right now." : (r.data && r.data.error) || "The GPU server did not answer.";
    $("gq-status").innerHTML = '<div class="validity bad" id="gq-state" role="status"><strong>GPU offline</strong> · ' + esc(why) +
      ' Sessions, data sets and results stay available; new benchmarks can start when it is back.</div>';
    $("gq-gpu").innerHTML = ""; $("gq-running").innerHTML = ""; $("gq-queued").innerHTML = ""; $("gq-recent").innerHTML = "";
    renderPrepWhere(null);
    return;
  }
  GQ.last = d;
  if (d.gpu) { GQ.hist.push(d.gpu); if (GQ.hist.length > 60) GQ.hist.shift(); }
  var busy = d.running.length > 0;
  $("gq-status").innerHTML = '<div class="validity ' + (busy ? "" : "ok") + '" id="gq-state" role="status"><strong>' + (busy ? "Busy" : "Free") + '</strong> · ' +
    (busy ? "running " + esc(KIND_LABEL[d.running[0].kind] || d.running[0].kind).toLowerCase() : "nothing running") + ' · ' + d.queued.length + ' waiting' +
    (d.provider === "mock" ? ' · <span class="small">demo mode (no GPU)</span>' : "") + '</div>';
  var dev = d.device || {}, g = d.gpu, h = GQ.hist;
  var cell = function (label, val, sp) { return '<div class="tel-metric"><span class="small muted">' + label + '</span><b>' + val + '</b>' + (sp || "") + '</div>'; };
  $("gq-gpu").innerHTML = '<div class="card" id="gq-gpu-card"><h2>GPU now' + (g && g.name || dev.name ? ' <span class="small muted">' + esc((g && g.name) || dev.name) + '</span>' : "") + '</h2>' +
    (g ? '<div class="tel-metrics" id="gq-metrics">' +
      cell("Utilisation", fmtNum(g.util_pct) + " %", gqSpark(h.map(function (x) { return x.util_pct; }), 100, "var(--series-1)")) +
      cell("Memory", fmtNum(g.mem_used_mb / 1024, 1) + " / " + fmtNum(g.mem_total_mb / 1024, 1) + " GB", gqSpark(h.map(function (x) { return x.mem_used_mb; }), g.mem_total_mb, "var(--series-2)")) +
      cell("Power", g.power_w == null ? "—" : fmtNum(g.power_w) + " W", gqSpark(h.map(function (x) { return x.power_w; }), null, "var(--accent)")) +
      cell("Temperature", g.temp_c == null ? "—" : fmtNum(g.temp_c) + " °C") + '</div>'
      : '<p class="muted" id="gq-nogpu">' + (d.gpu_reason === "demo mode: no GPU" ? "Demo mode: this server has no GPU, so there are no live readings (nothing is simulated)." : "No GPU reading on this server (no NVIDIA driver found).") + '</p>') +
    '<p class="small muted" style="margin-bottom:0">' + [dev.vram_total_mb ? fmtNum(dev.vram_total_mb / 1024, 1) + " GB VRAM" : null, dev.driver_version ? "driver " + dev.driver_version : null,
      dev.cuda ? "CUDA " + dev.cuda : null, d.engine ? "engine " + d.engine : null, "4-bit NF4 models"].filter(Boolean).map(esc).join(" · ") +
    '. Live readings only; a session\'s own GPU record is on its Cost &amp; Energy tab.</p></div>';
  renderPrepWhere(d);
  $("gq-running").innerHTML = '<div class="card" id="gq-running-card"><h2>Running now</h2>' + (d.running.length ? d.running.map(function (j) {
    var p = j.progress || {}, pct = p.percent == null ? null : +p.percent, el = secs(j.started_at), left = pct && pct > 2 && el != null ? el * (100 - pct) / pct : null;
    return '<div class="gq-job"><p style="margin:0 0 6px"><b>' + esc(KIND_LABEL[j.kind] || j.kind) + '</b>' + (j.models && j.models.length ? ': ' + esc(j.models.map(short).join(", ")) : "") +
      (j.schedule_id ? ' <span class="chip">scheduled</span>' : "") + '<br><span class="small muted">' + esc([(j.prepared || j.run_name || "").split("/").pop(), j.owner ? "by " + j.owner : null, "started " + fmtDate(j.started_at)].filter(Boolean).join(" · ")) + '</span></p>' +
      (j.kind === "prepare" ? '<p class="small" style="margin:0">' + esc(j.message || "Preparing…") + '</p>'
        : '<div class="bar"><span style="width:' + (pct || 0).toFixed(1) + '%"></span></div><p class="small" style="margin:6px 0 0">' + (pct == null ? "" : fmtNum(pct, 0) + "% · ") +
          (p.total ? fmtNum(p.completed) + " of " + fmtNum(p.total) + " flow answers" : esc(p.phase || "")) + (p.current_model ? " · now " + esc(short(p.current_model)) : "") +
          ' · running ' + gqDur(el) + (left != null ? ' · about ' + gqDur(left) + ' left' : "") + ' · <a href="#/new/run/' + enc(j.job_id) + '">Watch live</a></p>') + '</div>';
  }).join("") : '<p class="muted" id="gq-idle" style="margin-bottom:0">Nothing is running. A new benchmark starts at once.</p>') + '</div>';
  $("gq-queued").innerHTML = '<div class="card" id="gq-queued-card"><h2>Waiting (' + d.queued.length + ')</h2>' + (d.queued.length ?
    '<div class="table-wrap"><table id="gq-queue-table"><thead><tr><th class="num">#</th><th>What</th><th>Models</th><th>Data set</th><th>Owner</th><th>Queued</th></tr></thead><tbody>' +
    d.queued.map(function (j, i) {
      return '<tr><td class="num">' + (i + 1) + '</td><td>' + esc(KIND_LABEL[j.kind] || j.kind) + (j.schedule_id ? ' <span class="chip">scheduled</span>' : "") + '</td><td class="small">' + esc((j.models || []).map(short).join(", ") || "—") +
        '</td><td class="small mono">' + esc((j.prepared || j.run_name || "—").split("/").pop()) + '</td><td>' + esc(j.owner || "—") + '</td><td class="small">' + esc(fmtDate(j.created_at)) + '</td></tr>';
    }).join("") + '</tbody></table></div><p class="small muted" style="margin-bottom:0">One run at a time, first in first out.</p>'
    : '<p class="muted" id="gq-noqueue" style="margin-bottom:0">Nobody is waiting.</p>') + '</div>';
  $("gq-recent").innerHTML = '<div class="card" id="gq-recent-card"><h2>Just finished</h2>' + (d.recent.length ?
    '<div class="table-wrap"><table id="gq-recent-table"><thead><tr><th>What</th><th>Result</th><th>Models</th><th>Finished</th><th class="num">Took</th></tr></thead><tbody>' +
    d.recent.map(function (j) {
      var ok = j.status === "done", link = ok && j.kind !== "prepare" ? '<a class="mono" href="#/session/' + enc(j.job_id) + '">' + esc(KIND_LABEL[j.kind] || j.kind) + '</a>' : esc(KIND_LABEL[j.kind] || j.kind);
      return '<tr><td>' + link + '</td><td><span class="chip ' + (ok ? "ok" : j.status === "failed" ? "bad" : "") + '">' + esc(j.status) + '</span></td><td class="small">' + esc((j.models || []).map(short).join(", ") || "—") +
        '</td><td class="small">' + esc(fmtDate(j.finished_at)) + '</td><td class="num">' + gqDur(secs(j.started_at, j.finished_at)) + '</td></tr>';
    }).join("") + '</tbody></table></div>' : '<p class="muted" style="margin-bottom:0">Nothing has run yet.</p>') + '</div>';
}
// where a file uploaded now would be prepared (the same rule as New benchmark: prepRoute)
function renderPrepWhere(d) {
  var busy = d ? d.running.length + d.queued.length > 0 : true, cloud = !!(CP && BACKEND && BACKEND.cloud_prep);
  var where = !d ? (cloud ? "in the cloud" : localPrepSupported() ? "in your browser" : "when the GPU server is back")
    : !busy ? "on the GPU server" : cloud ? "in the cloud" : localPrepSupported() ? "in your browser" : "on the GPU server, after the queue";
  var why = !d ? "the GPU server is offline" : !busy ? "it is free" : "the GPU is busy, so preparing does not wait in its queue";
  $("gq-prep").innerHTML = '<div class="card" id="gq-prep-card"><h2>Data preparation</h2><p id="gq-prep-where">A file uploaded now is prepared <b>' + esc(where) + '</b> (' + esc(why) + ').</p>' +
    '<p class="small muted" style="margin-bottom:0">Chosen automatically. Every place runs the same preparation code, so the flows and their fingerprint are identical wherever they are prepared.</p></div>';
}
function renderUpcoming(r) {
  var host = $("gq-upcoming");
  if (!host) return;
  var next = r && r.ok ? (r.data.schedules || []).filter(function (x) { return x.active; }).sort(function (a, b) { return String(a.next_at).localeCompare(String(b.next_at)); }) : [];
  host.innerHTML = '<div class="card" id="gq-upcoming-card"><h2>Coming up</h2>' + (next.length ? '<ul class="lk-list small">' + next.slice(0, 5).map(function (x) {
    return '<li>' + esc(fmtDate(x.next_at)) + ' — repeat of <a class="mono" href="#/session/' + enc(x.source_job_id) + '">' + esc(shortId(x.source_job_id)) + '</a> (' + esc((x.models || []).map(short).join(", ")) + '), run ' + (x.runs_done + 1) + ' of ' + x.runs_total + '</li>';
  }).join("") + '</ul>' : '<p class="muted" style="margin-bottom:0">No scheduled re-runs.</p>') +
    '<p class="small muted" style="margin-bottom:0">Schedules are started from a session\'s Overview and managed on <a href="#/trends">Repeatability</a>.</p></div>';
}
// ---------- LEADERBOARD ----------
var LB_COLS = [["saw_eff_composite_100", "SAW (efficiency)", function (v) { return fmtNum(v, 1); }], ["mean_latency_s", "Latency / flow", fmtTime], ["mean_peak_vram_mb", "Working VRAM", fmtMB], ["total_peak_mb", "Total peak VRAM", fmtMB],
               ["mean_tokens_per_flow", "Tokens / flow", function (v) { return fmtNum(v); }], ["cost_per_1k_flows_usd", "Cost / 1k flows", fmtUsd], ["wh_per_flow", "Energy / flow", fmtWh]];
var LB = { tab: "setup" };
var LB_TABS = ["setup", "compare", "overall"];
function lbTab(t, routed) {
  LB.tab = t;
  LB_TABS.forEach(function (x) {
    var b = $("lbtab-" + x);
    b.setAttribute("aria-selected", String(x === t)); b.tabIndex = x === t ? 0 : -1;
    $("lb-" + x + "-panel").hidden = x !== t;
  });
  if (t === "compare") { if (!routed) location.hash = "#/compare"; return; }        // its own address, so links and reloads keep it
  if (/^#\/compare/.test(location.hash)) { location.hash = "#/leaderboard"; return; }
  loadLeaderboard();
}
function loadLeaderboard() { if (LB.tab === "overall") loadOverall(); else loadPerSetup(); }
function loadPerSetup() {
  var sort = (document.querySelector('input[name="lb-sort"]:checked') || {}).value || "efficiency";
  $("lb-groups").innerHTML = skel(5, true);
  api("/api/leaderboard?view=per_setup&sort=" + enc(sort)).then(function (r) {
    if (!r.ok) { $("lb-groups").innerHTML = '<div class="notice error">' + esc(r.data.error) + '</div>'; return; }
    var gs = r.data.groups || [];
    if (!gs.length) { $("lb-groups").innerHTML = '<div class="card empty"><p>No finished sessions yet.</p><a class="btn primary" href="#/new">New benchmark</a></div>'; return; }
    $("lb-groups").innerHTML = '<p class="muted small">' + (r.data.higher_is_better ? "Higher is better." : "Lower is better.") + ' Sessions on one setup are combined.' + infoI(r.data.rule) + '</p>' + gs.map(function (g, gi) {
      return '<div class="card lb-group"><div class="row" style="margin-top:0;justify-content:space-between"><h2>' + esc(g.gpu) + ' · ' + esc(g.input_type || "") + ' · ' + fmtNum(g.n_flows_per_session) + ' flows</h2>' +
        '<span class="chips"><span class="chip mono" title="Prepared-set hash">set ' + esc(String(g.prepared_set_sha256).slice(0, 12)) + '</span><span class="chip">' + g.n_sessions + ' session' + (g.n_sessions === 1 ? "" : "s") + '</span>' +
        (g.demo ? '<span class="chip warn">demo</span>' : "") + '</span></div>' +
        '<div class="table-wrap" style="margin-top:10px"><table class="lb-table" id="lb-table-' + gi + '"><thead><tr><th class="num">Rank</th><th>Model</th><th class="num">Sessions</th><th class="num">Flows</th>' +
        LB_COLS.map(function (c) { return '<th class="num">' + esc(c[1]) + '</th>'; }).join("") + '</tr></thead><tbody>' +
        g.rows.map(function (row) {
          return '<tr><td class="num">' + (row.rank == null ? '<span class="muted" title="no value for this key">—</span>' : row.rank) + '</td><td class="mono">' + esc(row.model) + extraTag(row.model) + '</td><td class="num">' + row.n_sessions +
            '</td><td class="num">' + fmtNum(row.n_flows) + '</td>' + LB_COLS.map(function (c) { return '<td class="num">' + (row[c[0]] == null ? '<span class="muted">—</span>' : c[2](row[c[0]])) + '</td>'; }).join("") + '</tr>';
        }).join("") + '</tbody></table></div>' +
        (g.n_sessions > 1 ? '<p class="small" style="margin-bottom:0"><a class="lb-compare" href="#/compare?sessions=' + g.sessions.map(enc).join(",") + '">Compare these models →</a></p>' : "") + '</div>';
    }).join("");
    addExports($("lb-groups"));
  });
}
var LB_BY = { efficiency: "SAW score (efficiency)" };
function loadOverall() {
  var by = (document.querySelector('input[name="lb-by"]:checked') || {}).value || "efficiency";
  $("lb-overall").innerHTML = skel(4, true);
  api("/api/leaderboard?view=overall&by=" + enc(by) + ($("lb-demo").checked ? "&demo=1" : "")).then(function (r) {
    if (!r.ok) { $("lb-overall").innerHTML = '<div class="notice error">' + esc(r.data.error) + '</div>'; return; }
    var d = r.data, rows = d.rows || [];
    if (!rows.length) {
      $("lb-overall").innerHTML = '<div class="card empty" id="lb-overall-empty"><p>' + "No setup has two or more models yet." +
        '</p><p class="muted small">Benchmark at least two models on the same flows.</p><a class="btn primary" href="#/new">New benchmark</a></div>';
      return;
    }
    var top = rows.filter(function (x) { return x.wins === rows[0].wins && x.win_rate === rows[0].win_rate && x.rivals_beaten === rows[0].rivals_beaten; });
    $("lb-overall").innerHTML =
      '<div class="card" id="lb-overall-head"><p style="margin:0">Most often #1 by ' + esc(LB_BY[by]) + ': <b>' + top.map(function (x) { return esc(x.model); }).join(", ") + '</b> — ' +
        rows[0].wins + ' of ' + rows[0].setups + ' setup' + (rows[0].setups === 1 ? "" : "s") + '.' + infoI(d.rule) + '</p>' +
        '<p class="muted small" style="margin-bottom:0">Counted over ' + d.n_setups + ' setup' + (d.n_setups === 1 ? "" : "s") + ' with 2+ models' +
        (d.n_setups_single_model ? ' (' + d.n_setups_single_model + ' with one model skipped)' : "") + '.' +
        ' Ranked on resource efficiency (latency, VRAM, tokens).</p></div>' +
      '<div class="card"><div class="table-wrap"><table class="lb-table" id="lb-overall-table"><thead><tr><th class="num">Rank</th><th>Model</th><th class="num">Wins</th><th class="num">Win rate</th>' +
        '<th class="num">Rivals beaten' + infoI("The share of other models it placed above, averaged over its setups.") + '</th><th class="num">Setups</th><th class="num">Sessions</th></tr></thead><tbody>' +
        rows.map(function (x) {
          return '<tr><td class="num">' + x.rank + '</td><td class="mono">' + esc(x.model) + extraTag(x.model) + (x.few_data ? ' <span class="chip warn" title="Measured on only one setup">few data</span>' : "") +
            '</td><td class="num">' + x.wins + '</td><td class="num">' + fmtPct(x.win_rate) + '</td><td class="num">' + fmtPct(x.rivals_beaten) + '</td><td class="num">' + x.setups + '</td><td class="num">' + x.n_sessions + '</td></tr>';
        }).join("") + '</tbody></table></div></div>';
    addExports($("lb-overall"));
  });
}
document.querySelectorAll('input[name="lb-sort"]').forEach(function (r) { r.addEventListener("change", loadPerSetup); });
document.querySelectorAll('input[name="lb-by"]').forEach(function (r) { r.addEventListener("change", loadOverall); });
$("lb-demo").addEventListener("change", loadOverall);
document.querySelectorAll("#lb-tabs [data-lbtab]").forEach(function (b) {
  b.addEventListener("click", function () { lbTab(b.getAttribute("data-lbtab")); });
  b.addEventListener("keydown", function (ev) {
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    var t = LB_TABS[(LB_TABS.indexOf(LB.tab) + (ev.key === "ArrowRight" ? 1 : LB_TABS.length - 1)) % LB_TABS.length]; lbTab(t); $("lbtab-" + t).focus();
  });
});
