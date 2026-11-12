// Observability-style GPU charts (Playwright, mock backend with the TEST-ONLY synthetic GPU
// readings: AGENTMETER_TEST_SYNTHETIC_GPU=1 AGENTMETER_IDLE_SAMPLE_S=1): one 2-model session ->
// Cost & Energy -> GPU over the run: labelled as synthetic; an agent lane; hovering one chart puts
// the crosshair on every chart and names the flow and agent running then; drag-to-zoom zooms them
// all and shows flow starts; "Zoom to <model>" and "Show the whole run"; your memory limit is a
// threshold line; every chart can be inspected and its numbers downloaded.
//   node tests/e2e/observability_flow.js [base_url] [out_dir]
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const ORIGIN = process.argv[2] || 'http://127.0.0.1:8766';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'obs-e2e-'));
fs.mkdirSync(S, { recursive: true });
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

// drag across a chart from page x `from` to page x `to` (kept inside the plot area)
async function dragAcross(p, sel, from, to) {
  const box = await p.locator(sel + ' svg').boundingBox();
  const y = box.y + box.height * 0.5, lo = box.x + box.width * 0.08, hi = box.x + box.width * 0.98;
  const x = v => Math.max(lo, Math.min(hi, v)), a = x(from), z = x(to);
  await p.mouse.move(a, y); await p.mouse.down();
  await p.mouse.move((a + z) / 2, y, { steps: 4 }); await p.mouse.move(z, y, { steps: 4 });
  await p.mouse.up();
}

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const p = await (await b.newContext({ viewport: { width: 1280, height: 1000 }, acceptDownloads: true })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  try {
    await p.goto(ORIGIN + '/app.html#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
    await p.evaluate(() => { localStorage.setItem('agentmeter.prepWhere', 'server');
      localStorage.setItem('agentmeter.limits', JSON.stringify({ max_peak_vram_mb: 4100 })); });
    await p.setInputFiles('#svc-file', path.join(REPO, 'data', 'sample_csv', 'cicids2017_sample.csv'));
    await p.fill('#svc-max-flows', '12'); await p.click('#svc-upload-btn');
    await p.waitForFunction(() => /Ready/.test(document.querySelector('#wiz-data-chip').textContent), null, { timeout: 90000 });
    for (const i of [0, 1]) await p.locator('#svc-models input').nth(i).check();
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/session\//.test(location.hash), null, { timeout: 240000 });
    const id = decodeURIComponent(p.url().split('#/session/')[1].split('/')[0]);
    await p.goto(ORIGIN + '/app.html#/session/' + encodeURIComponent(id) + '/cost');
    await p.waitForSelector('#gpu-power', { timeout: 30000 });

    log(/Synthetic test readings/.test(await p.textContent('#gpu-timeline')), 'synthetic readings are labelled as not a measurement');
    await p.goto(ORIGIN + '/app.html#/session/' + encodeURIComponent(id) + '/overview'); await p.waitForSelector('#svc-gpu-health');
    log(/GPU state: normal/.test(await p.textContent('#svc-gpu-health')) && /synthetic readings \(test\)/.test(await p.textContent('#svc-gpu-health')),
        'Overview: the GPU state during the run (normal; synthetic readings labelled)');
    await p.goto(ORIGIN + '/app.html#/session/' + encodeURIComponent(id) + '/cost'); await p.waitForSelector('#gpu-power', { timeout: 30000 });
    const segs = await p.$$eval('#gpu-lane .lane-seg', x => x.length);
    log(segs >= 12 * 2 * 4 * 0.9, `the agent lane shows each agent's turn (${segs} turns for 12 flows × 2 models × 4 agents)`);
    log(/Perceive|perceive/.test(await p.textContent('#gpu-lane-legend')), 'the lane has an agent legend');

    // shared crosshair: hover the power chart in the middle of the first model's window
    await p.locator('#gpu-power').scrollIntoViewIfNeeded();
    const box = await p.locator('#gpu-power svg').boundingBox();
    const seg = await p.locator('#gpu-lane .lane-seg').nth(10).boundingBox();     // an agent's turn, from the lane
    await p.mouse.move(seg.x + seg.width / 2, box.y + box.height * 0.5);
    await p.waitForTimeout(150);
    const tip = (await p.textContent('.tip-pop').catch(() => '')) || '';
    const crosses = await p.$$eval('#gpu-timeline .lc-cross', xs => xs.filter(x => x.getAttribute('visibility') === 'visible').length);
    log(crosses >= 4, `one crosshair on every chart and the lane at once (${crosses} visible)`);
    log(/ s, .*flow .* (agent|between agent calls)/.test(tip) && / W/.test(tip), 'the hover names the model, flow and agent with the power at that moment: ' + tip.slice(0, 110));
    await p.screenshot({ path: S + '/gpu-hover.png', fullPage: false });

    // synced drag-to-zoom
    const before = await p.$$eval('#gpu-util polyline', x => x[0].getAttribute('points').split(' ').length);
    // where the flows ran depends on timing (idle baseline, model load), so zoom around a flow's
    // start read from the chart rather than a fixed share of it: the band then always holds one
    const startX = await p.$eval('#gpu-power .lc-mark', m => m.getBoundingClientRect().x);
    await dragAcross(p, '#gpu-power', startX - box.width * 0.07, startX + box.width * 0.07);
    await p.waitForSelector('[data-gpu-zoom="reset"]', { timeout: 5000 });
    const after = await p.$$eval('#gpu-util polyline', x => x[0].getAttribute('points').split(' ').length);
    log(/Showing [\d.,]+–[\d.,]+ s/.test(await p.textContent('.gpu-zoom')), 'dragging across a chart zooms: the range is shown');
    log(after < before, `every chart zoomed together (utilisation chart ${before} → ${after} points)`);
    const marks = await p.$$eval('#gpu-power .lc-mark', x => x.length);
    log(marks > 0, `zoomed in, each flow's start is annotated (${marks} lines)`);
    await p.screenshot({ path: S + '/gpu-zoom.png', fullPage: false });
    await p.click('[data-gpu-zoom="reset"]');
    await p.waitForSelector('[data-gpu-zoom="reset"]', { state: 'detached', timeout: 5000 });
    log(true, '"Show the whole run" zooms back out');
    await p.click('[data-gpu-zoom="1"]');
    await p.waitForSelector('[data-gpu-zoom="reset"]', { timeout: 5000 });
    log(/Showing/.test(await p.textContent('.gpu-zoom')), '"Zoom to <model>" shows that model\'s window');
    await p.locator('#gpu-mem').dblclick();
    await p.waitForSelector('[data-gpu-zoom="reset"]', { state: 'detached', timeout: 5000 });
    log(true, 'double-click zooms back out');

    log(await p.$$eval('#gpu-mem .lc-thr', x => x.length) === 1 && /your limit/.test(await p.textContent('#gpu-mem')), 'your GPU memory limit is drawn as a threshold line');

    // Inspect: the numbers behind the charts, and their download
    await p.click('#gpu-timeline [data-exp="inspect"]');
    const rows = await p.$$eval('#gpu-timeline .inspect-table tbody tr', x => x.length);
    log(rows > 50 && /data points behind the charts/.test(await p.textContent('#gpu-timeline .inspect')), `Inspect shows the data behind the charts (${rows} rows shown)`);
    const [dl] = await Promise.all([p.waitForEvent('download'), p.click('#gpu-timeline [data-exp="chartcsv"]')]);
    const csv = fs.readFileSync(await dl.path(), 'utf8').split('\n');
    log(csv[0] === 'chart,series,x,y' && csv.length > 100 && csv.some(l => /,turn,/.test(l)), `Chart data (CSV) has every point, agent turns included (${csv.length - 1} rows)`);

    // thresholds from your limits: set under Recommendations, shown on Overview and Performance
    await p.goto(ORIGIN + '/app.html#/session/' + encodeURIComponent(id) + '/recommendations');
    await p.waitForSelector('#dh-form');
    log(await p.inputValue('#dh-max_peak_vram_mb') === '4100', 'your saved limits are filled in');
    await p.fill('#dh-max_mean_latency_s', '0.000001'); await p.fill('#dh-max_p95_latency_s', '1000');
    await p.click('#dh-go'); await p.waitForSelector('#dh-table', { timeout: 15000 });
    await p.goto(ORIGIN + '/app.html#/session/' + encodeURIComponent(id) + '/overview');
    await p.waitForSelector('#svc-models-table');
    const over = await p.$$eval('#svc-models-table td.over-limit', x => x.length), within = await p.$$eval('#svc-models-table td.in-limit', x => x.length);
    log(over === 2 && within === 2 && !(await p.isHidden('#lim-note')), `values are coloured against your limits (${over} over, ${within} within)`);
    await p.goto(ORIGIN + '/app.html#/session/' + encodeURIComponent(id) + '/performance');
    await p.waitForSelector('#trend-0', { state: 'attached' });
    log(/your p95 limit/.test(await p.textContent('#trend-0')), 'the latency chart draws your p95 limit');

    // variables: the Model filter on Repeatability
    const models = await p.evaluate(() => fetch('/api/jobs?limit=20').then(r => r.json()).then(d => d.jobs.find(j => j.kind !== 'prepare').models));
    await p.goto(ORIGIN + '/app.html#/trends?demo=1'); await p.waitForSelector('#tr-vars-model');      // mock runs are demo sessions
    await p.selectOption('#tr-vars-model', models[0]);
    log(await p.inputValue('#tr-vars-model') === models[0], 'Repeatability: a Model filter');
    await p.waitForSelector('#tr-summary-table');
    const series = await p.$$eval('#tr-summary-table tbody tr td:first-child', x => x.map(o => o.textContent));
    log(series.length >= 1 && series.every(t => t.indexOf(models[0].split('/').pop()) >= 0), `Trends: only that model's series (${series.length})`);
    await p.selectOption('#tr-vars-model', '');
    log((await p.$$eval('#tr-summary-table tbody tr', x => x.length)) >= 2, 'Trends: "All" shows every model again');
    await p.screenshot({ path: S + '/trends-vars.png', fullPage: false });
  } catch (e) {
    log(false, 'walk-through crashed: ' + e.message.split('\n')[0]);
  }
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  await b.close();
  console.log(out.join('\n'));
  process.exit(out.some(l => l.startsWith('FAIL')) ? 1 : 0);
})();
