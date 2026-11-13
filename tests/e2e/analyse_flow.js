// New Benchmark as one flow, steps 1-3 without a GPU (Playwright; run via tests/e2e/cloudprep_stack.sh):
//   an admin stores the IDS model -> with NO GPU server, a labelled CSV is uploaded on Analyse traffic
//   -> the preparation service scores every flow -> the page shows the alerts, recall / precision /
//   false-positive rate and the addresses (the uploader's) -> the threshold changes the figures in the
//   browser -> the analysis is listed -> the GPU server starts -> "Benchmark these alerts" hands the set
//   and its scores to it and opens model choice with the IDS gate at the page's threshold -> a mock
//   benchmark triages the alerts with the STORED scores -> no CSP blocks, no JS errors.
const path = require('path'), fs = require('fs'), cp = require('child_process');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const REPO = path.resolve(__dirname, '..', '..');
const OUT = process.argv[2] || fs.mkdtempSync('/tmp/analyse-e2e-');
fs.mkdirSync(OUT, { recursive: true });
const PP = process.env.PP || 8080, BP = process.env.BP || 8766;
const APP = `http://127.0.0.1:${PP}`, WORKER = process.env.CP_URL;
const CSV = path.join(REPO, 'data', 'sample_csv', 'cicids2017_sample.csv');
const IDS = path.join(REPO, 'models', 'ids');
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
    AGENTMETER_IDS_MODEL: path.join(IDS, 'demo_ids.joblib'),            // the same model the analysis used
    AGENTMETER_TEST_MODEL_REPOS: path.join(REPO, 'tests', 'fixtures', 'model_repos.json'), AGENTMETER_TEST_DISK_FREE_GB: '200' });
  backend = cp.spawn(process.env.PYTHON || 'python', ['scripts/serve.py', '--provider', 'mock', '--host', '127.0.0.1', '--port', String(BP),
                                                     '--results-dir', RESULTS], { cwd: REPO, env, stdio: ['ignore', fs.openSync(path.join(OUT, 'backend.log'), 'a'), fs.openSync(path.join(OUT, 'backend.log'), 'a')] });
}
async function cp_(p, token, opts = {}) {
  const r = await fetch(WORKER + p, Object.assign({ headers: Object.assign({ 'content-type': 'application/json' }, token ? { Authorization: 'Bearer ' + token } : {}) }, opts));
  return { status: r.status, data: await r.json().catch(() => ({})) };
}
async function adminToken() {
  return (await cp_('/api/auth/login', null, { method: 'POST', body: JSON.stringify({ username: 'admin', password: 'admin-password-123' }) })).data.access_token;
}
async function storeIdsModel(tok) {
  for (const [name, file] of [['ids_rf.joblib', 'demo_ids.joblib'], ['ids_rf.json', 'demo_ids.json'], ['ids_rf.hashes.npy', 'demo_ids.hashes.npy']]) {
    const bytes = fs.readFileSync(path.join(IDS, file));
    const u = await cp_('/api/admin/ids-model/upload-url', tok, { method: 'POST', body: JSON.stringify({ name, bytes: bytes.length }) });
    const put = await fetch(u.data.url, { method: 'PUT', body: bytes, headers: { 'content-type': u.data.content_type } });
    if (!put.ok) throw new Error('IDS upload ' + name + ': HTTP ' + put.status);
  }
  return cp_('/api/admin/ids-model/commit', tok, { method: 'POST', body: '{}' });
}
async function waitFor(fn, ms, step = 500) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await fn()) return true; await sleep(step); }
  return false;
}
const txt = (p, sel) => p.$eval(sel, e => e.textContent).catch(() => '');

