// Phase 44/46 — the read-only Validation-baseline page (Overview + Detailed analysis only),
// reachable only by its direct URL /baseline (not in the app's navigation).
//   node tests/e2e/baseline_page.js [base_url]       (server: python scripts/serve.py)
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const BASE = process.argv[2] || 'http://127.0.0.1:8766';
const S = process.env.E2E_OUT || fs.mkdtempSync(path.join(os.tmpdir(), 'baseline-e2e-'));
fs.mkdirSync(S, { recursive: true });
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);
(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const p = await (await b.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  p.on('console', m => { if (m.type() === 'error' && !/fonts|ERR_|Failed to load resource/.test(m.text())) errs.push(m.text()); });

  // Phase 46: the baseline is NOT in the app's navigation — direct URL only
  await p.goto(BASE + '/'); await p.waitForSelector('#view-home:not([hidden]), #view-offline:not([hidden])', { timeout: 15000 });
  log(!(await p.$('a[href$="/baseline"]')) && !/Validation baseline/.test(await p.textContent('header')), 'the app links no Validation baseline');
  await p.goto(BASE + '/baseline'); await p.waitForSelector('#saw-body tr', { timeout: 15000 });
  log(new URL(p.url()).pathname === '/baseline', 'the baseline page opens by its direct URL /baseline');
  const tabs = await p.$$eval('#nav button', bs => bs.map(x => x.textContent.trim()));
  log(tabs.join('|') === 'Overview|Detailed Analysis', 'only Overview + Detailed Analysis tabs: ' + tabs.join(', '));
  log((await p.$$('#saw-body tr')).length === 5, 'Overview ranks the 5 study models');
  for (const gone of ['#pull-btn', '#preset-save-btn', '#view-sessions', '#view-compare', '#view-config', '#view-settings'])
    log(!(await p.$(gone)), 'removed: ' + gone);
  await p.waitForFunction(() => !/Loading/.test(document.querySelector('#mc-note').textContent), null, { timeout: 15000 });
  const mc = await p.textContent('#mc-note');
  log(/Hugging Face/.test(mc), 'Overview HF model-context note is its own (not overwritten): ' + mc.slice(0, 70));
  await p.click('#nav [data-view="detailed"]');
  log(await p.isVisible('#view-detailed') && await p.isVisible('#glossary-panel'), 'Detailed Analysis shows, glossary moved there');
  log(!!(await p.$('#mcost-table')), 'misclassification-cost panel has its own table (#mcost-table)');
  // every in-page button/link in nav + footer leads somewhere that exists
  const dead = await p.evaluate(() => {
    const bad = [];
    document.querySelectorAll('[onclick*="data-view"]').forEach(el => {
      const m = el.getAttribute('onclick').match(/data-view=&quot;(\w+)&quot;|data-view="(\w+)"|data-view=\\?"?(\w+)/);
      const v = m && (m[1] || m[2] || m[3]);
      if (v && !document.getElementById('view-' + v)) bad.push(v);
    });
    return bad;
  });
  log(dead.length === 0, 'no in-page link to a removed view' + (dead.length ? ': ' + dead.join(',') : ''));
  const links = await p.$$eval('footer a', as => as.map(a => a.getAttribute('href')));
  for (const h of links) {
    const r = await p.evaluate(u => fetch(u).then(x => x.status), h.split('#')[0] || '/');
    log(r === 200, 'footer link works: ' + h + ' -> ' + r);
  }
  await p.screenshot({ path: S + '/baseline.png', fullPage: false });
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  console.log(out.join('\n')); await b.close();
  if (out.some(l => l.startsWith('FAIL'))) process.exit(1);
})().catch(e => { console.log(out.join('\n')); console.log('CRASH', e.message); process.exit(1); });
