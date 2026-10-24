// Compare, Leaderboard and the decision helper on per-session SUMMARIES — a port of
// agentmeter/server/sessions.py (validity_s / compare_s / leaderboard_s) and
// agentmeter/session/constraints.py (evaluate_facts). test/parity.test.ts checks these return
// exactly what the Python reference returns on the same fixtures. Nothing here computes a
// score, rank inside a session, SAW value or verdict: it reads the stored measurements.
import RULES from "./constraint_rules.json";

export type Summary = Record<string, any>;

export const METRICS: [string, string, string, boolean][] = [
  ["mean_latency_s", "Mean latency per flow", "s", true],
  ["median_latency_s", "Median latency per flow", "s", true],
  ["p95_latency_s", "p95 latency per flow", "s", true],
  ["p99_latency_s", "p99 latency per flow", "s", true],
  ["mean_peak_vram_mb", "Working VRAM, mean (excludes model weights)", "MB", true],
  ["max_peak_vram_mb", "Working VRAM, highest (excludes model weights)", "MB", true],
  ["total_peak_mb", "Total peak VRAM (weights + working)", "MB", true],
  ["weights_mb", "Model weights after load", "MB", true],
  ["mean_tokens_per_flow", "Tokens per flow", "tokens", true],
  ["mean_input_tokens_per_flow", "Input tokens per flow", "tokens", true],
  ["mean_output_tokens_per_flow", "Output tokens per flow", "tokens", true],
  ["handoff_share_of_total", "Agentic overhead (hand-off share)", "share", true],
  ["throughput_flows_per_s", "Throughput", "flows/s", false],
  ["cost_per_1k_flows_usd", "Cost per 1,000 flows", "USD", true],
  ["wh_per_flow", "Energy per flow", "Wh", true],
  ["wh_per_1k_flows", "Energy per 1,000 flows", "Wh", true],
  ["wh_per_flow_net_idle", "Energy per flow, net of idle", "Wh", true],
];
export const LEADERBOARD_SORTS: Record<string, string> = {
  saw: "saw_composite_100", efficiency: "saw_eff_composite_100", latency: "mean_latency_s", vram: "mean_peak_vram_mb", total_vram: "total_peak_mb",
  tokens: "mean_tokens_per_flow", cost: "cost_per_1k_flows_usd", energy: "wh_per_flow",
};

export const HIGHER_IS_BETTER = new Set(["saw_composite_100", "saw_eff_composite_100", "accuracy"]);
export const OVERALL_BY: Record<string, string> = { saw: "saw_composite_100", efficiency: "saw_eff_composite_100", accuracy: "accuracy" };

const nn = (v: unknown) => v !== null && v !== undefined;
/** Python json.dumps(v, sort_keys=True) equivalent for equality checks. */
export function canon(v: unknown): string {
  if (v === undefined || v === null) return "null";
  if (Array.isArray(v)) return "[" + v.map(canon).join(", ") + "]";
  if (typeof v === "object") return "{" + Object.keys(v as object).sort().map((k) => JSON.stringify(k) + ": " + canon((v as any)[k])).join(", ") + "}";
  return JSON.stringify(v);
}

export function validity(sa: Summary, sb: Summary) {
  const ia = sa.identity || {}, ib = sb.identity || {};
  const checks: any[] = [];
  const ha = ia.prepared_set_sha256, hb = ib.prepared_set_sha256;
  checks.push({ check: "prepared_set", label: "Same prepared set (flows hash)", same: !!(ha && ha === hb),
                a: (ha || "unknown").slice(0, 12), b: (hb || "unknown").slice(0, 12) });
  const ga = ia.gpu_label ?? null, gb = ib.gpu_label ?? null;
  checks.push({ check: "gpu", label: "Same GPU", same: !!(ga && ga === gb), a: ga, b: gb });
  const xa = ia.settings || {}, xb = ib.settings || {};
  const keys = [...new Set([...Object.keys(xa), ...Object.keys(xb)])];
  const diff = keys.filter((k) => canon(xa[k]) !== canon(xb[k])).sort();
  checks.push({ check: "settings", label: "Same settings (provider, quantisation, generation, agents, token caps, classes)",
                same: !diff.length && Object.keys(xa).length > 0, a: ia.settings_fingerprint ?? null, b: ib.settings_fingerprint ?? null, differs: diff });
  const ok = checks.every((c) => c.same);
  return { like_for_like: ok, label: ok ? "like-for-like" : "not like-for-like",
           differences: checks.filter((c) => !c.same).map((c) => c.label), checks };
}

