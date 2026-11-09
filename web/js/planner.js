// AgentMeter web app — planner.js: Cost planner (#/planner). A daily volume of alerts -> each model's
// GPU time, cost and energy per day, from its latest real benchmark on a GPU (INSIGHTS.projectCost).
// Reads the stored sessions only: no GPU needed. One of the app's scripts (web/app.html).
"use strict";
var PLAN = { sessions: null };
function loadPlanner(q) {
  if (q && q.flows) $("pl-flows").value = q.flows;
  $("pl-body").innerHTML = skel(4);
  allSessions().then(function (r) {
    if (!r.ok) { $("pl-body").innerHTML = '<div class="notice error">' + esc((r.data || {}).error || "Could not load the sessions.") + '</div>'; return; }
    PLAN.sessions = r.data.sessions || [];
    var gpus = {}, sel = $("pl-gpu"), cur = sel.value;
    INSIGHTS.measuredModels(PLAN.sessions).forEach(function (m) { gpus[m.gpu] = true; });
    sel.innerHTML = '<option value="">Every GPU measured</option>' + Object.keys(gpus).sort().map(function (g) { return '<option>' + esc(g) + '</option>'; }).join("");
    if (cur && gpus[cur]) sel.value = cur;
    renderPlanner();
  });
}
function renderPlanner() {
  if (!PLAN.sessions) return;
  var n = Math.max(0, parseInt($("pl-flows").value || "0", 10) || 0);
  var rows = INSIGHTS.projectCost(PLAN.sessions, { flows: n, gpu: $("pl-gpu").value || null });
  if (!rows.length) {
    $("pl-body").innerHTML = '<p class="muted" id="pl-none">No model has been benchmarked on a GPU yet, so there is nothing to project from. ' +
      'Run a benchmark (it needs the GPU server); its measured time per flow then shows up here.</p>';
    return;
  }
  var h = function (x) { return x == null ? "—" : x < 1 ? fmtNum(x * 60, 1) + " min" : fmtNum(x, 1) + " h"; };
  $("pl-body").innerHTML = '<p class="small muted" style="margin-top:0">' + fmtNum(n) + ' alerts a day. One GPU triages them one after another, as measured; ' +
    '"One GPU?" says whether the day\'s alerts take less than 24 hours on one GPU. A projection: the same GPU, settings and similar flows as the benchmark it comes from.</p>' +
    '<div class="table-wrap"><table id="pl-table"><thead><tr><th>Model</th><th>GPU</th><th class="num">Time per alert</th><th class="num">GPU time per day</th>' +
    '<th class="num">Cost per day</th><th class="num">Energy per day</th><th>One GPU?</th></tr></thead><tbody>' +
    rows.map(function (x) {
      return '<tr><td><span class="row-name" style="white-space:nowrap">' + esc(short(x.model)) + '</span><span class="row-sub small muted">measured in <a href="#/session/' +
          enc(x.source_session) + '">the benchmark of ' + esc(plDay(x.measured_at)) + '</a></span></td><td class="small">' + esc(x.gpu) + '</td><td class="num">' + fmtNum(x.latency_s_per_flow, 2) +
        ' s</td><td class="num">' + h(x.gpu_hours) + '</td><td class="num">' + (x.cost_usd == null ? "—" : "$" + fmtNum(x.cost_usd, 2) +
          '<span class="row-sub small muted">$' + fmtNum(x.cost_usd * 30, 0) + ' / 30 days</span>') +
        '</td><td class="num">' + (x.energy_wh == null ? "—" : fmtNum(x.energy_wh / 1000, 2) + " kWh") + '</td><td>' +
        (x.fits_in_a_day ? '<span class="chip ok">yes</span>' : '<span class="chip warn">needs ' + Math.ceil(x.gpu_hours / 24) + ' GPUs</span>') + '</td></tr>';
    }).join("") + '</tbody></table></div>' +
    '<p class="small muted">Cost needs a GPU price (the session records it on a rented GPU, or <code>pricing.gpu_usd_per_hour</code>); energy needs power readings from a real GPU. "—" means the benchmark did not record it.</p>';
}
// a session time (20261105_100000 or ISO) as its day, 2026-11-05
function plDay(s) { var m = /^(\d{4})(\d{2})(\d{2})_/.exec(String(s || "")); return m ? m[1] + "-" + m[2] + "-" + m[3] : String(s || "—").slice(0, 10); }
$("pl-flows").addEventListener("input", renderPlanner);
$("pl-gpu").addEventListener("change", renderPlanner);
$("pl-form").addEventListener("submit", function (ev) { ev.preventDefault(); renderPlanner(); });
