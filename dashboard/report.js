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

  var api = {
    compareSessions: compareSessions, sessionSummary: sessionSummary,
    metricValue: metricValue, vramOf: vramOf,
    sawCsv: sawCsv, perAgentCsv: perAgentCsv, perClassCsv: perClassCsv,
    comparisonCsv: comparisonCsv, reportHtml: reportHtml,
    HIGHER_BETTER: HIGHER_BETTER, CMP_METRICS: CMP_METRICS
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.REPORT = api;
})(typeof window !== "undefined" ? window : globalThis);