export function compare(sa: Summary, sb: Summary) {
  const va = validity(sa, sb);
  const ma = sa.metrics || {}, mb = sb.metrics || {};
  const cols = [...Object.keys(ma).map((m) => ({ session: sa.session_id, side: "A", model: m })),
                ...Object.keys(mb).map((m) => ({ session: sb.session_id, side: "B", model: m }))];
  const rows: any[] = [];
  for (const [key, label, unit, lower] of METRICS) {
    const vals = cols.map((c) => ((c.side === "A" ? ma : mb)[c.model] || {})[key] ?? null);
    const present = vals.filter(nn) as number[];
    const best = present.length > 1 ? (lower ? Math.min(...present) : Math.max(...present)) : null;
    rows.push({ metric: key, label, unit, lower_is_better: lower, values: vals,
                best_index: vals.map((v, i) => (best !== null && v === best ? i : -1)).filter((i) => i >= 0) });
  }
  if (sa.labelled && sb.labelled)
    rows.push({ metric: "accuracy", label: "Accuracy (context only)", unit: "share", lower_is_better: false, context_only: true,
                values: cols.map((c) => ((c.side === "A" ? ma : mb)[c.model] || {}).accuracy ?? null), best_index: [] });
  const strip = (s: Summary) => Object.fromEntries(Object.entries(s).filter(([k]) => k !== "identity" && k !== "facts"));
  return { a: strip(sa), b: strip(sb), validity: va, columns: cols, rows,
           note: va.like_for_like ? "Like-for-like: same flows, same GPU, same settings — differences are the models'."
             : "NOT like-for-like: " + va.differences.join("; ") + " differ, so differences between the sessions are not only the models'. Read side by side, not as a ranking." };
}

function wmean(pairs: [number | null | undefined, number][]): number | null {
  let tot = 0, acc = 0;
  for (const [v, n] of pairs) if (nn(v) && n) { tot += n; acc += (v as number) * n; }
  return tot ? acc / tot : null;
}

export function leaderboard(summaries: Summary[], sort = "latency") {
  const key = LEADERBOARD_SORTS[sort] || LEADERBOARD_SORTS.latency;
  const higher = HIGHER_IS_BETTER.has(key);
  const groups = new Map<string, any>();
  for (const s of summaries) {
    const k = setupKey(s);
    if (k === null) continue;
    if (!groups.has(k)) groups.set(k, { ...setupOf(s), sessions: [], models: new Map<string, [string, any][]>() });
    const g = groups.get(k);
    g.sessions.push(s.session_id);
    for (const [m, row] of Object.entries(s.metrics || {})) {
      if (!g.models.has(m)) g.models.set(m, []);
      g.models.get(m).push([s.session_id, row]);
    }
  }
  const out: any[] = [];
  for (const g of groups.values()) {
    let rows: any[] = [];
    for (const [m, items] of g.models as Map<string, [string, any][]>) {
      const agg: Record<string, number | null> = {};
      for (const k2 of Object.values(LEADERBOARD_SORTS)) agg[k2] = wmean(items.map(([, r]) => [r[k2], r.n_flows || 0]));
      rows.push({ model: m, n_sessions: items.length, n_flows: items.reduce((s, [, r]) => s + (r.n_flows || 0), 0),
                  sessions: items.map(([sid]) => sid), ...agg });
    }
    const ranked = rows.filter((r) => nn(r[key])).sort((x, y) => (higher ? y[key] - x[key] : x[key] - y[key]));
    ranked.forEach((r, i) => (r.rank = i + 1));
    rows = [...ranked, ...rows.filter((r) => !nn(r[key])).map((r) => ({ ...r, rank: null }))];
    out.push({ prepared_set_sha256: g.prepared_set_sha256, gpu: g.gpu, settings_fingerprint: g.settings_fingerprint,
               input_type: g.input_type, labelled: g.labelled, demo: g.demo, n_flows_per_session: g.n_flows,
               n_sessions: g.sessions.length, sessions: g.sessions, rows });
  }
  out.sort((x, y) => (Number(x.demo) - Number(y.demo)) || (y.n_sessions - x.n_sessions));
  return { sort, sort_metric: key, higher_is_better: higher, sorts: LEADERBOARD_SORTS, groups: out,
           rule: "Models are ranked only against others on the same flows, GPU and settings. Values are flow-weighted means over that setup's sessions." };
}

