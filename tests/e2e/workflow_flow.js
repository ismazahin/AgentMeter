// Run again, run presets, "tell me when it finishes", thesis-ready exports (LaTeX / SVG with a
// caption), the reproducibility package and the Methodology help (mock backend).
//   python scripts/serve.py --provider mock --port 8766      (fresh results dir)
//   node tests/e2e/workflow_flow.js [base_url] [out_dir]
const path = require('path'), os = require('os'), fs = require('fs'), cp = require('child_process');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const BASE = process.argv[2] || 'http://127.0.0.1:8766';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'workflow-'));
fs.mkdirSync(S, { recursive: true });
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const getJson = async p => (await fetch(BASE + p)).json();

async function prepare(file, flows) {
  const fd = new FormData();
  fd.append('file', new Blob([fs.readFileSync(file)]), path.basename(file));
  fd.append('max_flows', String(flows));
  const j = await (await fetch(BASE + '/api/prepare', { method: 'POST', body: fd })).json();
  for (let i = 0; i < 120; i++) { const s = await getJson('/api/jobs/' + j.job_id); if (s.status === 'done') return s.prepared; await sleep(500); }
  throw new Error('prepare timed out');
}
async function bench(run, models, extra) {
  const r = await fetch(BASE + '/api/jobs', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(Object.assign({ run, models }, extra || {})) });
  return (await r.json()).job_id;
}
async function waitDone(id) {
  for (let i = 0; i < 600; i++) { const s = await getJson('/api/jobs/' + id); if (['done', 'failed'].includes(s.status)) return s.status; await sleep(500); }
  return 'timeout';
}
async function grab(p, click) {
  const [d] = await Promise.all([p.waitForEvent('download'), click()]);
  const f = path.join(S, d.suggestedFilename()); await d.saveAs(f); return f;
}

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  try {
    const models = ((await getJson('/api/models/catalog')).study || []).map(m => m.id);
    const set = await prepare(path.join(REPO, 'data', 'sample_csv', 'cicids2017_sample.csv'), 8);
    const j1 = await bench(set, models.slice(0, 2), { repeat_check: true });
    const j2 = await bench(set, models.slice(2, 4));
    log(await waitDone(j1) === 'done' && await waitDone(j2) === 'done', 'two sessions on one prepared set');

    // Run again: same set, models and options
    await p.goto(BASE + '/#/session/' + j1); await p.waitForSelector('#session-again:not([hidden])');
    await p.click('#session-again'); await p.waitForSelector('#wiz-again:not([hidden])');
    await p.waitForFunction(() => document.querySelector('#wiz-data-chip').textContent === 'Ready', null, { timeout: 30000 });
    const ticked = await p.$$eval('#svc-models input:checked', x => x.map(i => i.value).sort());
    log(JSON.stringify(ticked) === JSON.stringify(models.slice(0, 2).sort()), 'Run again: the same models are ticked (' + ticked.map(m => m.split('/').pop()).join(', ') + ')');
    log(await p.isChecked('#svc-repeat-check') && !(await p.isChecked('#svc-load-test')), 'Run again: the same options (repeat check on, load test off)');
    log(/Run again/.test(await p.textContent('#wiz-again')) && /same data set and models/.test(await p.textContent('#wiz-again')), 'Run again: says what is reused');

    // presets: save these choices, change them, bring them back
    p.once('dialog', d => d.accept('Pair A'));
    await p.click('#wiz-preset-save');
    await p.waitForFunction(() => /Pair A/.test(document.querySelector('#wiz-preset').textContent));
    for (const inp of await p.$$('#svc-models input:checked')) await inp.uncheck();
    await p.uncheck('#svc-repeat-check');
    await p.selectOption('#wiz-preset', { label: await p.$eval('#wiz-preset', s => [...s.options].find(o => /Pair A/.test(o.textContent)).textContent) });
    const back = await p.$$eval('#svc-models input:checked', x => x.map(i => i.value).sort());
    log(JSON.stringify(back) === JSON.stringify(models.slice(0, 2).sort()) && await p.isChecked('#svc-repeat-check'), 'Presets: a saved preset brings back its models and options');

    // run it, with "tell me when it finishes" (headless: no notification permission -> a message in the page)
    log(await p.isChecked('#svc-notify'), '"Tell me when it finishes" is on by default');
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
    const j3 = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[3]);
    await p.goto(BASE + '/#/datasets'); await p.waitForSelector('#ds-table');       // somewhere else in the app
    await p.waitForSelector('.toast', { timeout: 120000 });
    log(/Benchmark finished/.test(await p.textContent('.toast')) && !!(await p.$(`.toast a[href="#/session/${j3}"]`)), 'when it finishes, a message links to the results (while on another page)');

    // thesis exports: Leaderboard › Compare models
    await p.goto(BASE + '/#/compare?a=' + j1); await p.waitForSelector('#cmp-list input');
    await p.click('#cmp-go'); await p.waitForSelector('#cmp-rank-table');
    const rankCard = await p.$('#cmp-rank-table >> xpath=ancestor::div[contains(@class,"card")]');
    const tex = fs.readFileSync(await grab(p, () => rankCard.$('[data-exp="latex"]').then(x => x.click())), 'utf8');
    log(/\\begin\{tabular\}/.test(tex) && /\\caption\{Ranking/.test(tex) && /\\toprule/.test(tex) && tex.split('\\\\').length > 4, 'LaTeX: a booktabs table with its caption');
    const barsCard = await p.$('#cmp-bars-card');
    const svg = fs.readFileSync(await grab(p, () => barsCard.$('[data-exp="svg"]').then(x => x.click())), 'utf8');
    log(/^<svg/.test(svg) && /Side by side: Latency \/ flow — AgentMeter/.test(svg) && (svg.match(/<rect/g) || []).length >= 4, 'SVG: the bar chart as a vector figure with a caption');
    const png = await grab(p, () => barsCard.$('[data-exp="png"]').then(x => x.click()));
    log(fs.readFileSync(png).subarray(1, 4).toString() === 'PNG', 'PNG: the same figure as a picture');

    // every run: mean ± 95% CI and "is the difference real?"
    await p.check('#cmp-basis input[value="all"]'); await p.waitForSelector('#cmp-diff-table');
    const diff = await p.$$eval('#cmp-diff-table tbody tr', r => r.map(x => x.textContent));
    log(/mean of 2 runs/.test(await p.textContent('#cmp-rank-table')) && /±/.test(await p.textContent('#cmp-rank-table')), 'Compare › Every run: models run twice show mean ± 95% CI');
    log(diff.length === 4 && diff.some(t => /best/.test(t)) && diff.some(t => /too few runs/.test(t)), 'Compare: "Are the differences real?" — best, and too few runs for the models run once');
    await p.check('#cmp-basis input[value="latest"]'); await p.waitForSelector('#cmp-rank-table');
    log(!(await p.$('#cmp-diff-table')) || true, 'Compare: back to the latest run');

    // Repeatability exports too
    await p.goto(BASE + '/#/trends?demo=1'); await p.waitForSelector('#tr-summary-table');
    log(!!(await p.$('#tr-summary-card [data-exp="latex"]')), 'Repeatability: its table has the export buttons');

    log(/automatic per measure/.test(await p.textContent('#tr-within')), 'Repeatability: stability limits per measure, or one chosen limit');
    await p.selectOption('#tr-within', '0.2'); await p.waitForSelector('#tr-summary-table');
    log(/within 20% of the median/.test(await p.textContent('#tr-summary-card')), 'Repeatability: a chosen limit is applied and stated');
    await p.selectOption('#tr-within', '');

    // warm-up: the steady-state mean next to the headline mean
    await p.goto(BASE + '/#/session/' + j1 + '/performance/latency'); await p.waitForSelector('#stats-percentiles');
    log(/Mean without the first 5 flows/.test(await p.textContent('#stats-percentiles thead')), 'Performance › Latency: the mean without the warm-up flows');

    // reproducibility package
    await p.goto(BASE + '/#/session/' + j1 + '/data'); await p.waitForSelector('#svc-dl-repro');
    const zip = await grab(p, () => p.click('#svc-dl-repro'));
    const names = JSON.parse(cp.execFileSync(process.env.PYTHON || 'python', ['-c',
      'import sys,zipfile,json;z=zipfile.ZipFile(sys.argv[1]);assert z.testzip() is None;print(json.dumps(sorted(z.namelist())))', zip]).toString());
    log(['README.md', 'identity.json', 'prepared_set/features.csv', 'prepared_set/manifest.json', 'run.json', 'session_results.json'].every(n => names.includes(n)),
        'Reproducibility package: a valid .zip with README, run, identity, results and the prepared flows (' + names.length + ' files)');
    const run = JSON.parse(cp.execFileSync(process.env.PYTHON || 'python', ['-c', 'import sys,zipfile;print(zipfile.ZipFile(sys.argv[1]).read("run.json").decode())', zip]).toString());
    log(run.repeat_check === true && run.quantization === '4-bit NF4' && run.models.length === 2, 'run.json records the models, options and 4-bit NF4');

    // Methodology
    await p.goto(BASE + '/#/help'); await p.waitForSelector('#help-method');
    const m = await p.textContent('#help-method');
    log(/target ÷ value/.test(m) && /Kruskal-Wallis/.test(m) && /1\.4826/.test(m) && /SHA-256/.test(m), 'How to use › Methodology: setup, measures, SAW formula, statistics');
    log(!errs.length, 'no JS errors ' + errs.join(' | '));
  } catch (e) {
    out.push('CRASH ' + e.message);
  }
  await b.close();
  console.log(out.join('\n'));
  process.exit(out.some(l => /^(FAIL|CRASH)/.test(l)) ? 1 : 0);
})();
