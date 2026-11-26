// AgentMeter web app — experiment.js: the "Size & agents" tab of a session that is one run of an
// experiment (one upload benchmarked at several model sizes and numbers of agents). It reads every
// run of the experiment from the session list (each run is an ordinary session whose summary carries
// its "variant" and "experiment") and draws them side by side: a line chart per model (x = number of
// agents, one line per model size), a heatmap, the table of every run and what changed.
"use strict";
var XP = { measure: "mean_latency_s", runs: null, id: null };
var XP_SIZES = ["4bit", "8bit", "16bit"];
var XP_SIZE_LABEL = { "4bit": "4-bit", "8bit": "8-bit", "16bit": "16-bit" };
var XP_COLOR = { "4bit": "var(--mseries-1)", "8bit": "var(--mseries-2)", "16bit": "var(--mseries-3)" };
// [metric key, label, unit, scale, lower is better]
var XP_MEASURES = [
  ["mean_latency_s", "Latency per flow", "s", 1, true],
  ["p95_latency_s", "p95 latency", "s", 1, true],
  ["max_peak_vram_mb", "Peak GPU memory", "GB", 1 / 1024, true],
  ["mean_tokens_per_flow", "Tokens per flow", "", 1, true],
  ["handoff_share_of_total", "Re-sent context", "% of tokens", 100, true],
  ["accuracy", "Reference-label match", "%", 100, false],
  ["cost_per_1k_flows_usd", "Cost per 1,000 flows", "USD", 1, true],
  ["wh_per_1k_flows", "Energy per 1,000 flows", "Wh", 1, true]
];
function xpMeasure(k) { return XP_MEASURES.filter(function (m) { return m[0] === k; })[0] || XP_MEASURES[0]; }
function xpVal(cell, k) { var v = cell.m[k], m = xpMeasure(k); return v == null || !isFinite(v) ? null : v * m[3]; }
function xpFmt(v, k) {
  if (v == null) return "—";
  var m = xpMeasure(k), a = Math.abs(v);
  if (m[2] === "s" && a > 0 && a < 1) return fmtNum(v * 1000, a < 0.01 ? 2 : 0) + " ms";     // demo runs take milliseconds
  var d = a >= 100 ? 0 : a >= 10 ? 1 : a >= 0.1 || a === 0 ? 2 : 4;
  return (m[2] === "USD" ? "$" : "") + fmtNum(v, d) + (m[2] === "USD" || !m[2] ? "" : m[2].charAt(0) === "%" ? m[2] : " " + m[2]);
}

// every run of this experiment, from the session list (works with and without a control plane)
function xpLoad(exp) {
  return allSessions(true).then(function (r) {
    var runs = ((r.ok && r.data.sessions) || []).filter(function (s) { return s.experiment && s.experiment.id === exp.id; });
    runs.sort(function (a, b) { return (a.experiment.index || 0) - (b.experiment.index || 0); });
    return runs;
  });
}
function xpCells(runs) {
  var out = [];
  runs.forEach(function (s) {
    var v = s.variant || { precision: "4bit", agents: 4 };
    Object.keys(s.metrics || {}).forEach(function (model) {
      out.push({ model: model, precision: v.precision, agents: v.agents, m: s.metrics[model] || {}, sid: s.session_id, status: s.status });
    });
  });
  return out;
}

