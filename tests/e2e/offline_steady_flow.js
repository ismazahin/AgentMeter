// With a control plane and no GPU rented, the site re-checks for the GPU every 30 s. While it stays
// offline, the open New Benchmark page must not be redrawn (that reset its lists, so the page
// looked like it reloaded itself every half minute). Run inside a control-plane stack:
//   APP=http://127.0.0.1:8080 node tests/e2e/offline_steady_flow.js
const path = require('path');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const APP = process.env.APP || 'http://127.0.0.1:8080';
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);

(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext()).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  try {
    await p.goto(APP + '/app.html'); await p.waitForSelector('#login-form:not([hidden])');
    await p.fill('#login-user', 'admin'); await p.fill('#login-pass', 'admin-password-123');
    await p.click('#login-go');
    await p.waitForSelector('#svc-gpu-off:not([hidden])', { timeout: 30000 });
    await p.goto(APP + '/app.html#/new?src=reuse');
    await p.waitForSelector('#src-reuse:not([hidden])');
    await p.waitForTimeout(3000);
    // count redraws of the lists: each redraw replaces their contents
    await p.evaluate(() => {
      window.__redraws = 0;
      ['svc-recent-sets', 'svc-stored-sets', 'an-list'].forEach(id => {
        const el = document.getElementById(id);
        if (el) new MutationObserver(() => { window.__redraws++; }).observe(el, { childList: true });
      });
    });
    await p.waitForTimeout(75000);                       // two re-checks of the GPU
    const n = await p.evaluate(() => window.__redraws);
    log(n === 0, 'GPU stays offline for 75 s: the prepared sets lists are not redrawn (' + n + ' redraws)');
    log(await p.isVisible('#src-reuse'), 'still on the Reuse a prepared set tab');
  } catch (e) { log(false, 'walk-through crashed: ' + e.message); }
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  await b.close();
  console.log(out.join('\n'));
  process.exit(out.some(l => l.startsWith('FAIL')) ? 1 : 0);
})();