const isDemo = (s: Summary) => !!s.demo || (s.identity || {}).provider === "mock";

export function leaderboardOverall(summaries: Summary[], by = "saw", includeDemo = false) {
  const key = OVERALL_BY[by] || OVERALL_BY.saw;
  const groups = new Map<string, Map<string, { sessions: string[]; pairs: [number | null, number][] }>>();
  for (const s of summaries) {
    const k = setupKey(s);
    if (k === null || (isDemo(s) && !includeDemo) || (key === "accuracy" && !s.labelled)) continue;
    if (!groups.has(k)) groups.set(k, new Map());
    const g = groups.get(k)!;
    for (const [m, row] of Object.entries((s.metrics || {}) as Record<string, any>)) {
      if (!g.has(m)) g.set(m, { sessions: [], pairs: [] });
      g.get(m)!.sessions.push(s.session_id);
      g.get(m)!.pairs.push([row[key] ?? null, row.n_flows || 0]);
    }
  }
  const stats = new Map<string, { model: string; setups: number; wins: number; beaten: number[]; sessions: string[] }>();
  let used = 0, single = 0;
  for (const g of groups.values()) {
    const vals = new Map<string, number>();
    for (const [m, x] of g) { const v = wmean(x.pairs); if (v !== null) vals.set(m, v); }
    if (vals.size < 2) { single++; continue; }
    used++;
    const all = [...vals.values()];
    for (const [m, v] of vals) {
      const better = all.filter((o) => o > v).length, worse = all.filter((o) => o < v).length;
      if (!stats.has(m)) stats.set(m, { model: m, setups: 0, wins: 0, beaten: [], sessions: [] });
      const st = stats.get(m)!;
      st.setups++;
      st.wins += better === 0 ? 1 : 0;
      st.beaten.push(worse / (vals.size - 1));
      for (const x of g.get(m)!.sessions) if (!st.sessions.includes(x)) st.sessions.push(x);
    }
  }
  const rows: any[] = [...stats.values()].map((st) => {
    let acc = 0;
    for (const b of st.beaten) acc += b;
    return { model: st.model, wins: st.wins, setups: st.setups, win_rate: st.wins / st.setups, rivals_beaten: acc / st.beaten.length,
             n_sessions: st.sessions.length, few_data: st.setups < 2 };
  });
  rows.sort((x, y) => (y.wins - x.wins) || (y.win_rate - x.win_rate) || (y.rivals_beaten - x.rivals_beaten) || (x.model < y.model ? -1 : x.model > y.model ? 1 : 0));
  rows.forEach((r, i) => (r.rank = i + 1));
  return { by, metric: key, bys: OVERALL_BY, include_demo: includeDemo, n_setups: used, n_setups_single_model: single, rows,
           rule: "Counted inside each setup (same flows, GPU and settings) with 2+ models: a win is 1st place there. Rivals beaten = the share of the other models it placed above, averaged over its setups." };
}