function renderVariants(res) {
  var host = $("spanel-variants"), exp = res && res.session && res.session.experiment;
  if (!host || !exp) return;
  if (XP.id !== exp.id) { XP.id = exp.id; XP.runs = null; }
  if (!XP.runs) {
    host.innerHTML = '<div class="card"><p class="muted">Loading the runs of this experiment…</p></div>';
    xpLoad(exp).then(function (runs) { XP.runs = runs; renderVariants(res); });
    return;
  }
  var runs = XP.runs, cells = xpCells(runs), total = exp.total || runs.length;
  var done = runs.filter(function (s) { return s.status === "Done" || s.status === "Demo"; }).length;
  var models = cells.reduce(function (a, c) { if (a.indexOf(c.model) < 0) a.push(c.model); return a; }, []);
  var sizes = XP_SIZES.filter(function (p) { return cells.some(function (c) { return c.precision === p; }); });
  var agents = cells.reduce(function (a, c) { if (a.indexOf(c.agents) < 0) a.push(c.agents); return a; }, []).sort(function (a, b) { return a - b; });
  var m = xpMeasure(XP.measure);
  var h = [];
  h.push('<div class="card" id="xp-head"><h2>Model size and number of agents</h2><p class="small muted" style="margin-top:0">' +
    esc(done + " of " + total + " runs finished") + '. Every run used the same flows, the same IDS threshold and the same GPU; only the model size and the number of agents change. ' +
    (done < total ? "Runs still running or waiting appear here as they finish." : "") + '</p>' +
    ((exp.skipped || []).length ? '<p class="small warn-text">Not run: ' + esc(exp.skipped.map(function (x) { return short(x.model) + " at " + (XP_SIZE_LABEL[x.precision] || x.precision); }).join(", ")) + ', which do not fit this GPU\'s memory.</p>' : "") + '</div>');
  h.push('<div class="card" id="xp-changed"><h2>What changed</h2>' + xpChanged(cells, models, sizes, agents) + '</div>');
  h.push('<div class="card" id="xp-charts"><h2>' + esc(m[1]) + '</h2><div class="xp-measures" role="group" aria-label="Measure">' +
    XP_MEASURES.map(function (x) { return '<button type="button" class="btn small-btn' + (x[0] === XP.measure ? " primary" : "") + '" data-xp-measure="' + x[0] + '">' + esc(x[1]) + '</button>'; }).join("") + '</div>' +
    '<div class="xp-legend">' + sizes.map(function (p) { return '<span><i style="background:' + XP_COLOR[p] + '"></i>' + XP_SIZE_LABEL[p] + '</span>'; }).join("") +
    '<span>x: number of agents · ' + (m[4] ? "lower is better" : "higher is better") + '</span></div>' +
    '<div class="xp-panels">' + models.map(function (md) { return '<div><h3 style="margin:10px 0 4px">' + esc(nick(md)) + '</h3>' + xpChart(cells, md, sizes, agents, XP.measure) + '</div>'; }).join("") + '</div></div>');
  h.push('<div class="card" id="xp-heat"><h2>Heatmap · ' + esc(m[1]) + '</h2><div class="xp-panels">' + models.map(function (md) { return '<div><h3 style="margin:10px 0 4px">' + esc(nick(md)) + '</h3>' + xpHeat(cells, md, sizes, agents, XP.measure) + '</div>'; }).join("") + '</div></div>');
  h.push('<div class="card" id="xp-table"><div class="row" style="margin-top:0;justify-content:space-between"><h2>Every run</h2><button type="button" class="btn small-btn" id="xp-csv">Download CSV</button></div>' + xpTable(cells, runs) + '</div>');
  host.innerHTML = h.join("");
  host.querySelectorAll("[data-xp-measure]").forEach(function (b) { b.addEventListener("click", function () { XP.measure = b.getAttribute("data-xp-measure"); renderVariants(res); }); });
  $("xp-csv").addEventListener("click", function () { xpCsv(cells, exp.id); });
}

