// Phases 41-46 — the DATA paths of the New-benchmark wizard (Playwright, same origin):
// upload -> data summary + prepared-set downloads (while picking models) -> run -> session;
// reuse a set by id; re-upload downloaded files; a bad file; an oversized upload; URL import
// (when E2E_URL_BASE names an https server with big.csv — see docs/DEPLOY.md for the
// TEST-ONLY loopback flags). The app-wide walk-through is tests/e2e/app_flow.js.
//   AGENTMETER_MAX_UPLOAD_MB=1 AGENTMETER_MAX_CSV_ROWS=100000 python scripts/serve.py --port 8766   # mock demo mode
//   node tests/e2e/service_flow.js [base_url] [out_dir]
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const ORIGIN = process.argv[2] || 'http://127.0.0.1:8766';
const URL_BASE = process.env.E2E_URL_BASE || '';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'svc-e2e-'));
fs.mkdirSync(S, { recursive: true });
fs.writeFileSync(S + '/bad.csv', 'a,b\n1,2\n');
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

async function upload(p, file, flows) {
  await p.goto(ORIGIN + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
  await p.setInputFiles('#svc-file', file);
  await p.fill('#svc-max-flows', String(flows));
  await p.click('#svc-upload-btn');
  await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 30000 });
}
async function ready(p, timeout) {
  await p.waitForFunction(() => /Ready|Failed/.test(document.querySelector('#wiz-data-chip').textContent), null, { timeout: timeout || 90000 });
  return p.textContent('#wiz-data-chip');
}
async function run(p, idx) {
  await p.waitForSelector('#svc-models input');
  const boxes = await p.$$('#svc-models input');
  for (const i of idx) await boxes[i].check();
  await p.click('#svc-run');
  await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
  const id = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[3]);
  await p.waitForFunction(j => location.hash.indexOf('#/session/' + j) === 0, id, { timeout: 180000 });
  await p.waitForSelector('#session-body:not([hidden])', { timeout: 30000 });
  return id;
}

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const ctx = await b.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  p.on('console', m => { if (m.type() === 'error' && !/fonts|ERR_|Failed to load resource/.test(m.text())) errs.push(m.text()); });

  // 0. old entry URLs open the wizard
  await p.goto(ORIGIN + '/service/prepare'); await p.waitForSelector('#wiz-step-data:not([hidden])', { timeout: 15000 });
  log(/#\/new$/.test(p.url()), '/service/prepare opens New benchmark (' + p.url() + ')');
  log(await p.isVisible('#svc-demo'), 'demo-mode notice shown without a GPU');
  log(/not a threat-detection/.test(await p.textContent('footer')), 'framing: measures efficiency, not a threat-detection product');

  // 1. upload a labelled CSV -> summary + downloads on step 2 while picking models
  await upload(p, REPO + '/data/sample_csv/cicids2017_sample.csv', 12);
  log(await ready(p) === 'Ready', 'prepared in the background -> Ready');
  const sum = await p.textContent('#svc-summary-card');
  log(/Labelled → efficiency \+ accuracy/.test(sum), 'summary: labelled -> efficiency + accuracy (context)');
  await p.click('#svc-summary-card summary');
  log((await p.$$('#svc-class-table tbody tr')).length === 5, 'summary: 5-class table (in file / pool / selected)');
  log(/12 of 40/.test(await p.textContent('#svc-counts')), 'summary: flows selected 12 of 40');
  log(/all 43 rows of the file/.test(await p.textContent('#svc-sampling')), 'summary: sampling method stated');
  const dls = await p.$$eval('#wiz-downloads a', as => as.map(a => a.textContent));
  log(dls.join(',') === 'features.csv,labels.csv,manifest.json', 'downloads on step 2: ' + dls.join(','));
  const setId = await p.textContent('#svc-set-id-value');
  const files = {};
  for (const f of dls) {
    const [d] = await Promise.all([p.waitForEvent('download'), p.click(`#wiz-downloads a[data-dl="${f}"]`)]);
    files[f] = S + '/' + f; await d.saveAs(files[f]);
  }
  const fhead = fs.readFileSync(files['features.csv'], 'utf8').split('\n')[0];
  log(!/(^|,)label(,|$)/i.test(fhead) && /Flow Duration/.test(fhead), 'features.csv has the 78 features and no label column');
  log(JSON.parse(fs.readFileSync(files['manifest.json'], 'utf8')).prepared_set.source.sha256.length === 64, 'manifest records the source sha256');
  await p.screenshot({ path: S + '/svc-models-step.png', fullPage: true });

  // 2. run 2 models; reload during the run re-attaches; the session page shows the results
  const boxes = await p.$$('#svc-models input');
  await boxes[0].check(); await boxes[3].check();
  log(await boxes[1].isDisabled() && await boxes[4].isDisabled(), 'UI blocks a 3rd model once 2 are selected');
  await p.click('#svc-run');
  await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
  const runHash = await p.evaluate(() => location.hash);
  const job1 = decodeURIComponent(runHash.split('/')[3]);
  await p.reload();
  await p.waitForFunction(j => location.hash.indexOf('#/session/' + j) === 0, job1, { timeout: 180000 });
  await p.waitForSelector('#session-body:not([hidden])', { timeout: 30000 });
  log(true, 'reload during the run re-attached and landed on the session');
  log((await p.$$('#svc-models-table tbody tr')).length === 2 && /Accuracy/.test(await p.textContent('#svc-models-table thead')),
      'session: 2 models, accuracy shown as context');
  await p.click('#session-tabs [data-tab="downloads"]'); await p.waitForSelector('#svc-downloads a');
  log((await p.$$eval('#svc-downloads a', as => as.map(a => a.textContent))).join(',') === 'features.csv,labels.csv,manifest.json',
      'session Downloads: prepared-set files');

  // 3. reuse a prepared set by id; an unknown id is refused
  await p.goto(ORIGIN + '/#/new?src=reuse'); await p.waitForSelector('#svc-bench-id');
  await p.fill('#svc-bench-id', setId); await p.click('#svc-bench-id-btn');
  await p.waitForFunction(() => /^#\/new\/models\?set=/.test(location.hash));
  log(await ready(p) === 'Ready' && (await p.textContent('#wiz-data-status')).includes(setId), 'reuse by prepared-set id -> Models with the set ready');
  await p.goto(ORIGIN + '/#/new?src=reuse'); await p.waitForSelector('#svc-bench-id');
  await p.fill('#svc-bench-id', 'csv_runs/does-not-exist'); await p.click('#svc-bench-id-btn');
  await p.waitForSelector('#svc-bench-id-error:not([hidden])');
  log(/not found/.test(await p.textContent('#svc-bench-id-error')), 'unknown prepared-set id rejected');

  // 4. re-upload the downloaded files
  await p.goto(ORIGIN + '/#/new?src=reuse'); await p.waitForSelector('#svc-import-form');
  await p.setInputFiles('#svc-imp-manifest', files['manifest.json']);
  await p.setInputFiles('#svc-imp-features', files['features.csv']);
  await p.click('#svc-import-btn');
  await p.waitForSelector('#svc-import-error:not([hidden])');
  log(/labels\.csv too/.test(await p.textContent('#svc-import-error')), 're-upload without labels.csv rejected with a reason: ' + await p.textContent('#svc-import-error'));
  await p.setInputFiles('#svc-imp-labels', files['labels.csv']);
  await p.click('#svc-import-btn');
  await p.waitForFunction(() => /^#\/new\/models\?set=/.test(location.hash));
  const h = decodeURIComponent(await p.evaluate(() => location.hash));
  log(/_import_/.test(h) && !h.includes(setId.split('/')[1]), 're-uploaded set stored under a new id');
  await ready(p);
  await run(p, [1]);
  log(/Single-model run/.test(await p.textContent('#svc-verdict')), 're-uploaded set benchmarks (single model)');

  // 5. PCAP -> efficiency only, no labels.csv
  await upload(p, REPO + '/data/sample_pcaps/sample_small.pcap', 8);
  await ready(p);
  log(/Unlabelled → efficiency only/.test(await p.textContent('#svc-summary-card')) && !(await p.$('#svc-class-table')), 'PCAP prepared set: efficiency only');
  log((await p.$$eval('#wiz-downloads a', as => as.map(a => a.textContent))).join(',') === 'features.csv,manifest.json', 'PCAP set has no labels.csv');
  await run(p, [2, 3]);
  log(!/Accuracy/.test(await p.textContent('#svc-models-table thead')), 'PCAP session: efficiency only');

  // 6. bad file -> the data card says why, Run is blocked
  await upload(p, S + '/bad.csv', 10);
  log(await ready(p, 30000) === 'Failed', 'bad CSV: data preparation fails');
  log(/none of the 78/.test(await p.textContent('#wiz-data-error')), 'bad CSV: the reason is shown');
  await (await p.$$('#svc-models input'))[0].check();
  log(await p.isDisabled('#svc-run'), 'Run is blocked while the data failed');

  // 7. too large to upload -> pointed to Import from a link (client-side, nothing sent)
  const lim = await p.evaluate(() => fetch('/api/service/config').then(r => r.json()).then(c => c.limits));
  const bigPath = S + '/toobig.csv'; fs.writeFileSync(bigPath, Buffer.alloc(lim.max_upload_bytes + 1024, 97));
  await p.goto(ORIGIN + '/#/new'); await p.waitForSelector('#svc-file');
  await p.setInputFiles('#svc-file', bigPath); await p.click('#svc-upload-btn');
  await p.waitForSelector('#svc-upload-error:not([hidden])');
  log(/Import from a link/.test(await p.textContent('#svc-upload-error')), 'oversized upload points to Import from a link (limit ' + lim.max_upload_label + ')');
  log(/Import from a link/.test(await p.textContent('#svc-limits')), 'upload limit shown with the link-import hint');

  // 8. Import from a link -> run
  if (URL_BASE) {
    await p.goto(ORIGIN + '/#/new'); await p.waitForSelector('#svc-file');
    await p.click('#tab-src-url');
    log(await p.isVisible('#svc-url') && !(await p.isVisible('#svc-drop')), 'link tab shows the URL field');
    await p.fill('#svc-url', URL_BASE + '/big.csv'); await p.fill('#svc-max-flows', '20');
    await p.click('#svc-upload-btn');
    await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 15000 });
    let phases = '';
    for (let i = 0; i < 600 && (await p.textContent('#wiz-data-chip')) === 'Preparing'; i++) {
      const t = await p.textContent('#wiz-data-status');
      const m = t.match(/^(Downloading|Sampling|Validating|Selecting|Writing|Queued)/);
      if (m && !phases.includes(m[1])) phases += m[1] + ' ';
      await p.waitForTimeout(250);
    }
    log(/Downloading|Sampling|Validating|Selecting/.test(phases), 'link import showed live phases: ' + phases.trim());
    log(await ready(p) === 'Ready', 'link import prepared');
    await p.click('#svc-summary-card summary');
    const us = await p.textContent('#svc-summary-card');
    log(/Imported from/.test(us) && /big\.csv/.test(us), 'the prepared set records the source URL');
    log(/stratified random sample/.test(await p.textContent('#svc-sampling')), 'large CSV sampled across the whole file');
    await run(p, [0, 1]);
    log((await p.$$('#svc-models-table tbody tr')).length === 2, 'link-imported set benchmarks with 2 models');
  } else {
    log(true, 'link import walk skipped (E2E_URL_BASE not set)');
  }

  // 9. Sessions page lists runs; phone width
  await p.goto(ORIGIN + '/#/sessions'); await p.waitForSelector('#sessions-table');
  const n = (await p.$$('#sessions-table tbody tr')).length;
  log(n >= 3, 'Sessions lists the benchmark runs (' + n + '), never a separate "prepare job"');
  await p.selectOption('#flt-input', 'PCAP');
  await p.waitForFunction(() => { const t = document.querySelector('#sessions-table'); return t && [...t.querySelectorAll('tbody tr')].every(r => /PCAP/.test(r.textContent)); });
  log(true, 'Sessions filter by input type');
  const m = await (await b.newContext({ viewport: { width: 375, height: 800 } })).newPage();
  let sw = 0;
  for (const [hash, sel] of [['#/new', '#svc-upload-form'], ['#/new?src=reuse', '#svc-import-form'], ['#/session/' + job1, '#session-body:not([hidden])'], ['#/sessions', '#sessions-table']]) {
    await m.goto(ORIGIN + '/' + hash); await m.waitForSelector(sel, { timeout: 30000 });
    sw = Math.max(sw, await m.evaluate(() => document.documentElement.scrollWidth));
  }
  log(sw <= 375, 'no horizontal page scroll at 375px (max scrollWidth ' + sw + ')');
  await m.screenshot({ path: S + '/svc-mobile.png', fullPage: true });

  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  console.log(out.join('\n')); console.log('screenshots: ' + S);
  await b.close();
  if (out.some(l => l.startsWith('FAIL'))) process.exit(1);
})().catch(e => { console.log(out.join('\n')); console.log('CRASH', e.message); process.exit(1); });
