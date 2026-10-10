// Phase 46 — walk-through of the ONE app (Playwright, mock backend):
// Home empty state -> New benchmark wizard (upload; link when E2E_URL_BASE is set) -> run ->
// Home shows the session -> session tabs (Summary, Detailed analysis, Agents, Recommendation,
// Downloads) + PDF -> reuse a prepared set -> Compare (like-for-like and not) -> Leaderboard ->
// Settings theme toggle -> phone width.
//   python scripts/serve.py --port 8766            (fresh results dir: Home must start empty)
//   node tests/e2e/app_flow.js [base_url] [out_dir]
const path = require('path'), os = require('os'), fs = require('fs'), cp = require('child_process');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const BASE = process.argv[2] || 'http://127.0.0.1:8766';
const URL_BASE = process.env.E2E_URL_BASE || '';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'app-e2e-'));
fs.mkdirSync(S, { recursive: true });
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);
const pdfText = f => cp.execFileSync(process.env.PYTHON || 'python', ['-c',
  'import sys,pypdf;print("\\n".join(p.extract_text() for p in pypdf.PdfReader(sys.argv[1]).pages))', f]).toString();

async function wizardUpload(p, file, flows, models) {
  await p.goto(BASE + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
  await p.setInputFiles('#svc-file', file);
  await p.fill('#svc-max-flows', String(flows));
  await p.click('#svc-upload-btn');
  await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 30000 });
  return pickAndRun(p, models);
}
async function pickAndRun(p, models) {
  await p.waitForSelector('#svc-models input');
  const boxes = await p.$$('#svc-models input');
  for (const i of models) await boxes[i].check();
  await p.click('#svc-run');
  await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
  const job = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[3]);
  await p.waitForFunction(id => location.hash.indexOf('#/session/' + id) === 0, job, { timeout: 180000 });
  await p.waitForSelector('#session-body:not([hidden])', { timeout: 30000 });
  return job;
}
async function tab(p, name) {
  await p.click(`#session-tabs [data-tab="${name}"]`);
  await p.waitForSelector(`#spanel-${name}:not([hidden])`);
}

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const ctx = await b.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true, colorScheme: 'dark' });
  const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  p.on('console', m => { if (m.type() === 'error' && !/fonts|ERR_|Failed to load resource/.test(m.text())) errs.push(m.text()); });

  // 1. Home: empty state, one header, no baseline data
  await p.goto(BASE + '/'); await p.waitForSelector('#view-home:not([hidden])', { timeout: 15000 });
  await p.waitForSelector('#home-empty:not([hidden])', { timeout: 15000 });
  log(true, 'Home empty state shown on first launch');
  const nav = await p.$$eval('.mainnav a:not([hidden])', as => as.map(a => a.textContent.trim()));
  log(nav.join('|') === 'Home|New benchmark|Sessions|Compare|Leaderboard', 'header nav: ' + nav.join(', '));
  log(await p.getAttribute('.top-right a.iconbtn', 'aria-label') === 'Settings', 'Settings icon button has an aria-label');
  log(/Demo mode \(no GPU\)/.test(await p.textContent('#svc-backend-badge')), 'GPU chip: ' + await p.textContent('#svc-backend-badge'));
  const body0 = await p.textContent('body');
  log(!/Validation baseline/.test(body0) && !/Mistral-7B|gemma-2/.test(await p.textContent('#view-home')), 'no Validation baseline in nav/Home, no study data on first launch');
  log((await p.$$('header')).length === 1, 'exactly one header');
  await p.screenshot({ path: S + '/01-home-empty.png', fullPage: true });

  // 2. wizard: upload a labelled CSV, pick models while the data prepares, run
  await p.goto(BASE + '/#/new'); await p.waitForSelector('#wiz-step-data:not([hidden])');
  const steps = await p.$$eval('#wiz-steps li', ls => ls.map(l => l.textContent.trim()));
  log(steps.join('|') === '1Data|2Models and settings|3Run', 'wizard has 3 steps: ' + steps.join(', '));
  const tabs = await p.$$eval('#wiz-step-data [role=tab]', ts => ts.map(t => t.textContent.trim()));
  log(tabs.join('|') === 'Upload a file|Import from a link|Reuse a prepared set', 'data tabs: ' + tabs.join(', '));
  log(await p.isVisible('#svc-other-attack'), 'Other Attack opt-in on the data step');
  await p.screenshot({ path: S + '/02-wizard-data.png', fullPage: true });
  await p.setInputFiles('#svc-file', REPO + '/data/sample_csv/cicids2017_sample.csv');
  await p.fill('#svc-max-flows', '10');
  await p.click('#svc-upload-btn');
  await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 30000 });
  log(await p.isVisible('#wiz-step-models'), 'Continue goes straight to Models while the data prepares');
  log(!/prepare job|benchmark job/i.test(await p.textContent('#view-new')), 'no "prepare job" / "benchmark job" wording');
  await p.waitForSelector('#svc-models input');
  const boxes = await p.$$('#svc-models input');
  await boxes[1].check(); await boxes[3].check();
  log(await boxes[0].isDisabled(), 'a 3rd model is blocked once 2 are picked');
  await p.waitForFunction(() => document.querySelector('#wiz-data-chip').textContent === 'Ready', null, { timeout: 60000 });
  log(/10 flows ready/.test(await p.textContent('#wiz-data-status')), 'data ready: ' + await p.textContent('#wiz-data-status'));
  log(!!(await p.$('#svc-summary-card')), 'data summary shown on step 2');
  await p.screenshot({ path: S + '/03-wizard-models.png', fullPage: true });
  await p.click('#svc-run');
  await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
  log(await p.isVisible('#wiz-step-run'), 'step 3: run with progress');
  const job1 = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[3]);
  await p.waitForFunction(id => location.hash.indexOf('#/session/' + id) === 0, job1, { timeout: 180000 });
  await p.waitForSelector('#session-body:not([hidden])', { timeout: 30000 });
  log(true, 'run finished -> session page ' + job1);

  // 3. the wizard can also run before the data is ready (queued behind the preparation)
  await p.goto(BASE + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
  await p.setInputFiles('#svc-file', REPO + '/data/sample_pcaps/sample_small.pcap');
  await p.fill('#svc-max-flows', '8');
  await p.click('#svc-upload-btn');
  await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 30000 });
  await p.waitForSelector('#svc-models input');
  const early = await p.textContent('#svc-run');
  await (await p.$$('#svc-models input'))[2].check();
  await p.click('#svc-run');
  await p.waitForFunction(() => /^#\/new\/run\//.test(location.hash), null, { timeout: 30000 });
  const job2 = decodeURIComponent((await p.evaluate(() => location.hash)).split('/')[3]);
  log(/Run when data is ready|Run benchmark/.test(early), 'Run offered before the data is ready (' + early + ')');
  await p.waitForFunction(id => location.hash.indexOf('#/session/' + id) === 0, job2, { timeout: 180000 });
  await p.waitForSelector('#session-body:not([hidden])', { timeout: 30000 });
  log(true, 'PCAP session (1 model) finished: ' + job2);

  // 4. optional: Import from a link
  if (URL_BASE) {
    await p.goto(BASE + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
    await p.click('#tab-src-url');
    log(await p.isVisible('#svc-url') && !(await p.isVisible('#svc-file')), 'link tab shows the URL field');
    await p.fill('#svc-url', URL_BASE.replace(/\/$/, '') + '/big.csv');
    await p.fill('#svc-max-flows', '6');
    await p.click('#svc-upload-btn');
    await p.waitForFunction(() => /^#\/new\/models\?prep=/.test(location.hash), null, { timeout: 30000 });
    await p.waitForFunction(() => document.querySelector('#wiz-data-chip').textContent === 'Ready', null, { timeout: 120000 });
    log(/6 flows ready/.test(await p.textContent('#wiz-data-status')), 'link import prepared: ' + await p.textContent('#wiz-data-status'));
  } else {
    await p.goto(BASE + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
    await p.click('#tab-src-url');
    log(await p.isVisible('#svc-url') && !(await p.isVisible('#svc-file')), 'link tab shows the URL field (download not walked: E2E_URL_BASE unset)');
  }

  // 5. Home now shows the latest session + recent sessions
  await p.goto(BASE + '/'); await p.waitForSelector('#home-latest:not([hidden])', { timeout: 15000 });
  log(await p.isHidden('#home-empty'), 'Home: empty state gone');
  log((await p.$$('#home-sessions-table tbody tr')).length >= 2, 'Home: recent sessions table lists the sessions');
  const homeCols = await p.$$eval('#home-sessions-table th', ths => ths.map(t => t.textContent.trim()));
  log(homeCols.join('|') === 'Session|Input|Flows|GPU|More efficient|Status|', 'Home table columns: ' + homeCols.join(', '));
  log((await p.$$eval('#home-sessions-table .status', ss => ss.map(s => s.textContent))).every(s => s === 'Demo'), 'status chips: Demo (mock runs)');
  log((await p.$$('#home-latest .hbar')).length >= 2, 'Home: head-to-head bars for the latest session');
  await p.screenshot({ path: S + '/04-home.png', fullPage: true });
  await p.click('#home-sessions-table a[data-open="' + job1 + '"]');
  await p.waitForSelector('#session-body:not([hidden])');

  // 6. session tabs (CSV, 2 models)
  const stabs = await p.$$eval('#session-tabs [role=tab]', ts => ts.map(t => t.textContent.trim()));
  log(stabs.join('|') === 'Summary|Detailed analysis|Agents|Recommendation|Downloads', 'session tabs: ' + stabs.join(', '));
  const chips = await p.textContent('#session-chips');
  log(/Demo \(no GPU\)/.test(chips) && /10 flows/.test(chips) && /CSV/.test(chips), 'session chips: GPU, flows, input type');
  log(await p.isVisible('#svc-dl-pdf') && await p.isVisible('#session-compare'), 'Download PDF + Compare buttons');
  log(!!(await p.$('#svc-verdict')) && (await p.$$('#svc-models-table tbody tr')).length === 2, 'Summary: verdict + 2-model table');
  log(/not measured/.test(await p.textContent('#svc-cost-card')), 'Summary: cost/energy "not measured" on a mock run');
  await p.screenshot({ path: S + '/05-session-summary.png', fullPage: true });

  await tab(p, 'detailed');
  log((await p.$$('#stats-percentiles tbody tr')).length === 2, 'Detailed: p50/p95/p99 per model');
  log((await p.$$('#stats-effects tbody tr')).length === 3, "Detailed: Cliff's delta + bootstrap CI rows");
  log(/too small/i.test(await p.textContent('#stats-small')), 'Detailed: small-sample note');
  await p.waitForSelector('#analysis-frame');
  const fr = await (await p.$('#analysis-frame')).contentFrame();
  await fr.waitForSelector('#saw-body tr', { timeout: 20000 });
  log((await fr.$$('#saw-body tr')).length === 2, 'Detailed: embedded Overview ranks the 2 session models');
  log(await fr.evaluate(() => document.documentElement.classList.contains('embed') && getComputedStyle(document.querySelector('.topbar .brand')).display === 'none'),
      'embedded analysis has no second header');
  await p.waitForFunction(() => parseInt(document.querySelector('#analysis-frame').style.height || '0', 10) > 600, null, { timeout: 10000 });
  log(true, 'embedded analysis reports its height to the app');
  await p.screenshot({ path: S + '/06-session-detailed.png', fullPage: true });

  await tab(p, 'agents');
  log((await p.$$('#agents-table-0 tbody tr')).length === 4, 'Agents: 4 agents per model');
  log(/hand-off tokens per flow/.test(await p.textContent('#agents-overhead-0')), 'Agents: agentic overhead');
  log(/empty/.test(await p.textContent('#agents-table-0')), 'Agents: failure counts');
  log((await p.$$('#agents-gantt .g-row')).length === 2 && (await p.$$('#agents-gantt .g-seg')).length === 8, 'Agents: per-flow timeline (2 models x 4 agents)');
  const flows = await p.$$eval('#agents-flow option', os => os.map(o => o.value));
  await p.selectOption('#agents-flow', flows[flows.length - 1]);
  log((await p.$$('#agents-gantt .g-seg')).length === 8, 'Agents: timeline follows the selected flow');
  await p.screenshot({ path: S + '/07-session-agents.png', fullPage: true });

  await tab(p, 'recommendation');
  log(!!(await p.$('#svc-stage3')), 'Recommendation: stage-3 model context');
  await p.fill('#dh-max_mean_latency_s', '0.0000001');
  await p.click('#dh-go'); await p.waitForSelector('#dh-table');
  log((await p.$$eval('#dh-table tbody tr', rs => rs.map(r => r.textContent))).every(t => /fails mean_latency/.test(t)), 'Decision helper: tiny latency limit -> fails mean_latency');
  await p.fill('#dh-max_mean_latency_s', '1000'); await p.fill('#dh-max_peak_vram_mb', '8000');
  await p.click('#dh-go'); await p.waitForFunction(() => /no_data/.test(document.querySelector('#dh-table').textContent));
  log((await p.$$eval('#dh-table tbody tr', rs => rs.map(r => r.textContent))).every(t => /no_data/.test(t)), 'Decision helper: VRAM limit on a mock run -> no_data');
  const pdfHref = await p.getAttribute('#dh-pdf', 'href');
  log(/max_mean_latency_s=1000/.test(pdfHref), 'Decision helper: PDF link carries the limits');
  await p.screenshot({ path: S + '/08-session-recommendation.png', fullPage: true });
  const [dpdf] = await Promise.all([p.waitForEvent('download'), p.click('#dh-pdf')]);
  await dpdf.saveAs(S + '/limits.pdf');
  const lt = pdfText(S + '/limits.pdf');
  log(/Decision helper/.test(lt) && /Max mean latency 1000/.test(lt), 'PDF: decision helper with the entered limits');
  log(/Agents . per-agent breakdown/.test(lt) && /Cost and energy/.test(lt) && /Statistical depth/.test(lt), 'PDF: Agents, Cost and energy, Statistical depth sections');

  await tab(p, 'downloads');
  await p.waitForSelector('#svc-downloads a');
  const dls = await p.$$eval('#svc-downloads a', as => as.map(a => a.textContent));
  log(dls.join(',') === 'features.csv,labels.csv,manifest.json', 'Downloads: prepared-set files ' + dls.join(','));
  const [pdf] = await Promise.all([p.waitForEvent('download'), p.click('#svc-dl-pdf')]);
  await pdf.saveAs(S + '/session.pdf');
  log(fs.readFileSync(S + '/session.pdf').subarray(0, 5).toString() === '%PDF-', 'header Download PDF works');

  // PCAP session: accuracy hidden, efficiency only
  await p.goto(BASE + '/#/session/' + job2); await p.waitForSelector('#session-body:not([hidden])');
  log(/unlabelled · efficiency only/.test(await p.textContent('#session-chips')) && !/Accuracy/.test(await p.textContent('#svc-models-table thead')), 'PCAP session: efficiency only, no accuracy column');
  await tab(p, 'detailed');
  log(/single-model/.test(await p.textContent('#stats-depth')), 'PCAP 1-model session: effect sizes explain they need two models');

  // 7. reuse the CSV's prepared set with the same 2 models -> a like-for-like pair
  await p.goto(BASE + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
  await p.click('#tab-src-reuse'); await p.waitForSelector('#svc-sets-table a[data-set]');
  const sets = await p.$$eval('#svc-sets-table a[data-set]', as => as.map(a => a.getAttribute('data-set')));
  const csvSet = await p.evaluate(id => fetch('/api/jobs/' + id).then(r => r.json()).then(j => j.run_name), job1);
  log(sets.includes(csvSet), 'Reuse lists the prepared set of session 1 (' + csvSet + ')');
  await p.click(`#svc-sets-table a[data-set="${csvSet}"]`);
  await p.waitForFunction(() => document.querySelector('#wiz-data-chip').textContent === 'Ready', null, { timeout: 30000 });
  const job3 = await pickAndRun(p, [1, 3]);
  log(true, 'reused prepared set ' + csvSet + ' -> session ' + job3);

  // 8. Compare
  await p.click('#session-compare'); await p.waitForSelector('#cmp-a option[value="' + job3 + '"]', { state: 'attached' });
  await p.waitForFunction(id => document.querySelector('#cmp-a').value === id, job3);
  log(await p.$eval('#cmp-a', s => s.value) === job3, 'Compare is preselected from the session page');
  await p.selectOption('#cmp-b', job1); await p.click('#cmp-go');
  await p.waitForSelector('#cmp-validity');
  log(/Like-for-like comparison/.test(await p.textContent('#cmp-validity')), 'Compare: same set + GPU + settings -> like-for-like');
  log((await p.$$('#cmp-table thead th')).length === 5 && (await p.$$('#cmp-table tbody tr')).length >= 14, 'Compare: every efficiency metric, 4 model columns');
  await p.screenshot({ path: S + '/09-compare-like.png', fullPage: true });
  await p.selectOption('#cmp-b', job2); await p.click('#cmp-go');
  await p.waitForFunction(() => /Not like-for-like/.test(document.querySelector('#cmp-validity').textContent));
  const vt = await p.textContent('#cmp-validity');
  log(/Same prepared set/.test(vt) && /differs/.test(vt), 'Compare: CSV vs PCAP -> NOT like-for-like, names the prepared set');
  log(!(await p.$('#cmp-table td.best')), 'Compare: no best-value marks when not like-for-like');
  await p.screenshot({ path: S + '/10-compare-not.png', fullPage: true });

  // 9. Leaderboard
  await p.click('.mainnav a[data-nav="leaderboard"]'); await p.waitForSelector('.lb-group');
  const groups = await p.$$('.lb-group');
  log(groups.length === 2, 'Leaderboard: 2 groups (CSV set, PCAP set) — ' + groups.length);
  const g0 = await p.textContent('.lb-group');
  log(/2 sessions/.test(g0), 'Leaderboard: the CSV group pools its 2 sessions');
  const ranks = await p.$$eval('#lb-table-0 tbody tr td:first-child', ts => ts.map(t => t.textContent));
  log(ranks.join(',') === '1,2', 'Leaderboard: ranks restart within each group (' + ranks + ')');
  await p.check('#lb-sort input[value="vram"]');
  await p.waitForFunction(() => { const t = document.querySelector('#lb-table-0 tbody td'); return t && /—/.test(t.textContent); });
  log(true, 'Leaderboard: sort by VRAM on mock data -> unranked (no VRAM reading)');
  await p.check('#lb-sort input[value="latency"]'); await p.waitForSelector('#lb-table-0');
  await p.screenshot({ path: S + '/11-leaderboard.png', fullPage: true });

  // 10. Settings: theme toggle (persisted)
  await p.click('.iconbtn[aria-label="Settings"]'); await p.waitForSelector('#view-settings:not([hidden])');
  await p.check('#set-theme-light');
  log(await p.evaluate(() => document.documentElement.getAttribute('data-theme')) === 'light', 'Settings: Light applies');
  await p.reload(); await p.waitForSelector('#view-settings:not([hidden])');
  log(await p.evaluate(() => document.documentElement.getAttribute('data-theme')) === 'light' && await p.isChecked('#set-theme-light'), 'Settings: theme persists across reload');
  const bgLight = await p.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await p.check('#set-theme-dark');
  const bgDark = await p.evaluate(() => getComputedStyle(document.body).backgroundColor);
  log(bgLight === 'rgb(246, 247, 249)' && bgDark === 'rgb(18, 22, 28)', 'Settings: tokens switch (' + bgLight + ' -> ' + bgDark + ')');
  await p.check('#set-theme-system');
  log(await p.evaluate(() => !document.documentElement.hasAttribute('data-theme')), 'Settings: System follows the OS');

  // 11. phone width
  const ph = await (await b.newContext({ viewport: { width: 375, height: 800 }, colorScheme: 'dark' })).newPage();
  ph.on('pageerror', e => errs.push('phone: ' + e.message));
  for (const [hash, sel] of [['#/', '#home-latest:not([hidden])'], ['#/new', '#svc-upload-form:not([hidden])'],
                             ['#/session/' + job1, '#session-body:not([hidden])'], ['#/leaderboard', '.lb-group'],
                             ['#/compare?a=' + job1 + '&b=' + job2, '#cmp-validity']]) {
    await ph.goto(BASE + '/' + hash); await ph.waitForSelector(sel, { timeout: 20000 });
    const over = await ph.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    log(over <= 0, 'phone 375px: no horizontal page scroll on ' + hash + ' (' + over + ')');
  }
  await ph.goto(BASE + '/#/session/' + job1); await ph.waitForSelector('#session-body:not([hidden])');
  const navH = await ph.evaluate(() => document.querySelector('.mainnav').getBoundingClientRect().height);
  log(navH > 44, 'phone: header nav wraps (' + Math.round(navH) + 'px)');
  const small = await ph.$$eval('.mainnav a, .btn, .iconbtn', els => els.filter(e => e.offsetParent && e.getBoundingClientRect().height < 44).length);
  log(small === 0, 'phone: nav links and buttons are >= 44px tall');
  await ph.screenshot({ path: S + '/12-phone-session.png', fullPage: true });
  await ph.goto(BASE + '/'); await ph.waitForSelector('#home-latest:not([hidden])');
  await ph.screenshot({ path: S + '/13-phone-home.png', fullPage: true });

  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  console.log(out.join('\n')); console.log('screenshots: ' + S); await b.close();
  if (out.some(l => l.startsWith('FAIL'))) process.exit(1);
})().catch(e => { console.log(out.join('\n')); console.log('CRASH', e.message); process.exit(1); });
