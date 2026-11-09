// The Start / Stop button for the preparation service (Playwright; run via
//   PREP_REGISTERED=1 CLOUDPREP_FLOWS=prep_button_flow bash tests/e2e/cloudprep_stack.sh):
// the control plane has NO fixed service address -> Analyse traffic offers "Start the preparation
// service" -> after the click, the service registers itself (here the test does what prep_local.py
// does: a signed POST /api/prep/register) -> the page notices and enables the upload -> an analysis
// runs through the REGISTERED address -> Stop -> the service says it is stopping -> the page offers
// Start again. The agentmeter-prep:// link itself is not followed (the test PC has no such program).
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
    await p.goto(APP + '/app.html#/new'); await p.waitForSelector('#an-svc-start', { timeout: 20000 });
    log(await p.$eval('#svc-upload-btn', e => e.disabled), 'no service: the upload is off and "Start the preparation service" is offered');
    log((await p.getAttribute('#an-svc-start', 'href')) === 'agentmeter-prep://start', 'the button opens agentmeter-prep://start');
    await p.screenshot({ path: path.join(OUT, 'start-button.png') });

    // the click (its link not followed here) -> the service registers -> the page notices
    await p.evaluate(() => document.addEventListener('click', e => { if (e.target.closest('#an-svc-start, #an-svc-stop')) e.preventDefault(); }, true));
    await p.click('#an-svc-start');
    log(/Starting/.test(await p.textContent('#an-svc-wait')), 'after the click the page says it is starting');
    log(await asService('register', { url: `http://127.0.0.1:${CPP}`, version: 'test' }) === 200, 'the service registered itself (signed)');
    await p.waitForSelector('#svc-upload-btn:not([disabled])', { timeout: 15000 });
    log(!!(await p.$('#an-svc-stop')), 'the page noticed: upload enabled, "Stop it" shown');

    // an analysis goes to the registered address
    await p.setInputFiles('#svc-file', CSV); await p.click('#svc-upload-btn');
    await p.waitForFunction(() => /^#\/new\/prep\/[0-9a-f]{32}$/.test(location.hash), null, { timeout: 60000 });
    const done = await p.waitForSelector('#an-figures .tiles, #an-detail .notice', { timeout: 120000 }).then(() => true, () => false);
    log(done && !!(await p.$('#an-figures .tiles')) || !!(await p.$('#an-detail .notice.warn')), 'the analysis ran on the registered service');

    // Stop -> 'stopping' -> Start offered again
    await p.goto(APP + '/app.html#/new'); await p.waitForSelector('#an-svc-stop');
    await p.evaluate(() => document.addEventListener('click', e => { if (e.target.closest('#an-svc-start, #an-svc-stop')) e.preventDefault(); }, true));
    await p.click('#an-svc-stop');
    log(await asService('register', { stopping: true }) === 200, 'the service said it is stopping');
    await p.waitForSelector('#an-svc-start', { timeout: 15000 });
    log(await p.$eval('#svc-upload-btn', e => e.disabled), 'the page offers Start again and the upload is off');
  } catch (e) {
    log(false, 'walk-through crashed: ' + e.message.split('\n')[0]);
  }
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  await b.close();
  const fails = out.filter(l => l.startsWith('FAIL')).length;
  console.log(`${out.length - fails} passed, ${fails} failed`);
  process.exit(fails ? 1 : 0);
})();
