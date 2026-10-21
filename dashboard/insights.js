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

  var api = { CRIT: CRIT, bare: bare, short: short, criteriaProfile: criteriaProfile, keyFindings: keyFindings,
              weightStability: weightStability, stabilitySentence: stabilitySentence };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.INSIGHTS = api;
})(typeof window !== "undefined" ? window : globalThis);
