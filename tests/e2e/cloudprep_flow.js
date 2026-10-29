// Cloud data preparation, chosen automatically (Playwright; run via tests/e2e/cloudprep_stack.sh):
//   GPU free -> the dataset is prepared on the GPU server -> a slow benchmark runs -> a new CSV and
//   a new PCAP are prepared IN THE CLOUD (cloudprep/app.py) while it runs, with no choice offered
//   -> each is stored on the GPU server under its own name and records that it was prepared in the
//   cloud -> the benchmark was still running meanwhile -> a reload mid-way follows the same cloud
//   job again -> nothing blocked by the CSP, no JS errors.
const path = require('path'), fs = require('fs'), cp = require('child_process');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const REPO = path.resolve(__dirname, '..', '..');
const OUT = process.argv[2] || fs.mkdtempSync('/tmp/cloudprep-e2e-');
const PP = process.env.PP || 8080, BP = process.env.BP || 8766;
const APP = `http://127.0.0.1:${PP}`, WORKER = process.env.CP_URL;
const CSV = path.join(REPO, 'data', 'sample_csv', 'cicids2017_sample.csv');
const PCAP = path.join(REPO, 'data', 'sample_pcaps', 'sample_small.pcap');
const RESULTS = path.join(OUT, 'backend-results');
const out = []; const log = (ok, msg) => { out.push((ok ? 'PASS ' : 'FAIL ') + msg); console.log((ok ? 'PASS ' : 'FAIL ') + msg); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

let backend = null;
function startBackend() {
  fs.mkdirSync(RESULTS, { recursive: true });
  const env = Object.assign({}, process.env, {
    AGENTMETER_RESULTS_DIR: RESULTS, AGENTMETER_RUN_TOKEN_SECRET: process.env.DEV_RUN_SECRET,
    AGENTMETER_BACKEND_SECRET: process.env.DEV_BACKEND_SECRET, AGENTMETER_WORKER_URL: WORKER,
    AGENTMETER_PUBLIC_URL: `http://127.0.0.1:${BP}`, AGENTMETER_ALLOWED_ORIGINS: APP, AGENTMETER_HEARTBEAT_S: '3',
    AGENTMETER_MOCK_LATENCY_S: '0.25',                       // a benchmark that keeps the GPU busy for a while
    AGENTMETER_TEST_MODEL_REPOS: path.join(REPO, 'tests', 'fixtures', 'model_repos.json'), AGENTMETER_TEST_DISK_FREE_GB: '200' });
  backend = cp.spawn(process.env.PYTHON || 'python', ['scripts/serve.py', '--provider', 'mock', '--host', '127.0.0.1', '--port', String(BP),
                                                     '--results-dir', RESULTS], { cwd: REPO, env, stdio: ['ignore', fs.openSync(path.join(OUT, 'backend.log'), 'a'), fs.openSync(path.join(OUT, 'backend.log'), 'a')] });
}
async function cpJson(p, token) {
  const r = await fetch(WORKER + p, { headers: token ? { Authorization: 'Bearer ' + token } : {} });
  return r.json();
}
async function adminToken() {
  const r = await fetch(WORKER + '/api/auth/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ username: 'admin', password: 'admin-password-123' }) });
  return (await r.json()).access_token;
}
async function waitFor(fn, ms, step = 500) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await fn()) return true; await sleep(step); }
  return false;
}
async function upload(p, file, flows) {
  await p.goto(APP + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
  await p.setInputFiles('#svc-file', file);
  await p.fill('#svc-max-flows', String(flows));
  await p.click('#svc-upload-btn');
}
const logLines = p => p.$$eval('#svc-local-log li', l => l.map(x => x.textContent));

(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1280, height: 1000 } })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  const csp = []; p.on('console', m => { if (/Content Security Policy|Refused to/.test(m.text())) csp.push(m.text().slice(0, 200)); });
  let backendOk = false;
  try {
    startBackend();
    const tok = await adminToken();
    backendOk = await waitFor(async () => (await cpJson('/api/backend', tok)).online, 30000);
    log(backendOk, 'the GPU backend registered with the control plane');
    log((await cpJson('/api/backend', tok)).cloud_prep === true, 'the control plane reports cloud preparation as available');

    await p.goto(APP + '/'); await p.waitForSelector('#login-form:not([hidden])');
    await p.fill('#login-user', 'admin'); await p.fill('#login-pass', 'admin-password-123'); await p.click('#login-go');
    await p.waitForSelector('#view-home:not([hidden])');
    await p.evaluate(() => localStorage.removeItem('agentmeter.prepWhere'));            // no override: automatic
    await p.goto(APP + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
    log(await p.$('#svc-prep-where') === null && await p.$('input[name="prep-where"]') === null, 'the user is offered no choice of where to prepare');
    log(/in the cloud instead/.test(await p.textContent('#svc-prep-note')), 'the page says where the data is prepared');

    // 1. GPU free -> the GPU server prepares it
    await upload(p, CSV, 30);
    await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 30000 });
    log(true, 'GPU free: the dataset is prepared on the GPU server');
    await p.waitForSelector('#svc-models input');
    const boxes = await p.$$('#svc-models input');
    for (const bx of boxes.slice(0, 2)) if (!(await bx.isChecked())) await bx.check();
    await p.waitForFunction(() => !document.getElementById('svc-run').disabled, null, { timeout: 60000 });
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
    const job = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[3]);
    await p.waitForFunction(() => document.querySelectorAll('#tel-host .tel-line').length >= 2, null, { timeout: 30000 });

    // 2. GPU busy -> the cloud prepares a CSV, without waiting in the GPU queue
    const t0 = Date.now();
    await upload(p, CSV, 20);
    await p.waitForFunction(() => /^#\/new\/models\?set=/.test(location.hash) || document.querySelector('#svc-upload-error:not([hidden])'), null, { timeout: 120000 });
    const csvSet = decodeURIComponent((await p.evaluate(() => location.hash)).split('set=')[1] || '');
    const lines = await logLines(p);
    const err = await p.evaluate(() => document.querySelector('#svc-upload-error:not([hidden])')?.textContent || '');
    log(/^csv_runs\//.test(csvSet), `GPU busy: the CSV was prepared in the cloud and stored on the GPU server as ${csvSet} (${((Date.now() - t0) / 1000).toFixed(0)} s) ${err}`);
    log(lines.some(l => /busy with a benchmark, so .* is prepared in the cloud/.test(l)) && lines.some(l => /Stored on the GPU server/.test(l)),
        'the steps are shown, saying why it went to the cloud');
    const st = await cpJson('/api/sessions/' + job, tok);
    log(st.status === 'Running', 'the benchmark was still running meanwhile (status ' + st.status + ')');
    const manPath = path.join(RESULTS, csvSet, 'manifest.json');
    const man = fs.existsSync(manPath) ? JSON.parse(fs.readFileSync(manPath, 'utf8')) : null;
    const manText = JSON.stringify(man || {});
    log(!!man && (/cloud preparation service/.test(manText) || /"provider":\s*"cloud"/.test(manText)),
        'the stored set records that it was prepared in the cloud');
    await p.screenshot({ path: path.join(OUT, 'cloud-csv.png'), fullPage: true });

    // 3. a PCAP while still busy; reload mid-way -> the same cloud job is followed again
    if ((await cpJson('/api/sessions/' + job, tok)).status === 'Running') {
      await upload(p, PCAP, 20);
      await p.waitForFunction(() => {
        const l = [...document.querySelectorAll('#svc-local-log li')].map(x => x.textContent);
        return /sample_small\.pcap/.test(l[0] || '') && l.some(t => /Uploaded\. The cloud is preparing it/.test(t));
      }, null, { timeout: 60000 });
      await p.reload(); await p.waitForSelector('#svc-upload-form:not([hidden])');
      await p.waitForFunction(() => /^#\/new\/models\?set=/.test(location.hash) || document.querySelector('#svc-upload-error:not([hidden])'), null, { timeout: 120000 });
      const pcapSet = decodeURIComponent((await p.evaluate(() => location.hash)).split('set=')[1] || '');
      const resumed = await logLines(p);
      log(/^pcap_runs\//.test(pcapSet) && resumed.some(l => /Following the cloud preparation of sample_small\.pcap/.test(l)),
          'after a reload the same cloud job is followed again and the PCAP stored as ' + pcapSet);
    } else log(false, 'the benchmark finished too soon to test the PCAP while busy');

    // 4. the prepared-in-the-cloud set benchmarks like any other
    await p.goto(APP + '/#/new/models?set=' + encodeURIComponent(csvSet)); await p.waitForSelector('#svc-models input');
    const ready = await p.waitForFunction(() => /Ready|20 flows/.test(document.querySelector('#wiz-data-card')?.textContent || ''), null, { timeout: 30000 }).then(() => true, () => false);
    log(ready, 'the cloud-prepared set is ready to benchmark');
  } catch (e) {
    log(false, 'walk-through crashed: ' + e.message.split('\n')[0]);
  }
  log(csp.length === 0, 'nothing blocked by the Content-Security-Policy ' + csp.join(' | '));
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  await b.close();
  if (backend) backend.kill('SIGTERM');
  const fails = out.filter(l => l.startsWith('FAIL')).length;
  console.log(`${out.length - fails} passed, ${fails} failed`);
  process.exit(fails ? 1 : 0);
})();