(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1280, height: 1100 } })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  const csp = []; p.on('console', m => { if (/Content Security Policy|Refused to/.test(m.text())) csp.push(m.text().slice(0, 200)); });
  try {
    const tok = await adminToken();
    const c = await storeIdsModel(tok);
    log(c.status === 200, 'an admin stored the IDS model (' + c.status + ')');
    log(!(await cp_('/api/backend', tok)).data.online, 'no GPU server is running');

    await p.goto(APP + '/app.html'); await p.waitForSelector('#login-form:not([hidden])');
    await p.fill('#login-user', 'admin'); await p.fill('#login-pass', 'admin-password-123'); await p.click('#login-go');
    await p.waitForSelector('#view-home:not([hidden])');
    await p.click('a[data-nav="new"]');
    await p.waitForSelector('#view-new:not([hidden]) #svc-upload-form');
    const navs = await p.$$eval('#main-nav a[data-nav]', l => l.map(a => a.getAttribute('data-nav')));
    log(JSON.stringify(navs) === JSON.stringify(['home', 'new', 'sessions', 'leaderboard', 'planner']), 'the sidebar: Home, New Benchmark, Sessions, Leaderboard, Cost planner (' + navs + ')');
    const steps = await p.$$eval('#wiz-steps li', l => l.map(x => x.textContent.replace(/^\d/, '')));
    log(steps.join('|') === 'Upload|Preparing|Your data|Models and settings|Run', 'five steps: ' + steps.join(' → '));
    log(!(await p.$eval('#svc-upload-btn', e => e.disabled)), 'GPU off: step 1 (upload) works');

    // 1. upload a labelled CSV with the GPU off
    await p.setInputFiles('#svc-file', CSV);
    await p.fill('#svc-max-flows', '20');
    await p.click('#svc-upload-btn');
    await p.waitForFunction(() => /^#\/new\/prep\/[0-9a-f]{32}$/.test(location.hash), null, { timeout: 60000 });
    const jobId = (await p.evaluate(() => location.hash)).split('/')[3];        // #/new/prep/<job>
    const shown = await p.waitForSelector('#an-figures .tiles', { timeout: 120000 }).then(() => true, () => false);
    log(shown, 'step 2 → 3: the preparation service prepared and scored the data, and "Your data" shows it');
    const cur = await p.$eval('#wiz-steps li[aria-current="step"]', e => e.textContent);
    log(/Your data/.test(cur) && /benchmark sample 20/.test(await txt(p, '#an-contents')), 'step 3 shows what is in it (' + cur + ')');
    const scored = await txt(p, '#an-figures .tile:first-child .v');
    const pool = JSON.parse((await (await fetch(WORKER + `/api/prep/jobs/${jobId}/files/ids_pool.json`, { headers: { Authorization: 'Bearer ' + tok } })).text()) || '{}');
    log(Number(scored.replace(/\D/g, '')) === pool.n_flows_in && pool.n_flows_in > 20, `every flow of the pool was scored (${scored}), not only the 20-flow sample`);
    log(/%/.test(await txt(p, '#an-t-recall .v')) && !!(await p.$('#an-classes')), 'labelled: recall, precision, false-positive rate and recall per class are shown');
    const header = await p.$$eval('#an-alerts-table th', l => l.map(x => x.textContent).join('|'));
    log(!/Source → destination/.test(header), 'this CSV has no addresses: no empty address column');
    log(/No finished benchmark to project from/.test(await txt(p, '#an-proj')), 'no benchmark yet: the projection says so');
    log(await p.$eval('#an-bench-go', e => e.disabled) && /GPU server must be running first/.test(await txt(p, '#an-gpu-off')),
        'GPU off: Continue is disabled — "To run a benchmark, the GPU server must be running first."');
    await p.screenshot({ path: path.join(OUT, 'analyse-gpu-off.png'), fullPage: true });

    // 2. the threshold changes the figures in the browser
    const before = await txt(p, '#an-t-alerts .v');
    await p.fill('#an-tau-n', '0.999'); await p.dispatchEvent('#an-tau-n', 'change');
    const high = await txt(p, '#an-t-alerts .v');
    await p.click('#an-tau-reset');
    log(Number(high.replace(/\D/g, '')) < Number(before.replace(/\D/g, '')) && (await txt(p, '#an-t-alerts .v')) === before,
        `a higher threshold gives fewer alerts (${before} -> ${high}), and the model's own restores them`);

    // 3. listed under Reuse a prepared set, with the data sets
    await p.goto(APP + '/app.html#/analyse'); await p.waitForFunction(() => location.hash === '#/new', null, { timeout: 10000 });
    await p.goto(APP + '/app.html#/datasets'); await p.waitForSelector('#an-table', { timeout: 20000 });
    log(await p.$(`#an-table a[href="#/new/prep/${jobId}"]`) !== null && !!(await p.$('#ds-body')),
        'old links redirect; Reuse a prepared set lists the analysis and the data sets');

    // 4. the GPU server starts -> benchmark the alerts, with the stored scores
    startBackend();
    log(await waitFor(async () => (await cp_('/api/backend', tok)).data.online, 40000), 'the GPU server started and registered');
    await p.goto(APP + '/app.html#/new/prep/' + jobId); await p.reload();
    await p.waitForSelector('#an-bench-go:not([disabled])', { timeout: 60000 });
    await p.fill('#an-tau-n', '0.3'); await p.dispatchEvent('#an-tau-n', 'change');
    await p.click('#an-bench-go');
    await p.waitForFunction(() => /^#\/new\/models\?set=.*&ids=0\.3$/.test(location.hash), null, { timeout: 60000 });
    const set = decodeURIComponent((await p.evaluate(() => location.hash)).split('set=')[1].split('&')[0]);
    log(fs.existsSync(path.join(RESULTS, set, 'ids_scores.csv')) && !fs.existsSync(path.join(RESULTS, set, 'ids_flows_identity.csv')),
        `the set reached the GPU server with its IDS scores and without the addresses (${set})`);
    await p.waitForSelector('#svc-models input');
    log(await p.$eval('#svc-ids', e => e.checked) && (await p.$eval('#svc-ids-tau', e => e.value)) === '0.3', 'model choice opens with the IDS gate at the page\'s threshold');
    const box = (await p.$$('#svc-models input'))[0];
    if (!(await box.isChecked())) await box.check();
    await p.waitForFunction(() => !document.getElementById('svc-run').disabled, null, { timeout: 60000 });
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
    const idsJson = path.join(RESULTS, set, 'ids.json');
    const used = await waitFor(() => fs.existsSync(idsJson) && /prepared set/.test(fs.readFileSync(idsJson, 'utf8')), 60000);
    log(used, 'the benchmark triaged the alerts with the STORED scores (no second scoring)');
    await p.screenshot({ path: path.join(OUT, 'analyse-benchmark.png'), fullPage: true });

    // 5. the Cost planner (GPU-free): mock benchmarks are demo runs, so there is nothing real to project from
    await p.goto(APP + '/app.html#/planner?flows=1234'); await p.waitForSelector('#view-planner:not([hidden]) #pl-body p');
    log((await p.$eval('#pl-flows', e => e.value)) === '1234' && !!(await p.$('#pl-none')), 'the Cost planner opens with the volume and says when nothing real was measured');
    await p.evaluate(() => {            // two measured real sessions (synthetic numbers) -> the table
      const s = (id, at, gpu, m) => ({ session_id: id, status: 'Done', demo: false, gpu, pipeline: null, finished_at: at, metrics: m });
      PLAN.sessions = [s('job_20261105_100000_' + 'a'.repeat(32), '20261105_100000', 'NVIDIA L4',
                       { 'Qwen/Qwen2.5-7B-Instruct': { mean_latency_s: 6.2, cost_per_1k_flows_usd: 1.38, wh_per_flow: 0.12, n_flows: 80 },
                         'microsoft/Phi-3-mini-4k-instruct': { mean_latency_s: 3.1, cost_per_1k_flows_usd: 0.69, wh_per_flow: 0.06, n_flows: 80 } }),
                     s('job_20261106_100000_' + 'b'.repeat(32), '20261106_100000', 'NVIDIA GeForce RTX 3090',
                       { 'Qwen/Qwen2.5-7B-Instruct': { mean_latency_s: 7.9, cost_per_1k_flows_usd: 0.48, wh_per_flow: 0.5, n_flows: 100 } })];
      document.getElementById('pl-flows').value = '20000'; renderPlanner();
    });
    const plRows = await p.$$eval('#pl-table tbody tr', l => l.map(r => r.textContent));
    log(plRows.length === 3 && /\d+ GPUs?/.test(plRows.join('')) && /% busy/.test(plRows.join('')), 'the planner projects each model per GPU and says when one GPU is not enough');
    await p.screenshot({ path: path.join(OUT, 'planner.png'), fullPage: true });
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
