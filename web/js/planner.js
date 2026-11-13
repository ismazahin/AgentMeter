// AgentMeter web app — planner.js: Cost planner (#/planner). A daily volume of alerts -> each model's
// GPU time, GPUs needed, cost and energy per day, from its latest real benchmark on a GPU
// (INSIGHTS.projectCost). Reads the stored sessions only: no GPU needed. One of the app's scripts (web/app.html).
"use strict";
var PLAN = { sessions: null, alerts: null };
var PL_TIGHT = 0.8;                         // busier than this, a busy hour or a slow alert has no room left
var PL_PICKS = [1000, 10000, 50000];
function plGpuName(g) { return String(g || "GPU").replace(/^NVIDIA\s+(GeForce\s+)?/i, ""); }
function plFlows() { return Math.max(0, parseInt(String($("pl-flows").value || "0").replace(/[^\d]/g, ""), 10) || 0); }
function loadPlanner(q) {
  if (q && q.flows) $("pl-flows").value = q.flows;
  $("pl-body").innerHTML = skel(4);
  allSessions().then(function (r) {
    if (!r.ok) { $("pl-body").innerHTML = '<div class="notice error">' + esc((r.data || {}).error || "Could not load the sessions.") + '</div>'; return; }
    PLAN.sessions = r.data.sessions || [];
    var gpus = {}, sel = $("pl-gpu"), cur = sel.value;
    INSIGHTS.measuredModels(PLAN.sessions).forEach(function (m) { gpus[m.gpu] = true; });
    var list = Object.keys(gpus).sort();
    sel.innerHTML = (list.length === 1 ? "" : '<option value="">Every GPU measured</option>') +
      list.map(function (g) { return '<option value="' + esc(g) + '">' + esc(plGpuName(g)) + '</option>'; }).join("");
    if (cur && gpus[cur]) sel.value = cur;
    renderPlanner();
  });
  // the alerts of the latest traffic analysis (control plane): one click fills the volume with them
  PLAN.alerts = null;
  if (CP) cpFetch("/api/analyses").then(function (r) {
    var a = r && r.ok ? (r.data.analyses || []).filter(function (x) { return x.ids && x.ids.n_flagged != null; })[0] : null;
    PLAN.alerts = a ? a.ids.n_flagged : null;
    plPicks();
  });
  plPicks();
}
function plPicks() {
  var n = plFlows();
  $("pl-picks").innerHTML = PL_PICKS.map(function (v) { return '<button type="button" data-pl-n="' + v + '" aria-pressed="' + String(n === v) + '">' + fmtNum(v) + '</button>'; }).join("") +
    (PLAN.alerts != null ? '<button type="button" class="from" data-pl-n="' + PLAN.alerts + '" aria-pressed="' + String(n === PLAN.alerts) + '">Use my last Analyse traffic: ' + fmtNum(PLAN.alerts) + '</button>' : "");
}
function renderPlanner() {
  if (!PLAN.sessions) return;
  var n = plFlows(), gpu = $("pl-gpu").value || null;
  plPicks();
  var rows = INSIGHTS.projectCost(PLAN.sessions, { flows: n, gpu: gpu });
  if (!rows.length) {
    $("pl-body").innerHTML = '<p class="muted" id="pl-none" style="margin:0">No model has been benchmarked on a GPU yet, so there is nothing to plan from. ' +
      'Run a benchmark on the GPU server and its measured time per alert shows here.</p>';
    return;
  }
  var many = !gpu && rows.some(function (x) { return x.gpu !== rows[0].gpu; });
  var cheapest = rows.filter(function (x) { return x.cost_usd != null; }).sort(function (a, b) { return a.cost_usd - b.cost_usd; })[0];
  var fastest = rows.slice().sort(function (a, b) { return a.latency_s_per_flow - b.latency_s_per_flow; })[0];
  $("pl-body").innerHTML = '<p class="pl-lede">' + fmtNum(n) + ' alerts a day' + (many ? " on each GPU measured" : ' on <b>' + esc(plGpuName(rows[0].gpu)) + '</b>') + ', one alert at a time, as measured.' +
      infoI("Each GPU answers one alert after another, at the time per alert its benchmark measured. A projection: it assumes the same GPU, the same settings and similar alerts as that benchmark.") + '</p>' +
    '<div class="table-wrap"><table id="pl-table" class="pl-table"><thead><tr><th>Model</th><th class="num">Time per alert</th><th class="num">GPU time per day</th>' +
    '<th class="num">GPUs needed</th><th class="num">Cost per day</th><th class="num">Energy per day</th></tr></thead><tbody>' +
    rows.map(function (x) {
      var h = x.gpu_hours || 0, gpus = Math.max(1, Math.ceil(h / 24)), busy = h / (24 * gpus), tight = busy > PL_TIGHT;
      return '<tr><td><span class="pl-nm">' + esc(nick(x.model)) + '</span>' + (x === cheapest ? '<span class="pl-tag c">Cheapest</span>' : "") +
          (x === fastest ? '<span class="pl-tag f">Fastest</span>' : "") +
          '<span class="pl-sub">' + (many ? esc(plGpuName(x.gpu)) + ", from" : "from") + ' the benchmark of <a href="#/session/' + enc(x.source_session) + '">' + esc(plDay(x.measured_at)) + '</a></span></td>' +
        '<td class="num">' + fmtNum(x.latency_s_per_flow, 2) + ' s</td>' +
        '<td class="num"><span class="pl-day' + (h > 24 ? " over" : "") + '" aria-hidden="true"><i style="width:' + Math.min(100, h / 24 * 100) + '%"></i></span>' + plHours(h) +
          '<span class="pl-sub">' + (h > 24 ? "more than one GPU can do in a day" : "of 24 hours") + '</span></td>' +
        '<td class="num pl-gp' + (tight ? " tight" : gpus === 1 ? " ok" : "") + '">' + gpus + ' GPU' + (gpus === 1 ? "" : "s") +
          '<span class="pl-sub">' + Math.round(100 * busy) + '% busy' + (tight ? ", tight" : "") + '</span></td>' +
        '<td class="num">' + (x.cost_usd == null ? '<span class="muted">not recorded</span>' : "$" + fmtNum(x.cost_usd, 2) + '<span class="pl-sub">$' + fmtNum(x.cost_usd * 30, 0) + ' a month</span>') + '</td>' +
        '<td class="num">' + (x.energy_wh == null ? '<span class="muted">not recorded</span>' : fmtNum(x.energy_wh / 1000, 2) + " kWh") + '</td></tr>';
    }).join("") + '</tbody></table></div>' +
    '<p class="pl-foot">Above ' + Math.round(100 * PL_TIGHT) + '% busy is tight: a busy hour or a slow alert has no room, so plan one more GPU. ' +
      'Cost uses the GPU price your benchmark recorded, and energy needs power readings from a real GPU.' + infoI("A GPU price is recorded on a rented GPU, or read from pricing.gpu_usd_per_hour in the configuration. Not recorded means the benchmark had no price or no power readings.") + '</p>';
  addExports($("pl-body").parentNode.parentNode);
}
function plHours(x) { return x < 1 ? fmtNum(x * 60, 0) + " min" : fmtNum(x, 1) + " h"; }
// a session time (20261105_100000 or ISO) as its day, 5 Nov
function plDay(s) {
  var m = /^(\d{4})(\d{2})(\d{2})_/.exec(String(s || "")) || /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ""));
  if (!m) return String(s || "").slice(0, 10) || "an earlier date";
  return (+m[3]) + " " + ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][+m[2] - 1];
}
$("pl-flows").addEventListener("input", renderPlanner);
$("pl-flows").addEventListener("blur", function () { var n = plFlows(); if (n) this.value = fmtNum(n); });
$("pl-gpu").addEventListener("change", renderPlanner);
$("pl-form").addEventListener("submit", function (ev) { ev.preventDefault(); renderPlanner(); });
$("pl-form").addEventListener("click", function (ev) {
  var b = ev.target.closest && ev.target.closest("[data-pl-n]");
  if (!b) return;
  $("pl-flows").value = fmtNum(+b.getAttribute("data-pl-n")); renderPlanner();
});