// --- decision helper ---------------------------------------------------------------------
/** Python format(value, spec) for the specs the rule notes use: [,][.N](f|g) and "g". */
export function pyFormat(v: number, spec: string): string {
  const m = /^(,)?(?:\.(\d+))?([fg])?$/.exec(spec);
  if (!m) return String(v);
  const comma = !!m[1], type = m[3] || "g";
  let prec = m[2] !== undefined ? parseInt(m[2], 10) : 6;
  let s: string;
  if (type === "f") s = v.toFixed(prec);
  else {
    if (prec === 0) prec = 1;
    if (v === 0) s = "0";
    else {
      const exp = Math.floor(Math.log10(Math.abs(Number(v.toPrecision(prec)))));
      if (exp < -4 || exp >= prec) {
        let [mant, e] = v.toExponential(prec - 1).split("e");
        if (mant.includes(".")) mant = mant.replace(/0+$/, "").replace(/\.$/, "");
        const en = parseInt(e, 10);
        s = `${mant}e${en < 0 ? "-" : "+"}${String(Math.abs(en)).padStart(2, "0")}`;
      } else {
        s = v.toFixed(Math.max(0, prec - 1 - exp));
        if (s.includes(".")) s = s.replace(/0+$/, "").replace(/\.$/, "");
      }
    }
  }
  if (comma) {
    const [i, d] = s.split(".");
    const neg = i.startsWith("-");
    const digits = (neg ? i.slice(1) : i).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    s = (neg ? "-" : "") + digits + (d !== undefined ? "." + d : "");
  }
  return s;
}

const pyStr = (v: unknown) => (typeof v === "number" ? (Number.isInteger(v) ? v.toFixed(1) : String(v)) : String(v));
function render(tpl: string, value: unknown, limit: unknown): string {
  return tpl.replace(/\{(value|limit)(?::([^{}]+))?\}/g, (_m, which, spec) => {
    const v = which === "value" ? value : limit;
    if (v === null || v === undefined) return "unknown";
    if (spec && typeof v === "number") return pyFormat(v, spec);
    return pyStr(v);
  });
}

export class ConstraintError extends Error {}
const NUM = /^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*$/;
const OPS: Record<string, (a: number, b: number) => boolean> = { le: (a, b) => a <= b, lt: (a, b) => a < b, ge: (a, b) => a >= b, gt: (a, b) => a > b };

export function parseLimits(raw: Record<string, unknown>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const key of Object.keys(RULES.inputs)) {
    const v = raw[key];
    if (v === null || v === undefined || String(v).trim() === "") continue;
    if (typeof v !== "number" && !NUM.test(String(v))) throw new ConstraintError(`${key} must be a number (got '${v}')`);
    const f = Number(v);
    if (!Number.isFinite(f) || f < 0) throw new ConstraintError(`${key} must be a finite number >= 0 (got '${v}')`);
    out[key] = f;
  }
  return out;
}

export function evaluateFacts(facts: Record<string, any>[], limits?: Record<string, unknown>) {
  const rb = RULES as any;
  const raw = limits ?? Object.fromEntries(Object.entries(rb.inputs).filter(([, v]: any) => v.default !== null).map(([k, v]: any) => [k, v.default]));
  const lim = parseLimits(raw);
  const per = facts.map((f) => {
    const rows: any[] = [], failed: string[] = [], nodata: string[] = [];
    for (const r of rb.rules) {
      const limit = lim[r.limit] ?? null;
      const have = f[r.field] ?? null;
      let res: string, note: string | null;
      if (limit === null) { res = "not_set"; note = null; }
      else if (have === null) { res = "no_data"; note = `no measurement for ${r.field}`; nodata.push(r.id); }
      else {
        const ok = OPS[r.op](Number(have), Number(limit));
        res = ok ? "meets" : "fails";
        note = render(r.note || "", have, limit);
        if (!ok) failed.push(r.id);
      }
      rows.push({ rule: r.id, description: r.description || "", field: r.field, op: r.op, input: r.limit, limit, value: have, result: res, note });
    }
    const setRows = rows.filter((x) => x.result !== "not_set");
    const overall = !setRows.length ? "no_constraints" : failed.length ? "fails" : nodata.length ? "no_data" : "meets";
    return { model: f.model, result: overall, failed, no_data: nodata, rows };
  });
  return {
    stage: "2 (fit scoring)", rulebase: rb.source, version: rb.version,
    inputs: Object.fromEntries(Object.entries(rb.inputs).map(([k, v]: any) => [k, { ...v, value: lim[k] ?? null }])),
    limits_set: Object.keys(lim).length > 0,
    framing: "Fit check of the measured numbers against your limits. It never changes a score, rank, SAW value or the verdict.",
    per_model: per,
  };
}

