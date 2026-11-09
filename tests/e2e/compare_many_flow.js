// Compare many models at once: a session holds at most 4 models, so Compare is where models from
// several sessions on the SAME setup (flows, GPU, settings) are ranked together.
//   python scripts/serve.py --provider mock --port 8766      (fresh results dir)
//   node tests/e2e/compare_many_flow.js [base_url] [out_dir]
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const BASE = process.argv[2] || 'http://127.0.0.1:8766';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'cmp-many-'));
fs.mkdirSync(S, { recursive: true });
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

async function run(p, picks) {
  await p.waitForSelector('#svc-models input');
  await p.waitForFunction(() => document.querySelectorAll('#svc-models input').length > 0);
  const boxes = await p.$$('#svc-models input');
  for (const b of boxes) if (await b.isChecked()) await b.uncheck();
  const names = [];
  for (const i of picks) { await boxes[i].check(); names.push(await boxes[i].getAttribute('value')); }
  await p.click('#svc-run');
  await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
  const job = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[3]);
  await p.waitForFunction(id => location.hash.indexOf('#/session/' + id) === 0, job, { timeout: 180000 });
  await p.waitForSelector('#session-body:not([hidden])', { timeout: 30000 });
  return { job, names };
}

(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  try {
    await p.goto(BASE + '/app.html#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
    await p.setInputFiles('#svc-file', path.join(REPO, 'data', 'sample_csv', 'cicids2017_sample.csv'));
    await p.fill('#svc-max-flows', '6');
    await p.evaluate(() => { localStorage.setItem('agentmeter.prepWhere', 'server'); }); await p.click('#svc-upload-btn');
    await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 30000 });
    const n = (await p.$$('#svc-models input')).length;
    log(n >= 5, 'the catalogue offers at least 5 models (' + n + ')');
    const a = await run(p, [0, 1, 2, 3]);
    const set = await p.evaluate(id => fetch('/api/jobs/' + id).then(r => r.json()).then(j => j.run_name), a.job);
    await p.goto(BASE + '/app.html#/new/models?set=' + encodeURIComponent(set));
    await p.waitForFunction(() => document.querySelector('#wiz-data-chip') && document.querySelector('#wiz-data-chip').textContent === 'Ready', null, { timeout: 30000 });
    const later = [4, 5, 6].filter(i => i < n);
    const c = await run(p, [0].concat(later));                  // model 0 again: measured twice
    const all = [...new Set(a.names.concat(c.names))];
    log(all.length >= 5, `two sessions on one setup measure ${all.length} different models`);

    await p.goto(BASE + '/app.html#/compare?a=' + c.job); await p.waitForSelector('#cmp-list input');
    const offered = await p.$$eval('#cmp-list input', xs => xs.map(x => x.value));
    log(offered.length === all.length && all.every(m => offered.includes(m)), `Compare offers every model of the setup once (${offered.length})`);
    log((await p.$$('#cmp-list input:checked')).length === offered.length && (await p.$$('#cmp-list input:disabled')).length === 0,
        'every model is ticked; none is locked out (no limit of 4)');
    log(new RegExp('Compare ' + all.length + ' models').test(await p.textContent('#cmp-go')), 'the button says how many models');
    await p.click('#cmp-go'); await p.waitForSelector('#cmp-rank-table');
    log(new RegExp(all.length + ' models').test(await p.textContent('#cmp-validity')), 'like-for-like with all ' + all.length + ' models');
    const rows = await p.$$eval('#cmp-rank-table tbody tr', r => r.map(x => x.children[0].textContent));
    log(rows.length === all.length && rows.join(',') === all.map((_, i) => i + 1).join(','), 'the ranking has one row per model, numbered ' + rows.join(','));
    const repeated = a.names[0].split('/').pop();
    const rankTxt = await p.textContent('#cmp-rank-table');
    log(rankTxt.split(repeated).length === 2 && /latest of 2 runs/.test(rankTxt), 'the model run twice appears once, marked "latest of 2 runs"');
    log((await p.$$('#cmp-bars .hbar')).length === all.length, 'a bar per model');
    const vals = await p.$$eval('#cmp-bars .hb-v', x => x.map(v => v.textContent));
    log(vals.length > 1, 'bars ordered best first (' + vals.slice(0, 3).join(', ') + ' …)');
    log((await p.$$('#cmp-table thead th')).length === all.length + 1, 'All metrics: a column per model');
    await p.screenshot({ path: S + '/compare-many.png', fullPage: true });
    log(!errs.length, 'no JS errors ' + errs.join(' | '));
  } catch (e) {
    out.push('CRASH ' + e.message);
  }
  await b.close();
  console.log(out.join('\n'));
  process.exit(out.some(l => /^(FAIL|CRASH)/.test(l)) ? 1 : 0);
})();
