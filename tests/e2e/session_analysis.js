// Phases 45/46 — a session's full analysis (Overview + Detailed) now lives in the app's
// Detailed-analysis tab (embedded /analysis?session=<id>&embed=1). Old "View full analysis"
// links (/?session=<id>) redirect there. CSV (2 models) and PCAP (1 model, accuracy hidden);
// the PDF has the stage-3 section and the "Rules fired" table.
//   python scripts/serve.py --port 8766        (mock demo mode, fresh results dir)
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
const pdfText = f => cp.execFileSync(process.env.PYTHON || 'python', ['-c',
  'import sys,pypdf;print("\\n".join(p.extract_text() for p in pypdf.PdfReader(sys.argv[1]).pages))', f]).toString();

async function session(p, file, flows, idx) {
  await p.goto(ORIGIN + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
  await p.setInputFiles('#svc-file', file); await p.fill('#svc-max-flows', String(flows));
  await p.click('#svc-upload-btn');
  await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 30000 });
  await p.waitForSelector('#svc-models input');
  const boxes = await p.$$('#svc-models input');
  for (const i of idx) await boxes[i].check();
  await p.click('#svc-run');
  await p.waitForFunction(() => /^#\/session\//.test(location.hash), null, { timeout: 180000 });
  await p.waitForSelector('#session-body:not([hidden])', { timeout: 30000 });
  return decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[2]);
}
async function embedded(p, job, labelled, nModels, tag) {
  await p.goto(ORIGIN + '/?session=' + job);                        // an old "View full analysis" link
  await p.waitForFunction(j => location.pathname === '/' && location.hash === '#/session/' + j + '/detailed', job, { timeout: 15000 });
  log(true, tag + ': old /?session= link redirects to the app\'s Detailed-analysis tab');
  await p.waitForSelector('#analysis-frame');
  const fr = await (await p.$('#analysis-frame')).contentFrame();
  await fr.waitForSelector('#saw-body tr', { timeout: 20000 });
  log((await fr.$$('#saw-body tr')).length === nModels, tag + ': embedded Overview ranks the session\'s ' + nModels + ' model(s)');
  log(/Session analysis \(non-validated\)/.test(await fr.textContent('#ov-title')), tag + ': labelled Session analysis (non-validated)');
  const noAcc = await fr.evaluate(() => document.body.classList.contains('no-accuracy'));
  log(noAcc === !labelled, tag + ': accuracy panels ' + (labelled ? 'shown (labelled)' : 'hidden (unlabelled)'));
  if (!labelled) {
    const miss = await fr.$$eval('.acc-missing', els => els.filter(e => e.offsetParent !== null).map(e => e.textContent).join(' '));
    log(/not measured for unlabelled input/i.test(miss), tag + ': "accuracy not measured for unlabelled input"');
  }
  await fr.click('#nav [data-view="detailed"]');
  log(await fr.isVisible('#view-detailed'), tag + ': embedded Detailed analysis opens (in-page switch)');
  await p.screenshot({ path: S + '/' + tag + '-detailed.png', fullPage: true });
}

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const p = await (await b.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  p.on('console', m => { if (m.type() === 'error' && !/fonts|ERR_|Failed to load resource/.test(m.text())) errs.push(m.text()); });

  const csvJob = await session(p, REPO + '/data/sample_csv/cicids2017_sample.csv', 10, [0, 3]);
  await p.click('#session-tabs [data-tab="recommendation"]'); await p.waitForSelector('#spanel-recommendation:not([hidden])');
  log(await p.isVisible('#svc-stage3') && /Measured verdict/.test(await p.textContent('#svc-stage3')), 'Recommendation tab: stage 3, measured verdict first');
  const [pdf] = await Promise.all([p.waitForEvent('download'), p.click('#svc-dl-pdf')]);
  await pdf.saveAs(S + '/csv-report.pdf');
  const txt = pdfText(S + '/csv-report.pdf');
  log(/Recommendation — stage 3/.test(txt) && /Measured head-to-head verdict/.test(txt), 'PDF has the stage-3 section, verdict first');
  log(/Rules fired/.test(txt) && /gated_access/.test(txt) && /custom_licence/.test(txt), 'PDF has the "Rules fired" table with rule ids');
  log(/never changes any score/.test(txt), 'PDF states metadata never changes a score');
  const pcapJob = await session(p, REPO + '/data/sample_pcaps/sample_small.pcap', 8, [2]);

  await embedded(p, csvJob, true, 2, 'csv');
  await embedded(p, pcapJob, false, 1, 'pcap');

  await p.goto(ORIGIN + '/#/session/job_20990101_000000_abcdef');
  await p.waitForSelector('#session-watch .notice.error', { timeout: 15000 });
  log(/no such job/i.test(await p.textContent('#session-watch')), 'unknown session id: a clear error');
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  console.log(out.join('\n')); await b.close();
  if (out.some(l => l.startsWith('FAIL'))) process.exit(1);
})().catch(e => { console.log(out.join('\n')); console.log('CRASH', e.message); process.exit(1); });
