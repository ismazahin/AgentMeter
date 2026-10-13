// Phase 47 — walk-through of the control plane (Playwright). Run via tests/e2e/cp_stack.sh, which
// starts the Worker (wrangler dev, local D1/R2), bootstraps the admin and serves web/ like Pages.
// This script starts / stops / restarts the mock GPU backend itself:
//   bootstrap admin logs in -> creates a user -> the user logs in (forced password change) ->
//   settings saved to the account (survive a reload) -> mock session (wizard; defaults applied) ->
//   the session renders from the Worker; weights used + a view-only preset; Detailed analysis on
//   the same origin; PDF + stripped features.csv download -> second session -> Compare,
//   Leaderboard -> backend STOPPED: still browsable, Compare/Leaderboard work, Run shows GPU
//   offline -> backend restarts and re-registers -> Run is back -> admin limits/credentials ->
//   logout revokes -> phone width.
const path = require('path'), fs = require('fs'), cp = require('child_process');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const REPO = path.resolve(__dirname, '..', '..');
const OUT = process.argv[2] || fs.mkdtempSync('/tmp/cp-e2e-');
const WP = process.env.WP || 8787, PP = process.env.PP || 8080, BP = process.env.BP || 8766;
const APP = `http://127.0.0.1:${PP}`, WORKER = `http://127.0.0.1:${WP}`;
const CSV = path.join(REPO, 'data', 'sample_csv', 'cicids2017_sample.csv');
const RESULTS = path.join(OUT, 'backend-results');
const out = []; const log = (ok, msg) => { out.push((ok ? 'PASS ' : 'FAIL ') + msg); console.log((ok ? 'PASS ' : 'FAIL ') + msg); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

let backend = null;
function startBackend(extraEnv = {}) {
  fs.mkdirSync(RESULTS, { recursive: true });
  const env = Object.assign({}, process.env, {
    AGENTMETER_RESULTS_DIR: RESULTS, AGENTMETER_RUN_TOKEN_SECRET: process.env.DEV_RUN_SECRET,
    AGENTMETER_BACKEND_SECRET: process.env.DEV_BACKEND_SECRET, AGENTMETER_WORKER_URL: WORKER,
    AGENTMETER_PUBLIC_URL: `http://127.0.0.1:${BP}`, AGENTMETER_ALLOWED_ORIGINS: APP, AGENTMETER_HEARTBEAT_S: '3' }, extraEnv);
  backend = cp.spawn(process.env.PYTHON || 'python', ['scripts/serve.py', '--provider', 'mock', '--host', '127.0.0.1', '--port', String(BP),
                                                     '--results-dir', RESULTS], { cwd: REPO, env, stdio: ['ignore', fs.openSync(path.join(OUT, 'backend.log'), 'a'), fs.openSync(path.join(OUT, 'backend.log'), 'a')] });
}
async function stopBackend() { if (backend) { backend.kill('SIGTERM'); await sleep(500); backend = null; } }
async function workerJson(p, token) {
  const r = await fetch(WORKER + p, { headers: token ? { Authorization: 'Bearer ' + token } : {} });
  return r.json();
}
async function loginApi(u, pw) {
  const r = await fetch(WORKER + '/api/auth/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ username: u, password: pw }) });
  return r.json();
}
async function waitFor(fn, ms, step = 500) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await fn()) return true; await sleep(step); }
  return false;
}
async function login(p, u, pw, remember = false) {
  await p.goto(APP + '/'); await p.waitForSelector('#login-form:not([hidden])');
  await p.fill('#login-user', u); await p.fill('#login-pass', pw);
  if (remember) await p.check('#login-remember');
  await p.click('#login-go');
}
async function menu(p) { if (await p.isHidden('#cp-menu')) await p.click('#cp-profile-btn'); await p.waitForSelector('#cp-menu:not([hidden])'); }
async function logout(p) { await menu(p); await p.click('#cp-logout'); await p.waitForSelector('#login-form:not([hidden])'); }
async function noHScroll(p) { return p.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1); }

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ acceptDownloads: true });
  const p = await ctx.newPage();
  const errors = [];
  p.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push(m.text()); });
  p.on('pageerror', e => errors.push(e.message));
  try {
    startBackend();
    const adminTok = (await loginApi('admin', 'admin-password-123')).access_token;
    log(await waitFor(async () => (await workerJson('/api/backend', adminTok)).online, 30000), 'the backend registered itself with the Worker (no URL pasted anywhere)');

    // 1. login page; wrong password; admin logs in
    await p.goto(APP + '/');
    await p.waitForSelector('#login-form:not([hidden])');
    log(await p.isHidden('.mainnav'), 'login page first (no public sign-up; navigation hidden)');
    log(/created by your admin/.test(await p.textContent('#view-login')), 'login page says accounts come from the admin');
    await p.fill('#login-user', 'admin'); await p.fill('#login-pass', 'wrong-password-1'); await p.click('#login-go');
    await p.waitForSelector('#login-error:not([hidden])');
    log(/wrong username or password/.test(await p.textContent('#login-error')), 'a wrong password is refused');
    await p.fill('#login-pass', 'admin-password-123'); await p.click('#login-go');
    await p.waitForSelector('#view-home:not([hidden])');
    log(/admin/i.test(await p.textContent('#cp-role')) && /admin/.test(await p.textContent('#cp-user')), 'the header shows a profile button with the name and an Admin badge');
    await menu(p);
    log(await p.isVisible('#nav-admin') && await p.isVisible('#cp-menu a[href="#/profile"]') && await p.isVisible('#cp-menu a[href="#/settings"]'),
        'the profile menu has Profile, Settings and (for the admin) Admin');
    await p.screenshot({ path: path.join(OUT, 'profile-menu.png') });
    await p.keyboard.press('Escape');
    log(await p.isHidden('#cp-menu'), 'Escape closes the profile menu');
    log(!(await p.evaluate(() => Object.keys(localStorage).concat(Object.keys(sessionStorage)).some(k => /access/.test(k)))), 'the access token is kept in memory only');
    log(!(await p.evaluate(() => localStorage.getItem('agentmeter.refresh'))) && !!(await p.evaluate(() => sessionStorage.getItem('agentmeter.refresh'))),
        'Remember me off (default): the login is kept only for this browser session (sessionStorage)');
    await p.goto(APP + '/#/profile'); await p.waitForSelector('#prof-devices-table');
    log(/Admin/.test(await p.textContent('#prof-role')) && /admin/.test(await p.textContent('#prof-username')), 'Profile shows the username and the Admin role');
    log((await p.$$('#prof-devices-table tbody tr')).length >= 1 && /this device/.test(await p.textContent('#prof-devices-table')), 'Profile lists the logged-in devices, this one marked');
    log(await p.isVisible('#set-password'), 'Change password lives on the Profile page');
    await p.screenshot({ path: path.join(OUT, 'profile.png'), fullPage: true });
    const extra = await loginApi('admin', 'admin-password-123');                     // another "device"
    const extraSid = JSON.parse(Buffer.from(extra.access_token.split('.')[1], 'base64url').toString()).sid;
    await p.goto(APP + '/#/'); await p.goto(APP + '/#/profile'); await p.waitForSelector(`tr[data-device="${extraSid}"]`);
    const before = (await p.$$('#prof-devices-table tbody tr')).length;
    await p.click(`#prof-devices-table button[data-revoke="${extraSid}"]`);
    await p.waitForFunction(n => document.querySelectorAll('#prof-devices-table tbody tr').length === n - 1, before);
    const gone = await fetch(WORKER + '/api/auth/refresh', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ refresh_token: extra.refresh_token }) });
    log(gone.status === 401, 'Profile: "Log out" on another device ends that login');
    // a new tab of the same browser (fresh sessionStorage) asks to log in again
    const p2 = await ctx.newPage(); await p2.goto(APP + '/'); await p2.waitForSelector('#login-form:not([hidden])');
    log(true, 'a new tab without Remember me starts at the login page'); await p2.close();

    // 2. admin creates a user
    await p.goto(APP + '/#/admin'); await p.waitForSelector('#adm-users-table');
    log(/1 of 5/.test(await p.textContent('#adm-count')), 'Admin: 1 of 5 accounts');
    await p.fill('#adm-new-user', 'alice'); await p.fill('#adm-new-pass', 'temporary-pass-1'); await p.click('#adm-new-go');
    await p.waitForSelector('tr[data-user="alice"]');
    log(/2 of 5/.test(await p.textContent('#adm-count')) && /must change password/.test(await p.textContent('tr[data-user="alice"]')), 'user alice created (must change the temporary password)');
    const creds = await p.textContent('#adm-creds');
    log(/Hugging Face token\s*not set/.test(creds) && /Telegram bot token\s*not set/.test(creds) && !/dev-/.test(creds), 'credential status shows set / not set only');
    await p.fill('#lim-sph', '20'); await p.click('#lim-save');
    await p.waitForFunction(() => /Saved/.test(document.getElementById('lim-saved').textContent));
    log(true, 'admin saved the system limits');

    // 3. alice logs in, forced password change
    await logout(p);
    log(!(await p.evaluate(() => localStorage.getItem('agentmeter.refresh') || sessionStorage.getItem('agentmeter.refresh'))), 'logout removes the refresh token');
    await login(p, 'alice', 'temporary-pass-1', true);
    await p.waitForSelector('#pwchange-form:not([hidden])');
    await p.fill('#pwc-new', 'alice-password-123'); await p.click('#pwc-go');
    await p.waitForSelector('#view-home:not([hidden])');
    log(!!(await p.evaluate(() => localStorage.getItem('agentmeter.refresh'))), 'Remember me on: the login is kept in localStorage');
    await menu(p);
    log(!(await p.isVisible('#nav-admin')) && /User/.test(await p.textContent('#cp-role')), 'alice (user) changed her password; User badge, no Admin link');
    await p.keyboard.press('Escape');
    const p3 = await ctx.newPage(); await p3.goto(APP + '/'); await p3.waitForSelector('#view-home:not([hidden])');
    log(true, 'Remember me on: a new tab opens already logged in'); await p3.close();
    await p.goto(APP + '/#/admin'); await p.waitForTimeout(500);
    log(await p.isVisible('#view-home'), 'a user cannot open the Admin page');

    // 4. settings saved to the account
    await p.goto(APP + '/#/settings'); await p.waitForSelector('#set-account:not([hidden])');
    await p.fill('#set-tg', '123456789'); await p.selectOption('#set-notify', 'done');
    await p.check('#set-models input[value="microsoft/Phi-3-mini-4k-instruct"]'); await p.check('#set-models input[value="Qwen/Qwen2.5-7B-Instruct"]');
    await p.fill('#set-flows', '4'); await p.fill('#set-tz', 'Asia/Kuala_Lumpur');
    await p.click('#set-preset-add');
    await p.fill('#pre-name-0', 'speed first'); await p.fill('#pre-accuracy-0', '0'); await p.fill('#pre-latency-0', '0.7'); await p.fill('#pre-vram-0', '0.2'); await p.fill('#pre-tokens-0', '0.1');
    await p.check('#set-theme-dark');
    await p.click('#set-save'); await p.waitForFunction(() => /Saved/.test(document.getElementById('set-saved').textContent));
    await p.reload(); await p.waitForSelector('#view-settings:not([hidden])'); await p.waitForSelector('#set-account:not([hidden])');
    await p.waitForTimeout(500);
    log(await p.inputValue('#set-tg') === '123456789' && await p.inputValue('#set-flows') === '4' && await p.inputValue('#set-tz') === 'Asia/Kuala_Lumpur',
        'settings persist in the account across a reload (still logged in via the refresh token)');
    log(await p.evaluate(() => document.documentElement.getAttribute('data-theme')) === 'dark', 'theme from the account applied');
    const tg = await p.evaluate(async (w) => (await fetch(w + '/api/settings/telegram-test', { method: 'POST' })).status, WORKER);
    log(tg === 401, 'Telegram test needs a login');

    // 5. a mock session through the wizard; defaults applied
    await p.goto(APP + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
    log(await p.inputValue('#svc-max-flows') === '4', 'default flows prefilled from Settings');
    await p.setInputFiles('#svc-file', CSV); await p.click('#svc-upload-btn');
    await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 30000 });
    await p.waitForSelector('#svc-models input');
    const checked = await p.$$eval('#svc-models input:checked', xs => xs.map(x => x.value).sort());
    log(JSON.stringify(checked) === JSON.stringify(['Qwen/Qwen2.5-7B-Instruct', 'microsoft/Phi-3-mini-4k-instruct']), 'default models pre-selected');
    await p.waitForFunction(() => !document.getElementById('svc-run').disabled, null, { timeout: 60000 });
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
    const job1 = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[3]);
    log(/^job_\d{8}_\d{6}_[0-9a-f]{32}$/.test(job1), 'job id carries 128 random bits: ' + job1);
    await p.waitForFunction(id => location.hash.indexOf('#/session/' + id) === 0, job1, { timeout: 180000 });
    await p.waitForSelector('#session-body:not([hidden])', { timeout: 60000 });
    const chips = await p.textContent('#session-chips');
    log(/alice/.test(chips), 'the session records who started it (alice)');
    const st = await workerJson('/api/sessions/' + job1, (await loginApi('admin', 'admin-password-123')).access_token);
    log(st.status === 'Demo' && st.has_results && st.owner === 'alice', 'stored in the control plane: status Demo, results, owner alice');
    await p.click('#stab-ranking'); await p.click('#ssub-weighting'); await p.waitForSelector('#svc-weights-used');
    log(/Stored scores use:/.test(await p.textContent('#svc-weights-used')), 'Model Ranking › Weighting shows the SAW weights the stored scores used');
    await p.selectOption('#svc-preset', '0');
    log(/speed first/.test(await p.textContent('#svc-preset-weights')) && await p.$$eval('#svc-preset-table tbody tr', r => r.length) === 2,
        'a preset re-ranks in the view only, naming its weights');
    await p.click('#ssub-scores');
    const frame = await (await p.waitForSelector('#analysis-frame')).contentFrame();
    await frame.waitForFunction(() => document.body && document.getElementById('banner') && document.body.classList.contains('session-mode') &&
      !/Loading/.test(document.getElementById('banner').textContent), null, { timeout: 30000 });
    log(!/unavailable/.test(await frame.textContent('#banner')) && /analysis\.html/.test(frame.url()), 'the embedded analysis renders on the same origin (results handed over by the app)');
    await p.click('#stab-data');
    const [dl] = await Promise.all([p.waitForEvent('download'), p.click('#svc-downloads-main a')]);
    const pdfPath = path.join(OUT, 'report.pdf'); await dl.saveAs(pdfPath);
    const pdfText = cp.execFileSync(process.env.PYTHON || 'python', ['-c', 'import sys,pypdf;print("\\n".join(p.extract_text() for p in pypdf.PdfReader(sys.argv[1]).pages))', pdfPath]).toString();
    log(/SAW weights used for these scores/.test(pdfText.replace(/\s+/g, ' ')), 'the stored PDF downloads with a token and states the SAW weights used');
    await p.waitForSelector('#svc-downloads a[data-dl="features.csv"]');
    const [fdl] = await Promise.all([p.waitForEvent('download'), p.click('#svc-downloads a[data-dl="features.csv"]')]);
    const fpath = path.join(OUT, 'features.csv'); await fdl.saveAs(fpath);
    const head = fs.readFileSync(fpath, 'utf8').split('\n')[0];
    log(!/src_ip|dst_ip|src_port|protocol_name|timestamp/.test(head) && /flow_id/.test(head), 'stored features.csv has no identification columns');
    const anon = await p.evaluate(async (a) => (await fetch(a.w + '/api/sessions/' + a.id + '/files/features.csv')).status, { w: WORKER, id: job1 });
    log(anon === 401, 'prepared-set files need a token');

    // 6. a second session (reuse the prepared set) -> Compare, Leaderboard
    const set = (await (await fetch(WORKER + '/api/sessions/' + job1, { headers: { Authorization: 'Bearer ' + (await loginApi('alice', 'alice-password-123')).access_token } })).json()).run_name;
    await p.goto(APP + '/#/new/models?set=' + encodeURIComponent(set)); await p.waitForSelector('#svc-models input');
    await p.waitForFunction(() => !document.getElementById('svc-run').disabled, null, { timeout: 30000 });
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/session\//.test(location.hash), null, { timeout: 180000 });
    const job2 = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[2]);
    await p.waitForSelector('#session-body:not([hidden])', { timeout: 60000 });
    await p.goto(APP + `/#/compare?a=${job1}&b=${job2}`); await p.waitForSelector('#cmp-table');
    log(/Like-for-like/.test(await p.textContent('#cmp-validity')), 'Compare: like-for-like (same prepared set, same settings)');
    await p.goto(APP + '/#/leaderboard'); await p.waitForSelector('.lb-table');
    log(await p.$$eval('.lb-table tbody tr', r => r.length) >= 2, 'Leaderboard ranks the models');

    // 7. GPU off: stop the backend
    await stopBackend();
    const offTok = (await loginApi('alice', 'alice-password-123')).access_token;
    log(await waitFor(async () => !(await workerJson('/api/backend', offTok)).online, 30000), 'backend stopped: the Worker marks it offline (missed heartbeats)');
    await p.goto(APP + '/#/sessions'); await p.reload();
    await p.waitForSelector('#sessions-table');
    log(await p.$$eval('#sessions-table tbody tr', r => r.length) === 2, 'GPU off: Sessions still lists both sessions');
    log(/GPU offline/.test(await p.textContent('#svc-backend-badge')), 'GPU off: the badge says GPU offline');
    await p.goto(APP + '/#/session/' + job1); await p.waitForSelector('#session-body:not([hidden])', { timeout: 30000 });
    log(true, 'GPU off: a session opens with all its tabs');
    await p.goto(APP + `/#/compare?a=${job1}&b=${job2}`); await p.waitForSelector('#cmp-table');
    await p.goto(APP + '/#/leaderboard'); await p.waitForSelector('.lb-table');
    log(true, 'GPU off: Compare and Leaderboard work');
    await p.goto(APP + '/#/new'); await p.waitForSelector('#wiz-offline:not([hidden])');
    log(await p.isDisabled('#svc-upload-btn'), 'GPU off: New benchmark says GPU offline; Prepare is disabled');
    await p.goto(APP + '/#/new/models?set=' + encodeURIComponent(set)); await p.waitForSelector('#svc-run');
    log(await p.isDisabled('#svc-run') && /GPU offline/.test(await p.textContent('#svc-run')), 'GPU off: Run shows GPU offline');

    // 8. the backend restarts and re-registers
    startBackend();
    log(await waitFor(async () => (await workerJson('/api/backend', offTok)).online, 30000), 'backend restarted and re-registered itself');
    await p.reload(); await p.waitForFunction(() => !/offline|checking/i.test(document.getElementById('svc-backend-badge').textContent), null, { timeout: 30000 });
    await p.waitForFunction(() => !document.getElementById('svc-run').disabled, null, { timeout: 30000 });
    log(true, 'GPU back: Run is enabled again without any configuration change');

    // 8b. a NEW rental: the backend's disk is wiped; reuse session 1's stored prepared set
    await stopBackend();
    fs.rmSync(RESULTS, { recursive: true, force: true });
    startBackend();
    const healthy = await waitFor(async () => { try { return (await fetch(`http://127.0.0.1:${BP}/health`)).ok; } catch (e) { return false; } }, 60000);
    await sleep(4000);                                    // its first heartbeat (every 3 s here) has registered it
    log(healthy && (await workerJson('/api/backend', offTok)).online, 'new rental (empty disk) registered itself');
    await p.goto(APP + '/#/new?src=reuse'); await p.reload();
    await p.waitForSelector('#svc-stored-table', { timeout: 30000 });
    await p.waitForFunction(() => !/Loading/.test(document.getElementById('svc-recent-sets').textContent), null, { timeout: 30000 });
    log(/No prepared|Nothing prepared/.test(await p.textContent('#svc-recent-sets')), 'new rental: nothing prepared on this server');
    log(await p.$$eval('#svc-stored-table tbody tr', r => r.length) === 1, 'Reuse lists the prepared set stored with the sessions (one set, two sessions)');
    await p.click('#svc-stored-table [data-restore]');
    await p.waitForFunction(() => /^#\/new\/models\?set=/.test(location.hash), null, { timeout: 30000 });
    await p.waitForSelector('#svc-summary-card', { timeout: 30000 });
    await p.waitForFunction(() => !document.getElementById('svc-run').disabled, null, { timeout: 30000 });
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/session\//.test(location.hash), null, { timeout: 180000 });
    const job3 = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[2]);
    await p.waitForSelector('#session-body:not([hidden])', { timeout: 60000 });
    const s1 = await workerJson('/api/sessions/' + job1, offTok), s3 = await workerJson('/api/sessions/' + job3, offTok);
    log(s1.identity.prepared_set_sha256 === s3.identity.prepared_set_sha256, 'the restored set has the same prepared-set hash as on the first rental');
    await p.goto(APP + `/#/compare?a=${job1}&b=${job3}`); await p.waitForSelector('#cmp-table');
    log(/Like-for-like/.test(await p.textContent('#cmp-validity')), 'Compare: rental-1 session vs rental-2 session on the restored set is like-for-like');
    await p.goto(APP + '/#/leaderboard'); await p.waitForSelector('.lb-table');
    log((await p.$$('.lb-group')).length === 1, 'Leaderboard keeps all three sessions in one group');

    // 8c. Stop a running benchmark (a slow mock run): never scored, never stored as a session
    await stopBackend(); startBackend({ AGENTMETER_MOCK_LATENCY_S: '0.4' });
    const healthy2 = await waitFor(async () => { try { return (await fetch(`http://127.0.0.1:${BP}/health`)).ok; } catch (e) { return false; } }, 30000);
    log(healthy2 && await waitFor(async () => (await workerJson('/api/backend', (await loginApi('admin', 'admin-password-123')).access_token)).online, 30000), 'backend restarted (slow mock runs)');
    await sleep(4000);                               // the next heartbeat re-registers it
    const lbBefore = (await workerJson('/api/leaderboard', (await loginApi('admin', 'admin-password-123')).access_token));
    await p.goto(APP + '/#/'); await p.waitForTimeout(1500);
    const set3 = (await workerJson('/api/sessions/' + job3, offTok)).run_name;          // the restored set on this rental
    await p.goto(APP + '/#/new/models?set=' + encodeURIComponent(set3)); await p.waitForSelector('#svc-models input');
    await p.waitForFunction(() => !document.getElementById('svc-run').disabled, null, { timeout: 30000 });
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/(new\/run|session)\//.test(location.hash), null, { timeout: 30000 });
    if (!/^#\/new\/run\//.test(await p.evaluate(() => location.hash))) log(false, 'Run opened the watch page');
    const jobS = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[3]);
    await p.waitForSelector('#svc-stop:not([hidden])', { timeout: 60000 });
    log(true, 'a running benchmark shows a Stop button to its owner');
    await p.waitForFunction(() => /now running/.test(document.getElementById('svc-progress-text').textContent), null, { timeout: 60000 });
    p.once('dialog', d => d.accept());
    await p.click('#svc-stop');
    await p.waitForFunction(() => /Stopped/.test(document.getElementById('svc-job-status').textContent), null, { timeout: 60000 });
    log(/not scored/.test(await p.textContent('#svc-progress-text')) && await p.isHidden('#svc-stop'), 'after confirming, the run stops: "Stopped … not scored", no Stop button');
    log(await p.isVisible('.again-link a'), 'a stopped run offers "Benchmark these flows again"');
    const stoppedRow = await waitFor(async () => (await workerJson('/api/sessions/' + jobS, (await loginApi('admin', 'admin-password-123')).access_token)).status === 'Stopped', 20000);
    log(stoppedRow, 'the control plane marks the session Stopped');
    const sj = await workerJson('/api/sessions/' + jobS, (await loginApi('admin', 'admin-password-123')).access_token);
    log(!sj.has_results, 'a stopped session has no stored results (never scored or uploaded)');
    const lbAfter = await workerJson('/api/leaderboard', (await loginApi('admin', 'admin-password-123')).access_token);
    log(JSON.stringify(lbAfter.groups) === JSON.stringify(lbBefore.groups), 'the Leaderboard is unchanged by a stopped run');
    await p.goto(APP + '/#/sessions'); await p.waitForSelector('#sessions-table');
    log(/Stopped/.test(await p.textContent(`tr:has(a[data-open="${jobS}"])`)), 'Sessions lists it as Stopped');
    await stopBackend(); startBackend();
    await waitFor(async () => (await workerJson('/api/backend', (await loginApi('admin', 'admin-password-123')).access_token)).online, 30000);

    // 9. logout revokes
    const rt = await p.evaluate(() => localStorage.getItem('agentmeter.refresh') || sessionStorage.getItem('agentmeter.refresh'));
    await logout(p);
    const reuse = await fetch(WORKER + '/api/auth/refresh', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ refresh_token: rt }) });
    log(reuse.status === 401, 'logout revokes the session (the old refresh token is refused)');

    // 10. phone width
    await p.setViewportSize({ width: 375, height: 800 });
    await p.goto(APP + '/'); await p.waitForSelector('#login-form:not([hidden])');
    log(await noHScroll(p), 'phone: login page has no horizontal scroll');
    await login(p, 'admin', 'admin-password-123'); await p.waitForSelector('#view-home:not([hidden])');
    for (const v of ['settings', 'admin', 'sessions']) {
      await p.goto(APP + '/#/' + v); await p.waitForTimeout(800);
      log(await noHScroll(p), 'phone: ' + v + ' has no horizontal scroll');
    }
    await p.screenshot({ path: path.join(OUT, 'phone-admin.png'), fullPage: true });
    log(!errors.length, 'no script errors (CSP blocks nothing the app needs)' + (errors.length ? ': ' + errors.slice(0, 3).join(' | ') : ''));
  } catch (e) {
    log(false, 'walk-through crashed: ' + e.message);
    try { await p.screenshot({ path: path.join(OUT, 'crash.png'), fullPage: true }); } catch (_) { /* ignore */ }
  } finally {
    await stopBackend();
    await b.close();
  }
  fs.writeFileSync(path.join(OUT, 'cp_flow.txt'), out.join('\n') + '\n');
  const failed = out.filter(l => l.startsWith('FAIL')).length;
  console.log(`\n${out.length - failed} passed, ${failed} failed  (${OUT})`);
  process.exit(failed ? 1 : 0);
})();
