// Live telemetry walk-through (Playwright, mock backend slowed down so a run lasts long enough
// to leave and come back):
//   AGENTMETER_MOCK_LATENCY_S=0.05 python scripts/serve.py --provider mock --port 8766
//   node tests/e2e/telemetry_flow.js [base_url] [out_dir]
// Start a run -> the panel fills on the run page -> leave for Sessions -> open the running
// session: its panel shows the whole run again (one panel only) and keeps updating -> done.
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const BASE = process.argv[2] || 'http://127.0.0.1:8766';
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'tel-e2e-'));
fs.mkdirSync(S, { recursive: true });
const SAMPLE = path.resolve(__dirname, '..', '..', 'data', 'sample_csv', 'cicids2017_sample.csv');
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const p = await (await b.newContext({ viewport: { width: 1280, height: 1000 } })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  try {
    await p.goto(BASE + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
    await p.setInputFiles('#svc-file', SAMPLE);
    await p.fill('#svc-max-flows', '40'); await p.click('#svc-upload-btn');
    await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 30000 });
    await p.waitForSelector('#svc-models input');
    const boxes = await p.$$('#svc-models input'); await boxes[0].check(); await boxes[1].check();
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
    const id = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[3]);
    await p.waitForFunction(() => document.querySelectorAll('#tel-host .tel-line').length >= 3, null, { timeout: 30000 });
    log((await p.$$('#tel-host #tel-steps li')).length === 7, 'run page: the panel shows the steps and the log');
    await p.goto(BASE + '/#/sessions'); await p.waitForSelector('#view-sessions:not([hidden])');
    await p.waitForTimeout(800);
    await p.goto(BASE + '/#/session/' + id);
    await p.waitForFunction(() => document.querySelectorAll('#watch-tel .tel-line').length >= 3, null, { timeout: 20000 });
    const n1 = await p.$$eval('#watch-tel .tel-line', l => l.length);
    log(await p.$$eval('#tel-card', c => c.length) === 1 && /Queued/.test(await p.textContent('#watch-tel #tel-log')),
        'back on the running session: one panel, the whole run from the start');
    await p.screenshot({ path: S + '/telemetry-watch.png', fullPage: true });
    await p.waitForFunction(n => document.querySelectorAll('#watch-tel .tel-line').length > n, n1, { timeout: 30000 });
    log(true, 'the panel keeps updating after coming back');
    await p.waitForFunction(() => location.hash.indexOf('#/session/') === 0 && !document.querySelector('#watch-card'), null, { timeout: 180000 });
    log(true, 'when the run is done the session opens its results');
  } catch (e) {
    log(false, 'walk-through crashed: ' + e.message.split('\n')[0]);
  }
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  await b.close();
  console.log(out.join('\n'));
  process.exit(out.some(l => l.startsWith('FAIL')) ? 1 : 0);
})();
