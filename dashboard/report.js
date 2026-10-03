/* AgentMeter — comparison + report/export helpers (Phase 17).
 *
 * Pure, READ-ONLY functions over one or more analysis.json objects. It never
 * recomputes the locked study numbers: SAW composites/tiers/ranks are read
 * verbatim from each file's phase8.saw_table. (Live re-ranking under the weight
 * sliders stays in saw.js; this module is for side-by-side comparison + export.)
 *
 * Works in the browser (window.REPORT) and under node (module.exports) for tests.
 */
(function (global) {
  // Metric directions (for "best" highlighting): true = higher is better.
  var HIGHER_BETTER = { composite: true, accuracy_pct: true, latency_s: false,
                        vram: false, tokens_total: false };

  function _sawRows(data) {
    var p8 = (data && data.phase8) || {};
    return (p8.saw_table && p8.saw_table.length) ? p8.saw_table : (p8.raw_criteria || []);
  }

  // VRAM cell: total device footprint when present, else marginal working memory.
  function vramOf(r) {
    if (r == null) return null;
    return (r.total_device_vram_mb != null) ? r.total_device_vram_mb : r.vram_mb;
  }

  function metricValue(cell, key) {
    if (cell == null) return null;
    if (key === "vram") return vramOf(cell);
    return (cell[key] != null) ? cell[key] : null;
  }

  // Per-session summary + model->row map, read from the file as-is.
  function sessionSummary(data) {
    var rows = _sawRows(data);
    var byModel = {}, models = [];
    rows.forEach(function (r) {
      if (r.model == null) return;
      byModel[r.model] = r;
      if (models.indexOf(r.model) < 0) models.push(r.model);
    });
    var ranked = rows.filter(function (r) { return r.rank != null; })
                     .slice().sort(function (a, b) { return a.rank - b.rank; });
    var top = ranked.length ? ranked[0].model : (models[0] || null);
    return { byModel: byModel, models: models, top: top,
             run_ids: (data && data.run_ids) || [] };
  }

  /* Assemble an aligned comparison across 2-3 sessions.
   * sessions: [{label, data}]  ->  { sessions:[{label,top,run_ids,models}],
   *   models:[union, ordered], rows:[{model, cells:[rowOrNull,...],
   *   best:{metric: index-of-best-session}}] } */
  function compareSessions(sessions) {
    var summaries = sessions.map(function (s) {
      var sm = sessionSummary(s.data);
      return { label: s.label, top: sm.top, run_ids: sm.run_ids,
               models: sm.models, byModel: sm.byModel };
    });
    // union of models, preserving first-seen order across sessions
    var models = [];
    summaries.forEach(function (sm) {
      sm.models.forEach(function (m) { if (models.indexOf(m) < 0) models.push(m); });
    });
    var rows = models.map(function (m) {
      var cells = summaries.map(function (sm) { return sm.byModel[m] || null; });
      var best = {};
      Object.keys(HIGHER_BETTER).forEach(function (key) {
        var bi = -1, bv = null;
        cells.forEach(function (c, i) {
          var v = metricValue(c, key);
          if (v == null) return;
          if (bv == null || (HIGHER_BETTER[key] ? v > bv : v < bv)) { bv = v; bi = i; }
        });
        if (bi >= 0) best[key] = bi;
      });
      return { model: m, cells: cells, best: best };
    });
    return {
      sessions: summaries.map(function (sm) {
        return { label: sm.label, top: sm.top, run_ids: sm.run_ids }; }),
      models: models, rows: rows
    };
  }

  // ---- CSV (exact from analysis.json; no recomputation) ----
  function _esc(v) {
    if (v == null) return "";
    v = String(v);
    return /[",\n\r]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  }
  function _csv(header, rows) {
    return [header].concat(rows).map(function (r) {
      return r.map(_esc).join(",");
    }).join("\r\n");
  }

  var SAW_COLS = ["model", "rank", "accuracy_pct", "latency_s", "vram_working_mb",
                  "total_device_vram_mb", "vram_mb", "tokens_total", "composite", "tier"];
  function sawCsv(data) {
    var rows = _sawRows(data).map(function (r) {
      return SAW_COLS.map(function (c) { return r[c]; });
    });
    return _csv(SAW_COLS, rows);
  }

  var AGENT_COLS = ["model", "agent_name", "mean_wall_s", "mean_ttft_s",
                    "mean_vram_delta_mb", "mean_input_tokens", "mean_output_tokens"];
  function perAgentCsv(data) {
    var t = (data && data.per_agent && data.per_agent.table) || [];
    return _csv(AGENT_COLS, t.map(function (r) {
      return AGENT_COLS.map(function (c) { return r[c]; }); }));
  }

  var CLASS_COLS = ["model", "class", "n", "mean_latency_s", "median_latency_s",
                    "mean_vram_mb", "mean_tokens", "accuracy"];
  function perClassCsv(data) {
    var t = (data && data.per_class && data.per_class.table) || [];
    return _csv(CLASS_COLS, t.map(function (r) {
      return CLASS_COLS.map(function (c) { return r[c]; }); }));
  }

  // Wide comparison CSV: one row per model, each session's key metrics side by side.
  var CMP_METRICS = ["composite", "accuracy_pct", "latency_s", "tokens_total"];
  function comparisonCsv(sessions) {
    var cmp = compareSessions(sessions);
    var header = ["model"];
    cmp.sessions.forEach(function (s) {
      CMP_METRICS.forEach(function (k) { header.push(s.label + " · " + k); });
      header.push(s.label + " · vram");
    });
    var rows = cmp.rows.map(function (row) {
      var out = [row.model];
      row.cells.forEach(function (c) {
        CMP_METRICS.forEach(function (k) { out.push(metricValue(c, k)); });
        out.push(metricValue(c, "vram"));
      });
      return out;
    });
    return _csv(header, rows);
  }

  // ---- print-friendly standalone HTML report (offline; print to PDF) ----
  function _num(v, d) { return (v == null || isNaN(v)) ? "—" : (+v).toFixed(d); }
  function _htmlEsc(s) {
    return String(s == null ? "" : s).replace(/[&<>]/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]; });
  }
  function reportHtml(data, title) {
    var sm = sessionSummary(data);
    var rows = _sawRows(data);
    var notes = (data && data.notes) || {};
    var sens = (data && data.sensitivity) || {};
    var stats = (data && data.statistics) || {};
    var dom = (data && data.per_agent && data.per_agent.dominant) || [];

    var findings = [];
    if (sens.top_stable && sens.stable_top_model)
      findings.push("Top-ranked model <b>" + _htmlEsc(sens.stable_top_model) +
        "</b> is stable across all weight sets.");
    else if (sm.top) findings.push("Top-ranked model (default weights): <b>" + _htmlEsc(sm.top) + "</b>.");
    if (notes.vram_finding) findings.push(_htmlEsc(notes.vram_finding));
    var sigMetrics = Object.keys(stats).filter(function (k) { return stats[k] && stats[k].significant; });
    if (sigMetrics.length) findings.push("Kruskal-Wallis: model differences are statistically significant for " +
      sigMetrics.map(function (m) { return _htmlEsc(m); }).join(" and ") + ".");

    var sawTable = "<table><thead><tr><th>#</th><th>Model</th><th>Accuracy</th><th>Latency</th>" +
      "<th>VRAM</th><th>Tokens</th><th>Composite</th><th>Tier</th></tr></thead><tbody>" +
      rows.slice().sort(function (a, b) { return (a.rank || 99) - (b.rank || 99); }).map(function (r) {
        return "<tr><td>" + (r.rank == null ? "—" : r.rank) + "</td><td class='l'>" + _htmlEsc(r.model) + "</td>" +
          "<td>" + _num(r.accuracy_pct, 1) + "%</td><td>" + _num(r.latency_s, 2) + " s</td>" +
          "<td>" + (vramOf(r) == null ? "—" : _num(vramOf(r) / 1024, 1) + " GB") + "</td>" +
          "<td>" + (r.tokens_total == null ? "—" : Math.round(r.tokens_total)) + "</td>" +
          "<td>" + _num(r.composite, 3) + "</td><td>" + _htmlEsc(r.tier || "—") + "</td></tr>";
      }).join("") + "</tbody></table>";

    var statsRows = Object.keys(stats).map(function (k) {
      var e = stats[k] || {};
      return "<tr><td class='l'>" + _htmlEsc(k) + "</td><td>" + _htmlEsc(e.test || "Kruskal-Wallis") +
        "</td><td>" + _num(e.H, 2) + "</td><td>" + (e.p_value == null ? "—" : (+e.p_value).toExponential(2)) +
        "</td><td>" + (e.significant ? "significant" : "not significant") + "</td></tr>";
    }).join("");
    var statsTable = statsRows ? ("<table><thead><tr><th>Metric</th><th>Test</th><th>H</th><th>p</th>" +
      "<th>Result</th></tr></thead><tbody>" + statsRows + "</tbody></table>") : "<p class='muted'>(no statistics in this file)</p>";

    var domRows = dom.map(function (d) {
      return "<tr><td class='l'>" + _htmlEsc(d.model) + "</td><td>" + _htmlEsc(d.latency_dominant_agent) +
        "</td><td>" + _htmlEsc(d.vram_dominant_agent) + "</td></tr>";
    }).join("");
    var domTable = domRows ? ("<table><thead><tr><th>Model</th><th>Latency-dominant agent</th>" +
      "<th>VRAM-dominant agent</th></tr></thead><tbody>" + domRows + "</tbody></table>") : "";

    return "<!DOCTYPE html><html><head><meta charset='utf-8'><title>" + _htmlEsc(title || "AgentMeter report") +
      "</title><style>" +
      "body{font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1c2128;max-width:900px;margin:24px auto;padding:0 16px;}" +
      "h1{font-size:22px;margin:0 0 2px;} h2{font-size:16px;margin:24px 0 8px;border-bottom:1px solid #e2e6ec;padding-bottom:4px;}" +
      ".muted{color:#5b6675;} table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;margin:6px 0;}" +
      "th,td{border-bottom:1px solid #e2e6ec;padding:6px 8px;text-align:right;} th:first-child,td:first-child,.l{text-align:left;}" +
      "thead th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#5b6675;}" +
      "ul{margin:6px 0 0;padding-left:20px;} li{margin:3px 0;} .foot{color:#8a8f98;font-size:12px;margin-top:28px;}" +
      "@media print{body{margin:0;} a{display:none;}}" +
      "</style></head><body>" +
      "<h1>AgentMeter — " + _htmlEsc(title || "Resource-efficiency report") + "</h1>" +
      "<p class='muted'>Run(s): " + _htmlEsc((sm.run_ids || []).join(", ") || "—") +
      " · generated " + new Date().toISOString().replace("T", " ").slice(0, 19) + " UTC</p>" +
      "<h2>Key findings</h2><ul>" + (findings.length ? findings.map(function (f) { return "<li>" + f + "</li>"; }).join("") :
        "<li class='muted'>—</li>") + "</ul>" +
      "<h2>SAW ranking (resource-efficiency composite)</h2>" + sawTable +
      "<h2>Statistics</h2>" + statsTable +
      (domTable ? "<h2>Per-agent diagnostics (dominant agent per model)</h2>" + domTable : "") +
      "<p class='foot'>Resource-efficiency measurement (latency, VRAM, tokens) + SAW composite. " +
      "Read-only view over analysis.json. Not a detection-quality ranking.</p>" +
      "</body></html>";
  }

  /* ============================================================
   * Phase 32B — Decision support (READ-ONLY; resource efficiency +
   * model selection only). Pure functions over analysis.json; they
   * never recompute the locked numbers. Ranking comes from saw.js and
   * is passed in as `ranked` (an ordered [{model,...}] list); when not
   * supplied these fall back to the stored saw_table rank verbatim.
   * ============================================================ */

  // The four SAW criteria, their per-model source field, and direction.
  // dir "min" = user sets a floor (accuracy); "max" = user sets a ceiling.
  var DS_CHECKS = [
    { key: "accuracy_pct", dir: "min", label: "accuracy",
      get: function (r) { return r.accuracy_pct; } },
    { key: "latency_s", dir: "max", label: "latency",
      get: function (r) { return r.latency_s; } },
    { key: "vram_mb", dir: "max", label: "VRAM",
      get: function (r) { return vramOf(r); } },
    { key: "tokens_total", dir: "max", label: "tokens",
      get: function (r) { return r.tokens_total; } }
  ];

  // Ordered model list from the live ranking if given, else stored rank.
  function _rankedModels(data, ranked) {
    if (ranked && ranked.length) return ranked.map(function (r) { return r.model; });
    return _sawRows(data).slice()
      .sort(function (a, b) { return (a.rank == null ? 99 : a.rank) - (b.rank == null ? 99 : b.rank); })
      .map(function (r) { return r.model; });
  }

  // Plain-language deployment recommendation. Returns TEXT strings (render with
  // textContent — no HTML). The headline follows the live ranking/weights; the
  // caveat is the non-negotiable honesty note, grounded in the LOCKED tier +
  // accuracy target (weight-independent), never softened.
  var _DS_PRIO = { accuracy: "accuracy", latency: "speed (latency)",
                   vram: "memory (VRAM)", tokens: "token economy" };
  function recommendation(data, ranked, weights) {
    var p8 = (data && data.phase8) || {};
    var rows = _sawRows(data);
    if (!rows.length) return { headline: "", caveat: "", top: null, allCritical: false };
    var topModel = _rankedModels(data, ranked)[0] || null;
    var w = weights || p8.weights || {};
    var prios = Object.keys(_DS_PRIO)
      .filter(function (k) { return w[k] != null; })
      .sort(function (a, b) { return (+w[b] || 0) - (+w[a] || 0); })
      .slice(0, 2).map(function (k) { return _DS_PRIO[k]; });
    var priTxt = prios.length ? prios.join(", then ") : "the configured weights";
    var accTarget = (p8.targets && p8.targets.accuracy_pct != null) ? +p8.targets.accuracy_pct : 80;
    // LOCKED facts: every model sits in the Critical tier AND below the accuracy target.
    var allCritical = rows.every(function (r) { return (r.tier || "") === "Critical"; });
    var headline = topModel
      ? ("Based on your current priorities (" + priTxt + "), " + topModel +
         " is the most resource-efficient choice.")
      : "";
    var caveat;
    if (allCritical) {
      caveat = "All " + rows.length + " models fall in the Critical tier (accuracy below the " +
        accTarget + "% target), so none is recommended for zero-shot deployment as-is" +
        (topModel ? ("; " + topModel + " is the best resource-efficient starting point.") : ".");
    } else {
      caveat = "This is decision support, not a deployment sign-off — check each model's tier " +
        "and the accuracy target before deploying.";
    }
    return { headline: headline, caveat: caveat, top: topModel,
             allCritical: allCritical, accTarget: accTarget };
  }

  // What-if constraint filter. limits: {accuracy_pct, latency_s, vram_mb,
  // tokens_total} — any null/undefined/"" is ignored. Pure filter over measured
  // values; a requested dimension absent from the data degrades gracefully
  // (reported in `ignored`, never invented).
  function constraintFilter(data, limits) {
    limits = limits || {};
    var rows = _sawRows(data);
    function provided(c) { return limits[c.key] != null && limits[c.key] !== "" && !isNaN(+limits[c.key]); }
    function measurable(c) { return rows.some(function (r) { return c.get(r) != null; }); }
    var active = DS_CHECKS.filter(function (c) { return provided(c) && measurable(c); });
    var ignored = DS_CHECKS.filter(function (c) { return provided(c) && !measurable(c); })
      .map(function (c) { return c.key; });
    var results = rows.map(function (r) {
      var failed = [];
      active.forEach(function (c) {
        var v = c.get(r);
        if (v == null) return;                       // field absent on this row -> can't fail it
        var lim = +limits[c.key];
        if (c.dir === "min" ? (v < lim) : (v > lim)) failed.push(c.label);
      });
      return { model: r.model, pass: failed.length === 0, failed: failed,
               values: { accuracy_pct: r.accuracy_pct, latency_s: r.latency_s,
                         vram_mb: vramOf(r), tokens_total: r.tokens_total } };
    });
    return { results: results, anyPass: results.some(function (x) { return x.pass; }),
             activeCount: active.length, ignored: ignored };
  }

  // Resource-only optimisation guidance from per-agent dominant-cost data.
  // STRICTLY about resource cost: it never mentions accuracy, detection quality,
  // fine-tuning, prompts, or RAG, and makes no quality-improvement claim.
  function optimisationHint(data, model) {
    var dom = (data && data.per_agent && data.per_agent.dominant) || [];
    var row = (model && dom.filter(function (d) { return d.model === model; })[0]) || dom[0] || null;
    if (!row) return { text: "", latency_agent: null, vram_agent: null };
    var la = row.latency_dominant_agent || null, va = row.vram_dominant_agent || null;
    var where = [];
    if (la) where.push('the "' + la + '" step dominates latency');
    if (va) where.push('"' + va + '" dominates working memory');
    var text = "Resource cost concentrates in the pipeline" +
      (where.length ? ": " + where.join(" and ") : "") + ". " +
      "To cut resource cost, cap that step's token budget (lower its max_new_tokens) or run a " +
      "lighter model for that one step — this reduces latency, memory and token use only; " +
      "it does not change the model's verdicts.";
    return { text: text, latency_agent: la, vram_agent: va };
  }

  // One-page stakeholder decision brief (standalone HTML; print to PDF via the
  // existing print path). Values pulled verbatim from analysis.json; ranking from
  // the live `ranked` if given. No recomputation of locked numbers.
  function decisionBrief(data, ranked, weights, title) {
    var sm = sessionSummary(data);
    var rows = _sawRows(data);
    var order = _rankedModels(data, ranked);
    var byM = {}; rows.forEach(function (r) { byM[r.model] = r; });
    var rec = recommendation(data, ranked, weights);
    var top = rec.top;
    var hint = optimisationHint(data, top);
    var notes = (data && data.notes) || {};

    var rankRows = order.map(function (m, i) {
      var r = byM[m] || {};
      return "<tr><td>" + (i + 1) + "</td><td class='l'>" + _htmlEsc(m) + "</td>" +
        "<td>" + _num(r.accuracy_pct, 1) + "%</td>" +
        "<td>" + _num(r.latency_s, 2) + " s</td>" +
        "<td>" + (vramOf(r) == null ? "&mdash;" : _num(vramOf(r) / 1024, 1) + " GB") + "</td>" +
        "<td>" + (r.tokens_total == null ? "&mdash;" : Math.round(r.tokens_total)) + "</td>" +
        "<td>" + _num(r.composite, 3) + "</td><td>" + _htmlEsc(r.tier || "&mdash;") + "</td></tr>";
    }).join("");

    var findings = [];
    findings.push("Models evaluated: " + _htmlEsc(order.join(", ")) + ".");
    if (rec.allCritical) findings.push("Accuracy reality: every model is in the <b>Critical</b> tier, " +
      "below the " + rec.accTarget + "% accuracy target &mdash; <b>none is deployment-ready as-is</b>.");
    if (hint.latency_agent || hint.vram_agent) findings.push("Where cost concentrates: " +
      _htmlEsc(hint.text));
    if (notes.vram_finding) findings.push(_htmlEsc(notes.vram_finding));

    return "<!DOCTYPE html><html><head><meta charset='utf-8'><title>" +
      _htmlEsc(title || "AgentMeter decision brief") + "</title><style>" +
      "body{font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1c2128;max-width:900px;margin:24px auto;padding:0 16px;}" +
      "h1{font-size:22px;margin:0 0 2px;} h2{font-size:16px;margin:22px 0 8px;border-bottom:1px solid #e2e6ec;padding-bottom:4px;}" +
      ".muted{color:#5b6675;} .rec{background:#f3f6fb;border:1px solid #d9e1ec;border-radius:10px;padding:12px 14px;margin:10px 0;}" +
      ".rec .head{font-weight:700;} .caveat{color:#8a3b00;font-weight:600;margin-top:6px;}" +
      "table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;margin:6px 0;}" +
      "th,td{border-bottom:1px solid #e2e6ec;padding:6px 8px;text-align:right;} th:first-child,td:first-child,.l{text-align:left;}" +
      "thead th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#5b6675;}" +
      "ul{margin:6px 0 0;padding-left:20px;} li{margin:4px 0;} .foot{color:#8a8f98;font-size:12px;margin-top:26px;}" +
      "@media print{body{margin:0;} a{display:none;}}" +
      "</style></head><body>" +
      "<h1>AgentMeter &mdash; " + _htmlEsc(title || "Decision brief") + "</h1>" +
      "<p class='muted'>Run(s): " + _htmlEsc((sm.run_ids || []).join(", ") || "&mdash;") +
      " &middot; generated " + new Date().toISOString().replace("T", " ").slice(0, 19) + " UTC</p>" +
      "<h2>Recommendation</h2><div class='rec'><div class='head'>" + _htmlEsc(rec.headline) + "</div>" +
      "<div class='caveat'>" + _htmlEsc(rec.caveat) + "</div></div>" +
      "<h2>Ranking (resource-efficiency composite)</h2>" +
      "<table><thead><tr><th>#</th><th>Model</th><th>Accuracy</th><th>Latency</th><th>VRAM</th>" +
      "<th>Tokens</th><th>Composite</th><th>Tier</th></tr></thead><tbody>" + rankRows + "</tbody></table>" +
      "<h2>Key findings</h2><ul>" + findings.map(function (f) { return "<li>" + f + "</li>"; }).join("") + "</ul>" +
      "<p class='foot'>Resource-efficiency measurement (latency, VRAM, tokens) + SAW composite, " +
      "read-only over analysis.json. Decision support only &mdash; not a detection-quality ranking " +
      "and not a deployment sign-off.</p></body></html>";
  }

  var api = {
    compareSessions: compareSessions, sessionSummary: sessionSummary,
    metricValue: metricValue, vramOf: vramOf,
    sawCsv: sawCsv, perAgentCsv: perAgentCsv, perClassCsv: perClassCsv,
    comparisonCsv: comparisonCsv, reportHtml: reportHtml,
    recommendation: recommendation, constraintFilter: constraintFilter,
    optimisationHint: optimisationHint, decisionBrief: decisionBrief,
    DS_CHECKS: DS_CHECKS,
    HIGHER_BETTER: HIGHER_BETTER, CMP_METRICS: CMP_METRICS
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.REPORT = api;
})(typeof window !== "undefined" ? window : globalThis);
