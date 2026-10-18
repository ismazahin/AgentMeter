// Model catalog phase 2 — walk-through of the model picker (Playwright, mock backend).
// Step 2 of New benchmark lists the study models and "More models to try" (extra models of
// configs/model_catalog.yaml with their suitability). Pick an extra + a study model, run, and
// check the "extra" label on the session page, the PDF, the Leaderboard and Settings.
//   AGENTMETER_TEST_MODEL_REPOS=tests/fixtures/model_repos.json AGENTMETER_RESULTS_DIR=$(mktemp -d) \
//     python scripts/serve.py --provider mock --port 8766
//   node tests/e2e/model_picker_flow.js [base_url] [out_dir]
const path = require('path'), os = require('os'), fs = require('fs'), cp = require('child_process');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const BASE = process.argv[2] || 'http://127.0.0.1:8766';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'picker-e2e-'));
fs.mkdirSync(S, { recursive: true });
const CSV = path.join(REPO, 'data', 'sample_csv', 'cicids2017_sample.csv');
const EXTRA = 'Qwen/Qwen2.5-3B-Instruct';
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const ctx = await b.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: 'dark' });
  const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  try {
    // 1. upload, then step 2 shows both model groups
    await p.goto(BASE + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])', { timeout: 15000 });
    await p.setInputFiles('#svc-file', CSV); await p.fill('#svc-max-flows', '5'); await p.click('#svc-upload-btn');
    await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 30000 });
    await p.waitForSelector('#svc-models input');
    log((await p.$$('#svc-models input')).length === 5, 'Study models: the 5 validated models are listed');
    await p.waitForSelector('#svc-extra:not([hidden])', { timeout: 15000 });
    const count = await p.textContent('#svc-extra-count');
    log(count === '(6)', 'More models to try: 6 offered (' + count + ')');
    log(/1 more listed model is not available here \(model info unavailable\)/.test(await p.textContent('#svc-extra-note')),
        'A model whose Hugging Face info is unavailable is not offered, and the note says so');
    await p.click('#svc-extra > summary');
    const card = await p.textContent(`label:has(input[value="${EXTRA}"])`);
    log(/3\.1 B params/.test(card) && /GB VRAM at 4-bit/.test(card) && /original weights → 4-bit NF4/.test(card) && /First use downloads ~6\.2 GB/.test(card),
        'Model card: size, 4-bit VRAM estimate, original weights → 4-bit NF4, first download size');
    log(/custom licence/.test(card), 'Model card: the rule warning (restrictive licence) is shown');
    await p.screenshot({ path: S + '/01-picker.png', fullPage: true });

    // 2. pick an extra + a study model; a third is disabled (max 2)
    await p.check(`input[value="${EXTRA}"]`);
    await p.check('#svc-models input[value="Qwen/Qwen2.5-7B-Instruct"]');
    log(await p.isDisabled('#svc-models input[value="google/gemma-2-9b-it"]') && await p.isDisabled('#svc-extra-models input:not(:checked)'),
        'Max 2 models across both groups: the others are disabled');
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
    const job = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[3]);
    await p.waitForFunction(id => location.hash.indexOf('#/session/' + id) === 0, job, { timeout: 180000 });
    await p.waitForSelector('#session-body:not([hidden])', { timeout: 30000 });

    // 3. the "extra" label: session page, agents, PDF
    const row = await p.textContent(`#svc-models-table tr:has(td:text-is("${EXTRA}extra")), #svc-models-table tbody`);
    log(/Qwen2\.5-3B-Instruct\s*extra/.test(row) && !/Qwen2\.5-7B-Instruct\s*extra/.test(row), 'Per-model table: the extra model is labelled "extra", the study model is not');
    await p.screenshot({ path: S + '/02-session.png', fullPage: true });
    await p.click('#session-tabs [data-tab="agents"]'); await p.waitForSelector('#stab-agents[aria-selected="true"]');
    log((await p.$$('.agents-model h2 .extra-tag')).length === 1, 'Agent Analysis: one model card carries the "extra" label');
    const pdf = path.join(S, 'report.pdf');
    fs.writeFileSync(pdf, Buffer.from(await (await p.request.get(BASE + '/api/jobs/' + encodeURIComponent(job) + '/report.pdf')).body()));
    const text = cp.execFileSync(process.env.PYTHON || 'python', ['-c',
      'import sys,pypdf;print(" ".join(p.extract_text() for p in pypdf.PdfReader(sys.argv[1]).pages))', pdf]).toString().replace(/\s+/g, ' ');
    log(/Qwen\/Qwen2\.5-3B-Instruct \(extra model, not in the validated baseline\)/.test(text), 'PDF: "extra model, not in the validated baseline"');

    // 3b. the progress page during a first-use download (job JSON faked: demo mode never downloads)
    const fake = 'job_20261018_120000_' + 'ab'.repeat(16);
    await p.route('**/api/jobs/' + fake, r => r.fulfill({ contentType: 'application/json', body: JSON.stringify({
      job_id: fake, status: 'running', models: [EXTRA, 'Qwen/Qwen2.5-7B-Instruct'], run_name: 'csv_runs/x',
      progress: { completed: 0, total: 10, per_model: {}, current_model: null, percent: 0 },
      model_download: { model: EXTRA, index: 1, count: 1, phase: 'downloading', done_bytes: 2.5 * 1073741824, total_bytes: 6.2 * 1073741824 } }) }));
    await p.goto(BASE + '/#/new/run/' + fake);
    await p.waitForFunction(() => /Downloading model/.test((document.getElementById('svc-job-phase') || {}).textContent || ''), null, { timeout: 15000 });
    const dtext = await p.textContent('#svc-progress-text');
    log(/Downloading Qwen2\.5-3B-Instruct — 2\.5 \/ 6\.2 GB · 40%/.test(dtext) && /not measured/.test(dtext),
        'Progress page: "Downloading model" with GB done / total and %, before the benchmark (' + dtext.slice(0, 70) + ')');
    await p.waitForTimeout(700);                                       // the bar animates (.4 s)
    log(await p.evaluate(() => document.getElementById('svc-progress-bar').style.width) === '40%', 'Progress bar shows the download %');
    await p.screenshot({ path: S + '/03-downloading.png', fullPage: true });
    await p.unroute('**/api/jobs/' + fake);

    // 4. Leaderboard and Settings
    await p.goto(BASE + '/#/leaderboard'); await p.waitForSelector('.lb-table');
    log((await p.$$('.lb-table .extra-tag')).length === 1, 'Leaderboard: the extra model is labelled');
    await p.goto(BASE + '/#/settings'); await p.waitForSelector('#view-settings:not([hidden])');
    log(true, 'Settings opens');
  } catch (e) {
    log(false, 'walk-through crashed: ' + e.message);
    await p.screenshot({ path: S + '/crash.png', fullPage: true }).catch(() => {});
  }
  log(!errs.length, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  await b.close();
  console.log(out.join('\n'));
  const failed = out.filter(l => l.startsWith('FAIL')).length;
  console.log(`\n${out.length - failed} passed, ${failed} failed  (${S})`);
  process.exit(failed ? 1 : 0);
})();
