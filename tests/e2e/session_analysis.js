// Phase 45 — per-session full analysis view + stage-3 recommendation (Playwright).
// Mock CSV session (2 models) and mock PCAP session (1 model) -> Benchmark page recent
// sessions / Recent jobs -> "View full analysis" -> Overview + Detailed from that session
// -> PDF has the stage-3 section and the "Rules fired" table.
//   python scripts/serve.py --port 8766        (mock demo mode)
//   node tests/e2e/session_analysis.js [base_url] [out_dir]
const path = require('path'), os = require('os'), fs = require('fs'), cp = require('child_process');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const ORIGIN = process.argv[2] || 'http://127.0.0.1:8766';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'session-e2e-'));
fs.mkdirSync(S, { recursive: true });
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

function pdfText(file) {   // reportlab compresses its streams: read the text with pypdf
  return cp.execFileSync(process.env.PYTHON || 'python', ['-c',
    'import sys,pypdf;print("\\n".join(p.extract_text() for p in pypdf.PdfReader(sys.argv[1]).pages))', file]).toString();
}
async function session(p, file, flows, idx) {   // prepare -> benchmark -> results; returns the job id
  await p.goto(ORIGIN + '/service#/prepare'); await p.waitForSelector('#svc-upload-form');
  await p.setInputFiles('#svc-file', file); await p.fill('#svc-max-flows', String(flows));
  await p.click('#svc-upload-btn');
  await p.waitForSelector('#svc-summary-card', { timeout: 90000 });
  await p.click('#svc-to-benchmark'); await p.waitForSelector('#svc-models input');
  const boxes = await p.$$('#svc-models input');
  for (const i of idx) await boxes[i].check();
  await p.click('#svc-run');
  await p.waitForSelector('#svc-verdict', { timeout: 180000 });
  return (await p.evaluate(() => location.hash)).replace('#/job/', '');
}
async function checkAnalysis(p, job, labelled, nModels, tag) {
  await p.waitForSelector('#session-panel:not([hidden])', { timeout: 15000 });
  await p.waitForSelector('#saw-body tr');
  const banner = await p.textContent('#banner');
  log(/Session analysis \(non-validated\)/.test(banner), tag + ': banner says "Session analysis (non-validated)"');
  log(await p.$eval('#banner', el => [...el.querySelectorAll('a')].some(a => new URL(a.href).pathname === '/' && !new URL(a.href).search)),
      tag + ': banner links to the separate Validation baseline');
  log(new URL(p.url()).searchParams.get('session') === job, tag + ': page is rendered from job ' + job);
  const panel = await p.textContent('#session-panel');
  log(/Sample size:\s*\d+ flows × \d model/.test(panel), tag + ': sample size shown: ' + (panel.match(/Sample size:[^M]*/) || [''])[0].trim());
  log((await p.$$('#session-caveats li')).length > 0, tag + ': caveats listed');
  log(/Recommendation — stage 3/.test(panel) && !!(await p.$('#session-stage3')), tag + ': stage-3 verdict + notes shown');
  log((await p.$$('#saw-body tr')).length === nModels, tag + ': Overview ranks exactly the session\'s ' + nModels + ' model(s)');
  const accHidden = await p.evaluate(() => document.body.classList.contains('no-accuracy'));
  log(accHidden === !labelled, tag + ': accuracy panels ' + (labelled ? 'shown (labelled)' : 'hidden (unlabelled)'));
  if (!labelled) {
    const miss = await p.$$eval('.acc-missing', els => els.filter(e => e.offsetParent !== null).map(e => e.textContent).join(' '));
    log(/not measured for unlabelled input/i.test(miss), tag + ': says "accuracy not measured for unlabelled input"');
    log(!/\b0(\.0)?%/.test(await p.textContent('#view-overview .hcard:not(.acc-missing)') || ''), tag + ': no fake 0% accuracy');
  }
  await p.screenshot({ path: S + '/' + tag + '-overview.png', fullPage: true });
  await p.click('#nav [data-view="detailed"]');
  log(await p.isVisible('#view-detailed'), tag + ': Detailed analysis opens');
  if (!labelled) log(await p.isVisible('#view-detailed .acc-missing'), tag + ': Detailed accuracy panels replaced by the not-measured note');
  await p.screenshot({ path: S + '/' + tag + '-detailed.png', fullPage: true });
}

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const ctx = await b.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  p.on('console', m => { if (m.type() === 'error' && !/fonts|ERR_|Failed to load resource/.test(m.text())) errs.push(m.text()); });

  // 1. labelled CSV, 2 models
  const csvJob = await session(p, REPO + '/data/sample_csv/cicids2017_sample.csv', 10, [0, 3]);
  log(await p.isVisible('#svc-stage3'), 'CSV results page shows the stage-3 card');
  log(/Measured head-to-head verdict|verdict/i.test(await p.textContent('#svc-stage3')), 'stage-3 card starts with the measured verdict');
  log(!!(await p.$('#svc-full-analysis')), 'results page has "View full analysis"');
  const [pdf] = await Promise.all([p.waitForEvent('download'), p.click('#svc-dl-pdf')]);
  await pdf.saveAs(S + '/csv-report.pdf');
  const txt = pdfText(S + '/csv-report.pdf');
  log(/Recommendation — stage 3/.test(txt) && /Measured head-to-head verdict/.test(txt), 'PDF has the stage-3 section, verdict first');
  log(/Rules fired/.test(txt) && /gated_access/.test(txt) && /vram_headroom/.test(txt), 'PDF has the "Rules fired" table with rule ids');
  log(/never changes any score/.test(txt), 'PDF states metadata never changes a score');

  // 2. unlabelled PCAP, 1 model
  const pcapJob = await session(p, REPO + '/data/sample_pcaps/sample_small.pcap', 8, [2]);
  log(await p.isVisible('#svc-stage3'), 'PCAP results page shows the stage-3 card');

  // 3. Benchmark page -> recent sessions -> View full analysis (CSV)
  await p.goto(ORIGIN + '/service#/benchmark'); await p.waitForSelector('#svc-sessions-table');
  const listed = await p.$$eval('#svc-sessions-table a[data-analysis]', as => as.map(a => a.getAttribute('data-analysis')));
  log(listed.includes(csvJob) && listed.includes(pcapJob), 'Benchmark page lists both finished sessions with "View full analysis"');
  await p.click(`#svc-sessions-table a[data-analysis="${csvJob}"]`);
  await checkAnalysis(p, csvJob, true, 2, 'csv');

  // 4. Recent jobs -> View full analysis (PCAP)
  await p.goto(ORIGIN + '/service#/jobs'); await p.waitForSelector(`a[data-analysis="${pcapJob}"]`, { timeout: 15000 });
  const rows = await p.$$eval('#svc-jobs tbody tr', trs => trs.map(tr => [tr.cells[1].textContent, !!tr.querySelector('a[data-analysis]')]));
  log(rows.some(r => r[0] === 'prepare') && rows.every(r => r[1] === (r[0] === 'benchmark')),
      'Recent jobs: every finished benchmark job (and no prepare job) has "View full analysis"');
  await p.click(`a[data-analysis="${pcapJob}"]`);
  await checkAnalysis(p, pcapJob, false, 1, 'pcap');

  // 5. the Validation baseline is still the baseline; a bad session id is refused
  await p.goto(ORIGIN + '/'); await p.waitForSelector('#saw-body tr');
  log((await p.$$('#saw-body tr')).length === 5 && await p.isHidden('#session-panel'), 'Validation baseline unchanged (5 models, no session panel)');
  await p.goto(ORIGIN + '/?session=job_20990101_000000_abcdef'); await p.waitForTimeout(1500);
  log(/Session analysis unavailable/.test(await p.textContent('#banner')), 'unknown session id: "Session analysis unavailable"');
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  console.log(out.join('\n')); await b.close();
  if (out.some(l => l.startsWith('FAIL'))) process.exit(1);
})().catch(e => { console.log(out.join('\n')); console.log('CRASH', e.message); process.exit(1); });
