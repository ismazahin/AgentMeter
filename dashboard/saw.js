/* AgentMeter SAW math — the SAME weighted-sum formula as agentmeter/analyze.py.
 *
 * composite(norm, weights) = Σ_k weights_k * norm_k  over {accuracy, latency, vram, tokens}
 * (weights are normalised to sum to 1 first, so the sliders always yield a valid
 *  convex combination). Tier from composite*100 vs the config thresholds. Ranking
 * is composite descending. This mirrors analyze._composite / _tier exactly.
 *
 * Works in the browser (window.SAW) and under node (module.exports) for testing.
 */
(function (global) {
  var CRIT = ["accuracy", "latency", "vram", "tokens"];

  function normalizeWeights(w) {
    var s = 0;
    CRIT.forEach(function (k) { s += (+w[k] || 0); });
    var out = {};
    CRIT.forEach(function (k) { out[k] = s ? (+w[k] || 0) / s : 0; });
    return out;
  }

  function composite(norm, weights) {
    var w = normalizeWeights(weights);
    var c = 0;
    CRIT.forEach(function (k) { c += (+norm[k] || 0) * w[k]; });
    return c;
  }

  function tierOf(score100, tiers) {
    if (score100 >= tiers.healthy_min) return "Healthy";
    if (score100 >= tiers.degraded_min) return "Degraded";
    return "Critical";
  }

  function rankModels(normalised, weights, tiers) {
    var rows = normalised.map(function (n) {
      return { model: n.model, composite: composite(n, weights) };
    });
    rows.sort(function (a, b) { return b.composite - a.composite; });
    rows.forEach(function (r, i) {
      r.rank = i + 1;
      r.tier = tierOf(r.composite * 100.0, tiers);
    });
    return rows;
  }

  var api = { CRIT: CRIT, normalizeWeights: normalizeWeights, composite: composite,
              tierOf: tierOf, rankModels: rankModels };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.SAW = api;
})(typeof window !== "undefined" ? window : globalThis);
