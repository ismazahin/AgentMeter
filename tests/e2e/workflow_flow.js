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
    await p.goto(BASE + '/app.html#/session/' + j1); await p.waitForSelector('#session-again:not([hidden])');
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
    log(!(await p.$('#svc-notify')) && await p.$eval('#svc-extras', d => d.open) && await p.isVisible('#svc-repeat-check'),
        'no "Tell me when it finishes" box (always on), and load scaling and repeat check sit in Optional extras, opened here by Run again');
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
    const j3 = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[3]);
    await p.goto(BASE + '/app.html#/datasets'); await p.waitForSelector('#ds-table');       // somewhere else in the app
    await p.waitForSelector('.toast', { timeout: 120000 });
    log(/Benchmark finished/.test(await p.textContent('.toast')) && !!(await p.$(`.toast a[href="#/session/${j3}"]`)), 'when it finishes, a message links to the results (while on another page)');

    // thesis exports: the Leaderboard ranking (an old Compare link opens By setup on that session's setup)
    await p.goto(BASE + '/app.html#/compare?a=' + j1); await p.waitForSelector('#lb-table-0');
    log(/^#\/leaderboard/.test(await p.evaluate(() => location.hash)), 'an old Compare link opens the Leaderboard');
    const tex = fs.readFileSync(await grab(p, () => p.$('#lb-panel > .exp-bar [data-exp="latex"]').then(x => x.click())), 'utf8');
    log(/\\begin\{tabular\}/.test(tex) && /\\toprule/.test(tex) && tex.split('\\\\').length > 4, 'LaTeX: the ranking as a booktabs table');
    log(!!(await p.$('#lb-why-table')) && /Speed/.test(await p.textContent('#lb-why-table thead')), 'Why this order: points per measure under the ranking');

    // Consistency: the runs of a model keep their downloads
    await p.goto(BASE + '/app.html#/trends'); await p.waitForSelector('#tr-intro');
    await p.waitForSelector('#tr-cards [data-tr-open], #tr-once-card');
    if (await p.$('#tr-cards [data-tr-open]')) {
      await p.click('#tr-cards [data-tr-open]'); await p.waitForSelector('#tr-chart-card');
      log(!!(await p.$('#tr-chart-card [data-exp="latex"]')), 'Consistency: every run of a model downloads as CSV / LaTeX');
    } else log(!!(await p.$('#tr-once-card')), 'Consistency: models run once are listed');

    // warm-up: the steady-state mean next to the headline mean
    await p.goto(BASE + '/app.html#/session/' + j1 + '/performance/latency'); await p.waitForSelector('#stats-percentiles');
    log(/Mean without the first 5 flows/.test(await p.textContent('#stats-percentiles thead')), 'Performance › Latency: the mean without the warm-up flows');

    // reproducibility package
    await p.goto(BASE + '/app.html#/session/' + j1 + '/data'); await p.waitForSelector('#svc-dl-repro');
    const zip = await grab(p, () => p.click('#svc-dl-repro'));
    const names = JSON.parse(cp.execFileSync(process.env.PYTHON || 'python', ['-c',
      'import sys,zipfile,json;z=zipfile.ZipFile(sys.argv[1]);assert z.testzip() is None;print(json.dumps(sorted(z.namelist())))', zip]).toString());
    log(['README.md', 'identity.json', 'prepared_set/features.csv', 'prepared_set/manifest.json', 'run.json', 'session_results.json'].every(n => names.includes(n)),
        'Reproducibility package: a valid .zip with README, run, identity, results and the prepared flows (' + names.length + ' files)');
    const run = JSON.parse(cp.execFileSync(process.env.PYTHON || 'python', ['-c', 'import sys,zipfile;print(zipfile.ZipFile(sys.argv[1]).read("run.json").decode())', zip]).toString());
    log(run.repeat_check === true && run.quantization === '4-bit NF4' && run.models.length === 2, 'run.json records the models, options and 4-bit NF4');

    // Ctrl+K: search pages, sessions, data sets and models
    await p.goto(BASE + '/app.html#/'); await p.waitForSelector('#gs-open');
    await p.keyboard.press('Control+k'); await p.waitForSelector('#gs:not([hidden])');
    const mname = models[0].split('/').pop();
    await p.keyboard.type(mname.slice(0, 6).toLowerCase());
    await p.waitForFunction(() => document.querySelectorAll('#gs-list li[data-i]').length > 1);
    const first = await p.textContent('#gs-list li[data-i="0"]');
    log(/Model/.test(first) && first.includes(mname), 'Ctrl+K: typing a model name finds the model first (' + first.slice(0, 60) + ')');
    await p.keyboard.press('Enter'); await p.waitForSelector('#view-sessions:not([hidden])');
    await p.waitForFunction(m => document.querySelector('#flt-q').value === m, models[0]);
    log(await p.isHidden('#gs'), 'Enter opens it: the sessions of that model');
    await p.keyboard.press('Control+k'); await p.keyboard.type('repeatab'); await p.keyboard.press('Enter');
    await p.waitForSelector('#view-trends:not([hidden])');
    log(true, 'Ctrl+K reaches pages by name');
    await p.keyboard.press('Control+k'); await p.keyboard.press('Escape');
    log(await p.isHidden('#gs'), 'Esc closes the search');
    log(/same result twice/.test(await p.textContent('#tr-intro')), 'Consistency: asks its question in plain words first');

    // Methodology
    await p.goto(BASE + '/app.html#/help'); await p.waitForSelector('#help-method');
    const m = await p.textContent('#help-method');
    log(/target ÷ value/.test(m) && /Kruskal-Wallis/.test(m) && /1\.4826/.test(m) && /SHA-256/.test(m), 'How to use › Methodology: setup, measures, SAW formula, statistics');
    // API documentation: the three OpenAPI documents, rendered without any third-party script
    await p.goto(BASE + '/docs/'); await p.waitForSelector('details.op');
    const cpOps = (await p.$$('details.op')).length;
    await p.click('.tab[data-doc="openapi-gpu-backend.json"]'); await p.waitForSelector('#benchmarks');
    const beOps = (await p.$$('details.op')).length;
    log(cpOps >= 50 && beOps >= 30, `API docs: control plane (${cpOps}) and GPU backend (${beOps}) endpoints listed`);
    await p.fill('#q', 'gpu queue'); await p.waitForTimeout(200);
    const shown = await p.$$eval('details.op .path', x => x.map(e => e.textContent));
    log(shown.includes('/api/gpu'), 'API docs: the filter finds /api/gpu');
    await p.click('#get__api_gpu summary');
    log(/run token: read/.test(await p.textContent('#get__api_gpu')) && /live reading/.test(await p.textContent('#get__api_gpu')), 'API docs: who may call each endpoint, and what it does');
    await p.goto(BASE + '/app.html#/help'); await p.waitForSelector('#help-method');
    log(!!(await p.$('a[href="docs/"]')), 'How to use links the API documentation');
    log(!errs.length, 'no JS errors ' + errs.join(' | '));
  } catch (e) {
    out.push('CRASH ' + e.message);
  }
  await b.close();
  console.log(out.join('\n'));
  process.exit(out.some(l => /^(FAIL|CRASH)/.test(l)) ? 1 : 0);
})();
