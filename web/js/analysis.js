// AgentMeter web app — analysis.js: Consistency (Leaderboard tab), charts, exports (CSV / LaTeX / SVG / PNG), the reproducibility package and the remaining session cards.
// One of the app's scripts (web/app.html loads them in order: core, home, wizard, session, analysis,
// compare, settings, boot); they share the page's global scope, so a function defined in one is used by the others.
"use strict";
// ---------- CONSISTENCY (Leaderboard tab, route #/trends; was Repeatability) ----------
// Do you get the same result twice? One card per model on a setup that ran 2 or more times: its runs
// as dots against the "same result" band, a verdict (the rules in insights.js: repeatSummary and its
// drift rule, unchanged), one plain sentence and the next step. Models run once are one line below.
var TR = { data: null, series: null, metric: null, sched: [], focus: null };
var TRF = { model: "", set: "", gpu: "", measure: "mean_latency_s", status: "" };
var TR_MEASURES = [["mean_latency_s", "Speed", 0.10], ["mean_tokens_per_flow", "Tokens", 0.05], ["max_peak_vram_mb", "GPU memory", 0.05]];
var TR_VERDICT = { stable: ["Steady", "ok"], varies: ["Varies", "warn"], moved: ["Changed recently", "bad"], few: ["One more run for a verdict", "none"] };
var SCHED_EVERY = [[6, "every 6 hours"], [12, "every 12 hours"], [24, "daily"], [48, "every 2 days"], [168, "weekly"]];
function everyLabel(h) { var e = SCHED_EVERY.filter(function (x) { return x[0] === h; })[0]; return e ? e[1] : "every " + h + " hours"; }
function loadTrends(q) {
  q = q || {};
  TR.series = q.series || (q.model && q.setup ? q.model + "||" + q.setup : null);
  TR.focus = q.session || null;
  if (q.metric) { TR.metric = q.metric; if (TR_MEASURES.some(function (m) { return m[0] === q.metric; })) TRF.measure = q.metric; }
  $("tr-examples").innerHTML =
    '<div>' + trStrip([2.40, 2.42, 2.38, 2.41], 0.1, null, 200, 80) + '<b>Steady</b><span>runs agree</span></div>' +
    '<div>' + trStrip([2.20, 2.75, 2.05, 2.55], 0.1, null, 200, 80) + '<b>Varies</b><span>runs disagree</span></div>' +
    '<div>' + trStrip([1.90, 1.94, 1.92, 2.60], 0.1, { moved: true }, 200, 80) + '<b>Changed recently</b><span>the latest run jumped</span></div>';
  $("tr-summary").innerHTML = skel(5, true); $("tr-body").innerHTML = ""; $("tr-once").innerHTML = "";
  allSessions().then(function (r) {
    if (!r.ok) { $("tr-summary").innerHTML = '<div class="notice error">' + esc(r.data.error) + '</div>'; return; }
    TR.data = r.data.sessions || [];
    renderTrends();
    if (TR.series) trOpen(TR.series);
  });
  loadSchedules();
}
// demo runs are simulated: checked only when there are no real runs at all (as on the Leaderboard)
function trDemo(data) { return !data.some(function (s) { return s.status === "Done"; }) && data.some(function (s) { return s.status === "Demo"; }); }
function trSetName(s) {
  var byId = {}; (TR.data || []).forEach(function (x) { byId[x.session_id] = x; });
  for (var i = 0; i < s.points.length; i++) {
    var rn = (byId[s.points[i].session_id] || {}).run_name;
    if (rn) return String(rn).split("/").pop().replace(/_\d{8}-\d{6}_[0-9a-f]+$/, "") + (s.setup.n_flows != null ? " (" + s.setup.n_flows + " flows)" : "");
  }
  return "set " + String(s.setup.prepared_set_sha256 || "").slice(0, 8);
}
// the runs of one measure as dots against the band around their median (an SVG drawn to the card's width)
function trStrip(vals, band, opt, W, H) {
  opt = opt || {}; W = W || 520; H = H || 90;
  var vs = vals.filter(function (v) { return v != null; });
  if (vs.length < 1) return '<div class="cs-none">Not measured in these runs</div>';
  var sorted = vs.slice().sort(function (a, b) { return a - b; }), h = Math.floor(sorted.length / 2), med = sorted.length % 2 ? sorted[h] : (sorted[h - 1] + sorted[h]) / 2;
  var lo = Math.min(Math.min.apply(null, vs), med * (1 - band)) * 0.96, hi = Math.max(Math.max.apply(null, vs), med * (1 + band)) * 1.04;
  if (hi === lo) { hi = lo + 1; }
  var x = function (i) { return 18 + i * ((W - 36) / Math.max(1, vals.length - 1)); }, y = function (v) { return H - 12 - (v - lo) / (hi - lo) * (H - 24); };
  var b0 = y(med * (1 + band)), b1 = y(med * (1 - band));
  return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;display:block" role="img" aria-label="' + esc(opt.label || "each run") + '">' +
    '<rect x="6" y="' + b0 + '" width="' + (W - 12) + '" height="' + Math.max(2, b1 - b0) + '" rx="5" fill="var(--success-soft)"/>' +
    '<line x1="6" x2="' + (W - 6) + '" y1="' + y(med) + '" y2="' + y(med) + '" stroke="var(--success)" stroke-dasharray="3 4" stroke-width="1.2"/>' +
    vals.map(function (v, i) {
      if (v == null) return "";
      var last = i === vals.length - 1, out = Math.abs(v - med) / Math.abs(med || 1) > band || (opt.moved && last);
      var focus = opt.focus != null && opt.focus === i;
      return (focus ? '<circle cx="' + x(i) + '" cy="' + y(v) + '" r="10" fill="none" stroke="var(--text)" stroke-width="2"/>' : "") +
        '<circle cx="' + x(i) + '" cy="' + y(v) + '" r="' + (last ? 6 : 5) + '" fill="' + (out ? "#E8742A" : "var(--accent)") + '" stroke="var(--surface)" stroke-width="2"><title>' +
        esc((opt.titles || [])[i] || "") + '</title></circle>';
    }).join("") + '</svg>';
}
function trFmt(key, v) { return key === "mean_latency_s" ? fmtTime(v) : key === "max_peak_vram_mb" ? fmtMB(v) : fmtNum(v); }
function trBetween(key, lo, hi) {
  return key === "mean_latency_s" ? "took between " + trFmt(key, lo) + " and " + trFmt(key, hi) + " per flow"
    : key === "max_peak_vram_mb" ? "needed between " + trFmt(key, lo) + " and " + trFmt(key, hi) + " of working GPU memory"
    : "used between " + trFmt(key, lo) + " and " + trFmt(key, hi) + " tokens per flow";
}
function trCard(x, s) {
  var key = TRF.measure, m = TR_MEASURES.filter(function (q) { return q[0] === key; })[0], ts = INSIGHTS.trendSeries(s, key);
  var vals = ts.points.map(function (p) { return p.v; }), vs = vals.filter(function (v) { return v != null; });
  var v = TR_VERDICT[x.status], name = trSetName(s);
  var lo = vs.length ? Math.min.apply(null, vs) : null, hi = vs.length ? Math.max.apply(null, vs) : null;
  var sorted = vs.slice().sort(function (a, b) { return a - b; }), h = Math.floor(sorted.length / 2), med = !sorted.length ? null : sorted.length % 2 ? sorted[h] : (sorted[h - 1] + sorted[h]) / 2;
  var swing = med ? (hi - lo) / med : 0, NAME = { mean_latency_s: "speed", mean_tokens_per_flow: "tokens", max_peak_vram_mb: "GPU memory" };
  var say, next = "";
  if (!vs.length) say = esc(m[1]) + " was not measured in these runs. Pick another measure above.";
  else if (x.status === "stable") say = "Every run " + trBetween(key, lo, hi) + ". You can trust this result.";
  else if (x.status === "varies") {
    var others = (x.over || []).filter(function (k) { return k !== key; }).map(function (k) { return NAME[k]; });
    say = (x.over || []).indexOf(key) >= 0 ? "Runs " + trBetween(key, lo, hi) + ", a " + fmtNum(100 * swing, 0) + "% swing. One run alone could mislead you."
      : "On " + NAME[key] + " the runs agree, but they differ on " + others.join(" and ") + ". Pick it under Measure to see the runs.";
    next = "Look at the runs outside the green band. A busy GPU or a different rental often explains it." +
      (x.checked_runs ? " " + x.checked_runs + " run" + (x.checked_runs === 1 ? " had" : "s had") + " an unusual GPU reading." : "");
  } else if (x.status === "moved") {
    var d = x.moved[0];
    say = d ? "The earlier runs agreed at about " + fmtUnit(d.unit, d.baseline) + " (" + d.metric_label.toLowerCase() + "). The latest was " + fmtUnit(d.unit, d.latest) + ", " +
      fmtNum(Math.abs(100 * d.change), 0) + "% " + (d.change > 0 ? "higher" : "lower") + "." : "The latest run left the usual spread of the earlier runs.";
    next = x.changed.length ? "Changed in the latest run: " + x.changed.map(INSIGHTS.changeText).join(", ") + "."
      : "Check what changed in the latest run: the GPU, the driver or the model version.";
  } else say = "Two runs so far: " + trFmt(key, vals[0]) + " and " + trFmt(key, vals[vals.length - 1]) + (key === "mean_latency_s" ? " per flow" : "") + ". One more run gives a verdict.";
  var focus = TR.focus ? ts.points.map(function (p) { return p.session_id; }).indexOf(TR.focus) : -1;
  var titles = ts.points.map(function (p) { return fmtDate(p.at) + ": " + (p.v == null ? "not measured" : trFmt(key, p.v)); });
  var lastSid = s.points[s.points.length - 1].session_id;
  return '<div class="cs-card' + (TR.series === s.id ? " sel" : "") + '" data-series="' + esc(s.id) + '">' +
    '<div class="cs-card-h"><div><h3>' + mdot(x.model) + esc(nick(x.model)) + extraTag(x.model) + '</h3><div class="sub">' + esc(name + ", " + (s.setup.gpu || "GPU") + ", " + x.runs + " runs") + '</div></div>' +
      '<span class="cs-chip ' + v[1] + '"><span class="cs-dot ' + v[1] + '"></span>' + esc(v[0]) + '</span></div>' +
    trStrip(vals, m[2], { moved: x.status === "moved", focus: focus >= 0 ? focus : null, titles: titles, label: m[1] + " of each run, oldest first" }) +
    '<div class="cs-axis" aria-hidden="true"><span>first run</span><span>' + esc(m[1].toLowerCase()) + ', each dot one run</span><span>latest</span></div>' +
    '<p class="say">' + esc(say) + '</p>' + (next ? '<p class="next">' + esc(next) + '</p>' : "") +
    '<div class="cs-card-f"><span>Last run ' + esc(x.last_at ? fmtDate(x.last_at) : "") + '</span><span class="actions">' +
      (x.status === "few" ? '<a class="btn primary small-btn" href="#/session/' + enc(lastSid) + '">Run it again</a>' : "") +
      '<button type="button" class="btn small-btn" data-tr-open="' + esc(s.id) + '">See every run</button></span></div></div>';
}
function renderTrends() {
  if (!TR.data || !window.INSIGHTS) return;
  var t = INSIGHTS.trends(TR.data, { includeDemo: trDemo(TR.data) });
  var bySeries = {}; t.series.forEach(function (s) { bySeries[s.id] = s; });
  var rows = INSIGHTS.repeatSummary(t).map(function (x) { x.s = bySeries[x.series]; x.set = trSetName(x.s); x.gpu = x.s.setup.gpu || "GPU"; return x; });
  var uniq = function (f) { var o = []; rows.forEach(function (x) { var v = f(x); if (v && o.indexOf(v) < 0) o.push(v); }); return o.sort(); };
  var opts = function (vals, fmt) { return [["", "All"]].concat(vals.map(function (v) { return [v, fmt ? fmt(v) : v]; })); };
  $("tr-fbar").innerHTML = fpill("tr-f-model", "Model", opts(uniq(function (x) { return x.model; }), nick), TRF.model, "") +
    fpill("tr-f-set", "Data set", opts(uniq(function (x) { return x.set; })), TRF.set, "") +
    fpill("tr-f-gpu", "GPU", opts(uniq(function (x) { return x.gpu; })), TRF.gpu, "") +
    fpill("tr-f-measure", "Measure", TR_MEASURES.map(function (m) { return [m[0], m[1]]; }), TRF.measure, "mean_latency_s");
  var shown = rows.filter(function (x) { return (!TRF.model || x.model === TRF.model) && (!TRF.set || x.set === TRF.set) && (!TRF.gpu || x.gpu === TRF.gpu); });
  var multi = shown.filter(function (x) { return x.runs >= 2; }), once = shown.filter(function (x) { return x.runs === 1; });
  var cnt = { stable: 0, varies: 0, moved: 0, few: 0 }; multi.forEach(function (x) { cnt[x.status]++; });
  if (TRF.status && !cnt[TRF.status]) TRF.status = "";
  $("tr-tally").innerHTML = !multi.length ? "" : '<button type="button" data-tr-status="" aria-pressed="' + String(!TRF.status) + '">All <b>' + multi.length + '</b></button>' +
    ["stable", "varies", "moved", "few"].filter(function (k) { return cnt[k]; }).map(function (k) {
      return '<button type="button" data-tr-status="' + k + '" aria-pressed="' + String(TRF.status === k) + '"><span class="cs-dot ' + TR_VERDICT[k][1] + '"></span>' + esc(TR_VERDICT[k][0]) + ' <b>' + cnt[k] + '</b></button>';
    }).join("");
  var cards = multi.filter(function (x) { return !TRF.status || x.status === TRF.status; });
  if (!rows.length) {
    $("tr-summary").innerHTML = '<div class="card empty" id="tr-empty"><h2>No finished runs yet</h2><p>Run a benchmark, then run it again on the same data and GPU. Each model then shows here with its runs side by side.</p>' +
      '<a class="btn primary" href="#/new">New benchmark</a></div>';
  } else if (!multi.length) {
    $("tr-summary").innerHTML = '<div class="card" id="tr-empty"><h2>No model has run twice yet</h2><p style="margin-bottom:0">A check needs at least two runs of the same model on the same data and GPU. Run one of the models below again, or repeat a run automatically.</p></div>';
  } else {
    $("tr-summary").innerHTML = '<div class="cs-cards" id="tr-cards">' + cards.map(function (x) { return trCard(x, x.s); }).join("") + '</div>';
  }
  $("tr-once").innerHTML = !once.length ? "" : '<div class="card" id="tr-once-card"><p style="margin:0"><b>' + once.length + ' model' + (once.length === 1 ? " has" : "s have") +
    ' only one run so far.</b> Run ' + (once.length === 1 ? "it" : "them") + ' again to check.</p><ul class="small">' + once.map(function (x) {
      return '<li><a href="#/session/' + enc(x.s.points[0].session_id) + '">' + esc(nick(x.model)) + '</a>' + extraTag(x.model) + ' on ' + esc(x.set + ", " + x.gpu) + '</li>';
    }).join("") + '</ul></div>';
  if (TR.series && bySeries[TR.series]) {
    TR.metric = TRF.measure;
    $("tr-body").innerHTML = historyCard(bySeries[TR.series], INSIGHTS.trendSeries(bySeries[TR.series], TR.metric), t);
    addExports($("tr-body"));
  } else $("tr-body").innerHTML = "";
}
function trOpen(id) {
  TR.series = id; renderTrends();
  var c = $("tr-chart-card"); if (c) c.scrollIntoView({ block: "start" });
}
function historyCard(s, ts, t) {
  var pts = ts.points.filter(function (p) { return p.v != null; });
  var head = '<div class="card" id="tr-chart-card"><div class="row" style="margin-top:0;justify-content:space-between"><h2>Every run of ' + esc(nick(s.model)) + ': ' + esc(ts.label.toLowerCase()) + extraTag(s.model) + '</h2>' +
    '<button type="button" class="btn small-btn" data-tr-close>Close</button></div><p class="muted small" style="margin-top:0">' +
    esc((s.setup.gpu || "GPU") + ", " + (s.setup.input_type || "") + ", " + (s.setup.n_flows == null ? "" : s.setup.n_flows + " flows") + ", prepared set " + String(s.setup.prepared_set_sha256).slice(0, 12) +
        ", settings " + String(s.setup.settings_fingerprint).slice(0, 8)) + ', ' + (ts.lower_is_better ? "lower is better" : "higher is better") + '</p>';
  if (!pts.length) return head + '<p class="muted" id="tr-none">Not measured in these runs (e.g. demo runs have no GPU energy or price).</p></div>';
  var d = ts.drift, verdict = !d.enough ? '<p class="small" id="tr-verdict">' + pts.length + ' run' + (pts.length === 1 ? "" : "s") + ' so far. A judgement needs at least ' + t.min_points + '.</p>'
    : '<p class="small" id="tr-verdict">Latest run ' + esc(fmtUnit(ts.unit, d.latest)) + ' vs ' + esc(fmtUnit(ts.unit, d.baseline)) + ' (median of the ' + (d.n - 1) + ' earlier runs), a change of ' +
      (d.change > 0 ? "+" : "") + fmtNum(100 * d.change, 1) + '%. ' + (d.flagged ? '<b>' + (d.worse ? "Worse" : "Better") + ' than the usual spread (±' + fmtNum(100 * d.band, 0) + '%).</b>' : 'Within the usual spread (±' + fmtNum(100 * d.band, 0) + '%).') + '</p>';
  return head + verdict + '<div id="tr-plot">' + trendSvg(ts, pts, d) + '</div>' +
    '<div class="table-wrap"><table id="tr-table"><thead><tr><th>Finished</th><th>Session</th><th class="num">' + esc(ts.label) + '</th><th class="num">Change vs previous</th><th>What was different</th></tr></thead><tbody>' +
    ts.points.slice().reverse().map(function (p) {
      return '<tr><td>' + esc(fmtDate(p.at)) + '</td><td><a class="mono" href="#/session/' + enc(p.session_id) + '">' + esc(shortId(p.session_id)) + '</a>' + (p.scheduled ? ' <span class="chip">scheduled</span>' : "") +
        '</td><td class="num">' + esc(fmtUnit(ts.unit, p.v)) + '</td><td class="num">' + (p.delta == null ? "—" : (p.delta > 0 ? "+" : "") + fmtNum(100 * p.delta, 1) + "%") +
        '</td><td class="small">' + ([p.changed.length ? p.changed.map(INSIGHTS.changeText).join(", ") : null,
          p.health && p.health.status === "check" ? "GPU not normal. " + (p.health.reasons || []).slice(0, 2).join(". ") : null].filter(Boolean).map(esc).join("<br>") || '<span class="muted">—</span>') + '</td></tr>';
    }).join("") + '</tbody></table></div></div>';
}
function trendSvg(ts, pts, d) {
  var W = 640, H = 260, L = 70, R = 20, T = 16, B = 40;
  var xs = pts.map(function (p) { return Date.parse(p.at) || 0; }), x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
  var ys = pts.map(function (p) { return p.v; }), my = Math.max.apply(null, ys.concat(d.enough ? [d.baseline] : [])) * 1.15 || 1;
  var X = function (v) { return x1 === x0 ? (L + W - R) / 2 : L + (W - L - R) * (v - x0) / (x1 - x0); }, Y = function (v) { return T + (H - T - B) * (1 - v / my); };
  var line = pts.map(function (p, i) { return (i ? "L" : "M") + X(xs[i]) + " " + Y(p.v); }).join(" ");
  return '<svg viewBox="0 0 ' + W + ' ' + H + '" class="frontier trend" role="img" aria-label="' + esc(ts.label + " over " + pts.length + " runs") + '">' +
    [0, 0.5, 1].map(function (f) { return '<line class="grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(my * f) + '" y2="' + Y(my * f) + '"/><text class="ax" x="' + (L - 6) + '" y="' + (Y(my * f) + 4) + '" text-anchor="end">' + esc(fmtUnit(ts.unit, my * f)) + '</text>'; }).join("") +
    '<text class="ax" x="' + L + '" y="' + (H - B + 18) + '">' + esc(fmtDate(pts[0].at).slice(0, 10)) + '</text>' +
    (pts.length > 1 ? '<text class="ax" x="' + (W - R) + '" y="' + (H - B + 18) + '" text-anchor="end">' + esc(fmtDate(pts[pts.length - 1].at).slice(0, 10)) + '</text>' : "") +
    (d.enough ? '<line class="base" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(d.baseline) + '" y2="' + Y(d.baseline) + '"/><text class="ax" x="' + (W - R) + '" y="' + (Y(d.baseline) - 5) + '" text-anchor="end">earlier median</text>' : "") +
    (pts.length > 1 ? '<path class="tline" d="' + line + '"/>' : "") +
    pts.map(function (p, i) {
      var tip = fmtDate(p.at) + ": " + fmtUnit(ts.unit, p.v) + (p.scheduled ? " (scheduled run)" : "");
      return '<g tabindex="0" data-hover="' + esc(tip) + '" aria-label="' + esc(tip) + '"><circle cx="' + X(xs[i]) + '" cy="' + Y(p.v) + '" r="6" style="fill:var(--series-1)"' + (p.scheduled ? ' class="sched"' : "") + '/></g>';
    }).join("") + '</svg>';
}
function loadSchedules() {
  var host = $("tr-schedules");
  backendFetch("/api/schedules").then(function (r) {
    if (!r.ok) {
      host.innerHTML = '<h2>Repeat a run automatically</h2><p class="muted" style="margin-bottom:0">' + esc(r.data.code === "backend_offline" || r.status === 0 ? "The GPU backend is offline. Schedules run there, so they cannot be listed now." : (r.data.error || "Could not load the schedules.")) + '</p>';
      return;
    }
    TR.sched = r.data.schedules || [];
    host.innerHTML = '<h2>Repeat a run automatically</h2><p class="muted small" style="margin-top:0">Pick a session and AgentMeter runs it again on a schedule, for example every night for a week, so this page fills in by itself. Start one from a session\'s Overview tab (Repeat on a schedule).</p>' +
      (TR.sched.length ? '<div class="table-wrap"><table id="tr-sched-table"><thead><tr><th>Repeats</th><th>Models</th><th>How often</th><th class="num">Runs</th><th>Next</th><th>Owner</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>' +
        TR.sched.map(schedRow).join("") + '</tbody></table></div>' : '<p class="muted" id="tr-sched-none" style="margin-bottom:0">No schedules yet.</p>');
  });
}
function schedRow(x) {
  var state = x.active ? (x.waiting ? '<span class="small muted">' + esc(fmtDate(x.next_at)) + ' — ' + esc(x.waiting) + '</span>' : esc(fmtDate(x.next_at)))
    : '<span class="chip">' + esc(x.ended || "ended") + '</span>' + (x.last_error ? ' <span class="small muted">' + esc(x.last_error) + '</span>' : "");
  return '<tr data-sch="' + esc(x.schedule_id) + '"><td><a class="mono" href="#/session/' + enc(x.source_job_id) + '">' + esc(shortId(x.source_job_id)) + '</a></td><td class="small">' + esc((x.models || []).map(short).join(", ")) +
    '</td><td>' + esc(everyLabel(x.every_hours)) + '</td><td class="num">' + x.runs_done + ' / ' + x.runs_total + '</td><td>' + state + '</td><td>' + esc(x.owner || "—") + '</td><td>' +
    (x.active ? '<button type="button" class="btn small-btn danger-lite" data-sch-cancel="' + esc(x.schedule_id) + '" data-sch-job="' + esc(x.source_job_id) + '">Cancel</button>' : "") + '</td></tr>';
}
function cancelSchedule(id, jobId, done) {
  confirmAction({ title: "Cancel this schedule?", body: "No further runs start. Runs already made are kept.", ok: "Cancel schedule", cancel: "Keep schedule", danger: true }).then(function (yes) {
  if (!yes) return;
  backendFetch("/api/schedules/" + enc(id) + "/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: jobId }) }).then(function (r) {
    if (!r.ok) { alert(r.data.error || "Could not cancel the schedule."); return; }
    done();
  });
  });
}
document.addEventListener("change", function (ev) {
  var k = { "tr-f-model": "model", "tr-f-set": "set", "tr-f-gpu": "gpu", "tr-f-measure": "measure" }[ev.target.id];
  if (k) { TRF[k] = ev.target.value; renderTrends(); }
});
document.addEventListener("click", function (ev) {
  var st = ev.target.closest && ev.target.closest("[data-tr-status]");
  if (st) { TRF.status = st.getAttribute("data-tr-status"); renderTrends(); return; }
  var o = ev.target.closest && ev.target.closest("[data-tr-open]");
  if (o) { trOpen(o.getAttribute("data-tr-open")); return; }
  if (ev.target.closest && ev.target.closest("[data-tr-close]")) { TR.series = null; renderTrends(); return; }
  var c = ev.target.closest && ev.target.closest("[data-sch-cancel]");
  if (c && c.closest("#tr-schedules")) cancelSchedule(c.getAttribute("data-sch-cancel"), c.getAttribute("data-sch-job"), loadSchedules);
});
// Overview: how often this session's models were measured on its setup, and whether they held
function wireRepLine(job) {
  var host = $("svc-rep-line");
  if (!host || !window.INSIGHTS) return;
  var st = String(job.status || "").toLowerCase();
  if (!(st === "done" || st === "demo")) return;
  allSessions().then(function (r) {
    if (!r.ok || !$("svc-rep-line") || $("svc-rep-line") !== host) return;
    var all = r.data.sessions || [], me = all.filter(function (x) { return x.session_id === job.job_id; })[0];
    var key = me && INSIGHTS.setupKey(me);
    if (!key) return;
    var rows = INSIGHTS.repeatSummary(INSIGHTS.trends(all, { includeDemo: me.status === "Demo" })).filter(function (x) {
      return x.series.slice(x.series.indexOf("||") + 2) === key && (me.models || []).indexOf(x.model) >= 0;
    });
    if (!rows.length) return;
    host.innerHTML = '<h2 style="margin-bottom:6px">Is this result consistent?</h2><ul class="lk-list" id="svc-rep-list">' + rows.map(function (x) {
      var what = x.runs === 1 ? "run once on this setup, run it again to check" : x.runs < 3 ? x.runs + " runs on this setup, one more gives a verdict" :
        x.runs + " runs, " + (x.status === "stable" ? "steady" : x.status === "moved" ? "changed recently" : "varies");
      return '<li><b>' + esc(nick(x.model)) + '</b>: <a href="' + repLink(x.model, key) + '&session=' + enc(job.job_id) + '">' + esc(what) + '</a></li>';
    }).join("") + '</ul>' + (rows.every(function (x) { return x.runs < 3; }) ? '<p class="small muted" style="margin:6px 0 0">Run this setup again (or repeat it on a schedule below) to check that the results hold.</p>' : "");
    host.hidden = false;
  });
}
// Overview: repeat this session on a schedule (finished sessions; the owner or an admin)
function scheduleCard(job) {
  var st = String(job.status || "").toLowerCase();
  if (!(st === "done" || st === "demo") || !canEditNotes(job)) return "";
  return '<form class="card" id="svc-schedule" novalidate><h2>Repeat on a schedule</h2><p class="muted small" style="margin-top:0">Runs this session again on this GPU with the same flows, models and settings, so <a href="#/trends">Leaderboard › Consistency</a> can show whether the numbers hold over time. Each run is an ordinary session in the queue, and a run waits while the previous one is still going.</p>' +
    '<div class="row" style="gap:16px;flex-wrap:wrap;align-items:flex-end"><div class="field" style="margin:0"><label for="sch-every">How often</label><select id="sch-every">' +
    SCHED_EVERY.map(function (e) { return '<option value="' + e[0] + '"' + (e[0] === 24 ? " selected" : "") + '>' + esc(e[1]) + '</option>'; }).join("") + '</select></div>' +
    '<div class="field" style="margin:0"><label for="sch-runs">Number of runs <span class="hint">(1–30)</span></label><input type="number" id="sch-runs" min="1" max="30" step="1" value="7" inputmode="numeric"></div>' +
    '<div class="actions" style="margin:0"><button type="submit" class="btn" id="sch-save">Start schedule</button></div></div>' +
    '<div id="sch-error" class="notice error" role="alert" hidden></div><div id="sch-list" class="small" style="margin-top:10px"></div></form>';
}
function wireSchedule(job) {
  var f = $("svc-schedule");
  if (!f) return;
  var list = function () {
    backendFetch("/api/schedules").then(function (r) {
      if (!r.ok || !$("sch-list")) return;
      var mine = (r.data.schedules || []).filter(function (x) { return x.source_job_id === job.job_id && x.active; });
      $("sch-list").innerHTML = mine.map(function (x) {
        return '<div class="row" style="margin:4px 0;gap:8px;align-items:center" data-sch="' + esc(x.schedule_id) + '"><span class="chip ok">active</span> ' + esc(everyLabel(x.every_hours)) + ', ' + x.runs_done + ' of ' + x.runs_total + ' runs done, next ' + esc(fmtDate(x.next_at)) +
          (x.waiting ? ' (' + esc(x.waiting) + ')' : "") + ' <button type="button" class="btn small-btn danger-lite" data-sch-cancel="' + esc(x.schedule_id) + '">Cancel</button></div>';
      }).join("");
    });
  };
  list();
  f.addEventListener("click", function (ev) {
    var c = ev.target.closest("[data-sch-cancel]");
    if (c) cancelSchedule(c.getAttribute("data-sch-cancel"), job.job_id, list);
  });
  f.addEventListener("submit", function (ev) {
    ev.preventDefault();
    $("sch-error").hidden = true;
    var runs = +$("sch-runs").value;
    if (!(runs >= 1 && runs <= 30 && Math.floor(runs) === runs)) { showError($("sch-error"), { error: "The number of runs must be a whole number from 1 to 30." }); return; }
    $("sch-save").disabled = true;
    backendFetch("/api/schedules", { method: "POST", headers: { "Content-Type": "application/json" },
                                     body: JSON.stringify({ job_id: job.job_id, every_hours: +$("sch-every").value, runs: runs }) }).then(function (r) {
      $("sch-save").disabled = false;
      if (!r.ok) { showError($("sch-error"), r.data); return; }
      list();
    });
  });
}
// Overview: notes and tags (owner or admin edits; with a control plane the Worker stores them)
function canEditNotes(job) { return CP ? !!job.can_edit : true; }
function notesCard(job) {
  var tags = job.tags || [], edit = canEditNotes(job);
  var view = (tags.length ? '<div class="tag-row">' + tags.map(function (t) { return '<span class="chip tag-chip">' + esc(t) + '</span>'; }).join("") + '</div>' : "") +
    (job.notes ? '<p class="notes-text">' + esc(job.notes) + '</p>' : "");
  if (!edit) return '<div class="card" id="svc-notes"><h2>Notes and tags</h2>' + (view || '<p class="muted small">No notes.</p>') + '<p class="muted small">Only the session\'s owner or an admin can edit them.</p></div>';
  return '<form class="card" id="svc-notes" novalidate><h2>Notes and tags</h2><p class="muted small" style="margin-top:0">Your own labels for this session, such as what you were testing. You can search them under Sessions, and they never change a result.</p>' +
    '<div class="field"><label for="svc-notes-text">Notes</label><textarea id="svc-notes-text" rows="3" maxlength="2000" placeholder="e.g. rerun after the tokenizer fix">' + esc(job.notes || "") + '</textarea></div>' +
    '<div class="field"><label for="svc-notes-tags">Tags <span class="hint">(comma-separated, up to 10)</span></label><input type="text" id="svc-notes-tags" class="wide" value="' + esc(tags.join(", ")) + '" placeholder="thesis ch4, baseline" autocomplete="off"></div>' +
    '<div class="actions"><button type="submit" class="btn" id="svc-notes-save">Save notes and tags</button><span class="small muted" id="svc-notes-saved" role="status"></span></div>' +
    '<div id="svc-notes-error" class="notice error" role="alert" hidden></div></form>';
}
function wireNotes(job) {
  var f = $("svc-notes");
  if (!f || f.tagName !== "FORM") return;
  f.addEventListener("submit", function (ev) {
    ev.preventDefault();
    $("svc-notes-error").hidden = true; $("svc-notes-saved").textContent = "";
    var body = { notes: $("svc-notes-text").value, tags: $("svc-notes-tags").value.split(",") };
    $("svc-notes-save").disabled = true;
    api("/api/sessions/" + enc(job.job_id), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(function (r) {
      $("svc-notes-save").disabled = false;
      if (!r.ok) { showError($("svc-notes-error"), r.data); return; }
      job.notes = r.data.notes; job.tags = r.data.tags;
      $("svc-notes-tags").value = (r.data.tags || []).join(", "); $("svc-notes-text").value = r.data.notes || "";
      $("svc-notes-saved").textContent = "Saved.";
    });
  });
}
// Performance -> Statistics: how many flows a reliable latency comparison needs
function sampleSizeCard(res) {
  var ss = window.INSIGHTS && INSIGHTS.sampleSize(res);
  if (!ss) return "";
  var pct = function (v) { return v == null ? "—" : fmtNum(100 * v, v < 0.1 ? 1 : 0) + "%"; };
  var lead = ss.single
    ? esc("With " + ss.models[0].n + " flows, this model's mean latency is known to within ±" + fmtTime(ss.ci_half_s) + " (±" + pct(ss.ci_rel) + ", 95%).")
    : "With " + ss.models[0].n + " and " + ss.models[1].n + " flows and this spread, latency differences smaller than about <b>" + esc(fmtTime(ss.mde_s)) +
      " (" + pct(ss.mde_rel) + ")</b> cannot be told apart reliably. This session's gap is " + esc(fmtTime(ss.gap_s)) + " (" + pct(ss.gap_rel) + "), which is " +
      (ss.gap_detectable ? "large enough to detect." : "<b>smaller than that</b>, so more flows would be needed to be sure.");
  return '<div class="card" id="svc-samplesize"><h2>How many flows do you need?</h2><p class="small" style="margin-top:0" id="svc-samplesize-lead">' + lead + '</p>' +
    '<div class="table-wrap"><table id="svc-samplesize-table"><thead><tr><th>' + (ss.single ? "Pin the mean latency to within" : "To detect a latency difference of") + '</th><th class="num">Flows per model</th><th>This session</th></tr></thead><tbody>' +
    ss.needed.map(function (r) {
      var have = Math.min.apply(null, ss.models.map(function (m) { return m.n; }));
      return '<tr><td>' + fmtNum(100 * r.target) + '%</td><td class="num">' + fmtNum(r.flows) + '</td><td>' + (have >= r.flows ? '<span class="chip ok">enough (' + have + ')</span>' : '<span class="chip">' + have + ' (' + fmtNum(r.flows - have) + ' more needed)</span>') + '</td></tr>';
    }).join("") + '</tbody></table></div>' +
    '<p class="muted small">Normal approximation from each model\'s per-flow latency spread (two-sided 5% significance, 80% power' + (ss.single ? ", 95% interval" : "") + '). This is a planning guide, not a guarantee. The tests above use the measured flows.</p></div>';
}
// Performance -> Statistics: repeatability across sessions on the same flows, GPU and settings
var REP_FMT = { s: fmtTime, tokens: function (v) { return fmtNum(v, 1); }, MB: fmtMB, Wh: fmtWh, share: fmtPct, points: function (v) { return fmtNum(v, 1); } };
function wireRepeat(res, job) {
  var host = $("svc-repeat-host"), id = job.job_id;
  if (!host || !window.INSIGHTS) return;
  var again = '<a class="btn" href="#/new/models?set=' + enc(job.run_name || "") + (CP && job.stored ? "&from=" + enc(id) : "") + '">Benchmark these flows again</a>';
  var head = '<h2>Consistency across runs</h2><p class="muted small" style="margin-top:0">The same models on the same flows, GPU and settings in other sessions. How much do the numbers move from run to run?</p>';
  sameSetup(id).then(function (d) {
    if (SESSION.id !== id) return;
    var r = { ok: d.ok, data: { error: d.error } };
    if (!d.merged) {
      host.innerHTML = '<div class="card" id="svc-repeat">' + head + '<p>' + (r.ok ? "No other session ran on these flows, GPU and settings yet." : "Could not look for other sessions: " + esc((r.data || {}).error || "")) +
        ' Run the same models again to see how stable these numbers are.</p><div class="actions">' + again + '</div></div>';
      return;
    }
    host.innerHTML = repeatCard(INSIGHTS.repeatability(d.merged, id), head, again, id);
    addExports(host);
  });
}
function repeatCard(rp, head, again, id) {
  var lead = rp.leader ? '<p class="finding-line">' + (rp.leader.current ? "Faster model in this session, <b>" + esc(short(rp.leader.current)) + "</b>, was also faster in " + rp.leader.agree + " of " + rp.leader.sessions + " sessions."
    : "The two models tie on mean latency in this session.") + '</p>' : "";
  var metrics = (rp.models[0] || { metrics: [] }).metrics.map(function (d) { return d.metric; });
  var rows = metrics.map(function (k) {
    var per = rp.models.map(function (m) { return { model: m.model, d: m.metrics.filter(function (x) { return x.metric === k; })[0] }; }).filter(function (x) { return x.d; });
    if (!per.length) return "";
    var f = REP_FMT[per[0].d.unit] || function (v) { return fmtNum(v, 2); };
    var lo = Math.min.apply(null, per.map(function (x) { return x.d.min; })), hi = Math.max.apply(null, per.map(function (x) { return x.d.max; }));
    var pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.05 || 1, a = lo - pad, b = hi + pad;
    var dots = per.map(function (x) {
      var lane = per.length > 1 ? (MODEL_SLOT[x.model] ? 7 : -7) : 0;
      return x.d.values.map(function (v) {
        var t = short(x.model) + ", " + per[0].d.label + ": " + f(v.value) + (v.current ? " (this session)" : " (" + shortId(v.session) + ")");
        return '<span class="dot' + (v.current ? " cur" : "") + '" tabindex="0" style="left:' + (100 * (v.value - a) / (b - a)) + '%;margin-top:' + (lane - 6) + 'px;--c:' + modelColor(x.model) + '" data-hover="' + esc(t) + '" aria-label="' + esc(t) + '"></span>';
      }).join("");
    }).join("");
    var spread = per.map(function (x) { return esc(short(x.model)) + ": " + (x.d.cov == null ? "one run" : "±" + fmtPct(x.d.cov) + " run to run"); }).join(", ");
    return '<div class="dot-row rep-row" data-metric="' + k + '"><span class="lbl">' + esc(per[0].d.label) + '<small>' + esc(f(a)) + ' to ' + esc(f(b)) + '</small></span><div><div class="dot-track">' + dots + '</div><p class="small muted rep-spread">' + spread + '</p></div></div>';
  }).join("");
  return '<div class="card" id="svc-repeat">' + head + '<p class="small">' + rp.n_sessions + ' sessions on this setup. The ringed dot is this session. "±" is the coefficient of variation (standard deviation ÷ mean) across runs.</p>' +
    lead + legendHtml(rp.models.map(function (m) { return m.model; })) + '<div class="dots">' + rows + '</div>' +
    '<details class="info"><summary>Show as table</summary><div class="table-wrap"><table id="svc-repeat-table"><thead><tr><th>Model</th><th>Metric</th><th class="num">Runs</th><th class="num">Mean</th><th class="num">SD</th><th class="num">CoV</th><th class="num">Min to max</th></tr></thead><tbody>' +
    rp.models.map(function (m) { return m.metrics.map(function (d) { var f = REP_FMT[d.unit] || function (v) { return fmtNum(v, 2); };
      return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td>' + esc(d.label) + '</td><td class="num">' + d.n + '</td><td class="num">' + f(d.mean) + '</td><td class="num">' + (d.sd == null ? "—" : f(d.sd)) + '</td><td class="num">' + (d.cov == null ? "—" : fmtPct(d.cov)) + '</td><td class="num">' + f(d.min) + ' to ' + f(d.max) + '</td></tr>'; }).join(""); }).join("") +
    '</tbody></table></div></details><div class="actions"><a class="btn" href="#/compare?a=' + enc(id) + '">Open on the Leaderboard</a>' + again + '</div></div>';
}
// Cost & Energy: project the measured per-flow numbers onto a daily workload
function projectionCard(res) {
  if (!window.INSIGHTS || !(res.per_model || []).length) return "";
  var pr = ((res.cost_energy || {}).price || {}).usd_per_hour;
  return '<form class="card" id="proj-card" novalidate><h2>Project to your workload</h2><p class="muted small" style="margin-top:0">Scales this session\'s measured time, cost and energy per flow to a daily volume, with flows one at a time on one GPU as measured. It is an estimate, not a quote.</p>' +
    '<div class="row"><div class="field"><label for="proj-flows">Flows per day</label><input type="number" id="proj-flows" min="0" step="1000" value="10000"></div>' +
    '<div class="field"><label for="proj-price">GPU price <span class="hint">(USD per hour)</span></label><input type="number" id="proj-price" min="0" step="0.01" placeholder="' + (pr != null ? "stored: " + pr : "e.g. 0.35") + '"></div>' +
    '<div class="field"><label for="proj-grid">Grid carbon intensity <span class="hint">(g CO₂ per kWh)</span></label><input type="number" id="proj-grid" min="0" step="10" value="475"></div></div>' +
    '<div id="proj-out" aria-live="polite"></div></form>';
}
function wireProjection(res) {
  var f = $("proj-card");
  if (!f) return;
  function draw() {
    var pv = $("proj-price").value, gv = $("proj-grid").value;
    var pj = INSIGHTS.workloadProjection(res, { flows_per_day: $("proj-flows").value, usd_per_hour: pv === "" ? null : pv, grid_g_per_kwh: gv === "" ? null : gv });
    var maxU = Math.max.apply(null, pj.models.map(function (m) { return m.utilisation || 0; }).concat([1]));
    $("proj-out").innerHTML = (pj.real ? "" : '<div class="notice warn" role="note">Demo (mock) run. The per-flow time is not a model measurement, so these figures only show the method.</div>') +
      '<div class="proj-load">' + pj.models.map(function (m) {
        var t = short(m.model) + ": " + fmtNum(m.gpu_hours_per_day, 2) + " GPU-hours a day = " + (m.utilisation == null ? "—" : fmtPct(m.utilisation)) + " of one GPU's day";
        return '<div class="cat-row"><span class="lbl">' + esc(short(m.model)) + '<small>' + (m.gpus_needed == null ? "—" : m.gpus_needed + " GPU" + (m.gpus_needed > 1 ? "s" : "")) + ' needed</small></span><div class="cat-track"><div class="proj-bar" tabindex="0" data-hover="' + esc(t) + '" aria-label="' + esc(t) + '" style="width:' + Math.min(100, 100 * (m.utilisation || 0) / maxU) + '%;--c:' + modelColor(m.model) + '"></div>' +
          (maxU > 1 ? '<span class="proj-one" style="left:' + (100 / maxU) + '%" title="one GPU\'s full day"></span>' : "") + '</div><span class="val">' + (m.utilisation == null ? "—" : fmtPct(m.utilisation)) + '</span></div>';
      }).join("") + '</div><p class="muted small">Each bar is the share of one GPU\'s 24 hours this volume takes' + (maxU > 1 ? ", and the line marks one full GPU" : "") + '.</p>' +
      '<div class="table-wrap"><table id="proj-table"><thead><tr><th>Model</th><th class="num">GPU-hours / day</th><th class="num">Max flows / GPU / day</th><th class="num">GPUs needed</th><th class="num">Cost / day</th><th class="num">Cost / 30 days</th><th class="num">Energy / day</th><th class="num">CO₂ / day</th></tr></thead><tbody>' +
      pj.models.map(function (m) {
        return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td class="num">' + fmtNum(m.gpu_hours_per_day, 2) + '</td><td class="num">' + fmtNum(m.capacity_per_gpu_day) + '</td><td class="num">' + (m.gpus_needed == null ? "—" : m.gpus_needed) +
          '</td><td class="num">' + (m.cost_per_day == null ? '<span class="muted">enter a GPU price</span>' : "$" + fmtNum(m.cost_per_day, 2)) + '</td><td class="num">' + (m.cost_per_month == null ? "—" : "$" + fmtNum(m.cost_per_month, 2)) +
          '</td><td class="num">' + (m.kwh_per_day == null ? '<span class="muted">not measured</span>' : fmtNum(m.kwh_per_day, 2) + " kWh") +
          '</td><td class="num">' + (m.co2_kg_per_day == null ? '<span class="muted">' + (m.kwh_per_day == null ? "needs measured energy" : "enter a grid value") + '</span>' : fmtNum(m.co2_kg_per_day, 2) + " kg") + '</td></tr>';
      }).join("") + '</tbody></table></div><p class="muted small">Price: ' + (pj.price_source === "yours" ? "your figure, $" + pj.usd_per_hour + "/h" : pj.price_source === "stored" ? "the session\'s stored GPU price, $" + pj.usd_per_hour + "/h" : "none, so enter one above") +
      '. Energy uses the measured Wh per 1,000 flows for the whole board. CO₂ is energy × grid carbon intensity. The default is 475 g/kWh, about the world average, so use your grid\'s value.</p>';
  }
  f.addEventListener("input", draw);
  f.addEventListener("submit", function (ev) { ev.preventDefault(); });
  draw();
}
// Cost & Energy: what one VALID answer costs (flows that end without a usable answer are paid for too)
function correctCard(res) {
  var rows = window.INSIGHTS && INSIGHTS.costPerValid(res);
  if (!rows || !rows.length) return "";
  var mx = Math.max.apply(null, rows.map(function (r) { return r.gpu_s_per_valid || 0; })) || 1;
  return '<div class="card" id="cost-valid"><h2>Cost per valid answer</h2><p class="muted small" style="margin-top:0">A flow that ends without a usable answer still costs GPU time. GPU time per valid answer = time per flow ÷ share of valid answers. Lower is better, and Output Reliability has more.</p>' +
    rows.map(function (r) {
      var t = short(r.model) + ": " + (r.gpu_s_per_valid == null ? "no valid answers" : fmtTime(r.gpu_s_per_valid) + " of GPU time per valid answer (" + fmtNum(r.flows_per_valid, 2) + " flows per valid one)");
      return '<div class="cat-row"><span class="lbl">' + esc(short(r.model)) + '<small>valid ' + fmtPct(r.valid_rate) + '</small></span><div class="cat-track"><div class="proj-bar" tabindex="0" data-hover="' + esc(t) + '" aria-label="' + esc(t) + '" style="width:' +
        (r.gpu_s_per_valid == null ? 0 : (100 * r.gpu_s_per_valid / mx).toFixed(1)) + '%;background:' + modelColor(r.model) + '"></div></div><span class="val">' + (r.gpu_s_per_valid == null ? "—" : fmtTime(r.gpu_s_per_valid)) + '</span></div>';
    }).join("") +
    '<div class="table-wrap" style="margin-top:10px"><table id="cost-valid-table"><thead><tr><th>Model</th><th class="num">Valid answers</th><th class="num">Flows per valid answer</th><th class="num">GPU time / valid</th><th class="num">Cost / 1,000 valid</th><th class="num">Energy / valid</th></tr></thead><tbody>' +
    rows.map(function (r) {
      return '<tr><td class="mono">' + esc(short(r.model)) + '</td><td class="num">' + fmtPct(r.valid_rate) + '</td><td class="num">' + fmtNum(r.flows_per_valid, 2) + '</td><td class="num">' + fmtTime(r.gpu_s_per_valid) +
        '</td><td class="num">' + (r.usd_per_1k_valid == null ? '<span class="muted">not measured</span>' : fmtUsd(r.usd_per_1k_valid)) + '</td><td class="num">' + (r.wh_per_valid == null ? '<span class="muted">not measured</span>' : fmtWh(r.wh_per_valid)) + '</td></tr>';
    }).join("") + '</tbody></table></div></div>';
}
function memoryCard(res) {
  var mem = res.memory;
  if (!mem) return '<div class="card"><p class="muted">This session predates the GPU memory breakdown.</p></div>';
  return '<div class="card" id="svc-memory-card"><h2>GPU memory</h2><div class="table-wrap"><table id="svc-memory-table"><thead><tr><th>Model</th>' +
    '<th class="num">Model weights<br><span class="small">(after load)</span></th><th class="num">Working VRAM, mean</th>' +
    '<th class="num">Working VRAM, highest</th><th class="num">Total peak<br><span class="small">(weights + working)</span></th>' +
    '<th class="num">Torch peak allocated / reserved</th><th class="num">NVML device peak</th></tr></thead><tbody>' +
    mem.per_model.map(function (r) {
      var na = '<span class="muted">' + esc(r.status === "measured" ? "—" : r.status) + '</span>';
      return '<tr><td class="mono">' + esc(r.model) + '</td><td class="num">' + (r.weights_mb != null ? fmtMB(r.weights_mb) : na) + '</td><td class="num">' + fmtMB(r.working_mean_mb) +
        '</td><td class="num">' + fmtMB(r.working_peak_mb) + '</td><td class="num"><b>' + (r.total_peak_mb != null ? fmtMB(r.total_peak_mb) : na) + '</b></td><td class="num">' +
        (r.total_peak_allocated_mb != null ? fmtNum(r.total_peak_allocated_mb) + " / " + fmtNum(r.total_peak_reserved_mb) + " MB" : na) + '</td><td class="num">' +
        (r.device_peak_nvml_mb != null ? fmtMB(r.device_peak_nvml_mb) : na) + '</td></tr>';
    }).join("") + '</tbody></table></div><details class="info"><summary>What these columns mean</summary><p class="muted small">' + esc(mem.notes.working) + ' ' + esc(mem.notes.total) +
    (mem.per_model.some(function (r) { return r.reason; }) ? ' ' + esc(mem.per_model.filter(function (r) { return r.reason; })[0].reason) + '.' : "") + '</p></details></div>';
}

// Performance: latency distribution (Latency) and effect sizes (Statistics), native
var EF_LABEL = { latency_s: "latency per flow", peak_vram_mb: "working VRAM (excludes model weights)", tokens_per_flow: "tokens per flow" };
function latencyCard(res) {
  var ld = res.latency_distribution, ef = res.effect_sizes;
  if (!ld) return '<div class="card"><p class="muted">This session predates the latency distribution.</p></div>';
  return '<div class="card" id="stats-depth"><h2>Latency distribution</h2>' +
    (ld.small_sample_note || (ef && ef.small_sample_note) ? '<div class="notice warn" id="stats-small" role="note">' + esc(ld.small_sample_note || ef.small_sample_note) + '</div>' : "") +
    histogramChart(res) +
    '<div class="table-wrap"><table id="stats-percentiles"><thead><tr><th>Model</th><th class="num">Flows</th><th class="num">p50</th><th class="num">p95</th><th class="num">p99</th><th class="num">Mean</th><th class="num">Mean without the first ' + (((ld.per_model[0] || {}).warmup || {}).n_first || 5) + ' flows</th><th>Warm-up check</th></tr></thead><tbody>' +
    ld.per_model.map(function (r) {
      return '<tr><td class="mono">' + esc(short(r.model)) + '</td><td class="num">' + fmtNum(r.n) + '</td><td class="num">' + fmtTime(r.p50) + '</td><td class="num">' + fmtTime(r.p95) +
        '</td><td class="num">' + fmtTime(r.p99) + '</td><td class="num">' + fmtTime(r.mean_s) + '</td><td class="num">' +
        ((r.warmup || {}).mean_steady_s != null ? fmtTime(r.warmup.mean_steady_s) + (r.warmup.steady_change != null ? ' <span class="small muted">(' + (r.warmup.steady_change > 0 ? "+" : "") + fmtNum(100 * r.warmup.steady_change, 1) + '%)</span>' : "") : '<span class="muted">—</span>') +
        '</td><td class="small">' + esc((r.warmup || {}).note || "—") + '</td></tr>';
    }).join("") + '</tbody></table></div>' +
    '<details class="info"><summary>How to read this</summary><p class="muted small">Histogram bins span ' + fmtTime(ld.bin_edges_s[0]) + ' to ' + fmtTime(ld.bin_edges_s[ld.bin_edges_s.length - 1]) +
    ' (shared across models). ' + esc(ld.warmup_rule) + '. The headline mean keeps every flow; "without the first flows" is the steady state, after model loading and cache warm-up. When the two differ a lot, run more flows so the warm-up weighs less.</p></details></div>';
}
function effectsCard(res) {
  var ef = res.effect_sizes;
  if (!ef) return '<div class="card"><p class="muted">This session predates the effect sizes.</p></div>';
  if (ef.skipped) return '<div class="card" id="stats-effects-card"><h2>Effect size and confidence</h2><p class="muted small">' + esc(ef.skipped) + '.</p></div>';
  var pairs = !ef.pairs ? "" : '<h3 style="margin-top:16px">Every pair (latency per flow)</h3><div class="table-wrap"><table id="stats-effect-pairs"><thead><tr><th>A vs B</th><th class="num">Cliff\'s delta</th><th>Magnitude</th><th class="num">Mean diff A − B</th><th>95% CI</th></tr></thead><tbody>' +
    ef.pairs.map(function (p) {
      var m = (p.metrics || []).filter(function (x) { return x.metric === "latency_s"; })[0];
      if (!m || !m.available) return '<tr><td>' + esc(short(p.model_a) + " vs " + short(p.model_b)) + '</td><td colspan="4" class="muted">no data</td></tr>';
      var d = function (v) { return (v < 0 ? "−" : "") + fmtTime(Math.abs(v)); };
      return '<tr><td>' + esc(short(p.model_a) + " vs " + short(p.model_b)) + '</td><td class="num">' + (m.cliffs_delta >= 0 ? "+" : "") + m.cliffs_delta.toFixed(2) + '</td><td>' + esc(m.magnitude) +
        '</td><td class="num">' + d(m.diff_mean_a_minus_b) + '</td><td>' + (m.ci_excludes_zero ? "excludes 0" : '<span class="muted">includes 0</span>') + '</td></tr>';
    }).join("") + '</tbody></table></div>';
  return '<div class="card" id="stats-effects-card"><h2>Effect size and confidence <span class="muted small">A = ' + esc(short(ef.model_a)) + ', B = ' + esc(short(ef.model_b)) + (ef.pairs ? " (" + esc(ef.pair_basis) + ")" : "") + '</span></h2>' +
    effectChart(res) +
    '<div class="table-wrap" style="margin-top:8px"><table id="stats-effects"><thead><tr><th>Metric</th><th class="num">Cliff\'s delta</th><th>Magnitude</th><th class="num">Mean diff A − B</th><th class="num">95% CI (bootstrap)</th><th class="num">Kruskal-Wallis p</th></tr></thead><tbody>' +
    ef.metrics.map(function (m) {
      if (!m.available) return '<tr><td>' + esc(EF_LABEL[m.metric] || m.metric) + '</td><td colspan="5" class="muted">' + esc(m.reason) + '</td></tr>';
      var f = m.metric === "latency_s" ? fmtTime : function (v) { return fmtNum(v, 1); };
      var d = function (v) { return (v < 0 ? "−" : "") + f(Math.abs(v)); };
      return '<tr><td>' + esc(EF_LABEL[m.metric] || m.metric) + '</td><td class="num">' + (m.cliffs_delta >= 0 ? "+" : "") + m.cliffs_delta.toFixed(2) + '</td><td>' + esc(m.magnitude) +
        '</td><td class="num">' + d(m.diff_mean_a_minus_b) + '</td><td class="num">[' + d(m.ci95_low) + ', ' + d(m.ci95_high) + ']' + (m.ci_excludes_zero ? "" : ' <span class="muted">includes 0</span>') +
        '</td><td class="num">' + (m.kruskal_wallis_p == null ? "—" : m.kruskal_wallis_p.toPrecision(3)) + '</td></tr>';
    }).join("") + '</tbody></table></div>' + pairs + '<details class="info"><summary>Method</summary><p class="muted small">' + esc(ef.method) + (ef.note ? " " + esc(ef.note) : "") + '</p></details></div>';
}
// histogram small multiples: shared bins and count scale; p50 / p95 / p99 as line styles
var PCT_STYLE = { p50: "solid", p95: "dashed", p99: "dotted" };
function histogramChart(res) {
  var h = window.INSIGHTS && INSIGHTS.latencyHistogram(res);
  if (!h || !h.ymax) return "";
  var e = h.edges, mid = (e[0] + e[e.length - 1]) / 2;
  return '<div class="histo" id="stats-histogram" role="group" aria-label="Latency histograms">' + h.models.map(function (m) {
    return '<div class="histo-panel" data-model="' + esc(m.model) + '"><div class="histo-head"><span class="mono small"><i class="sw" style="--c:' + modelColor(m.model) + '"></i>' + esc(short(m.model)) + '</span>' +
      '<span class="small muted">' + m.marks.map(function (k) { return '<span class="pk pk-' + k.key + '">' + k.key + ' ' + fmtTime(k.value) + '</span>'; }).join(" ") + '</span></div>' +
      '<div class="histo-plot">' + m.bins.map(function (b) {
        var t = short(m.model) + ", " + fmtTime(b.from) + " to " + fmtTime(b.to) + ": " + b.count + " flow" + (b.count === 1 ? "" : "s");
        return '<span class="hb" tabindex="0" data-hover="' + esc(t) + '" aria-label="' + esc(t) + '"><i style="height:' + (100 * b.count / h.ymax) + '%;--c:' + modelColor(m.model) + '"></i></span>';
      }).join("") + m.marks.map(function (k) {
        return k.pos == null ? "" : '<span class="pm" style="left:' + (100 * k.pos) + '%;border-left-style:' + PCT_STYLE[k.key] + '" aria-hidden="true"></span>';
      }).join("") + '</div></div>';
  }).join("") + '<div class="histo-axis"><span>' + fmtTime(e[0]) + '</span><span>' + fmtTime(mid) + '</span><span>' + fmtTime(e[e.length - 1]) + '</span></div>' +
    '<p class="muted small">Bars show flows per latency bin, with the same bins and scale for every model. Lines show <span class="pk pk-p50">p50</span> solid, <span class="pk pk-p95">p95</span> dashed, <span class="pk pk-p99">p99</span> dotted.</p></div>';
}
// effect sizes: Cliff's delta on one unitless axis; each metric's CI on its own axis (units differ)
var EF_UNIT = { latency_s: fmtTime, peak_vram_mb: function (v) { return fmtNum(v, 1) + " MB"; }, tokens_per_flow: function (v) { return fmtNum(v, 1) + " tokens"; } };
function effectChart(res) {
  var ef = window.INSIGHTS && INSIGHTS.effectSizes(res);
  if (!ef) return "";
  var a = short(ef.model_a), b = short(ef.model_b), signed = function (f, v) { return (v < 0 ? "−" : v > 0 ? "+" : "") + f(Math.abs(v)); };
  var bands = '<span class="band" style="left:' + 50 * (1 - 0.147) + '%;width:' + 100 * 0.147 + '%"></span>';
  var drows = ef.rows.map(function (r) {
    var lbl = '<span class="lbl">' + esc(EF_LABEL[r.metric] || r.metric) + '</span>';
    if (!r.available) return '<div class="ef-row na">' + lbl + '<span class="small muted">' + esc(r.reason) + '</span></div>';
    var t = (EF_LABEL[r.metric] || r.metric) + ": Cliff's delta " + (r.delta >= 0 ? "+" : "") + r.delta.toFixed(2) + " (" + r.magnitude + ")";
    return '<div class="ef-row">' + lbl + '<div class="ef-track">' + bands + '<span class="zero"></span><span class="dot" tabindex="0" style="left:' + 50 * (1 + r.delta) + '%" data-hover="' + esc(t) + '" aria-label="' + esc(t) + '"></span></div><span class="small">' + esc(r.magnitude) + '</span></div>';
  }).join("");
  var crows = ef.rows.filter(function (r) { return r.available; }).map(function (r) {
    var f = EF_UNIT[r.metric] || function (v) { return fmtNum(v, 2); };
    var t = (EF_LABEL[r.metric] || r.metric) + ": mean " + a + " − " + b + " = " + signed(f, r.diff) + ", 95% CI " + signed(f, r.low) + " to " + signed(f, r.high);
    return '<div class="ef-row">' + '<span class="lbl">' + esc(EF_LABEL[r.metric] || r.metric) + '<small>±' + esc(f(r.half_range)) + ' axis</small></span><div class="ef-track"><span class="zero"></span>' +
      '<span class="ci" style="left:' + 100 * r.pos.low + '%;width:' + Math.max(0.5, 100 * (r.pos.high - r.pos.low)) + '%"></span><span class="dot" tabindex="0" style="left:' + 100 * r.pos.diff + '%" data-hover="' + esc(t) + '" aria-label="' + esc(t) + '"></span></div>' +
      '<span class="small">' + (r.excludes_zero ? "real difference" : '<span class="muted">could be 0</span>') + '</span></div>';
  }).join("");
  return '<div class="ef" id="stats-effect-chart"><h3>How big is the difference?</h3><p class="muted small">Cliff\'s delta. −1 = every ' + esc(a) + ' value is lower than every ' + esc(b) + ' value, +1 = every one is higher, 0 = no difference. The shaded band is "negligible" (|δ| &lt; 0.147).</p>' +
    '<div class="ef-axis"><span></span><div><span>−1, ' + esc(a) + ' lower</span><span>0</span><span>' + esc(a) + ' higher, +1</span></div><span></span></div>' + drows +
    '<h3>Is it more than noise?</h3><p class="muted small">Mean of ' + esc(a) + ' minus ' + esc(b) + ' (dot) with its 95% bootstrap interval (bar). If the bar crosses the zero line, the difference could be zero. Each row has its own scale (the units differ).</p>' + crows + '</div>';
}
// The analysis page (charts, SAW ranking, robustness, …) embedded ONCE per session; each view
// asks it (by message, no reload) to show only its sections.
var EMBED = { parts: null, loaded: false };
function analysisSrc(id, parts) {
  var q = "?session=" + enc(id) + "&embed=1&solo=1&parts=" + enc((parts || []).join(",")) + "&theme=" + resolvedTheme();
  return CP ? "analysis.html" + q : apiUrl("/analysis" + q);     // CP: same origin; results passed by message
}
function embedOrigin() { return CP || !API ? location.origin : new URL(API, location.href).origin; }
function resetEmbed() { EMBED = { parts: null, loaded: false }; $("embed-host").innerHTML = ""; $("embed-host").hidden = true; }
function showEmbed(parts, tab) {
  var host = $("embed-host");
  if (!parts) { host.hidden = true; return; }
  EMBED.parts = parts;
  host.hidden = false;
  var f = $("analysis-frame");
  if (!f) {
    host.innerHTML = '<iframe class="embed" id="analysis-frame" title="Session analysis views" data-session="' + esc(SESSION.id) + '" src="' + esc(analysisSrc(SESSION.id, parts)) + '"></iframe>';
    f = $("analysis-frame");
    f.addEventListener("load", function () { EMBED.loaded = true; postParts(); });
    return;
  }
  postParts();
}
function postParts() {
  var f = $("analysis-frame");
  if (f && EMBED.loaded && EMBED.parts) f.contentWindow.postMessage({ type: "agentmeter-embed-show", parts: EMBED.parts }, embedOrigin());
  postExplanation();
}
// the session's written explanation (Deployment recommendation, Optimisation guidance) for the
// embedded analysis page; marker: whether this viewer sees the admin-only ✦
function postExplanation() {
  var f = $("analysis-frame"), e = SESSION.expl;
  if (!f || !EMBED.loaded || !e) return;
  var mark = !CP || (typeof isAdmin === "function" && isAdmin());
  f.contentWindow.postMessage({ type: "agentmeter-embed-explanation", session: SESSION.id,
    sections: { deployment: (e.sections || {}).deployment || "", optimisation: (e.sections || {}).optimisation || "" },
    marker: mark ? "Written by " + short(e.model) + " from this session's results. Only admins see this mark." : "" }, embedOrigin());
}
window.addEventListener("message", function (ev) {
  var f = $("analysis-frame");
  if (ev.origin !== embedOrigin() || !ev.data || !f || ev.source !== f.contentWindow) return;
  if (ev.data.type === "agentmeter-embed-height") f.style.height = Math.max(120, Math.min(30000, +ev.data.height || 0)) + "px";
  // the embedded analysis page (same origin with a control plane) asks for the stored results
  if (ev.data.type === "agentmeter-embed-ready" && ev.data.session === SESSION.id && SESSION.res)
    f.contentWindow.postMessage({ type: "agentmeter-embed-results", session: SESSION.id, results: SESSION.res }, embedOrigin());
});

// Agents tab
// an agent's colour is its position in the session's pipeline (slots 1-8, fixed order);
// user-defined agents show their own title
var AG_ORDER = ["perceive", "reason", "decide", "act"], AG_TITLE = {};
function setAgentSlots(res) {
  var ag = res.agents || {};
  AG_ORDER = ag.agent_order || ["perceive", "reason", "decide", "act"]; AG_TITLE = {};
  (ag.pipeline || []).forEach(function (a) { if (!a.builtin) AG_TITLE[a.name] = a.title; });
}
function agColor(a) { var i = AG_ORDER.indexOf(a); return i < 0 ? "var(--muted)" : "var(--ag-" + (i % 8 + 1) + ")"; }
function agName(a) { return AG_TITLE[a] || a; }
function agentsTab(res) {
  var ag = res.agents;
  if (!ag) return '<div class="card"><p class="muted">This session predates the per-agent breakdown.</p></div>';
  var h = [];
  ag.per_model.forEach(function (m, mi) {
    var o = m.overhead || {};
    h.push('<div class="card agents-model"><h2>' + esc(m.model) + extraTag(m.model) + '</h2><div class="table-wrap"><table class="agents-table" id="agents-table-' + mi + '"><thead><tr><th>Agent</th><th class="num">Latency</th><th>Share of flow time</th><th class="num">Tokens in</th><th class="num">Tokens out</th><th class="num">Token share</th><th class="num">Working VRAM<br><span class="small">(excludes model weights)</span></th><th>Failures</th></tr></thead><tbody>' +
      m.agents.map(function (a) {
        var f = a.failures || {}, fl = [];
        fl.push(f.empty_output + " empty");
        if (f.hit_token_cap != null) fl.push(f.hit_token_cap + " at token cap (" + f.token_cap + ")");
        if (f.unparseable_label != null) fl.push(f.unparseable_label + " unparseable label");
        return '<tr><td><span class="legend"><span style="--c:' + agColor(a.agent) + '">' + esc(agName(a.agent)) + '</span></span></td><td class="num">' + fmtTime(a.mean_latency_s) +
          '</td><td style="min-width:150px"><div class="hbar" style="grid-template-columns:1fr auto"><span class="track"><span class="fill" style="display:block;width:' + (100 * (a.latency_share || 0)) +
          '%;background:' + agColor(a.agent) + '"></span></span><span class="val">' + fmtPct(a.latency_share) + '</span></div></td><td class="num">' + fmtNum(a.mean_input_tokens) +
          '</td><td class="num">' + fmtNum(a.mean_output_tokens) + '</td><td class="num">' + fmtPct(a.token_share) + '</td><td class="num">' + fmtMB(a.mean_peak_vram_mb) +
          '</td><td class="small">' + esc(fl.join(", ")) + ' <span class="muted">(rate ' + fmtPct(a.failure_rate) + ')</span></td></tr>';
      }).join("") + '</tbody></table></div>' +
      '<p class="small" id="agents-overhead-' + mi + '"><b>Agentic overhead (estimate):</b> ' + fmtNum(o.handoff_tokens_per_flow) + ' hand-off tokens per flow = <b>' + fmtPct(o.handoff_share_of_total) +
      '</b> of all tokens (' + fmtPct(o.handoff_share_of_input) + ' of input). Task input ' + fmtNum(o.task_input_tokens_per_flow) + ', output ' + fmtNum(o.output_tokens_per_flow) + ' tokens per flow.<br>' +
      '<span class="muted">Method. This counts the output tokens of earlier agents that each later prompt sends again, from the recorded output-token numbers. It is an estimate, not a tokenizer count of the prompts.</span></p></div>');
  });
  var flows = Object.keys((ag.per_model[0] || {}).timeline || {});
  h.push('<div class="card" id="agents-timeline-card"><h2>Pipeline timeline for one flow</h2><div class="row" style="margin-top:0"><div class="field"><label for="agents-flow">Flow</label><select id="agents-flow">' +
    flows.map(function (f) { return '<option>' + esc(f) + '</option>'; }).join("") + '</select></div></div><div id="agents-gantt" class="gantt" style="margin-top:14px"></div>' +
    '<div class="legend">' + ag.agent_order.map(function (a) { return '<span style="--c:' + agColor(a) + '">' + esc(agName(a)) + '</span>'; }).join("") + '</div>' +
    '<p class="muted small">' + esc(ag.notes.timeline) + '</p></div>');
  h.push(handoffCard(res), compositionCard(res), removeAgentCard(res), budgetCard(res), classAgentCard(res), '<div class="card" id="flows-card"><h2>Flow explorer</h2><p class="muted small" id="flows-status">Loading the flows…</p><div id="flows-body"></div></div>');
  h.push('<div class="card flat"><h2>How these are measured</h2><ul class="list">' +
    ["latency", "vram", "overhead", "failures"].map(function (k) { return '<li>' + esc(ag.notes[k]) + '</li>'; }).join("") + '</ul></div>');
  return h.join("");
}
// hand-off diagram: which step's output each later prompt re-sends, and how many tokens
function handoffCard(res) {
  var g = window.INSIGHTS && INSIGHTS.handoffGraph(res);
  if (!g || !g.per_model.length) return "";
  var X = function (i) { return 10 + i * 200; }, W = 130, Y = 92, H = 58;
  var maxTok = Math.max.apply(null, g.per_model.map(function (m) { return Math.max.apply(null, m.edges.map(function (e) { return e.tokens || 0; }).concat([1])); }));
  var diagrams = g.per_model.map(function (m, mi) {
    var idx = {}; g.agents.forEach(function (a, i) { idx[a] = i; });
    var edges = m.edges.map(function (e, ei) {
      var a = idx[e.from], b = idx[e.to], sw = 1.5 + 4 * (e.tokens || 0) / maxTok, lbl = e.tokens == null ? "—" : fmtNum(e.tokens, e.tokens < 10 ? 1 : 0) + " tok";
      var t = agName(e.from) + " → " + agName(e.to) + ": " + lbl + " re-sent per flow";
      if (b === a + 1) {
        var x1 = X(a) + W, x2 = X(b) - 8, y = Y + H / 2;
        return '<g class="edge" data-hover="' + esc(t) + '"><line x1="' + x1 + '" y1="' + y + '" x2="' + x2 + '" y2="' + y + '" stroke-width="' + sw.toFixed(1) + '" marker-end="url(#ho-arrow-' + mi + ')"/>' +
          '<text x="' + (x1 + x2) / 2 + '" y="' + (y - 8) + '" text-anchor="middle">' + esc(lbl) + '</text></g>';
      }
      var xa = X(a) + W / 2, xb = X(b) + W / 2, lift = 26 + 22 * (b - a - 1) + (a % 2) * 8;
      return '<g class="edge" data-hover="' + esc(t) + '"><path d="M' + xa + ' ' + Y + ' C ' + xa + ' ' + (Y - lift - 20) + ', ' + xb + ' ' + (Y - lift - 20) + ', ' + xb + ' ' + (Y - 6) + '" stroke-width="' + sw.toFixed(1) + '" marker-end="url(#ho-arrow-' + mi + ')"/>' +
        '<text x="' + (xa + xb) / 2 + '" y="' + (Y - lift - 12) + '" text-anchor="middle">' + esc(lbl) + '</text></g>';
    }).join("");
    var nodes = m.nodes.map(function (n, i) {
      var t = agName(n.agent) + ": " + fmtTime(n.latency_s) + " per flow (" + fmtPct(n.share) + " of flow time), " + fmtNum(n.in_tokens) + " tokens in, " + fmtNum(n.out_tokens) + " out";
      return '<g class="node" data-hover="' + esc(t) + '"><rect x="' + X(i) + '" y="' + Y + '" width="' + W + '" height="' + H + '" rx="6"/><rect x="' + X(i) + '" y="' + Y + '" width="5" height="' + H + '" rx="2" style="fill:' + agColor(n.agent) + '"/>' +
        '<text x="' + (X(i) + 16) + '" y="' + (Y + 24) + '" class="nt">' + esc(agName(n.agent).length > 14 ? agName(n.agent).slice(0, 13) + "…" : agName(n.agent)) + '</text><text x="' + (X(i) + 16) + '" y="' + (Y + 44) + '" class="ns">' + esc(fmtTime(n.latency_s) + ", " + fmtPct(n.share)) + '</text></g>';
    }).join("");
    return '<div class="ho" data-model="' + esc(m.model) + '"><h3><i class="sw" style="--c:' + modelColor(m.model) + '"></i>' + esc(short(m.model)) + ' <span class="muted small">' + fmtNum(m.resent_tokens, 1) + ' tokens re-sent per flow</span></h3>' +
      '<div class="ho-scroll"><svg viewBox="0 0 ' + (20 + (g.agents.length - 1) * 200 + W) + ' 168" style="min-width:' + Math.max(0, (g.agents.length - 4) * 150) + 'px" role="img" aria-label="' + esc("Hand-offs for " + short(m.model) + ": " + m.edges.map(function (e) { return agName(e.from) + " to " + agName(e.to); }).join(", ")) + '">' +
      '<defs><marker id="ho-arrow-' + mi + '" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="10" markerHeight="10" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class="ah"/></marker></defs>' + edges + nodes + '</svg></div></div>';
  }).join("");
  return '<div class="card" id="agents-handoffs"><h2>Hand-offs between agents</h2><p class="muted small" style="margin-top:0">Each arrow is one step\'s answer sent again inside a later step\'s prompt. Its label and thickness are the tokens it carries per flow, which is the sender\'s mean output. This is where the agentic overhead comes from.</p>' + diagrams +
    '<details class="info"><summary>Show as table</summary><div class="table-wrap"><table id="agents-handoff-table"><thead><tr><th>Model</th><th>From</th><th>To</th><th class="num">Tokens re-sent / flow</th></tr></thead><tbody>' +
    g.per_model.map(function (m) { return m.edges.map(function (e) { return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td>' + esc(agName(e.from)) + '</td><td>' + esc(agName(e.to)) + '</td><td class="num">' + fmtNum(e.tokens, 1) + '</td></tr>'; }).join(""); }).join("") +
    '</tbody></table></div></details></div>';
}
// time per agent by attack class (stacked, in pipeline order)
function classAgentCard(res) {
  var c = window.INSIGHTS && INSIGHTS.classAgentTime(res);
  if (!c) return "";
  var legend = '<div class="ilegend">' + c.agents.map(function (a) { return '<span><i style="--c:' + agColor(a) + '"></i>' + esc(agName(a)) + '</span>'; }).join("") + '</div>';
  c.per_model.sort(function (a, b) { return (MODEL_SLOT[a.model] || 0) - (MODEL_SLOT[b.model] || 0); });
  var groups = c.per_model.map(function (m) {
    return '<div class="cat-group" data-model="' + esc(m.model) + '"><h3><i class="sw" style="--c:' + modelColor(m.model) + '"></i>' + esc(short(m.model)) + '</h3>' + m.rows.map(function (r) {
      return '<div class="cat-row"><span class="lbl">' + esc(r.cls) + (r.n != null ? '<small>' + r.n + ' flows</small>' : "") + '</span><div class="cat-track"><div class="cat-bar" style="width:' + (c.max_total ? 100 * r.total / c.max_total : 0) + '%">' +
        r.parts.map(function (p) {
          var t = short(m.model) + ", " + r.cls + ", " + agName(p.agent) + ": " + fmtTime(p.s) + " (" + fmtPct(p.share) + ")";
          return p.s ? '<span tabindex="0" style="flex:' + (100 * p.share).toFixed(3) + ' 1 0;--c:' + agColor(p.agent) + '" data-hover="' + esc(t) + '" aria-label="' + esc(t) + '"></span>' : "";
        }).join("") + '</div></div><span class="val">' + fmtTime(r.total) + '</span></div>';
    }).join("") + '</div>';
  }).join("");
  return '<div class="card" id="agents-by-class"><h2>Time per agent by attack class</h2><p class="muted small" style="margin-top:0">Mean time each step takes for flows of each class (bar length = the class\'s total per flow, same scale for every model). A resource breakdown, not a detection score.</p>' +
    legend + groups + '<details class="info"><summary>Show as table</summary><div class="table-wrap"><table id="agents-by-class-table"><thead><tr><th>Model</th><th>Class</th>' + c.agents.map(function (a) { return '<th class="num">' + esc(agName(a)) + '</th>'; }).join("") + '<th class="num">Total</th><th>Slowest step</th></tr></thead><tbody>' +
    c.per_model.map(function (m) { return m.rows.map(function (r) { return '<tr><td class="mono">' + esc(short(m.model)) + '</td><td>' + esc(r.cls) + '</td>' + r.parts.map(function (p) { return '<td class="num">' + fmtTime(p.s) + '</td>'; }).join("") + '<td class="num"><b>' + fmtTime(r.total) + '</b></td><td>' + esc(r.slowest_agent || "—") + '</td></tr>'; }).join(""); }).join("") +
    '</tbody></table></div></details></div>';
}
function wireAgents(res) {
  var sel = $("agents-flow");
  if (!sel) return;
  function draw() {
    var fid = sel.value, rows = res.agents.per_model.map(function (m) { return { model: m.model, segs: (m.timeline || {})[fid] || [] }; });
    var max = Math.max.apply(null, rows.map(function (r) { return r.segs.reduce(function (s, x) { return Math.max(s, x[1] + x[2]); }, 0); }).concat([1e-9]));
    $("agents-gantt").innerHTML = rows.map(function (r) {
      var total = r.segs.reduce(function (s, x) { return Math.max(s, x[1] + x[2]); }, 0);
      return '<div class="g-row"><span class="mono small">' + esc(short(r.model)) + '<br><span class="muted">' + fmtTime(total) + '</span></span><div class="g-track" role="img" aria-label="' +
        esc(r.segs.map(function (x) { return agName(x[0]) + " " + fmtTime(x[2]); }).join(", ")) + '">' +
        r.segs.map(function (x) {
          return '<span class="g-seg" title="' + esc(agName(x[0]) + ": " + fmtTime(x[2]) + " (from " + fmtTime(x[1]) + ")") + '" style="left:' + (100 * x[1] / max) + '%;width:' + (100 * x[2] / max) + '%;background:' + agColor(x[0]) + '"></span>';
        }).join("") + '</div></div>';
    }).join("");
  }
  sel.addEventListener("change", draw);
  draw();
}

// Recommendation tab: measured verdict, resource hint, stage 3 context, decision helper
var DH_KEYS = ["max_mean_latency_s", "max_p95_latency_s", "max_peak_vram_mb", "max_cost_per_1k_flows_usd"];
// the limits the user last checked (Does a model fit your limits?): kept in this browser and
// drawn as threshold lines / colours on the charts and tables (Grafana-style thresholds)
var LIMITS_KEY = "agentmeter.limits";
function userLimits() {
  var o = {};
  try { o = JSON.parse(localStorage.getItem(LIMITS_KEY) || "{}") || {}; } catch (e) { o = {}; }
  var out = {};
  DH_KEYS.forEach(function (k) { var v = +o[k]; if (v > 0) out[k] = v; });
  return out;
}
function saveLimits() {
  var o = {};
  DH_KEYS.forEach(function (k) { var v = +(($("dh-" + k) || {}).value || 0); if (v > 0) o[k] = v; });
  try { localStorage.setItem(LIMITS_KEY, JSON.stringify(o)); } catch (e) { /* not kept */ }
}
// a value that has a limit: tagged so paintLimits() colours it against the user's limits
function limAttr(key, v) { return v == null || !isFinite(+v) ? "" : ' data-lim="' + key + '" data-v="' + (+v) + '"'; }
var LIM_LABEL = { max_mean_latency_s: "mean latency", max_p95_latency_s: "p95 latency", max_peak_vram_mb: "peak GPU memory", max_cost_per_1k_flows_usd: "cost per 1,000 flows" };
function paintLimits(root) {
  var lim = userLimits(), any = false;
  (root || document).querySelectorAll("[data-lim]").forEach(function (el) {
    var cap = lim[el.getAttribute("data-lim")], v = +el.getAttribute("data-v");
    el.classList.remove("over-limit", "in-limit"); el.removeAttribute("title");
    if (!cap) return;
    any = true;
    el.classList.add(v > cap ? "over-limit" : "in-limit");
    el.title = (v > cap ? "Over" : "Within") + " your " + LIM_LABEL[el.getAttribute("data-lim")] + " limit";
  });
  var note = $("lim-note");
  if (note) note.hidden = !any;
}
// the limits changed: redraw the threshold lines and recolour the values on this session
function refreshLimits() {
  if (SESSION.res) {
    var t = $("stats-trend");
    if (t) { var box = document.createElement("div"); box.innerHTML = trendCard(SESSION.res); t.replaceWith(box.firstChild); }
    rerenderGpuTimeline(); wireLineCharts();
  }
  paintLimits();
}
function recLimitsTab(res) {
  var rel = res.relative_comparison, dh = res.decision_helper, h = [];
  var hint = window.REPORT ? window.REPORT.optimisationHint(res, ((res.per_model || [])[0] || {}).db_label) : { text: "" };
  // the verdict: its first sentence as the answer, the rest as a plain explanation
  var vtext = INSIGHTS.plainText(rel ? rel.verdict : (res.comparison || {}).statement || "Single-model run, so there is no head-to-head comparison.",
                                 (res.per_model || []).map(function (m) { return m.model; }));
  var vcut = vtext.search(/\.\s/), vhead = vcut > 0 ? vtext.slice(0, vcut + 1) : vtext, vrest = vcut > 0 ? vtext.slice(vcut + 2) : "";
  // one wording of the verdict: the Overview headline, with a link to the full verdict there
  var vline = rel && typeof headline === "function" ? headline(rel, res.per_model || []) + "." : vhead;
  h.push('<div class="card"><h2>Measured verdict</h2><p class="verdict">' + esc(vline) + '</p>' +
    '<p class="verdict-why"><a href="#/session/' + enc(SESSION.id || "") + '/overview" data-to-verdict>See the full verdict on Overview</a></p>' +
    (hint.text ? '<p class="small">' + esc(hint.text) + '</p>' : "") + '<p class="muted small">Decision support, not a deployment sign-off.</p></div>');
  h.push(pickerCard(res), fitCard(res));
  if (dh) {
    h.push('<form class="card" id="dh-form" novalidate><h2>Does a model fit your limits?</h2><p class="muted small">' + esc(dh.framing) +
      ' Rule set: <span class="mono">' + esc(dh.rulebase) + '</span>.</p><div class="row">' +
      DH_KEYS.map(function (k) {
        var inp = (dh.inputs || {})[k] || {};
        return '<div class="field"><label for="dh-' + k + '">' + esc(inp.label || k) + ' <span class="hint">(' + esc(inp.unit || "") + ')</span></label><input type="number" min="0" step="any" id="dh-' + k + '" name="' + k + '" placeholder="no limit" value="' + esc(userLimits()[k] || "") + '"></div>';
      }).join("") + '</div><div class="row"><button type="submit" class="btn primary" id="dh-go">Check models</button><a class="btn" id="dh-pdf" href="#" download>PDF with these limits</a></div>' +
      '<div id="dh-error" class="notice error" role="alert" hidden></div><div id="dh-result" style="margin-top:14px"></div></form>');
  }
  return h.join("");
}
// Recommendations: each model against common deployment profiles (fixed thresholds, view only)
var FIT_MARK = { pass: ["✓", "fits", "ok"], fail: ["✗", "does not fit", "bad"], no_data: ["–", "no data", ""] };
function fitCard(res) {
  var fit = window.INSIGHTS && INSIGHTS.deploymentFit(res);
  if (!fit || !fit.models.length) return "";
  var fmtV = function (p, v) { return p.unit === "MB" ? fmtMB(v) : p.unit === "s" ? fmtTime(v) : p.unit === "USD" ? fmtUsd(v) : fmtNum(v) + " flows/day"; };
  var group = null;
  var rows = fit.profiles.map(function (p, i) {
    var head = p.group !== group ? '<tr class="fit-group"><th colspan="' + (fit.models.length + 1) + '">' + esc(p.group) + '</th></tr>' : "";
    group = p.group;
    return head + '<tr data-profile="' + p.id + '"><td>' + esc(p.label) + '</td>' + fit.models.map(function (m) {
      var c = m.cells[i], k = FIT_MARK[c.result];
      var t = short(m.model) + ", " + p.label + ": " + k[1] + (c.value != null ? " (" + fmtV(p, c.value) + ")" : ", " + c.reason);
      return '<td class="fit-cell"><span class="chip ' + k[2] + '" tabindex="0" data-result="' + c.result + '" data-hover="' + esc(t) + '" aria-label="' + esc(t) + '"><b aria-hidden="true">' + k[0] + '</b> ' + k[1] + '</span>' +
        (c.value != null ? '<span class="muted small fit-v">' + esc(fmtV(p, c.value)) + '</span>' : "") + '</td>';
    }).join("") + '</tr>';
  }).join("");
  return '<div class="card" id="fit-card"><h2>Deployment fit at a glance</h2><p class="muted small" style="margin-top:0">Each model against common deployment profiles, using this session\'s measured numbers (total peak VRAM, p95 / mean time per flow, flows one GPU can do in a day, cost per 1,000 flows). These are fixed thresholds, and you can set your own below.</p>' +
    '<div class="table-wrap"><table id="fit-table"><thead><tr><th>Profile</th>' + fit.models.map(function (m) { return '<th><i class="sw" style="--c:' + modelColor(m.model) + '"></i>' + esc(short(m.model)) + '<br><span class="muted small">' + m.passes + ' of ' + fit.profiles.length + ' fit</span></th>'; }).join("") + '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
    '<p class="muted small">"no data" means it was not measured in this run, for example no GPU memory or price on a CPU or mock run. A fit is not a sign-off, because the run\'s flows, GPU and settings apply.</p></div>';
}
function recContextTab(res) {
  var s3 = res.recommendation_stage3, h = [], rel = res.relative_comparison;
  if (!s3) return '<div class="card"><p class="muted">This session predates the model-context rules.</p></div>';
  if (s3) {
    var notes = (s3.notes || []).map(function (n) {
      return '<li><span class="chip mono">' + esc(n.rule) + '</span> ' + esc(n.note) + ' <span class="muted small">(source ' + esc((n.sources || []).join(", ")) +
        (n.fetched_at ? ', <a href="' + esc(n.source_url) + '" target="_blank" rel="noopener">Hugging Face</a>, fetched ' + esc(n.fetched_at) : "") + ')</span></li>';
    }).join("");
    var rows = (s3.rules || []).map(function (r) {
      return '<tr><td class="mono">' + esc(r.rule) + '</td><td class="mono">' + esc(short(r.model)) + '</td><td class="mono small">' + esc(r.condition) + '</td><td>' + esc(r.result) +
        (r.result === "no_data" ? ' <span class="muted small">(' + esc(r.detail) + ')</span>' : "") + '</td><td>' + esc((r.sources || []).join(", ")) + '</td><td class="mono small">' + esc(r.fetched_at || "—") + '</td></tr>';
    }).join("");
    h.push('<div class="card" id="svc-stage3"><h2>Model context</h2><p><b>Measured verdict:</b> ' + esc(rel && typeof headline === "function" ? headline(rel, res.per_model || []) + "." : (s3.verdict || {}).text || "—") +
      ' <a href="#/session/' + enc(SESSION.id || "") + '/overview">See it on Overview</a>. The notes below add context and never change it.</p>' +
      (notes ? '<ul class="list" id="svc-stage3-notes">' + notes + '</ul>' : '<p class="muted" id="svc-stage3-notes">No context rule fired' +
        (s3.metadata_status === "ok" ? "" : ". The Hugging Face metadata was " + esc(s3.metadata_status) + ", so the metadata rules had no data") + '.</p>') +
      '<p class="muted small">' + esc(s3.framing || "") + ' Rule set: <span class="mono">' + esc(s3.rulebase || "") + '</span>.</p>' +
      '<details><summary>Rules evaluated (' + (s3.rules || []).length + ', ' + (s3.fired || []).length + ' fired)</summary><div class="table-wrap"><table id="svc-stage3-rules"><thead><tr><th>Rule</th><th>Model</th><th>Condition</th><th>Result</th><th>Source</th><th>Fetched at</th></tr></thead><tbody>' +
      rows + '</tbody></table></div></details></div>');
  }
  return h.join("");
}
function dhQuery() {
  var q = [];
  DH_KEYS.forEach(function (k) { var v = ($("dh-" + k) || {}).value; if (v !== undefined && v !== "") q.push(k + "=" + enc(v)); });
  return q.join("&");
}
function renderDecision(d) {
  $("dh-result").innerHTML = !d.limits_set ? '<p class="muted small">Enter at least one limit.</p>' :
    '<div class="table-wrap"><table id="dh-table"><thead><tr><th>Model</th><th>Result</th>' + DH_KEYS.map(function (k) { return '<th>' + esc(((d.inputs || {})[k] || {}).label || k) + '</th>'; }).join("") + '</tr></thead><tbody>' +
    d.per_model.map(function (m) {
      var chip = m.result === "meets" ? "ok" : m.result === "fails" ? "bad" : "";
      return '<tr data-model="' + esc(m.model) + '"><td class="mono">' + esc(short(m.model)) + '</td><td><span class="chip ' + chip + '">' + esc(m.result === "meets" ? "meets all limits" : m.result === "fails" ? "fails " + m.failed.join(", ") : m.result) + '</span></td>' +
        m.rows.map(function (r) { return '<td class="small">' + (r.result === "not_set" ? '<span class="muted">—</span>' : esc(r.result) + (r.note ? '<br><span class="muted">' + esc(r.note) + '</span>' : "")) + '</td>'; }).join("") + '</tr>';
    }).join("") + '</tbody></table></div>';
}
function wireDecision(res) {
  var f = $("dh-form");
  if (!f) return;
  var pdf = function () { var q = dhQuery(); setDl($("dh-pdf"), "/api/jobs/" + enc(SESSION.id) + "/report.pdf" + (q ? "?" + q : ""), "agentmeter_report_" + SESSION.id + ".pdf"); };
  pdf();
  f.addEventListener("input", pdf);
  f.addEventListener("submit", function (ev) {
    ev.preventDefault();
    $("dh-error").hidden = true;
    var q = dhQuery();
    saveLimits(); refreshLimits();
    api((CP ? "/api/sessions/" : "/api/jobs/") + enc(SESSION.id) + "/constraints" + (q ? "?" + q : "")).then(function (r) {
      if (!r.ok) { showError($("dh-error"), r.data); return; }
      renderDecision(r.data);
    });
  });
}

// Downloads tab
function downloadsTab(res, job) {
  return '<div class="card"><h2>Report and results</h2><div class="actions" id="svc-downloads-main">' +
    dlLink(pdfPath(job), "agentmeter_report_" + job.job_id + ".pdf", "PDF report", "btn primary") +
    '<button type="button" class="btn" id="svc-dl-json">session_results.json</button>' +
    '<button type="button" class="btn" id="svc-dl-saw">SAW CSV</button><button type="button" class="btn" id="svc-dl-agent">Per-agent CSV</button></div>' +
    '<h3 style="margin-top:16px">Reproducibility package</h3><p class="muted small" style="margin-top:0">One .zip with everything needed to check or repeat this session: the results, the exact settings and pipeline, ' +
    'the GPU, driver and library versions, the prepared flows with their fingerprint, the PDF and a README saying how to run it again.</p>' +
    '<div class="actions"><button type="button" class="btn" id="svc-dl-repro">Download reproducibility package (.zip)</button><span class="small muted" id="svc-repro-status" aria-live="polite"></span></div></div>' +
    '<div class="card"><h2>Prepared set</h2><p class="muted small">The exact flows this session measured. Download them to keep them, or reuse them in a new benchmark.</p>' +
    '<p class="mono small">' + esc(job.run_name || "") + '</p><div class="actions" id="svc-downloads"></div>' +
    '<div class="actions"><a class="btn" id="svc-again" href="#/new/models?set=' + enc(job.run_name || "") + (CP && job.stored ? "&from=" + enc(job.job_id) : "") + '">Benchmark these flows again</a></div></div>' +
    (CP && job.can_delete ? '<div class="card"><h2>Delete this session</h2><p class="muted small">Removes the stored session (results, PDF, prepared-set copy) for everyone. ' +
      'Only its owner or an admin can do this.</p><div class="actions"><button type="button" class="btn" id="svc-delete">Delete session</button></div>' +
      '<div id="svc-delete-error" class="notice error" role="alert" hidden></div></div>' : "");
}
// ---- reproducibility package: a .zip built in the browser (stored, no compression) ----
var CRC_T = (function () { var t = [], c, n, k; for (n = 0; n < 256; n++) { c = n; for (k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } return t; })();
function crc32(u) { var c = 0xFFFFFFFF; for (var i = 0; i < u.length; i++) c = CRC_T[(c ^ u[i]) & 0xFF] ^ (c >>> 8); return (c ^ 0xFFFFFFFF) >>> 0; }
function makeZip(files) {
  var enc8 = new TextEncoder(), parts = [], central = [], off = 0;
  var le = function (n, b) { var a = new Uint8Array(b); for (var i = 0; i < b; i++) a[i] = (n >>> (8 * i)) & 0xFF; return a; };
  var cat = function (arrs) { var n = arrs.reduce(function (a, x) { return a + x.length; }, 0), o = new Uint8Array(n), p = 0; arrs.forEach(function (x) { o.set(x, p); p += x.length; }); return o; };
  var d = new Date(), dt = ((d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1)), dd = (((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate());
  files.forEach(function (f) {
    var name = enc8.encode(f.name), data = typeof f.data === "string" ? enc8.encode(f.data) : f.data, crc = crc32(data);
    var head = cat([le(0x04034b50, 4), le(20, 2), le(0x0800, 2), le(0, 2), le(dt, 2), le(dd, 2), le(crc, 4), le(data.length, 4), le(data.length, 4), le(name.length, 2), le(0, 2), name]);
    parts.push(head, data);
    central.push(cat([le(0x02014b50, 4), le(20, 2), le(20, 2), le(0x0800, 2), le(0, 2), le(dt, 2), le(dd, 2), le(crc, 4), le(data.length, 4), le(data.length, 4),
      le(name.length, 2), le(0, 2), le(0, 2), le(0, 2), le(0, 2), le(0, 4), le(off, 4), name]));
    off += head.length + data.length;
  });
  var cd = cat(central);
  return new Blob(parts.concat([cd, cat([le(0x06054b50, 4), le(0, 2), le(0, 2), le(files.length, 2), le(files.length, 2), le(cd.length, 4), le(off, 4), le(0, 2)])]), { type: "application/zip" });
}
function fetchBytes(path) {
  var f = CP && WORKER_PATHS.test(path) ? cpFetch(path, { raw: true }) : backendFetch(path, { raw: true });
  return f.then(function (r) { return r && r.ok && r.arrayBuffer ? r.arrayBuffer().then(function (b) { return new Uint8Array(b); }) : null; }, function () { return null; });
}
function reproPackage(res, job, btn) {
  var st = $("svc-repro-status"), id = job.job_id, ident = res.session_identity || {}, env = res.environment || {};
  btn.disabled = true; st.textContent = "Collecting the files…";
  var setFiles = CP && job.stored ? ["manifest.json", "features.csv", "labels.csv"].filter(function (f) { return (job.files || []).indexOf(f) >= 0; }).map(function (f) { return ["/api/sessions/" + enc(id) + "/files/" + f, f]; })
    : job.run_name ? ["manifest.json", "features.csv", "labels.csv"].map(function (f) { return ["/api/prepared/" + setPath(job.run_name) + "/" + f, f]; }) : [];
  var wanted = setFiles.map(function (x) { return fetchBytes(x[0]).then(function (b) { return b ? { name: "prepared_set/" + x[1], data: b } : null; }); });
  wanted.push(fetchBytes(pdfPath(job)).then(function (b) { return b ? { name: "report.pdf", data: b } : null; }));
  if (SESSION.expl) wanted.push(Promise.resolve({ name: "explanation.json", data: JSON.stringify(SESSION.expl, null, 2) }));
  Promise.all(wanted).then(function (got) {
    got = got.filter(Boolean);
    var run = { session_id: id, created_at: job.created_at || null, finished_at: job.finished_at || null, owner: job.owner || (job.user || {}).uname || null,
                models: job.models || (res.session || {}).models || [], prepared_set: job.run_name || null, n_flows: (res.session || {}).n_flows,
                pipeline: job.pipeline || null, provider: env.provider || null,
                quantization: "4-bit NF4", settings: ident.settings || null };
    var again = location.origin + location.pathname + "#/new/models?set=" + enc(job.run_name || "") + "&models=" + (run.models || []).map(enc).join(",") + "&again=" + enc(id);
    var readme = "# AgentMeter reproducibility package\n\nSession: " + id + "\nCreated: " + new Date().toISOString() + "\n\n" +
      "AgentMeter measures how efficiently open-source LLMs (4-bit NF4) run a fixed multi-agent analysis of network flows. It measures latency, GPU memory, tokens, cost and energy.\n\n" +
      "## What is in this package\n\n" +
      "- `session_results.json` holds every stored result of the session (scores, per-model metrics, statistics, analyses).\n" +
      "- `run.json` records what was run, meaning the models, prepared set, pipeline, options and the settings that make up the setup.\n" +
      "- `identity.json` holds the setup fingerprint (prepared-set hash, GPU, settings fingerprint) and the environment (GPU, driver, CUDA, library versions).\n" +
      (got.some(function (f) { return /^prepared_set\//.test(f.name); }) ? "- `prepared_set/` holds the exact flows the models answered (`features.csv`, `labels.csv` when labelled, `manifest.json` with the hash).\n" : "- (the prepared set's files could not be fetched when this package was made)\n") +
      (got.some(function (f) { return f.name === "report.pdf"; }) ? "- `report.pdf` is the session report.\n" : "") +
      (got.some(function (f) { return f.name === "explanation.json"; }) ? "- `explanation.json` holds the written explanations, with the model and prompt version that wrote them.\n" : "") +
      "\n## The setup (what must match to compare)\n\n" +
      "- Prepared set (flows) SHA-256: `" + (ident.prepared_set_sha256 || "unknown") + "` (hash version " + (ident.prepared_set_hash_version || "?") + ")\n" +
      "- GPU: " + (ident.gpu_label || env.gpu_name || "unknown") + "\n" +
      "- Settings fingerprint: `" + (ident.settings_fingerprint || "unknown") + "`\n" +
      "- Driver / CUDA: " + (env.driver_version || "—") + " / " + (env.cuda_runtime_version || "—") + ", torch " + (env.torch || "—") + ", transformers " + (env.transformers || "—") + ", bitsandbytes " + (env.bitsandbytes || "—") + "\n" +
      "\n## How to run it again\n\n" +
      "1. Open AgentMeter and use **Run again** on the session page, or open " + again + "\n" +
      "   (if the prepared set is not on the GPU server, import `prepared_set/` under New benchmark › Reuse a prepared set, and its hash stays the same).\n" +
      "2. Use the same GPU type. Results are only comparable on the same prepared set, GPU and settings.\n" +
      "3. The new session appears on Leaderboard > Consistency next to this one, and a change beyond the usual spread is flagged.\n";
    var files = [{ name: "README.md", data: readme }, { name: "run.json", data: JSON.stringify(run, null, 2) },
                 { name: "identity.json", data: JSON.stringify({ session_identity: ident, environment: env }, null, 2) },
                 { name: "session_results.json", data: JSON.stringify(res, null, 2) }].concat(got);
    var a = document.createElement("a");
    a.href = URL.createObjectURL(makeZip(files)); a.download = "agentmeter_" + shortId(id).replace(/…$/, "") + "_reproducibility.zip";
    document.body.appendChild(a); a.click(); a.remove();
    st.textContent = files.length + " files packed."; btn.disabled = false;
  });
}
function download(name, text, type) {
  var a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: type || "text/csv" }));
  a.download = name; document.body.appendChild(a); a.click(); a.remove();
}
function wireDownloads(res, job) {
  $("svc-dl-json").addEventListener("click", function () { download("session_results.json", JSON.stringify(res, null, 2), "application/json"); });
  $("svc-dl-repro").addEventListener("click", function () { reproPackage(res, job, this); });
  $("svc-dl-saw").addEventListener("click", function () { download("agentmeter_session_saw.csv", window.REPORT.sawCsv(res)); });
  $("svc-dl-agent").addEventListener("click", function () { download("agentmeter_session_per_agent.csv", window.REPORT.perAgentCsv(res)); });
  if ($("svc-delete")) $("svc-delete").addEventListener("click", function () {
    confirmAction({ title: "Delete this session?", danger: true, ok: "Delete session",
      body: "Session " + job.job_id + " is deleted for everyone. This cannot be undone." }).then(function (yes) {
    if (!yes) return;
    api("/api/sessions/" + enc(job.job_id), { method: "DELETE" }).then(function (r) {
      if (!r.ok) { showError($("svc-delete-error"), r.data); return; }
      SESSION = { id: null, res: null, job: null }; location.hash = "#/sessions";
    });
    });
  });
  if (CP && job.stored) {                                   // the copy stored with the session (R2)
    var names = ["manifest.json", "features.csv", "labels.csv"].filter(function (f) { return (job.files || []).indexOf(f) >= 0; });
    $("svc-downloads").innerHTML = names.length ? names.map(function (f) {
      return dlLink("/api/sessions/" + enc(job.job_id) + "/files/" + f, job.job_id + "_" + f, f === "features.csv" ? "features.csv (no identification columns)" : f).replace('<a ', '<a data-dl="' + f + '" ');
    }).join("") : '<span class="muted small">No prepared-set copy was stored with this session.</span>';
    return;
  }
  if (!job.run_name) return;
  api("/api/prepared/" + setPath(job.run_name)).then(function (r) {
    $("svc-downloads").innerHTML = !r.ok ? '<span class="muted small">' + esc(r.data.error) + '</span>' : (r.data.downloads || []).map(function (f) {
      return dlLink("/api/prepared/" + setPath(job.run_name) + "/" + enc(f), job.run_name.split("/").pop() + "_" + f, f).replace('<a ', '<a data-dl="' + esc(f) + '" ');
    }).join("");
  });
}
