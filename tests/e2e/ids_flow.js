// The IDS gate walk-through (Playwright, mock backend with the bundled demo IDS):
//   python scripts/serve.py --provider mock --port 8766
//   node tests/e2e/ids_flow.js [base_url] [out_dir]
// Upload a CSV -> tick the IDS gate (the demo model is offered) -> run 2 models -> the session
// shows the IDS -> LLM triage card, the overview tile, and only the flagged flows were triaged.
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const BASE = process.argv[2] || 'http://127.0.0.1:8766';
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'ids-e2e-'));
fs.mkdirSync(S, { recursive: true });
const SAMPLE = path.resolve(__dirname, '..', '..', 'data', 'sample_csv', 'cicids2017_sample.csv');
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const p = await (await b.newContext({ viewport: { width: 1280, height: 1000 } })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  try {
    const h = await (await p.request.get(BASE + '/health')).json();
    log(h.ids && h.ids.demo === true, '/health offers the demo IDS');
    await p.goto(BASE + '/app.html#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
    await p.setInputFiles('#svc-file', SAMPLE);
    await p.fill('#svc-max-flows', '30');
    await p.evaluate(() => { localStorage.setItem('agentmeter.prepWhere', 'server'); });
    await p.click('#svc-upload-btn');
    await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 30000 });
    await p.waitForSelector('#svc-models input');
    const boxes = await p.$$('#svc-models input'); await boxes[0].check(); await boxes[1].check();
    log(!(await p.isDisabled('#svc-ids')), 'wizard: the IDS gate can be ticked');
    await p.check('#svc-ids');
    log(await p.isVisible('#svc-ids-box') && /Demo IDS/.test(await p.textContent('#svc-ids-note')), 'wizard: the IDS details and the demo warning show');
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
    const id = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[3]);
    const job = await (await p.request.get(BASE + '/api/jobs/' + encodeURIComponent(id))).json();
    log(job.ids === true || (job.ids && typeof job.ids === 'object'), 'the job carries the IDS option');
    await p.waitForFunction(() => location.hash.indexOf('#/session/') === 0 && !document.querySelector('#watch-card'), null, { timeout: 180000 });
    await p.waitForSelector('#svc-tiles');
    log(/saved/.test(await p.textContent('#svc-tiles .tile[data-metric="ids"]')), 'overview: the IDS gate tile');
    await p.click('#session-tabs [role=tab]:has-text("Output Reliability")');
    await p.waitForSelector('#rel-ids');
    const card = await p.textContent('#rel-ids');
    const m = card.match(/(\d+) of (\d+) flows flagged/);
    log(!!m && +m[1] > 0 && +m[1] <= +m[2], 'IDS card: flagged of incoming (' + (m ? m[0] : 'none') + ')');
    log((await p.$$('#ids-models-table tbody tr')).length === 2 && !!(await p.$('#ids-detect-table')), 'IDS card: detection table and one row per model');
    log(/Demo IDS/.test(card) && /trained on/.test(card), 'IDS card: demo IDS and training-overlap warnings');
    const chips = await p.textContent('#session-chips');
    log(m && new RegExp(m[1] + ' flows').test(chips), 'the session triaged only the flagged flows (' + chips.replace(/\s+/g, ' ').trim() + ')');
    await p.screenshot({ path: S + '/ids-card.png', fullPage: true });
  } catch (e) {
    log(false, 'walk-through crashed: ' + e.message.split('\n')[0]);
  }
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  await b.close();
  console.log(out.join('\n'));
  process.exit(out.some(l => l.startsWith('FAIL')) ? 1 : 0);
})();
