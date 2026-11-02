// "Runs that moved", server side: when a session finishes, its results are checked against the
// earlier runs of the same model on the same setup (same prepared set, GPU and settings) with
// the rule the Repeatability page uses (web/insights.js drift(); tests/parity keeps the two equal), so
// the owner can be alerted on Telegram when a model got worse. Reads stored summaries only.

export const TREND_METRICS: [string, string, string, boolean][] = [
  ["mean_latency_s", "Mean latency per flow", "s", true], ["p95_latency_s", "p95 latency per flow", "s", true],
  ["mean_tokens_per_flow", "Tokens per flow", "tokens", true], ["max_peak_vram_mb", "Peak working VRAM", "MB", true],
  ["wh_per_flow", "Energy per flow", "Wh", true], ["cost_per_1k_flows_usd", "Cost per 1,000 flows", "USD", true],
  ["valid_rate", "Usable answers", "share", false], ["saw_eff_composite_100", "SAW score (efficiency)", "points", false]];
export const DRIFT_MIN = 0.10;                       // a change under 10% is never flagged

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
function median(vs: number[]): number | null {
  const a = [...vs].sort((x, y) => x - y), n = a.length;
  return n ? (n % 2 ? a[(n - 1) / 2] : (a[n / 2 - 1] + a[n / 2]) / 2) : null;
}

export interface Drift { n: number; enough: boolean; baseline?: number; latest?: number; change?: number; noise?: number; band?: number; flagged?: boolean; worse?: boolean }

/** The latest value against the median of the earlier ones (web/insights.js drift()). */
export function drift(values: (number | null)[], lowerBetter: boolean): Drift {
  const vs = values.filter((v): v is number => v != null);
  if (vs.length < 3) return { n: vs.length, enough: false };
  const before = vs.slice(0, -1), latest = vs[vs.length - 1], base = median(before);
  if (!base) return { n: vs.length, enough: false };
  const mad = median(before.map((v) => Math.abs(v - base)))! * 1.4826;
  const change = (latest - base) / Math.abs(base), noise = mad / Math.abs(base), band = Math.max(DRIFT_MIN, 3 * noise);
  return { n: vs.length, enough: true, baseline: base, latest, change, noise, band,
           flagged: Math.abs(change) > band, worse: lowerBetter ? change > 0 : change < 0 };
}

export function setupKey(s: any): string | null {
  const i = s.identity || {};
  const set = i.prepared_set_sha256 || s.prepared_set_sha256, gpu = i.gpu_label || s.gpu, fp = i.settings_fingerprint || s.settings_fingerprint;
  return set && gpu && fp ? [set, gpu, fp].join("|") : null;
}

export interface Moved { model: string; metric: string; label: string; unit: string; baseline: number; latest: number; change: number; band: number; n: number; worse: boolean }

/** The models of `current` (the latest run) whose results moved beyond their usual spread,
 *  against `earlier` sessions (any order) on the same setup. Worse first, then by size. */
export function movedRuns(current: any, earlier: any[]): Moved[] {
  const key = setupKey(current);
  if (!key || !current.metrics) return [];
  const at = (s: any) => String(s.finished_at || s.created_at || "");
  const same = earlier.filter((s) => s.session_id !== current.session_id && s.metrics && setupKey(s) === key && at(s) <= at(current))
    .sort((a, b) => at(a).localeCompare(at(b)));
  const out: Moved[] = [];
  for (const model of Object.keys(current.metrics)) {
    const runs = [...same.filter((s) => s.metrics[model]).map((s) => s.metrics[model]), current.metrics[model]];
    for (const [k, label, unit, lowerBetter] of TREND_METRICS) {
      const d = drift(runs.map((m: any) => num(m?.[k])), lowerBetter);
      if (d.enough && d.flagged) out.push({ model, metric: k, label, unit, baseline: d.baseline!, latest: d.latest!, change: d.change!, band: d.band!, n: d.n, worse: !!d.worse });
    }
  }
  return out.sort((a, b) => Number(b.worse) - Number(a.worse) || Math.abs(b.change) - Math.abs(a.change));
}

const fmt = (unit: string, v: number) =>
  unit === "s" ? (v >= 1 ? `${v.toFixed(2)} s` : `${(v * 1000).toFixed(0)} ms`) : unit === "share" ? `${(v * 100).toFixed(0)}%`
    : unit === "USD" ? `$${v.toFixed(3)}` : unit === "MB" ? `${Math.round(v)} MB` : unit === "Wh" ? `${v.toFixed(3)} Wh` : `${Math.round(v * 10) / 10} ${unit}`;

/** Telegram lines for the moved results (at most `max`). */
export function movedLines(moved: Moved[], max = 6): string[] {
  const short = (m: string) => m.split("/").pop() || m;
  return moved.slice(0, max).map((x) =>
    `${x.worse ? "⚠ Worse" : "✓ Better"}: ${short(x.model)} ${x.label.toLowerCase()} ${fmt(x.unit, x.baseline)} → ${fmt(x.unit, x.latest)} ` +
    `(${x.change > 0 ? "+" : ""}${(x.change * 100).toFixed(0)}% vs the median of ${x.n - 1} earlier runs; usual range ±${(x.band * 100).toFixed(0)}%)`);
}