function xpChart(cells, model, sizes, agents, k) {
  var W = 320, H = 190, L = 44, R = 12, T = 12, B = 34;
  var pts = cells.filter(function (c) { return c.model === model && xpVal(c, k) != null; });
  if (!pts.length) return '<p class="small muted">No ' + esc(xpMeasure(k)[1].toLowerCase()) + ' reading for this model.</p>';
  var max = Math.max.apply(null, pts.map(function (c) { return xpVal(c, k); })) * 1.12 || 1;
  var lo = agents[0], hi = agents[agents.length - 1];
  var x = function (a) { return hi === lo ? L + (W - L - R) / 2 : L + (a - lo) / (hi - lo) * (W - L - R); };
  var y = function (v) { return T + (1 - v / max) * (H - T - B); };
  var s = ['<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(xpMeasure(k)[1] + " by number of agents, " + short(model)) + '">'];
  s.push('<line x1="' + L + '" y1="' + (H - B) + '" x2="' + (W - R) + '" y2="' + (H - B) + '" stroke="var(--border-2)"/>');
  [0, 0.5, 1].forEach(function (f) {
    var v = max / 1.12 * f, yy = y(v);
    s.push('<line x1="' + L + '" y1="' + yy + '" x2="' + (W - R) + '" y2="' + yy + '" stroke="var(--border)" stroke-dasharray="3 3"/><text x="' + (L - 4) + '" y="' + (yy + 3) + '" text-anchor="end">' + esc(xpFmt(v, k)) + '</text>');
  });
  agents.forEach(function (a) { s.push('<text x="' + x(a) + '" y="' + (H - B + 14) + '" text-anchor="middle">' + a + '</text>'); });
  s.push('<text x="' + ((L + W - R) / 2) + '" y="' + (H - 4) + '" text-anchor="middle">number of agents</text>');
  sizes.forEach(function (p) {
    var line = pts.filter(function (c) { return c.precision === p; }).sort(function (a, b) { return a.agents - b.agents; });
    if (!line.length) return;
    if (line.length > 1) s.push('<polyline fill="none" stroke="' + XP_COLOR[p] + '" stroke-width="2" points="' + line.map(function (c) { return x(c.agents) + "," + y(xpVal(c, k)); }).join(" ") + '"/>');
    line.forEach(function (c) {
      s.push('<circle cx="' + x(c.agents) + '" cy="' + y(xpVal(c, k)) + '" r="4" fill="' + XP_COLOR[p] + '"><title>' + esc(short(model) + ", " + XP_SIZE_LABEL[p] + ", " + c.agents + " agents: " + xpFmt(xpVal(c, k), k)) + '</title></circle>');
    });
  });
  s.push("</svg>");
  return s.join("");
}

function xpHeat(cells, model, sizes, agents, k) {
  var mine = cells.filter(function (c) { return c.model === model; });
  var vals = mine.map(function (c) { return xpVal(c, k); }).filter(function (v) { return v != null; });
  var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals), better = xpMeasure(k)[4];
  var shade = function (v) {
    if (v == null || !vals.length) return "";
    var f = hi === lo ? 0.5 : (v - lo) / (hi - lo);
    if (!better) f = 1 - f;                                       // darker = worse, whatever the direction
    return "background:color-mix(in srgb, var(--accent) " + Math.round(10 + 45 * f) + "%, transparent)";
  };
  return '<div class="table-wrap"><table class="xp-hm"><thead><tr><th>Size</th>' + agents.map(function (a) { return '<th class="num">' + a + ' agent' + (a === 1 ? "" : "s") + '</th>'; }).join("") + '</tr></thead><tbody>' +
    sizes.map(function (p) {
      return '<tr><th>' + XP_SIZE_LABEL[p] + '</th>' + agents.map(function (a) {
        var c = mine.filter(function (x) { return x.precision === p && x.agents === a; })[0], v = c ? xpVal(c, k) : null;
        return '<td class="v" style="' + shade(v) + '">' + (c ? esc(xpFmt(v, k)) : '<span class="muted small">not run</span>') + '</td>';
      }).join("") + '</tr>';
    }).join("") + '</tbody></table></div>';
}

function xpTable(cells, runs) {
  var keys = ["mean_latency_s", "max_peak_vram_mb", "mean_tokens_per_flow", "handoff_share_of_total", "accuracy", "cost_per_1k_flows_usd"];
  var sorted = cells.slice().sort(function (a, b) { return a.model < b.model ? -1 : a.model > b.model ? 1 : XP_SIZES.indexOf(a.precision) - XP_SIZES.indexOf(b.precision) || a.agents - b.agents; });
  var pending = runs.filter(function (s) { return !(s.metrics && Object.keys(s.metrics).length); });
  return '<div class="table-wrap"><table id="xp-cells"><thead><tr><th>Model</th><th>Size</th><th class="num">Agents</th>' +
    keys.map(function (k) { return '<th class="num">' + esc(xpMeasure(k)[1]) + '</th>'; }).join("") + '<th>Run</th></tr></thead><tbody>' +
    sorted.map(function (c) {
      return '<tr><td class="mono">' + esc(nick(c.model)) + '</td><td>' + XP_SIZE_LABEL[c.precision] + '</td><td class="num">' + c.agents + '</td>' +
        keys.map(function (k) { return '<td class="num">' + esc(xpFmt(xpVal(c, k), k)) + '</td>'; }).join("") +
        '<td><a href="#/session/' + enc(c.sid) + '/overview">Open</a></td></tr>';
    }).join("") +
    pending.map(function (s) {
      var v = s.variant || {};
      return '<tr class="muted"><td class="mono">' + esc((s.models || []).map(short).join(", ")) + '</td><td>' + esc(XP_SIZE_LABEL[v.precision] || "") + '</td><td class="num">' + esc(String(v.agents || "")) + '</td>' +
        '<td colspan="' + keys.length + '">' + statusChip(s.status) + '</td><td><a href="#/session/' + enc(s.session_id) + '">Open</a></td></tr>';
    }).join("") + '</tbody></table></div>';
}

