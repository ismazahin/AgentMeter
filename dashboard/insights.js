/* AgentMeter session insights — read-only derivations from a stored session_results.json.
 *
 * Every function here only re-reads numbers the analysis already stored (no new
 * measurement, no re-scoring of the locked study): key findings, the per-criterion
 * profile, and the other views of the session tabs. The app (web/index.html) draws
 * them; the maths lives here so it is node-tested (tests/test_insights.py).
 *
 * Works in the browser (window.INSIGHTS) and under node (module.exports).
 */
(function (global) {
  var CRIT = ["accuracy", "latency", "vram", "tokens"];

  function bare(m) { return String(m || "").replace(/^mock:/, ""); }
  function short(m) { return bare(m).split("/").pop(); }
  function num(v) { return v != null && isFinite(+v) ? +v : null; }
  function time(v) {
    if (v == null) return "—";
    return v < 1 ? (v * 1000).toFixed(v < 0.01 ? 2 : 1) + " ms" : v.toFixed(2) + " s";
  }
  function pct(v, d) { return v == null ? "—" : (v * 100).toFixed(d == null ? 1 : d) + "%"; }

  // ---- Overview: per-criterion profile ----------------------------------------------
  // The four SAW criteria, normalised against the study targets (1 = meets the target),
  // per model. A criterion with no readings for any model is flagged, not drawn as 0.
  function criteriaProfile(res) {
    var p8 = res.phase8 || {}, lab = !!(res.session || {}).accuracy_available;
    var rows = (p8.normalised || []).map(function (n) {
      var s = {};
      CRIT.forEach(function (k) { s[k] = num(n[k]); });
      return { model: bare(n.model), scores: s };
    });
    var missing = {};
    CRIT.forEach(function (k) {
      missing[k] = (k === "accuracy" && !lab) || rows.every(function (r) { return !r.scores[k]; });
    });
    return { criteria: CRIT.slice(), rows: rows, missing: missing, targets: p8.targets || {} };
  }

  // ---- Overview: key findings ------------------------------------------------------
  // Short sentences, each tied to one stored number. Order: the measured difference,
  // the tail, where the time goes, the agentic overhead, cost, accuracy (context).
  function keyFindings(res) {
    var out = [], rel = res.relative_comparison, pm = res.per_model || [];
    if (rel && rel.metrics) {
      var wins = rel.metrics.filter(function (m) {
        return m.available && m.winner && m.winner !== "tie" && m.metric !== "accuracy" && m.pct_better != null;
      }).sort(function (a, b) { return b.pct_better - a.pct_better; });
      if (wins.length) {
        var w = wins[0];
        out.push({ key: "gap", text: "Biggest measured gap: " + short(w.winner) + " is " + w.pct_better + "% better on " + w.label +
          (w.p_value != null ? " (p = " + (+w.p_value).toPrecision(2) + ")" : "") + "." });
      } else {
        out.push({ key: "gap", text: "No efficiency metric separates the models beyond the tie range." });
      }
    }
    var tails = pm.map(function (m) {
      var e = (m.efficiency || {}).end_to_end || {};
      return { model: m.model, ratio: e.median_latency_s ? e.p95_latency_s / e.median_latency_s : null, p95: e.p95_latency_s };
    }).filter(function (t) { return t.ratio != null; }).sort(function (a, b) { return b.ratio - a.ratio; });
    if (tails.length) {
      var t = tails[0];
      out.push({ key: "tail", text: "Slowest 5% of flows: " + short(t.model) + " takes " + t.ratio.toFixed(1) + "× its median time (p95 " + time(t.p95) + ")." });
    }
    var ag = (res.agents || {}).per_model || [];
    var tops = ag.map(function (m) {
      var top = (m.agents || []).slice().sort(function (a, b) { return (b.latency_share || 0) - (a.latency_share || 0); })[0];
      return top && top.latency_share != null ? { model: m.model, agent: top.agent, share: top.latency_share } : null;
    }).filter(Boolean);
    if (tops.length > 1 && tops.every(function (x) { return x.agent === tops[0].agent; })) {
      out.push({ key: "agent", text: "Where the time goes: the " + tops[0].agent + " step is the slowest for every model (" +
        tops.map(function (x) { return pct(x.share, 0) + " of each flow for " + short(x.model); }).join(", ") + ")." });
    } else {
      tops.forEach(function (x) { out.push({ key: "agent", text: "Where the time goes (" + short(x.model) + "): the " + x.agent + " step takes " + pct(x.share, 0) + " of each flow." }); });
    }
    var ov = ag.map(function (m) { return { model: m.model, share: num((m.overhead || {}).handoff_share_of_total) }; })
      .filter(function (o) { return o.share != null; }).sort(function (a, b) { return b.share - a.share; });
    if (ov.length) {
      out.push({ key: "overhead", text: "Agentic overhead: re-sent hand-off text is " + pct(ov[0].share, 0) + " of all tokens for " + short(ov[0].model) + " (estimate)." });
    }
    var ce = ((res.cost_energy || {}).per_model || []).filter(function (r) { return r.cost_per_1k_flows_usd != null; })
      .sort(function (a, b) { return a.cost_per_1k_flows_usd - b.cost_per_1k_flows_usd; });
    if (ce.length) {
      out.push({ key: "cost", text: "Cheapest to run: " + short(ce[0].model) + " at $" + ce[0].cost_per_1k_flows_usd.toFixed(4) + " per 1,000 flows" +
        (ce.length > 1 ? " ($" + ce[ce.length - 1].cost_per_1k_flows_usd.toFixed(4) + " for " + short(ce[ce.length - 1].model) + ")" : "") + "." });
    } else if (res.cost_energy) {
      out.push({ key: "cost", text: "Cost and energy were not measured in this run: " + ((((res.cost_energy || {}).per_model || [])[0] || {}).cost_reason || "no GPU") + "." });
    }
    if ((res.session || {}).accuracy_available) {
      var acc = pm.map(function (m) { return { model: m.model, a: num((m.accuracy || {}).accuracy) }; }).filter(function (x) { return x.a != null; })
        .sort(function (a, b) { return b.a - a.a; });
      if (acc.length > 1 && Math.abs(acc[0].a - acc[acc.length - 1].a) < 1e-9) {
        out.push({ key: "accuracy", text: "Accuracy (context): the models tie at " + pct(acc[0].a) + "." });
      } else if (acc.length) {
        out.push({ key: "accuracy", text: "Accuracy (context): " + short(acc[0].model) + " is highest at " + pct(acc[0].a) + "." });
      }
    }
    return out;
  }

  // ---- Ranking -> Robustness: how far can one weight move before #1 changes? ---------
  // For each criterion k its weight t runs 0..1 while the other weights keep their stored
  // ratios (w_j * (1 - t) / (1 - w_k)); each model's composite is linear in t, so the
  // leader only changes where two lines cross. Returns, per criterion, the segments of
  // t with their leader, and the stable interval around the stored weight.
  var EPS = 1e-9;
  function weightStability(res, weights) {
    var p8 = res.phase8 || {}, lab = !!(res.session || {}).accuracy_available;
    var w0 = {}, tot = 0;
    CRIT.forEach(function (k) { w0[k] = (k === "accuracy" && !lab) ? 0 : Math.max(0, +((weights || p8.weights || {})[k]) || 0); tot += w0[k]; });
    CRIT.forEach(function (k) { w0[k] = tot ? w0[k] / tot : 0.25; });
    var models = (p8.normalised || []).map(function (n) { var s = {}; CRIT.forEach(function (k) { s[k] = num(n[k]) || 0; }); return { model: bare(n.model), s: s }; });
    function leaderAt(k, t) {
      var rest = 1 - w0[k], best = null, bestV = -Infinity, tie = false;
      models.forEach(function (m) {
        var v = t * m.s[k];
        CRIT.forEach(function (j) { if (j !== k) v += (rest > EPS ? w0[j] * (1 - t) / rest : (1 - t) / 3) * m.s[j]; });
        if (v > bestV + EPS) { best = m.model; bestV = v; tie = false; } else if (Math.abs(v - bestV) <= EPS) tie = true;
      });
      return tie ? null : best;          // null: the top place is a tie at this weight
    }
    var crits = CRIT.filter(function (k) { return lab || k !== "accuracy"; });
    var rows = crits.map(function (k) {
      var noData = models.every(function (m) { return !m.s[k]; }), STEPS = 1000, segs = [];
      for (var i = 0; i <= STEPS; i++) {
        var t = i / STEPS, l = leaderAt(k, t), last = segs[segs.length - 1];
        if (last && last.leader === l) last.to = t; else segs.push({ from: t, to: t, leader: l });
      }
      // a tie at a single point (e.g. weight 1 on a criterion where every model scores the same)
      // is not a range: fold it into its neighbour and say so
      var pointTie = null;
      segs = segs.filter(function (sg, i) {
        var drop = sg.leader === null && sg.from === sg.to && segs.length > 1;
        if (drop) { pointTie = sg.from; if (i > 0) segs[i - 1].to = sg.to; else segs[1].from = sg.from; }
        return !drop;
      });
      var here = leaderAt(k, w0[k]), stable = null;
      segs.forEach(function (sg) { if (sg.leader === here && w0[k] >= sg.from - 0.0005 && w0[k] <= sg.to + 0.0005) stable = [sg.from, sg.to]; });
      return { criterion: k, stored: w0[k], leader: here, no_data: noData, segments: segs, stable: stable,
               changes: segs.length > 1, point_tie: pointTie };
    });
    return { models: models.map(function (m) { return m.model; }), weights: w0, rows: rows };
  }
  function stabilitySentence(row) {
    var r2 = function (v) { return v.toFixed(2); };
    if (row.no_data) return "No readings for this criterion in the run: its weight changes nothing.";
    if (!row.leader) return "At the stored weight (" + r2(row.stored) + ") the top place is a tie.";
    if (!row.changes) return short(row.leader) + " stays first for any weight from 0 to 1" +
      (row.point_tie != null ? " (at exactly " + r2(row.point_tie) + " every model ties)" : "") + ".";
    var s = row.stable;
    return short(row.leader) + " stays first while this weight is between " + r2(s[0]) + " and " + r2(s[1]) + " (stored " + r2(row.stored) + ").";
  }

  // ---- Accuracy: precision / recall / F1 per class, from the stored confusion matrices ----
  // rows = true label, columns = predicted. "Unparseable" is a column only: a miss for the
  // true class (lowers recall), never a false positive of a class.
  var UNPARSEABLE = "Unparseable";
  function classMetrics(res) {
    if (!(res.session || {}).accuracy_available) return { available: false, models: [] };
    var models = (res.per_model || []).map(function (m) {
      var cm = (m.accuracy || {}).confusion || {}, classes = Object.keys(cm).filter(function (c) { return c !== UNPARSEABLE; });
      var rows = classes.map(function (c) {
        var tp = +(cm[c] || {})[c] || 0, support = 0, fp = 0;
        Object.keys(cm[c] || {}).forEach(function (p) { support += +cm[c][p] || 0; });
        classes.forEach(function (t) { if (t !== c) fp += +((cm[t] || {})[c]) || 0; });
        var fn = support - tp, prec = tp + fp ? tp / (tp + fp) : null, rec = support ? tp / support : null;
        var f1 = prec != null && rec != null && prec + rec > 0 ? 2 * prec * rec / (prec + rec) : (prec == null && rec == null ? null : 0);
        return { cls: c, support: support, tp: tp, fp: fp, fn: fn, precision: prec, recall: rec, f1: f1 };
      });
      // macro average over the classes present in the data; an undefined precision (class never
      // predicted) counts as 0, as scikit-learn's zero_division=0 does
      function macro(k) { var v = rows.filter(function (r) { return r.support > 0; }); return v.length ? v.reduce(function (a, r) { return a + (r[k] || 0); }, 0) / v.length : null; }
      var mix = [], unp = 0, n = 0;
      classes.forEach(function (t) {
        Object.keys(cm[t] || {}).forEach(function (p) {
          var c = +cm[t][p] || 0; n += c;
          if (p === UNPARSEABLE) unp += c;
          else if (p !== t && c > 0) mix.push({ truth: t, predicted: p, count: c, share: null });
        });
      });
      mix.forEach(function (x) { var sup = rows.filter(function (r) { return r.cls === x.truth; })[0].support; x.share = sup ? x.count / sup : null; });
      mix.sort(function (a, b) { return b.count - a.count || (a.truth < b.truth ? -1 : 1); });
      return { model: bare(m.model), classes: rows, macro: { precision: macro("precision"), recall: macro("recall"), f1: macro("f1") },
               mixups: mix.slice(0, 5), unparseable: unp, n: n };
    }).filter(function (m) { return m.classes.length; });
    return { available: models.length > 0, models: models };
  }

  // ---- Performance -> Latency: histogram small multiples (shared bins, shared count scale) ----
  function latencyHistogram(res) {
    var ld = res.latency_distribution;
    if (!ld || !(ld.bin_edges_s || []).length) return null;
    var e = ld.bin_edges_s, lo = e[0], span = (e[e.length - 1] - lo) || 1;
    var pos = function (v) { return v == null ? null : Math.max(0, Math.min(1, (v - lo) / span)); };
    var ymax = 0;
    var models = (ld.per_model || []).map(function (r) {
      var bins = (r.histogram || []).map(function (c, i) { ymax = Math.max(ymax, c); return { from: e[i], to: e[i + 1], count: c }; });
      return { model: bare(r.model), n: r.n, bins: bins, small_sample: !!r.small_sample,
               marks: ["p50", "p95", "p99"].map(function (k) { return { key: k, value: r[k], pos: pos(r[k]) }; }) };
    });
    return { edges: e, ymax: ymax, models: models, metric: ld.metric };
  }

  // ---- Performance -> Statistics: effect sizes (Cliff's delta, unitless) + per-metric CI ----
  var MAGNITUDE = [["negligible", 0.147], ["small", 0.33], ["medium", 0.474], ["large", 1]];   // Romano et al.
  function effectSizes(res) {
    var ef = res.effect_sizes;
    if (!ef || ef.skipped || !ef.metrics) return null;
    return { model_a: bare(ef.model_a), model_b: bare(ef.model_b), bands: MAGNITUDE,
      rows: ef.metrics.map(function (m) {
        if (!m.available) return { metric: m.metric, available: false, reason: m.reason };
        var half = Math.max(Math.abs(m.ci95_low), Math.abs(m.ci95_high), Math.abs(m.diff_mean_a_minus_b)) * 1.15 || 1;
        return { metric: m.metric, available: true, delta: m.cliffs_delta, magnitude: m.magnitude, diff: m.diff_mean_a_minus_b,
                 low: m.ci95_low, high: m.ci95_high, excludes_zero: !!m.ci_excludes_zero, p: m.kruskal_wallis_p,
                 // symmetric axis around 0 for the CI strip; positions are fractions of it
                 half_range: half, pos: { low: (m.ci95_low + half) / (2 * half), high: (m.ci95_high + half) / (2 * half), diff: (m.diff_mean_a_minus_b + half) / (2 * half) } };
      }) };
  }

  // ---- Agent Analysis: the hand-off graph ----------------------------------------------
  // agents.handoffs[x] lists the agents whose output x's prompt re-sends; an edge carries the
  // sender's mean output tokens per flow (the basis of the stored overhead estimate).
  function handoffGraph(res) {
    var ag = res.agents;
    if (!ag || !ag.handoffs) return null;
    var order = ag.agent_order || ["perceive", "reason", "decide", "act"];
    return { agents: order, per_model: (ag.per_model || []).map(function (m) {
      var by = {};
      (m.agents || []).forEach(function (a) { by[a.agent] = a; });
      var edges = [];
      order.forEach(function (to) {
        (ag.handoffs[to] || []).forEach(function (from) {
          edges.push({ from: from, to: to, tokens: num((by[from] || {}).mean_output_tokens) });
        });
      });
      var nodes = order.map(function (a) {
        var x = by[a] || {};
        return { agent: a, latency_s: num(x.mean_latency_s), share: num(x.latency_share), in_tokens: num(x.mean_input_tokens), out_tokens: num(x.mean_output_tokens) };
      });
      var resent = edges.reduce(function (t, e) { return t + (e.tokens || 0); }, 0);
      return { model: bare(m.model), nodes: nodes, edges: edges, resent_tokens: resent };
    }) };
  }

  // ---- Agent Analysis: time per agent by attack class ---------------------------------------
  function classAgentTime(res) {
    var rows = ((res.per_class || {}).per_agent || []);
    if (!rows.length) return null;
    var order = ((res.agents || {}).agent_order) || ["perceive", "reason", "decide", "act"];
    var models = [], classes = [], cell = {};
    rows.forEach(function (r) {
      var m = bare(r.model);
      if (models.indexOf(m) < 0) models.push(m);
      if (classes.indexOf(r["class"]) < 0) classes.push(r["class"]);
      cell[m + "\u0000" + r["class"] + "\u0000" + r.agent_name] = r;
    });
    var ymax = 0;
    var per_model = models.map(function (m) {
      return { model: m, rows: classes.map(function (c) {
        var parts = order.map(function (a) { var r = cell[m + "\u0000" + c + "\u0000" + a]; return { agent: a, s: r ? num(r.mean_wall_s) : null }; });
        var total = parts.reduce(function (t, p) { return t + (p.s || 0); }, 0), n = null;
        order.forEach(function (a) { var r = cell[m + "\u0000" + c + "\u0000" + a]; if (r && n == null) n = r.n; });
        parts.forEach(function (p) { p.share = total ? (p.s || 0) / total : null; });
        ymax = Math.max(ymax, total);
        var top = parts.slice().sort(function (x, y) { return (y.s || 0) - (x.s || 0); })[0];
        return { cls: c, n: n, total: total, parts: parts, slowest_agent: top && top.s ? top.agent : null };
      }) };
    });
    return { agents: order, classes: classes, max_total: ymax, per_model: per_model };
  }

  // ---- Cost & Energy: project the measured per-flow numbers onto a daily workload -----------
  // Same basis as the stored cost (mean latency x GPU price, flows one at a time on one GPU):
  // GPU-hours/day = N x latency / 3600; one GPU handles 86,400 / latency flows a day.
  function workloadProjection(res, opts) {
    var n = Math.max(0, +((opts || {}).flows_per_day) || 0), price = num((opts || {}).usd_per_hour);
    var ce = {};
    ((res.cost_energy || {}).per_model || []).forEach(function (r) { ce[bare(r.model)] = r; });
    var stored = num((((res.cost_energy || {}).price) || {}).usd_per_hour);
    var real = ((res.environment || {}).provider) === "real";
    return { flows_per_day: n, usd_per_hour: price != null ? price : stored, price_source: price != null ? "yours" : stored != null ? "stored" : null, real: real,
      models: (res.per_model || []).map(function (m) {
        var lat = num((((m.efficiency || {}).end_to_end) || {}).mean_latency_s), c = ce[bare(m.model)] || {};
        var gpuH = lat != null ? n * lat / 3600 : null, cap = lat ? 86400 / lat : null, p = price != null ? price : stored;
        var whK = num(c.wh_per_1k_flows);
        return { model: bare(m.model), mean_latency_s: lat, gpu_hours_per_day: gpuH, capacity_per_gpu_day: cap,
                 utilisation: cap ? n / cap : null, gpus_needed: cap ? Math.max(1, Math.ceil(n / cap - 1e-9)) : null,
                 cost_per_day: gpuH != null && p != null ? gpuH * p : null,
                 cost_per_month: gpuH != null && p != null ? gpuH * p * 30 : null,
                 kwh_per_day: whK != null ? n / 1000 * whK / 1000 : null };
      }) };
  }

  // ---- Cost & Energy: what one correct answer costs (labelled runs; accuracy as context) ------
  function costPerCorrect(res) {
    if (!(res.session || {}).accuracy_available) return null;
    var ce = {};
    ((res.cost_energy || {}).per_model || []).forEach(function (r) { ce[bare(r.model)] = r; });
    return (res.per_model || []).map(function (m) {
      var a = num((m.accuracy || {}).accuracy), lat = num((((m.efficiency || {}).end_to_end) || {}).mean_latency_s), c = ce[bare(m.model)] || {};
      var ok = a != null && a > 0;
      return { model: bare(m.model), accuracy: a,
               gpu_s_per_correct: ok && lat != null ? lat / a : null,
               usd_per_1k_correct: ok && c.cost_per_1k_flows_usd != null ? c.cost_per_1k_flows_usd / a : null,
               wh_per_correct: ok && c.wh_per_flow != null ? c.wh_per_flow / a : null,
               flows_per_correct: ok ? 1 / a : null };
    });
  }

  // ---- Recommendations: fit against common deployment profiles ------------------------------
  // Fixed, documented thresholds (view only; "Your limits" checks your own). Each cell is
  // pass / fail / no_data against one stored number of the model.
  var PROFILES = [
    { id: "gpu8", group: "GPU memory", label: "Fits an 8 GB GPU", metric: "total_peak_mb", max: 8192, unit: "MB" },
    { id: "gpu16", group: "GPU memory", label: "Fits a 16 GB GPU", metric: "total_peak_mb", max: 16384, unit: "MB" },
    { id: "gpu24", group: "GPU memory", label: "Fits a 24 GB GPU", metric: "total_peak_mb", max: 24576, unit: "MB" },
    { id: "rt2", group: "Response time", label: "Interactive: p95 ≤ 2 s", metric: "p95_latency_s", max: 2, unit: "s" },
    { id: "rt10", group: "Response time", label: "Near real-time: p95 ≤ 10 s", metric: "p95_latency_s", max: 10, unit: "s" },
    { id: "rt60", group: "Response time", label: "Batch: mean ≤ 60 s", metric: "mean_latency_s", max: 60, unit: "s" },
    { id: "vol10k", group: "Volume on one GPU", label: "10,000 flows a day", metric: "capacity_per_day", min: 10000, unit: "flows" },
    { id: "vol100k", group: "Volume on one GPU", label: "100,000 flows a day", metric: "capacity_per_day", min: 100000, unit: "flows" },
    { id: "usd1", group: "Budget", label: "≤ $1 per 1,000 flows", metric: "cost_per_1k_flows_usd", max: 1, unit: "USD" },
    { id: "usd10", group: "Budget", label: "≤ $10 per 1,000 flows", metric: "cost_per_1k_flows_usd", max: 10, unit: "USD" }
  ];
  function deploymentFit(res) {
    var mem = {}, ce = {};
    ((res.memory || {}).per_model || []).forEach(function (r) { mem[bare(r.model)] = r; });
    ((res.cost_energy || {}).per_model || []).forEach(function (r) { ce[bare(r.model)] = r; });
    var models = (res.per_model || []).map(function (m) {
      var e = ((m.efficiency || {}).end_to_end) || {}, k = bare(m.model), mm = mem[k] || {}, cc = ce[k] || {};
      var v = { total_peak_mb: num(mm.total_peak_mb), p95_latency_s: num(e.p95_latency_s), mean_latency_s: num(e.mean_latency_s),
                capacity_per_day: num(e.mean_latency_s) ? 86400 / e.mean_latency_s : null, cost_per_1k_flows_usd: num(cc.cost_per_1k_flows_usd) };
      var why = { total_peak_mb: mm.reason || mm.status || "not measured", cost_per_1k_flows_usd: cc.cost_reason || cc.cost_status || "not measured" };
      var cells = PROFILES.map(function (p) {
        var x = v[p.metric];
        if (x == null) return { id: p.id, result: "no_data", value: null, reason: why[p.metric] || "not measured" };
        var ok = p.max != null ? x <= p.max : x >= p.min;
        return { id: p.id, result: ok ? "pass" : "fail", value: x };
      });
      return { model: k, values: v, cells: cells, passes: cells.filter(function (c) { return c.result === "pass"; }).length };
    });
    return { profiles: PROFILES, models: models };
  }

  // ---- Performance -> Latency: latency over the run (stored latency_trend), shared y range ----
  function latencyTrend(res) {
    var lt = res.latency_trend;
    if (!lt || !(lt.per_model || []).length) return null;
    var ymax = 0;
    var models = lt.per_model.map(function (m) {
      (m.points || []).forEach(function (p) { ymax = Math.max(ymax, p[1] || 0, p[2] || 0); });
      return { model: bare(m.model), n: m.n, window: m.window, thinned: !!m.thinned, drift: m.drift || {},
               points: (m.points || []).map(function (p) { return { flow: p[0], latency_s: p[1], median_s: p[2] }; }) };
    });
    return { ymax: ymax, method: lt.method, models: models };
  }

  // ---- Cost & Energy: the GPU over the session (stored gpu_timeline) -----------------------
  function gpuTimeline(res) {
    var g = res.gpu_timeline;
    if (!g) return null;
    if (!g.sampled) return { sampled: false, reason: g.reason };
    var pts = (g.points || []).map(function (p) { return { t: p[0], power_w: p[1], util_pct: p[2], mem_mb: p[3] }; });
    var max = function (k) { return pts.reduce(function (m, p) { return p[k] != null && p[k] > m ? p[k] : m; }, 0); };
    return { sampled: true, method: g.method, interval_s: g.interval_s, n_samples: g.n_samples, has_util: !!g.has_util, has_mem: !!g.has_mem,
             t_max: pts.length ? pts[pts.length - 1].t : 0, power_max: max("power_w"), mem_max: max("mem_mb"), points: pts,
             windows: (g.windows || []).map(function (w) { return { model: bare(w.model), from_s: w.from_s, to_s: w.to_s }; }),
             idle: (g.idle || []).map(function (w) { return { model: bare(w.model), from_s: w.from_s, to_s: w.to_s }; }),
             per_model: (g.per_model || []).map(function (m) { var o = {}; Object.keys(m).forEach(function (k) { o[k] = m[k]; }); o.model = bare(m.model); return o; }) };
  }

  // ---- Repeatability: the same models on the same flows, GPU and settings, across sessions ----
  // Input: the /api/compare/merged answer (runs + rows of stored values, one column per run).
  // Spread per model and metric (sample SD, coefficient of variation, min-max range) and how
  // often the faster model of this session was also the faster one in the other sessions.
  var REPEAT_METRICS = [["mean_latency_s", "Mean latency per flow", "s"], ["p95_latency_s", "p95 latency", "s"],
    ["mean_tokens_per_flow", "Tokens per flow", "tokens"], ["total_peak_mb", "Total peak VRAM", "MB"],
    ["wh_per_flow", "Energy per flow", "Wh"], ["accuracy", "Accuracy (context)", "share"], ["saw_composite_100", "SAW score", "points"]];
  function repeatability(merged, sessionId) {
    var runs = (merged && merged.runs) || [], rows = {};
    ((merged && merged.rows) || []).forEach(function (r) { rows[r.metric] = r.values || []; });
    var models = [];
    runs.forEach(function (r) { if (models.indexOf(r.model) < 0) models.push(r.model); });
    var sessions = [];
    runs.forEach(function (r) { if (sessions.indexOf(r.session) < 0) sessions.push(r.session); });
    var mine = runs.filter(function (r) { return r.session === sessionId; }).map(function (r) { return r.model; });
    var out = models.filter(function (m) { return mine.indexOf(m) >= 0; }).map(function (m) {
      var idx = []; runs.forEach(function (r, i) { if (r.model === m) idx.push(i); });
      var metrics = REPEAT_METRICS.filter(function (d) { return rows[d[0]]; }).map(function (d) {
        var vals = idx.map(function (i) { return { session: runs[i].session, value: num(rows[d[0]][i]), current: runs[i].session === sessionId }; })
          .filter(function (v) { return v.value != null; });
        if (!vals.length) return null;
        var xs = vals.map(function (v) { return v.value; }), n = xs.length, mean = xs.reduce(function (a, b) { return a + b; }, 0) / n;
        var sd = n > 1 ? Math.sqrt(xs.reduce(function (a, x) { return a + (x - mean) * (x - mean); }, 0) / (n - 1)) : null;
        var lo = Math.min.apply(null, xs), hi = Math.max.apply(null, xs);
        return { metric: d[0], label: d[1], unit: d[2], n: n, values: vals, mean: mean, sd: sd, min: lo, max: hi,
                 cov: sd != null && mean ? sd / Math.abs(mean) : null, range_pct: mean ? (hi - lo) / Math.abs(mean) : null };
      }).filter(Boolean);
      return { model: bare(m), n_runs: idx.length, metrics: metrics };
    });
    // the faster model (mean latency) in every session that ran both of this session's models
    var leader = null;
    if (mine.length === 2 && rows.mean_latency_s) {
      var lead = function (sid) {
        var v = mine.map(function (m) { var i = -1; runs.forEach(function (r, j) { if (r.session === sid && r.model === m) i = j; }); return i < 0 ? null : num(rows.mean_latency_s[i]); });
        if (v[0] == null || v[1] == null) return undefined;
        return v[0] === v[1] ? null : bare(v[0] < v[1] ? mine[0] : mine[1]);
      };
      var cur = lead(sessionId), both = sessions.filter(function (sid) { return lead(sid) !== undefined; });
      leader = { metric: "mean_latency_s", current: cur, sessions: both.length,
                 agree: both.filter(function (sid) { return lead(sid) === cur; }).length };
    }
    return { n_sessions: sessions.length, models: out, leader: leader };
  }

  // ---- Performance -> GPU & Throughput: the opt-in load-scaling step (stored load_scaling) ----
  function loadScaling(res) {
    var ls = res.load_scaling;
    if (!ls) return null;
    if (!ls.measured) return { measured: false, reason: ls.reason };
    var levels = [], tp = 0, p95 = 0;
    var models = (ls.per_model || []).map(function (m) {
      var lv = (m.levels || []).map(function (l) {
        if (levels.indexOf(l.concurrency) < 0) levels.push(l.concurrency);
        if (!l.error) { tp = Math.max(tp, l.throughput_flows_per_s || 0); p95 = Math.max(p95, l.p95_latency_s || 0); }
        return l;
      });
      var ok = lv.filter(function (l) { return !l.error; }), top = ok[ok.length - 1];
      return { model: bare(m.model), levels: lv, failed_at: (lv.filter(function (l) { return l.error; })[0] || {}).concurrency || null,
               error: (lv.filter(function (l) { return l.error; })[0] || {}).error || null,
               best_speedup: top ? top.speedup_vs_1 : null, best_concurrency: top ? top.concurrency : null };
    });
    levels.sort(function (a, b) { return a - b; });
    return { measured: true, levels: levels, max_throughput: tp, max_p95: p95, models: models, method: ls.method, note: ls.note };
  }

  var api = { CRIT: CRIT, bare: bare, short: short, criteriaProfile: criteriaProfile, keyFindings: keyFindings,
              weightStability: weightStability, stabilitySentence: stabilitySentence, classMetrics: classMetrics,
              latencyHistogram: latencyHistogram, effectSizes: effectSizes, handoffGraph: handoffGraph, classAgentTime: classAgentTime,
              workloadProjection: workloadProjection, costPerCorrect: costPerCorrect, deploymentFit: deploymentFit,
              latencyTrend: latencyTrend, gpuTimeline: gpuTimeline, repeatability: repeatability,
              loadScaling: loadScaling };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.INSIGHTS = api;
})(typeof window !== "undefined" ? window : globalThis);
