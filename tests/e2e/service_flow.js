// Phases 41-43b — headless walk-through of the two-step service (Playwright):
// Prepare (upload | URL) -> prepared set -> Benchmark -> results -> PDF.
// Needs a LIVE server (one process) and Node Playwright; not part of pytest.
//   AGENTMETER_MAX_UPLOAD_MB=1 python scripts/serve.py --port 8766   # mock demo mode
//   node tests/e2e/service_flow.js [base_url] [out_dir]
// URL import is walked too when E2E_URL_BASE names an https server with big.csv on it
// (the server must then run with the TEST-ONLY AGENTMETER_TEST_ALLOW_LOOPBACK_URLS=1 +
// AGENTMETER_TEST_URL_CAFILE for a loopback server — see docs/DEPLOY.md).
// Exits non-zero if any check fails. Screenshots go to out_dir (default: a temp dir).
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const ORIGIN = process.argv[2] || 'http://127.0.0.1:8766';
const BASE = ORIGIN + '/service';
const URL_BASE = process.env.E2E_URL_BASE || '';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'svc-e2e-'));
fs.mkdirSync(S, { recursive: true });
fs.writeFileSync(S + '/bad.csv', 'a,b\n1,2\n');
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

async function prepareUpload(p, file, flows) {
  await p.goto(BASE + '#/prepare'); await p.waitForSelector('#svc-upload-form');
  await p.setInputFiles('#svc-file', file);
  await p.fill('#svc-max-flows', String(flows));
  await p.click('#svc-upload-btn');
}
async function chooseAndRun(p, idx) {
  await p.waitForSelector('#svc-models input');
  const boxes = await p.$$('#svc-models input');
  for (const i of idx) await boxes[i].check();
  await p.click('#svc-run');
  await p.waitForSelector('#svc-verdict', { timeout: 180000 });
}

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const ctx = await b.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  p.on('console', m => { if (m.type() === 'error' && !/fonts|ERR_|Failed to load resource/.test(m.text())) errs.push(m.text()); });

  // 0. entry URLs + menu
  await p.goto(ORIGIN + '/service/prepare'); await p.waitForSelector('#view-prepare:not([hidden])');
  log(/#\/prepare$/.test(p.url()), '/service/prepare opens the Prepare step');
  log(await p.isVisible('#svc-demo'), 'demo-mode notice shown without a GPU');
  const navTxt = await p.textContent('.top nav');
  log(/Prepare data/.test(navTxt) && /Benchmark/.test(navTxt), 'menu has Prepare data + Benchmark');
  log(/not an analysis/.test(await p.textContent('#view-prepare .hero')), 'Prepare framed as data preparation, not analysis');

  // 1. Prepare (upload CSV) -> async job -> prepared set
  await prepareUpload(p, REPO + '/data/sample_csv/cicids2017_sample.csv', 12);
  await p.waitForFunction(() => location.hash.startsWith('#/job/') || location.hash.startsWith('#/prepared/'), null, { timeout: 15000 });
  log(true, 'upload returned a prepare job (async): ' + await p.evaluate(() => location.hash));
  await p.waitForSelector('#svc-summary-card', { timeout: 60000 });
  log(/^#\/prepared\/csv_runs\//.test(await p.evaluate(() => location.hash)), 'job done -> prepared-set page');
  const sum = await p.textContent('#svc-summary-card');
  log(/Labelled → accuracy \+ efficiency/.test(sum), 'summary: labelled -> accuracy + efficiency');
  log((await p.$$('#svc-class-table tbody tr')).length === 5, 'summary: 5-class table (in file / pool / selected)');
  log(/12 of 40/.test(sum), 'summary: flows selected 12 of 40');
  log(/all 43 rows of the file/.test(await p.textContent('#svc-sampling')), 'summary: sampling method stated');
  const dls = await p.$$eval('#svc-downloads a', as => as.map(a => a.textContent));
  log(dls.join(',') === 'features.csv,labels.csv,manifest.json', 'downloads: features.csv, labels.csv, manifest.json');
  const setId = await p.textContent('#svc-set-id-value');
  const files = {};
  for (const f of dls) {
    const [d] = await Promise.all([p.waitForEvent('download'), p.click(`#svc-downloads a[data-dl="${f}"]`)]);
    files[f] = S + '/' + f; await d.saveAs(files[f]);
  }
  const fhead = fs.readFileSync(files['features.csv'], 'utf8').split('\n')[0];
  log(!/(^|,)label(,|$)/i.test(fhead) && /Flow Duration/.test(fhead), 'features.csv has the 78 features and no label column');
  log(JSON.parse(fs.readFileSync(files['manifest.json'], 'utf8')).prepared_set.source.sha256.length === 64, 'manifest records the source sha256');
  await p.screenshot({ path: S + '/svc-prepared.png', fullPage: true });

  // 2. Benchmark this set -> models -> run -> results -> PDF
  await p.click('#svc-to-benchmark');
  await p.waitForSelector('#svc-run-set-card');
  log((await p.textContent('#svc-run-set-card')).includes(setId), 'benchmark page shows the prepared-set id');
  const boxes = await p.$$('#svc-models input');
  await boxes[0].check(); await boxes[3].check();
  log(await boxes[1].isDisabled() && await boxes[4].isDisabled(), 'UI blocks a 3rd model once 2 are selected');
  await p.click('#svc-run');
  await p.waitForFunction(() => location.hash.startsWith('#/job/'));
  const jobHash = await p.evaluate(() => location.hash);
  await p.waitForFunction(() => document.querySelector('#svc-job-status').textContent !== '…');
  await p.reload();
  await p.waitForSelector('#svc-verdict', { timeout: 180000 });
  log(await p.evaluate(() => location.hash) === jobHash, 'reload re-attached to the same benchmark job');
  log(/24 \/ 24 flows · done/.test(await p.textContent('#svc-progress-text')), 'progress 24/24 flows done');
  log((await p.$$('#svc-models-table tbody tr')).length === 2 && !!(await p.$('#svc-accuracy-table')), 'results: 2 models + accuracy by class');
  const [pdf] = await Promise.all([p.waitForEvent('download'), p.click('#svc-dl-pdf')]);
  const pdfPath = S + '/svc-report.pdf'; await pdf.saveAs(pdfPath);
  log(fs.readFileSync(pdfPath).subarray(0, 5).toString() === '%PDF-', 'PDF report downloads: ' + pdf.suggestedFilename());
  await p.screenshot({ path: S + '/svc-results.png', fullPage: true });

  // 3. Benchmark by prepared-set id
  await p.goto(BASE + '#/benchmark'); await p.waitForSelector('#svc-bench-id');
  await p.fill('#svc-bench-id', setId); await p.click('#svc-bench-id-btn');
  await p.waitForSelector('#svc-run-set-card');
  log((await p.evaluate(() => location.hash)).includes(setId.split('/')[1]), 'Benchmark accepts a prepared-set id');
  await p.goto(BASE + '#/benchmark'); await p.waitForSelector('#svc-bench-id');
  await p.fill('#svc-bench-id', 'csv_runs/does-not-exist'); await p.click('#svc-bench-id-btn');
  await p.waitForSelector('#svc-bench-id-error:not([hidden])');
  log(/not found/.test(await p.textContent('#svc-bench-id-error')), 'unknown prepared-set id rejected');

  // 4. Benchmark by re-uploading the downloaded files
  await p.goto(BASE + '#/benchmark'); await p.waitForSelector('#svc-import-form');
  await p.setInputFiles('#svc-imp-manifest', files['manifest.json']);
  await p.setInputFiles('#svc-imp-features', files['features.csv']);
  await p.click('#svc-import-btn');
  await p.waitForSelector('#svc-import-error:not([hidden])');
  log(/labels\.csv too/.test(await p.textContent('#svc-import-error')), 're-upload without labels.csv rejected with a reason');
  await p.setInputFiles('#svc-imp-labels', files['labels.csv']);
  await p.click('#svc-import-btn');
  await p.waitForSelector('#svc-run-set-card');
  log(!(await p.evaluate(() => location.hash)).includes(setId.split('/')[1]) && /_import_/.test(await p.evaluate(() => location.hash)),
      're-uploaded prepared set stored under a new id');
  await chooseAndRun(p, [1]);
  log(/Single-model run/.test(await p.textContent('#svc-verdict')), 're-uploaded set benchmarks (single model)');

  // 5. PCAP -> efficiency only
  await prepareUpload(p, REPO + '/data/sample_pcaps/sample_small.pcap', 8);
  await p.waitForSelector('#svc-summary-card', { timeout: 90000 });
  log(/Unlabelled → efficiency only/.test(await p.textContent('#svc-summary-card')) && !(await p.$('#svc-class-table')), 'PCAP prepared set: efficiency only');
  log((await p.$$eval('#svc-downloads a', as => as.map(a => a.textContent))).join(',') === 'features.csv,manifest.json', 'PCAP set has no labels.csv');
  await p.click('#svc-to-benchmark');
  await chooseAndRun(p, [2, 3]);
  log(!(await p.$('#svc-accuracy-table')) && /efficiency only/.test(await p.textContent('#svc-verdict')), 'PCAP results: efficiency only');

  // 6. bad file -> the prepare job fails with the reason
  await prepareUpload(p, S + '/bad.csv', 10);
  await p.waitForSelector('#svc-job-error:not([hidden])', { timeout: 30000 });
  log(/none of the 78/.test(await p.textContent('#svc-job-error')), 'bad CSV: prepare job fails with the reason');

  // 7. too large to upload -> pointed to URL import (client-side, nothing sent)
  const lim = await p.evaluate(() => fetch('/api/service/config').then(r => r.json()).then(c => c.limits));
  const bigPath = S + '/toobig.csv'; fs.writeFileSync(bigPath, Buffer.alloc(lim.max_upload_bytes + 1024, 97));
  await p.goto(BASE + '#/prepare'); await p.waitForSelector('#svc-file');
  await p.setInputFiles('#svc-file', bigPath); await p.click('#svc-upload-btn');
  await p.waitForSelector('#svc-upload-error:not([hidden])');
  log(/Import from URL/.test(await p.textContent('#svc-upload-error')), 'oversized upload points to Import from URL (limit ' + lim.max_upload_label + ')');
  log(/Import from URL/.test(await p.textContent('#svc-limits')), 'upload limit shown with the URL-import hint');

  // 8. Prepare by URL -> benchmark
  if (URL_BASE) {
    await p.goto(BASE + '#/prepare'); await p.waitForSelector('#svc-file');
    await p.check('#svc-src-url');
    log(await p.isVisible('#svc-url') && !(await p.isVisible('#svc-drop')), 'URL mode shows the link field');
    await p.fill('#svc-url', URL_BASE + '/big.csv'); await p.fill('#svc-max-flows', '20');
    await p.click('#svc-upload-btn');
    await p.waitForFunction(() => location.hash.startsWith('#/job/'), null, { timeout: 15000 });
    let sawPhase = '';
    for (let i = 0; i < 600 && !(await p.evaluate(() => location.hash.startsWith('#/prepared/'))); i++) {
      const ph = await p.evaluate(() => (document.querySelector('#svc-progress-text') || {}).getAttribute ? document.querySelector('#svc-progress-text').getAttribute('data-phase') : '');
      if (ph && !sawPhase.includes(ph)) sawPhase += ph + ' ';
      await p.waitForTimeout(250);
    }
    log(/downloading|sampling|validating|selecting/.test(sawPhase), 'URL prepare job showed live phases: ' + sawPhase.trim());
    await p.waitForSelector('#svc-summary-card', { timeout: 60000 });
    const us = await p.textContent('#svc-summary-card');
    log(/Imported from/.test(us) && /big\.csv/.test(us), 'URL prepared set records the source URL');
    log(/stratified random sample/.test(await p.textContent('#svc-sampling')), 'URL CSV sampled across the whole file');
    await p.screenshot({ path: S + '/svc-url-prepared.png', fullPage: true });
    await p.click('#svc-to-benchmark');
    await chooseAndRun(p, [0, 1]);
    log((await p.$$('#svc-models-table tbody tr')).length === 2, 'URL-prepared set benchmarks with 2 models');
    const [pdf2] = await Promise.all([p.waitForEvent('download'), p.click('#svc-dl-pdf')]);
    await pdf2.saveAs(S + '/svc-url-report.pdf');
    log(fs.readFileSync(S + '/svc-url-report.pdf').subarray(0, 5).toString() === '%PDF-', 'URL run PDF report downloads');
  } else {
    log(true, 'URL import walk skipped (E2E_URL_BASE not set)');
  }

  // 9. recent jobs lists both kinds; phone width
  await p.goto(BASE + '#/jobs'); await p.waitForSelector('#svc-jobs');
  const kinds = await p.$$eval('#svc-jobs tbody td:nth-child(2)', t => t.map(x => x.textContent));
  log(kinds.includes('prepare') && kinds.includes('benchmark'), 'recent jobs lists prepare and benchmark jobs');
  const m = await (await b.newContext({ viewport: { width: 375, height: 800 } })).newPage();
  await m.goto(BASE + '#/prepared/' + setId); await m.waitForSelector('#svc-summary-card', { timeout: 30000 });
  let sw = await m.evaluate(() => document.documentElement.scrollWidth);
  await m.goto(BASE + jobHash); await m.waitForSelector('#svc-verdict', { timeout: 30000 });
  sw = Math.max(sw, await m.evaluate(() => document.documentElement.scrollWidth));
  await m.goto(BASE + '#/benchmark'); await m.waitForSelector('#svc-import-form');
  sw = Math.max(sw, await m.evaluate(() => document.documentElement.scrollWidth));
  log(sw <= 375, 'no horizontal page scroll at 375px (max scrollWidth ' + sw + ')');
  await m.screenshot({ path: S + '/svc-mobile.png', fullPage: true });

  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  console.log(out.join('\n'));
  console.log('screenshots: ' + S);
  await b.close();
  if (out.some(l => l.startsWith('FAIL'))) process.exit(1);
})().catch(e => { console.log(out.join('\n')); console.log('CRASH', e.message); process.exit(1); });
