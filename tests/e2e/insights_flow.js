// Session insights — walk-through of the added analysis views of a session (Playwright,
// mock backend): one labelled 2-model CSV session and one unlabelled 1-model PCAP session.
// The arithmetic is unit-tested in tests/test_insights.py; this checks the views render,
// react to their controls, stay honest on missing data, and fit a phone screen.
//   AGENTMETER_RESULTS_DIR=$(mktemp -d) python scripts/serve.py --provider mock --port 8766
//   node tests/e2e/insights_flow.js [base_url] [out_dir]
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const ORIGIN = process.argv[2] || 'http://127.0.0.1:8766';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'insights-e2e-'));
fs.mkdirSync(S, { recursive: true });
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

async function session(p, file, flows, idx) {
  await p.goto(ORIGIN + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
  await p.setInputFiles('#svc-file', file); await p.fill('#svc-max-flows', String(flows));
  await p.click('#svc-upload-btn');
  await p.waitForFunction(() => /Ready/.test(document.querySelector('#wiz-data-chip').textContent), null, { timeout: 90000 });
  for (const i of idx) await p.locator('#svc-models input').nth(i).check();   // re-resolved: the list re-renders as the catalog loads
  await p.click('#svc-run');
  await p.waitForFunction(() => /^#\/session\//.test(location.hash), null, { timeout: 180000 });
  await p.waitForSelector('#session-body:not([hidden])', { timeout: 30000 });
  return decodeURIComponent(p.url().split('#/session/')[1].split('/')[0]);
}
async function tab(p, id, t, sel) {
  await p.goto(ORIGIN + '/#/session/' + encodeURIComponent(id) + '/' + t);
  await p.waitForSelector(sel, { timeout: 30000 });
}
async function hoverText(p, sel) {        // scroll first: a scroll closes the popover, as it should
  await p.locator(sel).first().scrollIntoViewIfNeeded(); await p.waitForTimeout(150);
  await p.locator(sel).first().hover(); await p.waitForSelector('.tip-pop', { timeout: 3000 });
  return p.textContent('.tip-pop');
}
const texts = (p, sel) => p.$$eval(sel, els => els.map(e => e.textContent.trim()));

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const ctx = await b.newContext({ viewport: { width: 1280, height: 900 } });
  const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  p.on('console', m => { if (m.type() === 'error' && !/fonts|ERR_|Failed to load resource/.test(m.text())) errs.push(m.text()); });
  try {
    const csv = await session(p, REPO + '/data/sample_csv/cicids2017_sample.csv', 40, [0, 3]);
    const pcap = await session(p, REPO + '/data/sample_pcaps/sample_small.pcap', 8, [2]);

    // Phase 1 — Overview: key findings + criteria profile
    await tab(p, csv, 'overview', '#svc-findings');
    const f = await p.$$eval('#svc-findings li', ls => ls.map(l => l.getAttribute('data-finding')));
    log(['gap', 'tail', 'agent', 'overhead', 'cost', 'accuracy'].every(k => f.includes(k)), 'overview: key findings cover gap, tail, agent, overhead, cost, accuracy (' + f.join(',') + ')');
    log((await p.$$('#svc-profile .prof-row')).length === 4 && (await p.$$('#svc-profile .prof-row[data-crit="latency"] .ibar')).length === 2,
        'overview: criteria profile has 4 criteria x 2 models');
    log(/no readings/.test(await p.textContent('#svc-profile .prof-row[data-crit="vram"]')), 'overview: VRAM without readings is labelled, not drawn as a score');
    log(/Latency: 1\.00 of 1\.00/.test(await hoverText(p, '#svc-profile .prof-row[data-crit="latency"] .ibar')), 'overview: hovering a profile bar shows its value');
    log((await p.$$('#svc-profile-table tbody tr')).length === 2, 'overview: profile has a table view');
    await tab(p, pcap, 'overview', '#svc-findings');
    log(!(await texts(p, '#svc-findings li')).some(t => /Accuracy/.test(t)) && /no labels/.test(await p.textContent('#svc-profile .prof-row[data-crit="accuracy"]')),
        'overview (PCAP): no accuracy finding; profile says "no labels"');

    // Phase 2 — Ranking -> Robustness: weight range in which #1 stays first
    await tab(p, csv, 'ranking/robustness', '#svc-stability');
    log((await p.$$('#svc-stability .stab-row')).length === 4 && (await p.$$('#svc-stability .stab-mark')).length === 4,
        'robustness: one strip per criterion, each with the stored-weight marker');
    const stab = await texts(p, '#svc-stability .stab-text');
    log(/no readings/i.test(stab[2]) && stab.every(t => /tie|stays first|no readings/i.test(t)), 'robustness: every strip says who stays first, a tie, or no readings');
    log((await p.$$('#svc-stability-table tbody tr')).length === 4, 'robustness: table view');
    await tab(p, pcap, 'ranking/robustness', '#svc-stability');
    log(/Single-model run/.test(await p.textContent('#svc-stability')), 'robustness (1 model): says there is no ranking to change');

    // Phase 3 — Accuracy: precision / recall / F1 per class + mix-ups
    await tab(p, csv, 'accuracy', '#svc-class-metrics');
    log((await p.$$('#svc-class-metrics .dot-row')).length === 6 && (await p.$$('#svc-class-metrics .dot-row:first-child .dot')).length === 2,
        'accuracy: F1 per class (5 classes + macro) with one dot per model');
    log(/F1 \d\.\d\d \(precision/.test(await hoverText(p, '#svc-class-metrics .dot')), 'accuracy: hovering a dot shows F1, precision and recall');
    log((await p.$$('#svc-class-metrics-table tbody tr')).length === 6, 'accuracy: table view with macro average');
    log((await p.$$('#svc-mixups .mix')).length === 2 && /answered as/.test(await p.textContent('#svc-mixups')) && /Unparseable answers: \d+ of 40/.test(await p.textContent('#svc-mixups')),
        'accuracy: most common mix-ups and unparseable answers per model');

    // Phase 4 — Performance: latency histogram small multiples + effect-size charts
    await tab(p, csv, 'performance/latency', '#stats-histogram');
    log((await p.$$('#stats-histogram .histo-panel')).length === 2 && (await p.$$('#stats-histogram .histo-panel:first-child .pm')).length === 3,
        'latency: one histogram per model with p50/p95/p99 lines');
    log(/: \d+ flows?$/.test(await hoverText(p, '#stats-histogram .hb')), 'latency: hovering a bin shows its range and flow count');
    await tab(p, csv, 'performance/statistics', '#stats-effect-chart');
    log((await p.$$('#stats-effect-chart .ef-row')).length === 5 && /no VRAM readings/.test(await p.textContent('#stats-effect-chart')),
        "statistics: Cliff's delta for 3 metrics (VRAM unavailable, said so) + a CI strip for the 2 measured");
    log(/95% CI/.test(await hoverText(p, '#stats-effect-chart .ci ~ .dot')), 'statistics: hovering a CI dot shows the interval');
    await tab(p, pcap, 'performance/statistics', '#stats-effects-card');
    log(!(await p.$('#stats-effect-chart')), 'statistics (1 model): no effect chart');

    // every view at phone width: no horizontal page scroll
    await p.setViewportSize({ width: 375, height: 800 });
    let maxW = 0;
    for (const t of ['overview', 'ranking/robustness', 'accuracy', 'performance/latency', 'performance/statistics']) {
      await tab(p, csv, t, '#session-body:not([hidden])'); await p.waitForTimeout(300);
      maxW = Math.max(maxW, await p.evaluate(() => document.documentElement.scrollWidth));
    }
    log(maxW <= 375, 'no horizontal page scroll at 375px (max scrollWidth ' + maxW + ')');
  } catch (e) {
    log(false, 'walk-through crashed: ' + e.message.split('\n')[0]);
  }
  log(errs.length === 0, 'no page errors ' + errs.join(' | '));
  await b.close();
  console.log(out.join('\n'));
  const failed = out.filter(l => l.startsWith('FAIL')).length;
  console.log('\n' + (out.length - failed) + ' passed, ' + failed + ' failed  (' + S + ')');
  process.exit(failed ? 1 : 0);
})();
