// The experiment walk-through (Playwright, mock backend):
//   python scripts/serve.py --provider mock --port 8766
//   node tests/e2e/experiment_flow.js [base_url] [out_dir]
// New benchmark -> open "Compare model sizes and number of agents" -> 1 model x 4-bit + 8-bit x
// 1, 4 and 10 agents = 6 runs -> the runs go one after another -> the session's "Size & agents"
// tab shows a chart per model, a heatmap, every run and what changed.
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const BASE = process.argv[2] || 'http://127.0.0.1:8766';
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'experiment-e2e-'));
fs.mkdirSync(S, { recursive: true });
const SAMPLE = path.resolve(__dirname, '..', '..', 'data', 'sample_csv', 'cicids2017_sample.csv');
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const p = await (await b.newContext({ viewport: { width: 1280, height: 1000 } })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  try {
    await p.goto(BASE + '/app.html#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
    await p.setInputFiles('#svc-file', SAMPLE);
    await p.fill('#svc-max-flows', '8');
    await p.evaluate(() => { localStorage.setItem('agentmeter.prepWhere', 'server'); });
    await p.click('#svc-upload-btn');
    await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 30000 });
    await p.waitForSelector('#svc-models input');
    await p.locator('#svc-models input').nth(0).check();
    await p.waitForFunction(() => /Ready/.test(document.querySelector('#wiz-data-chip').textContent), null, { timeout: 90000 });
    log(/Run benchmark/.test(await p.textContent('#svc-run')), 'a standard benchmark by default (4-bit, 4 agents)');
    await p.click('#cmp-box summary');
    await p.check('#cmp-prec input[value="8bit"]');
    await p.check('#cmp-agents input[value="1"]'); await p.check('#cmp-agents input[value="10"]');
    const line = await p.textContent('#cmp-line');
    log(/^6 runs: 1 model × 2 sizes × 3 numbers of agents/.test(line), 'the runs are counted as they are ticked: ' + line);
    log(/Run 6 benchmarks/.test(await p.textContent('#svc-run')), 'Run says how many benchmarks');
    log(/10 agents/.test(await p.textContent('#cmp-pipes')) && /Justify/.test(await p.textContent('#cmp-pipes')), 'each number of agents lists its pipeline');
    await p.check('#cmp-prec input[value="16bit"]'); await p.check('#cmp-agents input[value="2"]'); await p.check('#cmp-agents input[value="6"]');
    log(await p.isDisabled('#svc-run') && /At most 12 runs/.test(await p.textContent('#cmp-line')), 'more than 12 runs cannot start (3 sizes x 5 = 15 ticked)');
    await p.uncheck('#cmp-prec input[value="16bit"]'); await p.uncheck('#cmp-agents input[value="2"]'); await p.uncheck('#cmp-agents input[value="6"]');
    await p.screenshot({ path: S + '/01-compare.png', fullPage: true });
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
    await p.waitForFunction(() => /Run \d of 6/.test(document.querySelector('#svc-job-phase').textContent), null, { timeout: 60000 });
    log(true, 'the watch page names the run: ' + await p.textContent('#svc-job-phase'));
    await p.waitForFunction(() => /\/variants$/.test(location.hash), null, { timeout: 300000 });
    await p.waitForSelector('#xp-charts svg', { timeout: 60000 });
    log(await p.isVisible('#stab-variants'), 'the session shows the Size & agents tab');
    log(/6 of 6 runs finished/.test(await p.textContent('#xp-head')), 'every run finished: ' + (await p.textContent('#xp-head')).trim().slice(0, 60));
    log((await p.$$('#xp-charts svg polyline')).length === 2 && (await p.$$('#xp-charts svg circle')).length === 6, 'one line per model size, a point per run');
    log((await p.$$('#xp-cells tbody tr')).length === 6, 'the table lists every run');
    log(/From 1 to 10 agents/.test(await p.textContent('#xp-changed')), 'what changed: ' + (await p.textContent('#xp-changed')).trim().slice(0, 120));
    await p.screenshot({ path: S + '/02-variants.png', fullPage: true });
    await p.click('[data-xp-measure="mean_tokens_per_flow"]');
    log(/Tokens per flow/.test(await p.textContent('#xp-charts h2')), 'the measure can be switched');
    await p.click('#xp-cells tbody tr:first-child a');
    await p.waitForSelector('#session-chips .chip');
    log(/Run \d of 6/.test(await p.textContent('#session-chips')), 'a run links back to its experiment');
  } catch (e) {
    log(false, 'walk-through crashed: ' + e.message.split('\n')[0]);
  }
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  await b.close();
  console.log(out.join('\n'));
  process.exit(out.some(l => l.startsWith('FAIL')) ? 1 : 0);
})();