// what changed, in plain sentences built from the measured cells (no model writes these)
function xpChanged(cells, models, sizes, agents) {
  var out = [];
  var at = function (md, p, a) { return cells.filter(function (c) { return c.model === md && c.precision === p && c.agents === a; })[0]; };
  var times = function (a, b) { return a && b ? fmtNum(b / a, 1) + " times" : null; };
  if (agents.length > 1) {
    var a0 = agents[0], a1 = agents[agents.length - 1];
    models.forEach(function (md) {
      sizes.forEach(function (p) {
        var c0 = at(md, p, a0), c1 = at(md, p, a1);
        if (!c0 || !c1) return;
        var lt = times(c0.m.mean_latency_s, c1.m.mean_latency_s), tk = times(c0.m.mean_tokens_per_flow, c1.m.mean_tokens_per_flow);
        var acc = c0.m.accuracy != null && c1.m.accuracy != null ? " Reference-label match went from " + fmtPct(c0.m.accuracy) + " to " + fmtPct(c1.m.accuracy) + "." : "";
        if (lt || tk) out.push("From " + a0 + " to " + a1 + " agents, " + nick(md) + " (" + XP_SIZE_LABEL[p] + ") needed " +
          [lt ? lt + " the time per flow" : null, tk ? tk + " the tokens" : null].filter(Boolean).join(" and ") + "." + acc);
      });
    });
    if (out.length) out.push("Every extra agent re-reads the outputs of all earlier agents, so the re-sent context grows with each one.");
  }
  if (sizes.length > 1) {
    models.forEach(function (md) {
      var a = agents.indexOf(4) >= 0 ? 4 : agents[0], base = at(md, sizes[0], a);
      sizes.slice(1).forEach(function (p) {
        var c = at(md, p, a);
        if (!base || !c) return;
        var mem = base.m.max_peak_vram_mb && c.m.max_peak_vram_mb ? fmtNum(c.m.max_peak_vram_mb / base.m.max_peak_vram_mb, 1) + " times the GPU memory" : null;
        var lt = base.m.mean_latency_s && c.m.mean_latency_s ? (c.m.mean_latency_s >= base.m.mean_latency_s ? fmtNum(c.m.mean_latency_s / base.m.mean_latency_s, 1) + " times the time per flow" : fmtPct(1 - c.m.mean_latency_s / base.m.mean_latency_s) + " less time per flow") : null;
        if (mem || lt) out.push("At " + a + " agents, " + nick(md) + " at " + XP_SIZE_LABEL[p] + " used " + [mem, lt].filter(Boolean).join(" and ") + " compared with " + XP_SIZE_LABEL[sizes[0]] + ".");
      });
    });
  }
  if (!out.length) return '<p class="muted small">The comparison appears once at least two runs of the same model have finished.</p>';
  return '<ul class="small" style="margin:0;padding-left:18px">' + out.map(function (t) { return "<li>" + esc(t) + "</li>"; }).join("") + '</ul>';
}

function xpCsv(cells, id) {
  var keys = XP_MEASURES.map(function (m) { return m[0]; });
  var rows = [["model", "precision", "agents", "session_id"].concat(keys)].concat(cells.map(function (c) {
    return [c.model, c.precision, c.agents, c.sid].concat(keys.map(function (k) { return c.m[k] == null ? "" : c.m[k]; }));
  }));
  var csv = rows.map(function (r) { return r.map(function (v) { v = String(v); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }).join(","); }).join("\n");
  var a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = id + ".csv";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
}
