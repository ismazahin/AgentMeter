// Leaderboard › By setup (setup list + ranking panel) and Overall (podium + model list + placement
// panel), on a single mock GPU server (design: docs/superpowers/specs/2026-11-12-per-user-separation-design.md).
//   AGENTMETER_RESULTS_DIR=$(mktemp -d) python scripts/serve.py --provider mock --port 8766
//   node tests/e2e/leaderboard_flow.js [base_url] [out_dir]
// Seeds three sessions (tests/e2e/lb_seed.js): one setup with 4 models, one with only 1 (not enough).
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const ORIGIN = process.argv[2] || 'http://127.0.0.1:8766';
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'lb-e2e-'));
fs.mkdirSync(S, { recursive: true });
process.env.LB_BASE = ORIGIN;
const seed = require('./lb_seed.js');
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const p = await (await b.newContext({ viewport: { width: 1360, height: 900 } })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  try {
    const sd = await seed();
    await p.goto(ORIGIN + '/app.html#/leaderboard'); await p.evaluate(() => localStorage.removeItem('agentmeter.lbf'));
    await p.reload(); await p.waitForSelector('#lb-demo-only');
    log(/Only demo runs so far/.test(await p.textContent('#lb-demo-only')), 'demo runs are hidden by default, and the page says so');
    await p.click('#lb-show-demo'); await p.waitForSelector('#lb-slist');
    const items = await p.$$eval('#lb-slist .sitem', xs => xs.map(x => x.textContent));
    log(items.length === 2 && /4 models/.test(items[0]) && /Not enough/.test(items[1]), 'By setup: the setup list, most sessions first, with a Not enough badge');
    log(await p.getAttribute('#lb-slist .sitem', 'aria-current') === 'true' && (await p.$$('#lb-table-0 tbody tr')).length === 4, 'the first setup is open: 4 models ranked');
    log(/Mistral 7B/.test(await p.textContent('#lb-table-0')) && !/mistralai\//.test(await p.textContent('#lb-table-0')), 'everyday model names');
    // the measure switch re-ranks
    await p.click('#lb-sort [data-sort="latency"]'); await p.waitForFunction(() => /Latency/.test((document.querySelector('#lb-table-0 thead')?.textContent || '')));
    log(await p.getAttribute('#lb-sort [data-sort="latency"]', 'aria-pressed') === 'true' && /lower is better/.test(await p.textContent('#lb-table-0 thead')), 'Rank by Latency: lower is better');
    await p.click('#lb-sort [data-sort="efficiency"]'); await p.waitForFunction(() => /SAW/.test((document.querySelector('#lb-table-0 thead')?.textContent || '')));
    // the sessions behind a model
    await p.click('#lb-table-0 [data-open="0"]');
    log(await p.getAttribute('#lb-table-0 [data-open="0"]', 'aria-expanded') === 'true' && (await p.$$('#lb-table-0 tr.exp a[href^="#/session/"]')).length === 2,
        'a model opens its sessions (Mistral 7B ran in both), each with Open');
    log(/lb-compare/.test(await p.innerHTML('#lb-panel')) && !!(await p.$('#lb-gap')), 'Compare these models, and the gap between 1st and 2nd');
    // the one-model setup: not enough, with the next step
    await p.click('#lb-slist .sitem:nth-child(3)'); await p.waitForSelector('#lb-not-enough');
    log(/Not enough to rank yet/.test(await p.textContent('#lb-not-enough')) && /at least 2 models/.test(await p.textContent('#lb-not-enough')), 'one model: Not enough to rank yet, and why');
    log((await p.getAttribute('#lb-run-another', 'href')) === '#/new/models?set=' + encodeURIComponent(sd.b), 'Run another model on this set opens New Benchmark on that set');
    log(/setup=/.test(await p.evaluate(() => location.hash)), 'the chosen setup is in the address');
    await p.reload(); await p.waitForSelector('#lb-not-enough');
    log(true, 'a reload keeps the chosen setup');
    // filters
    await p.selectOption('#lb-input', 'PCAP').catch(() => {});
    log((await p.$$('#lb-input option')).length === 2, 'Input only offers what was measured (All, CSV)');
    await p.click('#lb-demo'); await p.waitForSelector('#lb-demo-only');
    await p.click('#lb-show-demo'); await p.waitForSelector('#lb-slist');
    // Overall
    await p.click('#lbtab-overall'); await p.waitForSelector('#lb-podium');
    const pod = await p.$$eval('#lb-podium .pcol', xs => xs.map(x => x.className + ' ' + x.textContent));
    log(pod.length === 3 && /\bw\b/.test(pod[1]) && /Top/.test(pod[1]) && /1\s*win of 1 setup/.test(pod[1]), 'Overall: a podium of three, the winner in the middle');
    log((await p.$$('#lb-models .mrow')).length === 4 && (await p.$$('#lb-models .mrow .strip i.ne')).length === 1, 'every model listed with its place strip (n: not enough)');
    log(/1st of 4/.test(await p.textContent('#lb-overall-table')) && /not enough/.test(await p.textContent('#lb-overall-table')), 'the chosen model\'s places, the one-model setup as not enough');
    await p.click('#lb-models .mrow:nth-child(3)'); await p.waitForFunction(() => /model=/.test(location.hash));
    log(/2nd of 4/.test(await p.textContent('#lb-overall-table')) && await p.getAttribute('#lb-models .mrow:nth-child(3)', 'aria-current') === 'true', 'choosing another model shows its places');
    await p.waitForTimeout(400);
    await p.click('#lb-overall-table [data-open="0"]');
    await p.waitForSelector('#lb-overall-table [data-open="0"][aria-expanded="true"]', { timeout: 5000 });
    log(/points behind/.test(await p.textContent('#lb-overall-table tr.exp')) || /Open/.test(await p.textContent('#lb-overall-table tr.exp')), 'a place opens the sessions behind it');
    await p.screenshot({ path: S + '/lb-overall.png' });
    await p.emulateMedia({ colorScheme: 'dark' }); await p.screenshot({ path: S + '/lb-overall-dark.png' });
    // phone width: one column
    await p.setViewportSize({ width: 390, height: 844 }); await p.click('#lbtab-setup'); await p.waitForSelector('#lb-slist');
    log(await p.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'phone width: no sideways scroll');
    await p.screenshot({ path: S + '/lb-phone.png', fullPage: true });
  } catch (e) { log(false, 'walk-through crashed: ' + e.message); }
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  await b.close();
  console.log(out.join('\n'));
  process.exit(out.some(l => l.startsWith('FAIL')) ? 1 : 0);
})();
