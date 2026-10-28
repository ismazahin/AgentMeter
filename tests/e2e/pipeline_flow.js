// Custom agents — walk-through of the wizard's agent editor and a session run with the
// user's own pipeline (Playwright, mock backend). The rules are unit-tested in
// tests/test_custom_pipeline.py; this checks the editor guides the user to a valid
// pipeline, sends it, and that the session's Agent Analysis shows every agent.
//   AGENTMETER_RESULTS_DIR=$(mktemp -d) python scripts/serve.py --provider mock --port 8766
//   node tests/e2e/pipeline_flow.js [base_url] [out_dir]
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const ORIGIN = process.argv[2] || 'http://127.0.0.1:8766';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'pipeline-e2e-'));
fs.mkdirSync(S, { recursive: true });
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);
const texts = (p, sel) => p.$$eval(sel, els => els.map(e => e.textContent.trim()));

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const ctx = await b.newContext({ viewport: { width: 1280, height: 900 } });
  const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  p.on('console', m => { if (m.type() === 'error' && !/fonts|ERR_|Failed to load resource/.test(m.text())) errs.push(m.text()); });
  let posted = null;
  p.on('request', r => { if (r.method() === 'POST' && /\/api\/jobs$/.test(r.url())) posted = JSON.parse(r.postData() || '{}'); });
  try {
    await p.goto(ORIGIN + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
    await p.setInputFiles('#svc-file', REPO + '/data/sample_csv/cicids2017_sample.csv'); await p.fill('#svc-max-flows', '8');
    await p.evaluate(() => { const r = document.getElementById('svc-prep-server'); if (r) r.checked = true; }); await p.click('#svc-upload-btn');   // server-side preparation (prep_flow.js covers this computer)
    await p.waitForFunction(() => /Ready/.test(document.querySelector('#wiz-data-chip').textContent), null, { timeout: 90000 });
    await p.locator('#svc-models input').nth(0).check();

    // the default: no editor, the settings say so
    log(await p.isChecked('#pl-mode-default') && await p.isHidden('#pl-editor') && /Perceive → Reason → Decide → Act \(default\)/.test(await p.textContent('#wiz-settings')),
        'default pipeline: editor hidden, settings name the default chain');
    await p.check('#pl-mode-custom');
    log((await p.$$('#pl-list .pl-item')).length === 4 && /Ready/.test(await p.textContent('#pl-check')), '"Build my own" starts from the default four');

    // no answer agent: refused before the run
    await p.click('#pl-list .pl-item[data-i="3"] [data-pl="del"]'); await p.click('#pl-list .pl-item[data-i="2"] [data-pl="del"]');
    log(await p.isDisabled('#svc-run') && /Add an agent that gives the answer/.test(await p.textContent('#pl-check')),
        'without a label agent: Run is disabled and the editor says why');
    // a Classify template fixes it; a blank agent of your own needs instructions
    await p.selectOption('#pl-add-kind', 't:classify'); await p.click('#pl-add');
    log(!(await p.isDisabled('#svc-run')), 'adding the Classify template (a label agent) makes it runnable');
    await p.selectOption('#pl-add-kind', 't:agent'); await p.click('#pl-add');
    log(await p.isDisabled('#svc-run') && /title/.test(await p.textContent('#pl-check')), 'a blank agent of your own must be filled in first');
    const own = '#pl-list .pl-item[data-i="3"]';
    await p.fill(own + ' [data-f="title"]', 'Escalation'); await p.fill(own + ' [data-f="name"]', 'escalate');
    await p.fill(own + ' [data-f="instructions"]', 'Say in one line whether a human analyst must look at this flow now, and why.');
    await p.locator(own + ' [data-f="title"]').blur();
    log(!(await p.isDisabled('#svc-run')) && /Perceive → Reason → Classify → Escalation/.test(await p.textContent('#pl-check')), 'your own agent, filled in: the pipeline is ready');
    // reads follow renames; a label agent placed after a verdict is refused
    log(await p.isChecked(own + ' [data-read="2"]'), 'a new agent reads the agent before it by default');
    await p.selectOption('#pl-add-kind', 't:report'); await p.click('#pl-add');
    await p.click('#pl-list .pl-item[data-i="4"] [data-pl="up"]'); await p.click('#pl-list .pl-item[data-i="3"] [data-pl="up"]');
    log(/verdict agent must come after/.test(await p.textContent('#pl-check')) && await p.isDisabled('#svc-run'), 'a verdict before the label is refused');
    await p.click('#pl-list .pl-item[data-i="2"] [data-pl="down"]');
    // Report (reads only the agent before it, no features) now comes before Escalation: no input
    log(/Report: it has no input/.test(await p.textContent('#pl-check')) && await p.isDisabled('#svc-run'), 'an agent whose only input moved after it is refused');
    await p.check('#pl-list .pl-item[data-i="3"] [data-read="2"]');
    const chain = (await p.textContent('#pl-check')).replace(/\s+/g, ' ');
    log(/Perceive → Reason → Classify → Report → Escalation/.test(chain), 'reordering works: ' + chain);
    // the tab order is still in the list after a move (focus stays on the moved control)
    log(await p.evaluate(() => !!document.activeElement.closest('#pl-list')), 'focus stays in the editor after a move');
    await p.screenshot({ path: S + '/pl-editor.png', fullPage: true });

    // it persists in this browser
    await p.goto(ORIGIN + '/#/new'); await p.reload(); await p.waitForSelector('#svc-upload-form:not([hidden])');
    log(await p.evaluate(() => JSON.parse(localStorage.getItem('agentmeter.pipeline')).agents.length) === 5, 'the pipeline is remembered in this browser');

    // run it
    await p.setInputFiles('#svc-file', REPO + '/data/sample_csv/cicids2017_sample.csv'); await p.fill('#svc-max-flows', '8');
    await p.evaluate(() => { const r = document.getElementById('svc-prep-server'); if (r) r.checked = true; }); await p.click('#svc-upload-btn');   // server-side preparation (prep_flow.js covers this computer)
    await p.waitForFunction(() => /Ready/.test(document.querySelector('#wiz-data-chip').textContent), null, { timeout: 90000 });
    await p.locator('#svc-models input').nth(0).check(); await p.locator('#svc-models input').nth(1).check();
    log(await p.isChecked('#pl-mode-custom') && /Perceive → Reason → Classify → Report → Escalation \(your own pipeline, 5 agents\)/.test(await p.textContent('#wiz-settings')),
        'after a reload: still "Build my own", settings show the chain');
    await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/session\//.test(location.hash) || !document.querySelector('#svc-run-error').hidden, null, { timeout: 180000 });
    if (!(await p.isHidden('#svc-run-error'))) throw new Error('run refused: ' + await p.textContent('#svc-run-error'));
    const pl = (posted || {}).pipeline || [];
    log(pl.length === 5 && pl[0] === 'perceive' && pl[2].kind === 'label' && pl[4].name === 'escalate' && pl[4].reads.join() === 'classify' && pl[4].max_new_tokens === 192,
        'the run request carries the pipeline (' + JSON.stringify(pl.map(a => a.name || a)) + ')');
    await p.waitForSelector('#session-body:not([hidden])', { timeout: 120000 });
    const id = decodeURIComponent(p.url().split('#/session/')[1].split('/')[0]);
    await p.goto(ORIGIN + '/#/session/' + encodeURIComponent(id) + '/agents'); await p.waitForSelector('#agents-pipeline');
    log((await texts(p, '#agents-pipeline .pl-view > li b')).join(',') === 'perceive,reason,Classify,Report,Escalation', 'Agent Analysis: the pipeline card lists the 5 agents with their titles');
    log((await p.$$('#agents-table-0 tbody tr')).length === 5 && (await p.$$('#agents-table-1 tbody tr')).length === 5, 'Agent Analysis: 5 agent rows per model');
    const colours = await p.$$eval('#agents-table-0 tbody tr .legend span', ss => ss.map(s => s.getAttribute('style')));
    log(colours.join() === [1, 2, 3, 4, 5].map(i => '--c:var(--ag-' + i + ')').join(), 'each agent keeps its pipeline position colour (slots 1-5)');
    log((await p.$$('#agents-gantt .g-row:first-child .g-seg')).length === 5, 'timeline: 5 segments per flow');
    log(/Escalation/.test(await p.textContent('#agents-handoffs')) && (await p.$$eval('#agents-handoffs .ho', hs => hs[0].querySelectorAll('.node').length)) === 5, 'hand-offs: 5 nodes, titles shown');
    await p.screenshot({ path: S + '/pl-session.png', fullPage: true });
    log((await p.$$('#agents-remove .remove-table tbody tr')).length === 10 && (await p.$$('#agents-composition .cat-row')).length === 10,
        'custom pipeline: removing-an-agent and prompt-composition cover all 5 agents per model');
    // the phone width still fits
    await p.setViewportSize({ width: 375, height: 800 }); await p.waitForTimeout(300);
    const w1 = await p.evaluate(() => document.documentElement.scrollWidth);
    await p.goto(ORIGIN + '/#/new'); await p.waitForTimeout(400);
    const w2 = await p.evaluate(() => document.documentElement.scrollWidth);
    log(Math.max(w1, w2) <= 375, 'no horizontal page scroll at 375px (' + w1 + ', ' + w2 + ')');
  } catch (e) {
    log(false, 'walk-through crashed: ' + e.message.split('\n')[0]);
  }
  log(errs.length === 0, 'no page errors ' + errs.join(' | '));
  await b.close();
  console.log(out.join('\n'));
  const failed = out.filter(l => l.startsWith('FAIL')).length;
  console.log('\n' + (out.length - failed) + ' passed, ' + failed + ' failed  (' + S + ')');
  process.exit(failed ? 1 : 0);
})();
