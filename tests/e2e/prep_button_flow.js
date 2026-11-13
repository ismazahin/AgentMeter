// Continue starts the preparation service (Playwright; run via
//   PREP_REGISTERED=1 CLOUDPREP_FLOWS=prep_button_flow bash tests/e2e/cloudprep_stack.sh):
// the control plane has NO fixed service address -> Continue is enabled anyway -> the click opens
// agentmeter-prep://start and the page says it is starting -> the service registers itself (here the
// test does what prep_local.py does: a signed POST /api/prep/register) -> the page notices and carries
// on with the same upload by itself, through the REGISTERED address -> Stop -> the service says it is
// stopping -> the page hides the running line. The agentmeter-prep:// link itself is not followed
// (the test PC has no such program).
const path = require('path'), fs = require('fs'), crypto = require('crypto');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const REPO = path.resolve(__dirname, '..', '..');
const OUT = process.argv[2] || fs.mkdtempSync('/tmp/prep-button-e2e-');
fs.mkdirSync(OUT, { recursive: true });
const PP = process.env.PP || 8080, CPP = process.env.CPP || 8790;
const APP = `http://127.0.0.1:${PP}`, WORKER = process.env.CP_URL, SECRET = process.env.PREP_SECRET;
const CSV = path.join(REPO, 'data', 'sample_csv', 'cicids2017_sample.csv');
const out = []; const log = (ok, msg) => { out.push((ok ? 'PASS ' : 'FAIL ') + msg); console.log((ok ? 'PASS ' : 'FAIL ') + msg); };

async function asService(route, payload) {                 // what scripts/prep_local.py sends
  const p = `/api/prep/${route}`, body = JSON.stringify(payload), ts = String(Math.floor(Date.now() / 1000));
  const sig = crypto.createHmac('sha256', SECRET).update(`POST\n${p}\n${ts}\n${crypto.createHash('sha256').update(body).digest('hex')}`).digest('hex');
  const r = await fetch(WORKER + p, { method: 'POST', body, headers: { 'content-type': 'application/json', 'X-AM-Timestamp': ts, 'X-AM-Signature': sig } });
  return r.status;
}

(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1280, height: 1000 } })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  try {
    await p.goto(APP + '/app.html'); await p.waitForSelector('#login-form:not([hidden])');
    await p.fill('#login-user', 'admin'); await p.fill('#login-pass', 'admin-password-123'); await p.click('#login-go');
    await p.waitForSelector('#view-home:not([hidden])');
    await p.goto(APP + '/app.html#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])', { timeout: 20000 });
    await p.waitForTimeout(1500);
    log(!(await p.$eval('#svc-upload-btn', e => e.disabled)) && !(await p.$('#svc-other-attack')) && !/not running/.test(await p.textContent('#view-new')),
        'no service: Continue is enabled, with no Start button, no warning and no Other Attack option');
    await p.screenshot({ path: path.join(OUT, 'continue.png') });

    // Continue -> opens agentmeter-prep://start (not followed here) -> "Starting" -> the service registers
    const opened = [];
    await p.evaluate(() => document.addEventListener('click', e => { const a = e.target.closest('a[href^="agentmeter-prep://"]'); if (a) { e.preventDefault(); window.__opened = (window.__opened || []).concat(a.href); } }, true));
    await p.setInputFiles('#svc-file', CSV); await p.click('#svc-upload-btn');
    await p.waitForSelector('#an-svc-wait', { timeout: 5000 });
    opened.push(...(await p.evaluate(() => window.__opened || [])));
    log(opened.length === 1 && /^agentmeter-prep:\/\/start/.test(opened[0]), 'Continue opened agentmeter-prep://start (' + opened.join(', ') + ')');
    log(/Starting the preparation service/.test(await p.textContent('#an-svc-wait')) && await p.$eval('#svc-upload-btn', e => e.disabled) && !(await p.$('#an-svc-hint')),
        'the page says it is starting, Continue waits, and the install hint is not shown yet');
    await p.evaluate(() => { AN_SVC.hintAt = 0; anServiceNotice(); });
    log(/prep_local\.py install/.test(await p.textContent('#an-svc-hint')), 'after 20 seconds the first-time install hint shows');
    log(await asService('register', { url: `http://127.0.0.1:${CPP}`, version: 'test' }) === 200, 'the service registered itself (signed)');

    // the same upload carries on by itself, through the registered address
    await p.waitForFunction(() => /^#\/new\/prep\/[0-9a-f]{32}$/.test(location.hash), null, { timeout: 60000 });
    log(true, 'the upload carried on by itself once the service was up');
    const done = await p.waitForSelector('#an-figures .tiles, #an-detail .notice', { timeout: 120000 }).then(() => true, () => false);
    log(done && !!(await p.$('#an-figures .tiles')) || !!(await p.$('#an-detail .notice.warn')), 'the analysis ran on the registered service');

    // Stop -> 'stopping' -> the running line goes, Continue stays enabled
    await p.goto(APP + '/app.html#/new'); await p.waitForSelector('#an-svc-stop');
    await p.evaluate(() => document.addEventListener('click', e => { if (e.target.closest('#an-svc-stop')) e.preventDefault(); }, true));
    await p.click('#an-svc-stop');
    log(await asService('register', { stopping: true }) === 200, 'the service said it is stopping');
    await p.waitForSelector('#an-svc-stop', { state: 'detached', timeout: 15000 });
    log(!(await p.$eval('#svc-upload-btn', e => e.disabled)) && await p.$eval('#an-unavailable', e => e.hidden), 'stopped: the running line goes and Continue stays enabled');
  } catch (e) {
    log(false, 'walk-through crashed: ' + e.message.split('\n')[0]);
  }
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  await b.close();
  const fails = out.filter(l => l.startsWith('FAIL')).length;
  console.log(`${out.length - fails} passed, ${fails} failed`);
  process.exit(fails ? 1 : 0);
})();
