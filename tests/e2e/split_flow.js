// Phase E — the static front-end and the backend on DIFFERENT origins (Playwright).
//   node tests/e2e/split_flow.js offline <front_url>
//       backend NOT running: landing + "GPU backend offline", no broken forms
//   node tests/e2e/split_flow.js online <front_url> <passcode> [blocked_front_url] [url_import_base]
//       backend running with AGENTMETER_ALLOWED_ORIGINS=<front origin> and AGENTMETER_PASSCODE:
//       badge, passcode prompt, Prepare (upload + URL) -> Benchmark -> PDF, CORS blocks other origins
// front_url serves web/ with a config.json whose api_base is the backend URL.
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const [, , PHASE, FRONT, PASS, BLOCKED, URL_BASE] = process.argv;
const REPO = path.resolve(__dirname, '..', '..');
const S = process.env.E2E_OUT || fs.mkdtempSync(path.join(os.tmpdir(), 'split-e2e-'));
fs.mkdirSync(S, { recursive: true });
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const ctx = await b.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));

  if (PHASE === 'offline') {
    await p.goto(FRONT); await p.waitForSelector('#view-offline:not([hidden])', { timeout: 15000 });
    log(/GPU backend offline/.test(await p.textContent('#svc-backend-badge')), 'badge says GPU backend offline');
    log(/GPU backend offline/.test(await p.textContent('#svc-offline-notice')), 'clear offline notice');
    log(/measures/.test(await p.textContent('#view-offline .hero')) && /not a threat-detection/.test(await p.textContent('#view-offline .hero')),
        'landing explains the service (measures LLM efficiency, not threat detection)');
    log(await p.isHidden('#view-prepare') && await p.isHidden('#svc-upload-form'), 'no broken Prepare form while offline');
    log(await p.isHidden('#svc-steps'), 'step bar hidden while offline');
    await p.goto(FRONT + '#/benchmark'); await p.waitForSelector('#view-offline:not([hidden])');
    log(await p.isHidden('#view-benchmark'), 'Benchmark page also falls back to the offline landing');
    await p.screenshot({ path: S + '/offline.png', fullPage: true });
    const m = await (await b.newContext({ viewport: { width: 375, height: 800 } })).newPage();
    await m.goto(FRONT); await m.waitForSelector('#view-offline:not([hidden])');
    const sw = await m.evaluate(() => document.documentElement.scrollWidth);
    log(sw <= 375, 'offline landing fits 375px (scrollWidth ' + sw + ')');
    await m.screenshot({ path: S + '/offline-mobile.png', fullPage: true });
  } else {
    // 1. online: badge + mode line before anyone clicks Run
    await p.goto(FRONT); await p.waitForSelector('#svc-upload-form:not([hidden])', { timeout: 15000 });
    log(/DEMO \(mock\)/.test(await p.textContent('#svc-backend-badge')), 'badge says DEMO (mock) on a mock backend');
    log(await p.isHidden('#view-offline'), 'offline landing hidden when the backend answers');
    const apiBase = await p.evaluate(() => fetch('config.json').then(r => r.json()).then(c => c.api_base));
    log(new URL(apiBase).origin !== new URL(FRONT).origin, 'front-end and backend are different origins: ' + new URL(FRONT).origin + ' -> ' + apiBase);

    // 2. Prepare (upload) -> passcode prompt (wrong, then right) -> prepared set
    await p.setInputFiles('#svc-file', REPO + '/data/sample_csv/cicids2017_sample.csv');
    await p.fill('#svc-max-flows', '12');
    await p.click('#svc-upload-btn');
    await p.waitForSelector('#svc-pass-dialog[open]', { timeout: 10000 });
    log(true, 'passcode asked before the first Prepare');
    await p.fill('#svc-pass-input', 'wrong-passcode'); await p.click('#svc-pass-ok');
    await p.waitForSelector('#svc-pass-dialog[open] #svc-pass-error:not([hidden])', { timeout: 10000 });
    log(/wrong passcode/.test(await p.textContent('#svc-pass-error')), 'a wrong passcode is refused and asked again');
    await p.fill('#svc-pass-input', PASS); await p.click('#svc-pass-ok');
    await p.waitForSelector('#svc-summary-card', { timeout: 60000 });
    log(/^#\/prepared\//.test(await p.evaluate(() => location.hash)), 'cross-origin Prepare job -> prepared set');
    const dl = await p.$$eval('#svc-downloads a', as => as.map(a => a.href));
    log(dl.every(h => h.startsWith(apiBase)), 'prepared-set downloads point straight at the backend');
    const [d] = await Promise.all([p.waitForEvent('download'), p.click('#svc-downloads a[data-dl="features.csv"]')]);
    await d.saveAs(S + '/features.csv');
    log(/Flow Duration/.test(fs.readFileSync(S + '/features.csv', 'utf8').split('\n')[0]), 'features.csv downloads cross-origin');

    // 3. Benchmark -> results -> PDF (no second passcode prompt: kept for the session)
    await p.click('#svc-to-benchmark'); await p.waitForSelector('#svc-models input');
    log(/DEMO \(mock\)/.test(await p.textContent('#svc-run-mode')), 'run-mode line says DEMO (mock) next to Run');
    const boxes = await p.$$('#svc-models input'); await boxes[2].check(); await boxes[3].check();
    await p.click('#svc-run');
    await p.waitForSelector('#svc-verdict', { timeout: 180000 });
    log(!(await p.isVisible('#svc-pass-dialog[open]')), 'passcode reused within the browser session');
    log(/DEMO \(mock\)/.test(await p.textContent('#svc-env')), 'results carry the DEMO (mock) environment');
    const pdfHref = await p.getAttribute('#svc-dl-pdf', 'href');
    log(pdfHref.startsWith(apiBase), 'PDF link goes straight to the backend');
    const [pdf] = await Promise.all([p.waitForEvent('download'), p.click('#svc-dl-pdf')]);
    await pdf.saveAs(S + '/report.pdf');
    log(fs.readFileSync(S + '/report.pdf').subarray(0, 5).toString() === '%PDF-', 'PDF downloads cross-origin');
    const [js] = await Promise.all([p.waitForEvent('download'), p.click('#svc-dl-json')]);
    log(/session_results\.json$/.test(js.suggestedFilename()), 'results JSON download');
    await p.screenshot({ path: S + '/online-results.png', fullPage: true });

    // 4. a finished result opens WITHOUT the passcode (fresh browser = no session passcode)
    const jobHash = await p.evaluate(() => location.hash);
    const p2 = await (await b.newContext()).newPage();
    await p2.goto(FRONT + jobHash); await p2.waitForSelector('#svc-verdict', { timeout: 30000 });
    log(!(await p2.isVisible('#svc-pass-dialog[open]')), 'finished results open by their job URL without a passcode');

    // 5. Prepare by URL (cross-origin), optional
    if (URL_BASE) {
      await p.goto(FRONT + '#/prepare'); await p.waitForSelector('#svc-upload-form:not([hidden])');
      await p.check('#svc-src-url'); await p.fill('#svc-url', URL_BASE + '/big.csv'); await p.fill('#svc-max-flows', '10');
      await p.click('#svc-upload-btn');
      await p.waitForSelector('#svc-summary-card', { timeout: 180000 });
      log(/Imported from/.test(await p.textContent('#svc-summary-card')), 'URL import works cross-origin');
      await p.click('#svc-to-benchmark'); await p.waitForSelector('#svc-models input');
      const bx = await p.$$('#svc-models input'); await bx[0].check();
      await p.click('#svc-run'); await p.waitForSelector('#svc-verdict', { timeout: 180000 });
      const [pdf2] = await Promise.all([p.waitForEvent('download'), p.click('#svc-dl-pdf')]);
      await pdf2.saveAs(S + '/report-url.pdf');
      log(fs.readFileSync(S + '/report-url.pdf').subarray(0, 5).toString() === '%PDF-', 'URL-prepared set -> Benchmark -> PDF');
    }

    // 6. an origin that is NOT allowed cannot use the backend (CORS)
    if (BLOCKED) {
      const p3 = await (await b.newContext()).newPage();
      await p3.goto(BLOCKED); await p3.waitForSelector('#view-offline:not([hidden])', { timeout: 15000 });
      log(true, 'a front-end on a non-allowed origin is blocked by CORS (shows offline): ' + new URL(BLOCKED).origin);
    }
  }
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  console.log(out.join('\n')); console.log('screenshots: ' + S);
  await b.close();
  if (out.some(l => l.startsWith('FAIL'))) process.exit(1);
})().catch(e => { console.log(out.join('\n')); console.log('CRASH', e.message); process.exit(1); });
