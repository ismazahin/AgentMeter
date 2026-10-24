// Session insights — walk-through of the added analysis views of a session (Playwright,
// mock backend): one labelled 2-model CSV session and one unlabelled 1-model PCAP session.
// The arithmetic is unit-tested in tests/test_insights.py; this checks the views render,
// react to their controls, stay honest on missing data, and fit a phone screen.
//   AGENTMETER_RESULTS_DIR=$(mktemp -d) python scripts/serve.py --provider mock --port 8766
//   node tests/e2e/insights_flow.js [base_url] [out_dir]
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const ORIGIN = process.argv[2] || 'http://127.0.0.1:8766';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'insights-e2e-'));
fs.mkdirSync(S, { recursive: true });
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

async function session(p, file, flows, idx) {
  await p.goto(ORIGIN + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
  await p.setInputFiles('#svc-file', file); await p.fill('#svc-max-flows', String(flows));
  await p.click('#svc-upload-btn');
  await p.waitForFunction(() => /Ready/.test(document.querySelector('#wiz-data-chip').textContent), null, { timeout: 90000 });
  for (const i of idx) await p.locator('#svc-models input').nth(i).check();   // re-resolved: the list re-renders as the catalog loads
  await p.click('#svc-run');
  await p.waitForFunction(() => /^#\/session\//.test(location.hash), null, { timeout: 180000 });
  await p.waitForSelector('#session-body:not([hidden])', { timeout: 30000 });
  return decodeURIComponent(p.url().split('#/session/')[1].split('/')[0]);
}
async function tab(p, id, t, sel) {
  await p.goto(ORIGIN + '/#/session/' + encodeURIComponent(id) + '/' + t);
  await p.waitForSelector(sel, { timeout: 30000 });
}
async function hoverText(p, sel) {        // the hover value of a chart mark (data-hover)
  await p.locator(sel).first().scrollIntoViewIfNeeded(); await p.waitForTimeout(150);
  await p.locator(sel).first().hover(); await p.waitForSelector('.tip-pop', { timeout: 3000 });
  return p.textContent('.tip-pop');
}
const texts = (p, sel) => p.$$eval(sel, els => els.map(e => e.textContent.trim()));

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const ctx = await b.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  p.on('console', m => { if (m.type() === 'error' && !/fonts|ERR_|Failed to load resource/.test(m.text())) errs.push(m.text()); });
  try {
    const csv = await session(p, REPO + '/data/sample_csv/cicids2017_sample.csv', 40, [0, 3]);
    const pcap = await session(p, REPO + '/data/sample_pcaps/sample_small.pcap', 8, [2]);

    // Phase 1 — Overview: key findings + criteria profile
    await tab(p, csv, 'overview', '#svc-findings');
    const f = await p.$$eval('#svc-findings li', ls => ls.map(l => l.getAttribute('data-finding')));
    log(['gap', 'tail', 'agent', 'overhead', 'cost', 'valid'].every(k => f.includes(k)) && !f.includes('accuracy'), 'overview: key findings cover gap, tail, agent, overhead, cost, validity — no accuracy (' + f.join(',') + ')');
    log((await p.$$('#svc-profile .prof-row')).length === 3 && (await p.$$('#svc-profile .prof-row[data-crit="latency"] .ibar')).length === 2 && !(await p.$('#svc-profile .prof-row[data-crit="accuracy"]')),
        'overview: criteria profile has the 3 efficiency criteria x 2 models');
    log(/no readings/.test(await p.textContent('#svc-profile .prof-row[data-crit="vram"]')), 'overview: VRAM without readings is labelled, not drawn as a score');
    log(/Latency: 1\.00 of 1\.00/.test(await hoverText(p, '#svc-profile .prof-row[data-crit="latency"] .ibar')), 'overview: hovering a profile bar shows its value');
    log((await p.$$('#svc-profile-table tbody tr')).length === 2, 'overview: profile has a table view');
    await tab(p, pcap, 'overview', '#svc-findings');
    log(!(await texts(p, '#svc-findings li')).some(t => /ccuracy/.test(t)) && (await p.$$('#svc-profile .prof-row')).length === 3,
        'overview (PCAP): no accuracy finding; the same 3 efficiency criteria');

    // Analysis expansion, phase 1 — scorecard, what changed, rank stability, efficiency frontier
    await tab(p, csv, 'overview', '#svc-scorecard');
    log((await p.$$('#svc-scorecard-table tbody tr')).length === 2 && (await p.$$('#svc-scorecard-table thead th')).length === 7 &&
        (await p.$$('#svc-scorecard-table td[data-grade="best"]')).length >= 1, 'overview: scorecard grades each model on 6 dimensions');
    await p.waitForSelector('#svc-changed');
    log(/No earlier session|Since the last run/.test(await p.textContent('#svc-changed')), 'overview: "Since the last run" card answers (no earlier run yet)');
    await tab(p, csv, 'ranking/robustness', '#rank-boot');
    log((await p.$$('#rank-boot-bars .hbar')).length === 2 && (await p.$$('#rank-boot-table tbody tr')).length === 2 && /resampled 500 times/.test(await p.textContent('#rank-boot')),
        'robustness: bootstrap — how often each model comes first (500 resamples)');
    await tab(p, csv, 'ranking/tradeoffs', '#frontier-plot svg');
    log((await p.$$('#frontier-plot circle')).length === 2 && (await p.$$('#rank-frontier-table tbody tr')).length === 2 && /on the frontier|beaten/.test(await hoverText(p, '#frontier-plot g[data-hover] circle')),
        'trade-offs: efficiency frontier — one dot per model, hover says frontier or beaten');
    await p.selectOption('#frontier-y', 'tokens'); await p.waitForTimeout(200);
    log(/Tokens per flow/.test(await p.textContent('#rank-frontier-table thead')), 'trade-offs: the second axis can be changed (tokens)');

    // Analysis expansion, phase 3 — capacity (queue simulation), cold start, what drives latency
    await tab(p, csv, 'performance/capacity', '#cap-table');
    log((await p.$$('#cap-table tbody tr')).length === 2 && (await p.$$('#cap-table tbody tr[data-keeps="true"]')).length === 2, 'capacity: at the suggested rate both models keep up');
    const cap = Number((await p.textContent('#cap-table tbody tr td:nth-child(2)')).replace(/,/g, ''));
    await p.fill('#cap-rate', String(Math.ceil(cap * 3))); await p.waitForTimeout(300);
    log((await p.$$('#cap-table tbody tr[data-keeps="false"]')).length >= 1 && /backlog grows/.test(await p.textContent('#cap-table')), 'capacity: three times the capacity -> falls behind, backlog per hour shown');
    log((await p.$$('#cold-start-table tbody tr')).length === 2 && !/not recorded/.test(await p.textContent('#cold-start-table')), 'capacity: cold start — load time and first-flow penalty per model');
    await tab(p, csv, 'performance/drivers', '#drivers-card');
    log((await p.$$('#drivers-card .drivers-table')).length === 2 && (await p.$$('#drivers-card .drivers-table tbody tr')).length >= 2, 'drivers: the flow features that go with latency, per model');
    log(!!(await p.$('#scaling-card')), 'drivers: prompt size and latency card (a slope, or why there is none)');

    // Analysis expansion, phase 4 — prompt composition, removing an agent, token budgets, other pipelines
    await tab(p, csv, 'agents', '#agents-composition');
    log((await p.$$('#agents-composition .cat-row')).length === 8 && /cacheable \d/.test(await p.textContent('#agents-composition')), 'agents: what the prompts are made of (2 models x 4 agents), cacheable share');
    log((await p.$$('#agents-remove .remove-table tbody tr')).length === 8 && /cannot be removed/.test(await p.textContent('#agents-remove')), 'agents: what removing each agent saves; the label agent cannot be removed');
    log((await p.$$('#agents-budget-table tbody tr')).length === 8 && /can be lower|fits|raise it/.test(await p.textContent('#agents-budget-table')), 'agents: token budget advice per agent');
    await p.waitForSelector('#agents-pipelines');
    log(/No other session ran a different pipeline/.test(await p.textContent('#agents-pipelines')), 'agents: other pipelines on these flows (none yet, says how)');

    // Phase 2 — Ranking -> Robustness: weight range in which #1 stays first
    await tab(p, csv, 'ranking/robustness', '#svc-stability');
    log((await p.$$('#svc-stability .stab-row')).length === 3 && (await p.$$('#svc-stability .stab-mark')).length === 3,
        'robustness: one strip per efficiency criterion (latency, VRAM, tokens), each with the stored-weight marker');
    const stab = await texts(p, '#svc-stability .stab-text');
    log(/no readings/i.test(stab[1]) && stab.every(t => /tie|stays first|no readings/i.test(t)), 'robustness: every strip says who stays first, a tie, or no readings');
    log((await p.$$('#svc-stability-table tbody tr')).length === 3, 'robustness: table view');
    await tab(p, pcap, 'ranking/robustness', '#svc-stability');
    log(/Single-model run/.test(await p.textContent('#svc-stability')), 'robustness (1 model): says there is no ranking to change');

    // Output Reliability (replaces the accuracy view): usable answers, failures, a reference-label check
    await tab(p, csv, 'reliability', '#rel-table');
    log((await p.$$('#rel-bars .hbar')).length === 2 && /valid \(\d+ of 40 flows\)/.test(await hoverText(p, '#rel-bars .track')), 'reliability: one valid-answer bar per model; hover shows the count');
    log((await p.$$('#rel-table tbody tr')).length === 2 && /Act agrees with decide|act agrees with decide/i.test(await p.textContent('#rel-table thead')) && /Reference-label check/.test(await p.textContent('#rel-table thead')),
        'reliability: table with failures, Decide/Act agreement and the reference-label check');
    log((await p.$$('#rel-failures-table tbody tr')).length === 8 && !/F1|precision|recall|confusion/i.test(await p.textContent('#spanel-reliability')),
        'reliability: failures per agent (2 models x 4 agents); no detection metrics');
    log((await p.$$('#rel-consistency-table tbody tr')).length === 2 && /repeat check/.test(await p.textContent('#rel-consistency')),
        'reliability: consistency of answers (format, repeat, technique); says how to get the repeat check');
    await tab(p, pcap, 'reliability', '#rel-table');
    log(!/Reference-label check/.test(await p.textContent('#rel-table thead')), 'reliability (PCAP): works without labels, no reference-label check');

    // Phase 4 — Performance: latency histogram small multiples + effect-size charts
    await tab(p, csv, 'performance/latency', '#stats-histogram');
    log((await p.$$('#stats-histogram .histo-panel')).length === 2 && (await p.$$('#stats-histogram .histo-panel:first-child .pm')).length === 3,
        'latency: one histogram per model with p50/p95/p99 lines');
    log(/: \d+ flows?$/.test(await hoverText(p, '#stats-histogram .hb')), 'latency: hovering a bin shows its range and flow count');
    await tab(p, csv, 'performance/statistics', '#stats-effect-chart');
    log((await p.$$('#stats-effect-chart .ef-row')).length === 5 && /no VRAM readings/.test(await p.textContent('#stats-effect-chart')),
        "statistics: Cliff's delta for 3 metrics (VRAM unavailable, said so) + a CI strip for the 2 measured");
    log(/95% CI/.test(await hoverText(p, '#stats-effect-chart .ci ~ .dot')), 'statistics: hovering a CI dot shows the interval');
    await tab(p, pcap, 'performance/statistics', '#stats-effects-card');
    log(!(await p.$('#stats-effect-chart')), 'statistics (1 model): no effect chart');

    // Phase 5 — Agent Analysis: hand-off diagram + time per agent by class
    await tab(p, csv, 'agents', '#agents-handoffs');
    const ho = await p.$$eval('#agents-handoffs .ho', hs => hs.map(h => [h.querySelectorAll('.node').length, h.querySelectorAll('.edge').length]));
    log(ho.length === 2 && ho.every(x => x[0] === 4 && x[1] >= 3), 'agents: hand-off diagram per model (4 steps, arrows) ' + JSON.stringify(ho));
    const resent = await p.$$eval('#agents-handoffs .ho h3 .muted', e => e.map(x => x.textContent));
    log(resent.every(t => /tokens re-sent per flow/.test(t)), 'agents: tokens re-sent per flow stated per model');
    log(/re-sent per flow/.test(await hoverText(p, '#agents-handoffs .edge:has(line)')), 'agents: hovering an arrow shows its tokens');
    const cat = await p.$$eval('#agents-by-class .cat-group', gs => gs.map(g => g.querySelectorAll('.cat-row').length));
    log(cat.length === 2 && cat.every(n => n === 5),
        'agents: time per agent for each of the 5 classes, per model');
    log(/ms \(\d/.test(await hoverText(p, '#agents-by-class .cat-bar span')), 'agents: hovering a segment shows step time and share');

    // Phase 6 — Cost & Energy: workload projection + cost per correct answer
    await tab(p, csv, 'cost', '#proj-table');
    log(/Demo \(mock\)/.test(await p.textContent('#proj-out')) && /enter a GPU price/.test(await p.textContent('#proj-table')),
        'cost: projection says it is a demo and asks for a GPU price when none is stored');
    await p.fill('#proj-price', '2'); await p.fill('#proj-flows', '86400');
    const pr = await p.$$eval('#proj-table tbody tr', rs => rs.map(r => [...r.cells].map(c => c.textContent.trim())));
    log(pr.length === 2 && pr.every(r => /^\$\d/.test(r[4]) && /^\$\d/.test(r[5])) && /your figure, \$2\/h/.test(await p.textContent('#proj-out')),
        'cost: entering flows and a price fills cost per day and per 30 days ' + JSON.stringify(pr[0]));
    log((await p.$$('#cost-valid .cat-row')).length === 2 && (await p.$$('#cost-valid-table tbody tr')).length === 2, 'cost: GPU time per valid answer per model');
    await tab(p, pcap, 'cost', '#proj-table');
    log(!!(await p.$('#cost-valid')) && !(await p.$('#cost-correct')), 'cost (PCAP, no labels): cost per valid answer still shown (it needs no labels)');

    // Phase 7 — Recommendations: deployment fit at a glance
    await tab(p, csv, 'recommendations/limits', '#fit-table');
    const fit = await p.$$eval('#fit-table tbody tr[data-profile]', rs => rs.map(r => [...r.querySelectorAll('[data-result]')].map(c => c.getAttribute('data-result'))));
    log(fit.length === 10 && fit.every(r => r.length === 2), 'recommendations: 10 profiles x 2 models');
    const fr = Object.fromEntries((await p.$$eval('#fit-table tbody tr[data-profile]', rs => rs.map(r => r.getAttribute('data-profile')))).map((id, i) => [id, fit[i]]));
    log(fr.gpu8.every(x => x === 'no_data') && fr.usd1.every(x => x === 'no_data') && fr.rt2.every(x => x === 'pass'),
        'recommendations (mock): memory and budget have no data, response time fits');
    log(/no data — /.test(await hoverText(p, '#fit-table tr[data-profile="gpu8"] [data-result]')), 'recommendations: hovering a "no data" cell says why');
    log(!!(await p.$('#dh-form')), 'recommendations: "Your limits" form still below the matrix');

    // Phase 8 — new measurements: latency over the run, GPU timeline, repeatability, load scaling
    await tab(p, csv, 'performance/latency', '#stats-trend');
    log((await p.$$('#stats-trend .trend-panel')).length === 2 && (await texts(p, '#stats-trend .trend-note')).every(t => /over the run|Too few flows/.test(t)),
        'latency over the run: one panel per model with its drift verdict');
    const tb = await p.locator('#trend-0 svg').boundingBox();
    await p.mouse.move(tb.x + tb.width * 0.5, tb.y + tb.height * 0.5);
    log(/flow \d+: .*rolling median/.test(await p.textContent('.tip-pop').catch(() => '')), 'latency over the run: crosshair shows the flow, its time and the rolling median');
    await tab(p, csv, 'cost', '#gpu-timeline');
    log(/Not sampled: mock\/CPU run/.test(await p.textContent('#gpu-timeline')), 'GPU over the run (mock): says it was not sampled and why');
    await tab(p, csv, 'performance/gpu', '#load-card');
    log(/Not measured in this session/.test(await p.textContent('#load-card')), 'load scaling: not measured unless asked for');
    await tab(p, csv, 'performance/statistics', '#svc-repeat');
    log(/No other session ran on these flows/.test(await p.textContent('#svc-repeat')), 'repeatability: no other run yet, offers to benchmark the flows again');
    // run the same flows again, this time with load scaling
    await p.click('#svc-repeat a.btn[href^="#/new/models?set="]');
    await p.waitForFunction(() => /Ready/.test(document.querySelector('#wiz-data-chip').textContent), null, { timeout: 60000 });
    await p.locator('#svc-models input').nth(1).check();            // never benchmarked: no estimate
    await p.waitForFunction(() => /Estimate/.test(document.querySelector('#wiz-settings').textContent), null, { timeout: 15000 });
    log(/no earlier session of .* in demo mode yet/.test(await p.textContent('#wiz-settings')), 'estimate: a model never run before gets no estimate, and says so');
    await p.locator('#svc-models input').nth(1).uncheck();
    for (const i of [0, 3]) await p.locator('#svc-models input').nth(i).check();
    log(/Estimate\s*about \d+ s of model time \(from earlier sessions in demo mode; model loading/.test(await p.textContent('#wiz-settings')),
        'estimate: time before Run from the earlier session of the same models');
    await p.check('#svc-load-test'); await p.click('#svc-run');
    await p.waitForFunction(() => /^#\/session\//.test(location.hash), null, { timeout: 180000 });
    const again = decodeURIComponent(p.url().split('#/session/')[1].split('/')[0]);
    await tab(p, again, 'performance/gpu', '#load-table');
    log((await p.$$('#load-table tbody tr')).length === 6 && (await p.$$('#load-card .lc')).length === 2, 'load scaling: 2 models x 3 levels, throughput and p95 charts');
    await tab(p, csv, 'performance/statistics', '#svc-repeat .rep-row');
    log(/2 sessions on this setup/.test(await p.textContent('#svc-repeat')) && (await p.$$('#svc-repeat .dot.cur')).length > 0,
        'repeatability: the first session now sees the re-run, ringed dot marks itself');
    const r1 = await (await fetch(ORIGIN + '/api/jobs/' + encodeURIComponent(csv) + '/result')).json();
    const r2 = await (await fetch(ORIGIN + '/api/jobs/' + encodeURIComponent(again) + '/result')).json();
    log(r1.session.run_id !== r2.session.run_id, 'each session keeps its own results after a re-run on the same flows');

    // Phase B — sample-size advisor + carbon
    await tab(p, csv, 'performance/statistics', '#svc-samplesize');
    log((await p.$$('#svc-samplesize-table tbody tr')).length === 3 && /cannot be told apart reliably/.test(await p.textContent('#svc-samplesize-lead')),
        'statistics: how many flows are needed for 5 / 10 / 20% latency differences');
    await tab(p, pcap, 'performance/statistics', '#svc-samplesize');
    log(/mean latency is known to within/.test(await p.textContent('#svc-samplesize-lead')), 'statistics (1 model): precision of the mean instead');
    await tab(p, csv, 'cost', '#proj-table');
    log(/CO₂ \/ day/.test(await p.textContent('#proj-table thead')) && /needs measured energy/.test(await p.textContent('#proj-table tbody')),
        'cost: CO₂ column, honest when energy was not measured');

    // Phase C — notes, tags, search
    await tab(p, csv, 'overview', '#svc-notes');
    await p.fill('#svc-notes-text', 'rerun after the tokenizer fix'); await p.fill('#svc-notes-tags', 'thesis ch4, baseline, Baseline');
    await p.click('#svc-notes-save'); await p.waitForFunction(() => /Saved/.test(document.getElementById('svc-notes-saved').textContent));
    log(await p.inputValue('#svc-notes-tags') === 'thesis ch4, baseline', 'notes: saved; tags trimmed and de-duplicated');
    await p.goto(ORIGIN + '/#/sessions'); await p.waitForSelector('#sessions-table');
    await p.fill('#flt-q', 'tokenizer'); await p.waitForTimeout(900);
    const found = await p.$$eval('#sessions-table tbody tr', rs => rs.map(r => r.textContent));
    log(found.length === 1 && /rerun after the tokenizer fix/.test(found[0]), 'sessions: search finds the session by its notes');
    await p.fill('#flt-q', ''); await p.waitForTimeout(900);
    await p.click('#sessions-table [data-tag="thesis ch4"]'); await p.waitForTimeout(900);
    log((await p.$$('#sessions-table tbody tr')).length === 1 && !(await p.isHidden('#flt-tag-row')), 'sessions: clicking a tag filters by it');
    await p.click('#flt-tag'); await p.waitForTimeout(900);
    log((await p.$$('#sessions-table tbody tr')).length >= 3, 'sessions: removing the tag filter lists all again');

    // Phase D — tab summaries, downloads, first-visit guides
    for (const [t, k] of [['ranking/scores', 'ranking'], ['reliability', 'reliability'], ['performance/latency', 'performance'], ['agents', 'agents'], ['cost', 'cost'], ['recommendations/limits', 'recommendations']]) {
      await tab(p, csv, t, `.tab-summary[data-summary="${k}"]`);
    }
    log(true, 'every session tab opens with a one-line summary');
    await tab(p, csv, 'performance/latency', '#stats-depth .exp-bar');
    const [dl] = await Promise.all([p.waitForEvent('download'), p.click('#stats-depth .exp-bar [data-exp="csv"]')]);
    const csvText = fs.readFileSync(await dl.path(), 'utf8');
    log(/^Model,Flows,p50,p95,p99/.test(csvText) && csvText.trim().split('\n').length === 3, 'downloads: a card\'s table as CSV (' + dl.suggestedFilename() + ')');
    const [png] = await Promise.all([p.waitForEvent('download'), p.click('#stats-trend .exp-bar [data-exp="png"]')]);
    log(fs.readFileSync(await png.path()).slice(1, 4).toString() === 'PNG', 'downloads: a line chart as PNG');
    await p.goto(ORIGIN + '/#/'); await p.waitForSelector('#home-guide:not([hidden])');
    await p.click('#home-guide-hide'); await p.reload(); await p.waitForSelector('#view-home:not([hidden])'); await p.waitForTimeout(300);
    log(await p.isHidden('#home-guide'), 'home: the first-visit guide stays hidden after "Got it"');

    // Phase E — flow explorer
    await tab(p, csv, 'agents', '#flows-table');
    log((await p.$$('#flows-table tbody tr')).length === 25 && /40 flows × 2 models/.test(await p.textContent('#flows-status')), 'flow explorer: 40 flows x 2 models, 25 per page');
    await p.selectOption('#flows-filter', 'wrong'); await p.waitForTimeout(200);
    const wrong = await p.$$eval('#flows-table tbody tr', rs => rs.map(r => r.textContent));
    log(wrong.length > 0 && wrong.every(t => /✗/.test(t)), 'flow explorer: "wrong answer" keeps only flows a model got wrong');
    await p.click('#flows-table [data-flow]'); await p.waitForSelector('.flow-detail');
    log((await p.$$('.flow-detail .flow-col')).length === 2 && (await p.$$('.flow-detail .flow-col:first-child .flow-text')).length === 4,
        'flow explorer: a flow opens with each model\'s four agent answers');
    await tab(p, pcap, 'agents', '#flows-table');
    log(!(await p.$('#flows-filter option[value="wrong"]')) && !(await p.$('#flows-filter option[value="disagree"]')), 'flow explorer (unlabelled, 1 model): no "wrong" or "disagree" filters');

    // Sessions list: a short note from the row; no Delete without a control plane (nothing to delete it with)
    await p.goto(ORIGIN + '/#/sessions'); await p.waitForSelector('#sessions-table');
    await p.click(`tr:has(a[data-open="${pcap}"]) [data-note]`); await p.waitForSelector('.note-row textarea');
    await p.fill('.note-row textarea', 'pcap smoke test'); await p.click('.note-row button[type="submit"]');
    const snip = await p.waitForSelector(`#sessions-table tr:has(a[data-open="${pcap}"]) .note-snip`, { timeout: 15000 }).then(e => e.textContent()).catch(() => '');
    log(snip === 'pcap smoke test' && !(await p.$('#sessions-table [data-del]')), 'sessions: "Note" from the list saves; no Delete button without a control plane (' + JSON.stringify(snip) + ')');

    // up to 4 models in one session (Phase H): one verdict over all of them, every pair, 4 colours
    const four = await session(p, REPO + '/data/sample_csv/cicids2017_sample.csv', 20, [0, 1, 2, 3]);
    await tab(p, four, 'overview', '#svc-headline');
    log(/most efficient of 4 models|no model beats all 3 others|All 4 models tie/.test(await p.textContent('#svc-headline')), 'four models: the headline speaks about all 4 (' + await p.textContent('#svc-headline') + ')');
    log((await p.$$('#svc-models-table tbody tr')).length === 4 && /Fair comparison/.test(await p.textContent('#svc-caveats')), 'four models: per-model table has 4 rows; caveat says "Fair comparison"');
    await tab(p, four, 'performance/comparison', '#svc-relative-table');
    log((await p.$$('#svc-relative-table thead th')).length === 6 && (await p.$$('#svc-pairs-table tbody tr')).length === 6 && (await p.$$('#svc-pair-ranking li')).length === 4,
        'four models: comparison has 4 model columns, 6 pairs and a 4-place ranking');
    await tab(p, four, 'performance/latency', '#stats-histogram');
    const sw = await p.$$eval('#session-body .ilegend i, #session-body .sw', els => [...new Set(els.map(e => e.getAttribute('style') || ''))].filter(s => /series/.test(s)));
    log(sw.length >= 4 && sw.every(x => /mseries-[1-4]/.test(x)), 'four models: charts use the 4-model palette (' + sw.length + ' colours)');
    await tab(p, four, 'performance/statistics', '#stats-effect-pairs');
    log((await p.$$('#stats-effect-pairs tbody tr')).length === 6, 'four models: effect sizes for every pair');

    // every view at phone width: no horizontal page scroll
    await p.setViewportSize({ width: 375, height: 800 });
    let maxW = 0;
    for (const t of ['overview', 'ranking/robustness', 'ranking/tradeoffs', 'performance/capacity', 'performance/drivers', 'reliability', 'performance/latency', 'performance/statistics', 'agents', 'cost', 'recommendations/limits', 'performance/gpu']) {
      await tab(p, csv, t, '#session-body:not([hidden])'); await p.waitForTimeout(300);
      maxW = Math.max(maxW, await p.evaluate(() => document.documentElement.scrollWidth));
    }
    for (const t of ['overview', 'performance/comparison', 'performance/statistics']) {
      await tab(p, four, t, '#session-body:not([hidden])'); await p.waitForTimeout(300);
      maxW = Math.max(maxW, await p.evaluate(() => document.documentElement.scrollWidth));
    }
    await p.goto(ORIGIN + '/#/sessions'); await p.waitForSelector('#sessions-table'); await p.waitForTimeout(300);
    maxW = Math.max(maxW, await p.evaluate(() => document.documentElement.scrollWidth));
    log(maxW <= 375, 'no horizontal page scroll at 375px (max scrollWidth ' + maxW + ')');
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