// --- merged compare: every session on the SAME SETUP (port of sessions.py comparable_s / compare_many_s) ---
export const MERGED_EXTRA: [string, string, string, boolean][] = [["saw_composite_100", "SAW composite (stored, fixed targets)", "score", false],
  ["saw_eff_composite_100", "SAW composite, efficiency only (stored)", "score", false]];

export function setupKey(s: Summary): string | null {
  const i = s.identity || {};
  const k = [i.prepared_set_sha256, i.gpu_label, i.settings_fingerprint];
  return k.every((x) => x) ? JSON.stringify(k) : null;
}
function setupOf(s: Summary) {
  const i = s.identity || {};
  return { prepared_set_sha256: i.prepared_set_sha256 ?? null, gpu: i.gpu_label ?? null, settings_fingerprint: i.settings_fingerprint ?? null,
           input_type: s.input_type ?? null, n_flows: s.n_flows ?? null, labelled: !!s.labelled, demo: i.provider === "mock" };
}
const BRIEF = ["session_id", "models", "created_at", "finished_at", "owner_username", "n_flows", "status", "run_name", "input_type"];
const brief = (s: Summary) => Object.fromEntries(BRIEF.map((k) => [k, s[k] ?? null]));

export function comparable(target: Summary, summaries: Summary[]) {
  const key = setupKey(target);
  const same = summaries.filter((s) => key && s.session_id !== target.session_id && setupKey(s) === key);
  return { session: brief(target), setup: key ? setupOf(target) : null, sessions: same.map(brief),
           rule: "Comparable = the same flows (prepared-set hash), the same GPU and the same settings." };
}

export function compareMany(summaries: Summary[]) {
  const keys = summaries.map(setupKey);
  if (summaries.length < 2 || keys[0] === null || keys.some((k) => k !== keys[0]))
    return { error: "not_same_setup", differs: summaries.filter((s, i) => keys[i] === null || keys[i] !== keys[0]).map((s) => s.session_id) };
  const labelled = summaries.every((s) => s.labelled);
  const runs: any[] = [];
  for (const s of summaries)
    for (const [m, row] of Object.entries((s.metrics || {}) as Record<string, any>))
      runs.push({ session: s.session_id, model: m, created_at: s.created_at ?? null, owner: s.owner_username ?? null, n_flows: row.n_flows ?? null, metrics: row });
  const defs = [...METRICS, ...MERGED_EXTRA];
  const rows: any[] = [];
  for (const [key, label, unit, lower] of defs) {
    const vals = runs.map((r) => r.metrics[key] ?? null);
    const present = vals.filter(nn) as number[];
    const best = present.length > 1 ? (lower ? Math.min(...present) : Math.max(...present)) : null;
    rows.push({ metric: key, label, unit, lower_is_better: lower, values: vals,
                best_index: vals.map((v, i) => (best !== null && v === best ? i : -1)).filter((i) => i >= 0) });
  }
  if (labelled)
    rows.push({ metric: "accuracy", label: "Accuracy (context only)", unit: "share", lower_is_better: false, context_only: true,
                values: runs.map((r) => r.metrics.accuracy ?? null), best_index: [] });
  const byModel = new Map<string, { model: string; runs: string[]; pairs: Record<string, [number | null, number][]> }>();
  for (const r of runs) {
    if (!byModel.has(r.model)) byModel.set(r.model, { model: r.model, runs: [], pairs: {} });
    const b = byModel.get(r.model)!;
    b.runs.push(r.session);
    for (const [key] of [...defs, ["accuracy"]]) (b.pairs[key as string] ||= []).push([r.metrics[key as string] ?? null, r.n_flows || 0]);
  }
  const models = [...byModel.values()].map((b) => ({ model: b.model, n_runs: b.runs.length, sessions: b.runs,
    values: Object.fromEntries(Object.entries(b.pairs).map(([k, v]) => [k, wmean(v)])) }));
  return { setup: setupOf(summaries[0]), sessions: summaries.map((s) => s.session_id),
           runs: runs.map(({ metrics, ...r }) => r), rows, by_model: models, labelled,
           note: "Same flows, same GPU, same settings: differences are the models'. Values are the stored ones; the best per row is marked. A model in several sessions is shown per run, or as its flow-weighted mean (a view only — stored scores never change)." };
}
