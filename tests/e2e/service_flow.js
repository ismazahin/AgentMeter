// Phase 41 — headless walk-through of the benchmark service (Playwright).
// Needs a LIVE server (one process) and Node Playwright; not part of pytest.
//   python scripts/pull_eval_server.py --port 8766        # no GPU -> mock demo mode
//   node tests/e2e/service_flow.js [base_url] [out_dir]
// Exits non-zero if any check fails. Screenshots go to out_dir (default: a temp dir).
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const BASE = (process.argv[2] || 'http://127.0.0.1:8766') + '/service';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'svc-e2e-'));
fs.writeFileSync(S + '/bad.csv', 'a,b\n1,2\n');
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);
(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  p.on('console', m => { if (m.type() === 'error' && !/fonts|ERR_|Failed to load resource/.test(m.text())) errs.push(m.text()); });

  // 1. upload CSV
  await p.goto(BASE); await p.waitForSelector('#svc-upload-form');
  log(await p.isVisible('#svc-demo'), 'demo-mode notice shown without a GPU');
  await p.setInputFiles('#svc-file', REPO + '/data/sample_csv/cicids2017_sample.csv');
  await p.fill('#svc-max-flows', '12');
  await p.click('#svc-upload-btn');
  await p.waitForSelector('#svc-summary-card', { timeout: 30000 });
  const sum = await p.textContent('#svc-summary-card');
  log(/Labelled → accuracy \+ efficiency/.test(sum), 'summary: labelled -> accuracy + efficiency');
  log((await p.$$('#svc-class-table tbody tr')).length === 5, 'summary: 5-class distribution table');
  log(/12 of 40/.test(sum), 'summary: flows selected 12 of 40');
  log(/exact/.test(sum) && /class_balance/.test(await p.innerHTML('#svc-rules-table')), 'summary: feature match + rules table');
  await p.screenshot({ path: S + '/svc-summary.png', fullPage: true });

  // 2. model cap
  const boxes = await p.$$('#svc-models input');
  await boxes[0].check(); await boxes[3].check();
  log(await boxes[1].isDisabled() && await boxes[4].isDisabled(), 'UI blocks a 3rd model once 2 are selected');
  log(!(await p.isDisabled('#svc-run')), 'Run enabled with 2 models');
  await p.click('#svc-run');
  await p.waitForFunction(() => location.hash.startsWith('#/job/'));
  const jobHash = await p.evaluate(() => location.hash);
  log(true, 'Run created a job: ' + jobHash);

  // 3. progress + reload re-attach
  await p.waitForFunction(() => document.querySelector('#svc-job-status').textContent !== '…');
  const st1 = await p.textContent('#svc-job-status');
  await p.reload();
  await p.waitForFunction(() => document.querySelector('#svc-job-status').textContent !== '…');
  log(await p.evaluate(() => location.hash) === jobHash, 'reload kept the same job in the URL (status before reload: ' + st1 + ')');
  await p.waitForSelector('#svc-verdict', { timeout: 120000 });
  const prog = await p.textContent('#svc-progress-text');
  log(/24 \/ 24 flows · done/.test(prog), 'progress shows 24/24 flows done: ' + prog);
  log((await p.textContent('#svc-job-status')) === 'done', 'status done');

  // 4. results
  log((await p.$$('#svc-models-table tbody tr')).length === 2, 'results: per-model table has 2 models');
  log(!!(await p.$('#svc-relative-table')), 'results: head-to-head table');
  log(!!(await p.$('#svc-absolute')), 'results: absolute SAW statement');
  log(!!(await p.$('#svc-accuracy-table')), 'results: accuracy by class (labelled)');
  const cav = await p.textContent('#svc-caveats');
  log(/NON-VALIDATED/.test(cav) && /Balanced sample/.test(cav), 'results: caveats carried');
  await p.screenshot({ path: S + '/svc-results.png', fullPage: true });
  const [dl] = await Promise.all([p.waitForEvent('download'), p.click('#svc-dl-saw')]);
  log(/saw\.csv$/.test(dl.suggestedFilename()), 'SAW CSV export via report.js');
  const [pdf] = await Promise.all([p.waitForEvent('download'), p.click('#svc-dl-pdf')]);
  const pdfPath = S + '/svc-report.pdf'; await pdf.saveAs(pdfPath);
  const magic = fs.readFileSync(pdfPath).subarray(0, 5).toString();
  log(/\.pdf$/.test(pdf.suggestedFilename()) && magic === '%PDF-', 'Download PDF report gives a real PDF: ' + pdf.suggestedFilename());

  // reopen from recent jobs in a NEW page (close browser, come back)
  const p2 = await (await b.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  await p2.goto(BASE + '#/jobs'); await p2.waitForSelector('#svc-jobs');
  log((await p2.textContent('#svc-jobs')).includes(jobHash.split('/')[2]), 'recent jobs lists the job');
  await p2.click('#svc-jobs a'); await p2.waitForSelector('#svc-verdict', { timeout: 30000 });
  log(true, 'reopened past result from recent jobs');

  // 5. PCAP -> efficiency only
  await p.goto(BASE + '#/upload'); await p.waitForSelector('#svc-file');
  await p.setInputFiles('#svc-file', REPO + '/data/sample_pcaps/sample_small.pcap');
  await p.fill('#svc-max-flows', '8');
  await p.click('#svc-upload-btn');
  await p.waitForSelector('#svc-summary-card', { timeout: 60000 });
  const ps = await p.textContent('#svc-summary-card');
  log(/Unlabelled → efficiency only/.test(ps) && !(await p.$('#svc-class-table')), 'PCAP summary: efficiency only, no class table');
  const pb = await p.$$('#svc-models input'); await pb[2].check(); await pb[3].check();
  await p.click('#svc-run');
  await p.waitForSelector('#svc-verdict', { timeout: 120000 });
  const head = await p.textContent('#svc-models-table thead');
  log(!/Accuracy/.test(head) && !(await p.$('#svc-accuracy-table')), 'PCAP results: no accuracy column/table');
  log(/efficiency only/.test(await p.textContent('#svc-verdict')), 'PCAP verdict says efficiency only');

  // 6. bad file
  await p.goto(BASE + '#/upload'); await p.waitForSelector('#svc-file');
  await p.setInputFiles('#svc-file', S + '/bad.csv');
  await p.click('#svc-upload-btn');
  await p.waitForSelector('#svc-upload-error:not([hidden])');
  log(/none of the 78/.test(await p.textContent('#svc-upload-error')), 'bad CSV rejected with reason');

  // 7. phone width
  const m = await (await b.newContext({ viewport: { width: 375, height: 800 } })).newPage();
  await m.goto(BASE + jobHash); await m.waitForSelector('#svc-verdict', { timeout: 30000 });
  const sw = await m.evaluate(() => document.documentElement.scrollWidth);
  log(sw <= 375, 'no horizontal page scroll at 375px (scrollWidth ' + sw + ')');
  await m.screenshot({ path: S + '/svc-mobile.png', fullPage: true });

  // agent rows come back in pipeline order (the API JSON may have sorted keys)
  const agentsOrder = await m.$$eval('#svc-agent-table tbody td:first-child', tds => tds.map(t => t.textContent));
  log(agentsOrder.join(',') === 'perceive,reason,decide,act', 'per-agent rows in pipeline order: ' + agentsOrder);

  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  console.log(out.join('\n'));
  console.log('screenshots: ' + S);
  await b.close();
  if (out.some(l => l.startsWith('FAIL'))) process.exit(1);
})().catch(e => { console.log(out.join('\n')); console.log('CRASH', e.message); process.exit(1); });
