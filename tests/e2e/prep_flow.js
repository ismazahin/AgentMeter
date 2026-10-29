// "Prepare on this computer" walk-through (Playwright, mock backend):
//   python scripts/serve.py --provider mock --port 8766
//   [PYODIDE_DIST=<a local copy of the Pyodide 0.28.3 "full" files>] node tests/e2e/prep_flow.js [base_url] [out_dir]
// The page runs with the SAME Content-Security-Policy as on Pages (web/_headers), so a CSP that
// would block Pyodide fails here. Without PYODIDE_DIST Pyodide loads from its CDN as in production.
// CSV and PCAP are prepared in the browser (Pyodide worker), uploaded as a prepared set, and the
// wizard continues to model selection; the stored set's data equals a server preparation's.
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const BASE = process.argv[2] || 'http://127.0.0.1:8766';
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'prep-e2e-'));
fs.mkdirSync(S, { recursive: true });
const REPO = path.resolve(__dirname, '..', '..');
const DIST = process.env.PYODIDE_DIST;
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);
const CSP = (() => {
  const t = fs.readFileSync(path.join(REPO, 'web', '_headers'), 'utf8');
  return /Content-Security-Policy:\s*(.+)/.exec(t)[1].trim();
})();

async function prepare(p, file, flows) {
  await p.goto(BASE + '/#/new'); await p.waitForSelector('#svc-upload-form:not([hidden])');
  await p.evaluate(() => localStorage.setItem('agentmeter.prepWhere', 'local'));   // the GPU-busy fallback without cloud preparation
  await p.setInputFiles('#svc-file', file);
  await p.fill('#svc-max-flows', String(flows));
  await p.click('#svc-upload-btn');
  await p.waitForFunction(() => /^#\/new\/models\?set=/.test(location.hash) || document.querySelector('#svc-upload-error:not([hidden])'), null, { timeout: 240000 });
  return p.evaluate(() => ({ hash: location.hash, err: document.querySelector('#svc-upload-error:not([hidden])')?.textContent || '',
                             log: [...document.querySelectorAll('#svc-local-log li')].map(l => l.textContent) }));
}

(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const ctx = await b.newContext({ viewport: { width: 1280, height: 1000 } });
  // production CSP on every page and script of the app (the backend itself sends none)
  await ctx.route(BASE + '/**', async (route) => {
    const r = await route.fetch();
    const h = { ...r.headers() };
    if (/text\/html|javascript/.test(h['content-type'] || '')) h['content-security-policy'] = CSP;
    await route.fulfill({ response: r, headers: h });
  });
  if (DIST) await ctx.route('https://cdn.jsdelivr.net/pyodide/v0.28.3/full/**', (route) => {
    const f = path.join(DIST, new URL(route.request().url()).pathname.split('/').pop());
    const type = f.endsWith('.mjs') || f.endsWith('.js') ? 'text/javascript' : f.endsWith('.wasm') ? 'application/wasm' : f.endsWith('.json') ? 'application/json' : 'application/octet-stream';
    return fs.existsSync(f) ? route.fulfill({ status: 200, body: fs.readFileSync(f), headers: { 'content-type': type, 'access-control-allow-origin': '*' } }) : route.fulfill({ status: 404, body: 'missing' });
  });
  const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  const csp = []; p.on('console', m => { if (/Content Security Policy|Refused to/.test(m.text())) csp.push(m.text().slice(0, 200)); });
  try {
    const t0 = Date.now();
    const a = await prepare(p, path.join(REPO, 'data', 'sample_csv', 'cicids2017_sample.csv'), 20);
    log(/set=csv_runs%2F/.test(a.hash) || /set=csv_runs\//.test(decodeURIComponent(a.hash)),
        'CSV prepared on this computer and stored on the GPU server (' + ((Date.now() - t0) / 1000).toFixed(0) + ' s) ' + a.err);
    log(a.log.some(l => /Loading Python in your browser/.test(l)) && a.log.some(l => /Stored on the GPU server/.test(l)),
        'the steps are shown while it prepares');
    await p.waitForFunction(() => /Ready|20 flows/.test(document.querySelector('#wiz-data-card')?.textContent || ''), null, { timeout: 30000 });
    const set = decodeURIComponent(a.hash.split('set=')[1]);
    const man = await p.evaluate(s => fetch('/api/prepared/' + s).then(r => r.json()), set);
    log(JSON.stringify(man).includes('browser'), 'the stored set records that it was prepared in the browser');
    await p.screenshot({ path: S + '/prep-csv.png', fullPage: true });
    const b2 = await prepare(p, path.join(REPO, 'data', 'sample_pcaps', 'sample_small.pcap'), 20);
    log(/pcap_runs/.test(decodeURIComponent(b2.hash)), 'PCAP converted to flows on this computer (CICFlowMeter in the browser) ' + b2.err);
    fs.writeFileSync(S + '/sets.json', JSON.stringify({ csv: set, pcap: decodeURIComponent(b2.hash.split('set=')[1]) }));
  } catch (e) {
    log(false, 'walk-through crashed: ' + e.message.split('\n')[0]);
  }
  log(csp.length === 0, 'nothing blocked by the Content-Security-Policy ' + csp.join(' | '));
  log(errs.length === 0, 'no JS errors ' + errs.join(' | '));
  await b.close();
  console.log(out.join('\n'));
  process.exit(out.some(l => l.startsWith('FAIL')) ? 1 : 0);
})();
