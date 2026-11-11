// The session explainer, end to end with the TEST-ONLY explainer (no GPU):
//   AGENTMETER_TEST_EXPLAINER=1 AGENTMETER_RESULTS_DIR=$(mktemp -d) python scripts/serve.py --provider mock --port 8766
//   node tests/e2e/explain_flow.js [base_url] [out_dir]
// A 2-model session -> the progress panel has a "Write explanation" step -> sentences built from
// checked statements replace the tab headlines and add an Overview summary -> a sentence tagged
// with the wrong statement and a pattern with a made-up quote are dropped -> Reliability shows
// What the answers show -> Deployment and Optimisation show the written openings -> on a single
// server (no accounts) the operator sees the admin-only mark -> explanation.json via API.
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const ORIGIN = process.argv[2] || 'http://127.0.0.1:8766';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'explain-e2e-'));
fs.mkdirSync(S, { recursive: true });
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const p = await (await b.newContext({ viewport: { width: 1280, height: 1000 } })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  try {
    await p.goto(ORIGIN + '/app.html#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
    await p.evaluate(() => localStorage.setItem('agentmeter.prepWhere', 'server'));
    await p.setInputFiles('#svc-file', path.join(REPO, 'data', 'sample_csv', 'cicids2017_sample.csv'));
    await p.fill('#svc-max-flows', '12'); await p.click('#svc-upload-btn');
    await p.waitForFunction(() => /Ready/.test(document.querySelector('#wiz-data-chip').textContent), null, { timeout: 90000 });
    for (const i of [0, 1]) await p.locator('#svc-models input').nth(i).check();
    await p.click('#svc-run');
    await p.waitForSelector('#tel-steps li', { timeout: 60000 });
    log(/Write explanation/.test(await p.textContent('#tel-steps')), 'progress: a "Write explanation" step');
    await p.waitForFunction(() => /^#\/session\//.test(location.hash), null, { timeout: 240000 });
    const id = decodeURIComponent(p.url().split('#/session/')[1].split('/')[0]);

    const ex = await p.evaluate(async (u) => (await fetch(u)).json(), ORIGIN + '/api/jobs/' + encodeURIComponent(id) + '/explanation');
    const sec = ex.sections || {}, ag = (ex.detail || {})['tab.agents'] || {};
    log(ex.status === 'ok' && ex.model === 'test-explainer' && Object.keys(ex.statements || {}).length > 10, 'API: explanation.json with the model and the checked statements');
    log((ag.dropped || []).some(x => /not in its statements/.test(x.why)), 'API: the sentence tagged with the wrong statement was dropped');
    log((ex.patterns_dropped || []).some(x => /word for word/.test(x.why)), 'API: the pattern with a made-up quote was dropped');
    const first = t => (t || '').split('. ')[0];

    await p.goto(ORIGIN + '/app.html#/session/' + encodeURIComponent(id) + '/overview');
    await p.waitForSelector('#svc-ai-summary', { timeout: 15000 });
    const sum = await p.textContent('#svc-ai-summary');
    log(!!sec.summary && sum.includes(first(sec.summary)) && /of 100 on the efficiency score/.test(sum), 'Overview: the written summary');
    log(!/\bAI\b|written by/i.test(await p.evaluate(() => document.querySelector('#svc-ai-summary').innerText)), 'Overview: no AI label in the visible text');
    log((await p.$$('#svc-ai-summary .ai-mark')).length === 1, 'single server (operator): the admin-only mark is shown');
    const tip = await p.getAttribute('#svc-ai-summary .ai-mark', 'data-tip');
    log(/Written by test-explainer/.test(tip) && /checked statements/.test(tip), 'the mark says which model wrote it and lists the statements it used');

    await p.goto(ORIGIN + '/app.html#/session/' + encodeURIComponent(id) + '/ranking'); await p.waitForTimeout(800);
    log((await p.textContent('#spanel-ranking .takeaway .tk-h')).includes(sec['tab.ranking.h']), 'Ranking: the written headline');
    await p.goto(ORIGIN + '/app.html#/session/' + encodeURIComponent(id) + '/agents'); await p.waitForTimeout(800);
    const agh = await p.textContent('#spanel-agents .takeaway .tk-h');
    log(agh.includes(sec['tab.agents.h']) && (await p.$$('#spanel-agents .takeaway .tk-h .ai-mark')).length === 1, 'Agents: the written headline with its mark: ' + agh.slice(0, 70));
    await p.goto(ORIGIN + '/app.html#/session/' + encodeURIComponent(id) + '/reliability'); await p.waitForTimeout(800);
    log(await p.locator('#svc-ai-patterns .ap-item').count() === (ex.patterns || []).length && (ex.patterns || []).length >= 1, 'Reliability: What the answers show, one item per kept pattern');
    const ap = await p.textContent('#svc-ai-patterns');
    log(/Found in \d+ of \d+/.test(ap) && ap.includes(ex.patterns[0].quote) && !/no agent ever wrote/.test(ap), 'the pattern shows the code count and its quote, the made-up quote is not shown');

    await p.goto(ORIGIN + '/app.html#/session/' + encodeURIComponent(id) + '/recommendations/guidance');
    const fr = p.frameLocator('#analysis-frame');
    await fr.locator('#ds-rec-headline').waitFor({ timeout: 20000 });
    await p.waitForTimeout(1500);
    log((await fr.locator('#ds-rec-headline').textContent()).includes(first(sec.deployment)), 'Deployment recommendation: the written opening');
    log((await fr.locator('#ds-optim').textContent()).includes(first(sec.optimisation)), 'Optimisation guidance: the written opening');
    log(await fr.locator('#ds-rec-detail table').count() === 1, 'the computed points table stays under it');
    await p.screenshot({ path: S + '/explain.png', fullPage: true });
  } catch (e) { log(false, 'walk-through crashed: ' + e.message); }
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  await b.close();
  console.log(out.join('\n'));
  process.exit(out.some(l => l.startsWith('FAIL')) ? 1 : 0);
})();
