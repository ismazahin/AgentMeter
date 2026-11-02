// Data sets and GPU & Queue pages (mock backend, slow mock answers so a run is seen running).
//   AGENTMETER_MOCK_LATENCY_S=0.2 python scripts/serve.py --provider mock --port 8766   (fresh results dir)
//   node tests/e2e/datasets_gpu_flow.js [base_url] [out_dir]
const path = require('path'), os = require('os'), fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || '/opt/node22/lib/node_modules/playwright')); }
const BASE = process.argv[2] || 'http://127.0.0.1:8766';
const REPO = path.resolve(__dirname, '..', '..');
const S = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), 'ds-gpu-'));
fs.mkdirSync(S, { recursive: true });
const out = []; const log = (ok, msg) => out.push((ok ? 'PASS ' : 'FAIL ') + msg);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const getJson = async p => (await fetch(BASE + p)).json();

async function prepare(file, flows) {
  const fd = new FormData();
  fd.append('file', new Blob([fs.readFileSync(file)]), path.basename(file));
  fd.append('max_flows', String(flows));
  const j = await (await fetch(BASE + '/api/prepare', { method: 'POST', body: fd })).json();
  for (let i = 0; i < 120; i++) {
    const s = await getJson('/api/jobs/' + j.job_id);
    if (s.status === 'done') return s.prepared;
    if (s.status === 'failed') throw new Error('prepare failed: ' + s.error);
    await sleep(500);
  }
  throw new Error('prepare timed out');
}
async function bench(run, models) {
  const r = await fetch(BASE + '/api/jobs', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ run, models }) });
  return (await r.json()).job_id;
}
async function waitDone(id) {
  for (let i = 0; i < 600; i++) { const s = await getJson('/api/jobs/' + id); if (['done', 'failed'].includes(s.status)) return s.status; await sleep(500); }
  return 'timeout';
}

(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  try {
    const cat = await getJson('/api/models/catalog');
    const models = (cat.study || []).map(m => m.id);
    const csvSet = await prepare(path.join(REPO, 'data', 'sample_csv', 'cicids2017_sample.csv'), 12);
    const pcapSet = await prepare(path.join(REPO, 'data', 'sample_pcaps', 'sample_small.pcap'), 6).catch(() => null);
    log(!!csvSet, 'prepared a CSV set (' + csvSet + ')' + (pcapSet ? ' and a PCAP set (' + pcapSet + ')' : ''));

    // GPU & Queue while a run is going and another waits
    const j1 = await bench(csvSet, models.slice(0, 2));
    const j2 = await bench(csvSet, models.slice(2, 4));
    await p.goto(BASE + '/#/gpu'); await p.waitForSelector('#gq-running-card');
    await p.waitForFunction(() => /Busy/.test(document.querySelector('#gq-state')?.textContent || ''), null, { timeout: 20000 });
    log(/1 waiting/.test(await p.textContent('#gq-state')), 'GPU & Queue: busy, 1 waiting (' + (await p.textContent('#gq-state')).trim() + ')');
    log(/Benchmark/.test(await p.textContent('#gq-running-card')) && !!(await p.$('#gq-running-card .bar')) && !!(await p.$(`#gq-running-card a[href="#/new/run/${j1}"]`)),
        'Running now: the benchmark, a progress bar and a link to watch it live');
    const qrows = await p.$$eval('#gq-queue-table tbody tr', r => r.map(x => x.textContent));
    log(qrows.length === 1 && qrows[0].startsWith('1') && qrows[0].includes(models[2].split('/').pop()), 'Waiting: the second run, first in line');
    log(/demo mode/i.test(await p.textContent('#gq-gpu-card')) && !(await p.$('#gq-metrics')), 'GPU now: demo mode says it has no live readings (nothing simulated)');
    log(/in your browser|on the GPU server, after the queue/.test(await p.textContent('#gq-prep-where')) && /GPU is busy/.test(await p.textContent('#gq-prep-where')),
        'Data preparation: a file uploaded now would not wait in the GPU queue');
    log(/No scheduled re-runs/.test(await p.textContent('#gq-upcoming-card')), 'Coming up: no scheduled re-runs');
    await p.screenshot({ path: S + '/gpu-busy.png', fullPage: true });
    log(await waitDone(j1) === 'done' && await waitDone(j2) === 'done', 'both runs finished');
    await p.waitForFunction(() => /Free/.test(document.querySelector('#gq-state')?.textContent || ''), null, { timeout: 20000 });
    log(true, 'the page updates by itself: Free once the queue is empty');
    log(!!(await p.$('#gq-idle')) && !!(await p.$('#gq-noqueue')), 'nothing running, nobody waiting');
    const rec = await p.$$eval('#gq-recent-table tbody tr', r => r.map(x => x.textContent));
    log(rec.length >= 3 && rec.filter(t => /Benchmark/.test(t) && /done/.test(t)).length === 2 && !!(await p.$(`#gq-recent-table a[href="#/session/${j2}"]`)),
        'Just finished: both benchmarks (and the preparations), linked to their sessions');
    log(/on the GPU server/.test(await p.textContent('#gq-prep-where')) && /free/.test(await p.textContent('#gq-prep-where')), 'Data preparation: on the GPU server now that it is free');

    // Data sets
    await p.goto(BASE + '/#/datasets'); await p.waitForSelector('#ds-table');
    const rows = await p.$$eval('#ds-table tbody tr[data-ds]', r => r.map(x => x.textContent));
    log(rows.length === (pcapSet ? 2 : 1), 'Data sets: one row per prepared set (' + rows.length + ')');
    const csvName = csvSet.split('/').pop();
    const csvRow = rows.find(t => t.includes(csvName)) || '';
    log(/12/.test(csvRow) && /labelled/.test(csvRow), 'the CSV set: 12 flows, labelled');
    const cells = await p.$$eval(`#ds-table tbody tr[data-ds]`, (r, n) => { const x = r.find(t => t.textContent.includes(n)); return [...x.children].map(c => c.textContent.trim()); }, csvName);
    log(cells[3] === '2' && cells[4] === '4' && /^[0-9a-f]{12}$/.test(cells[2]), `used by 2 sessions and 4 models; fingerprint ${cells[2]}`);
    if (pcapSet) log(rows.some(t => t.includes(pcapSet.split('/').pop()) && /not benchmarked yet/.test(t)), 'the PCAP set is listed though nobody benchmarked it yet');
    await p.selectOption('#ds-type', 'PCAP');
    log((await p.$$('#ds-table tbody tr[data-ds]')).length === (pcapSet ? 1 : 0), 'Type filter: PCAP only');
    await p.selectOption('#ds-type', '');
    await p.fill('#ds-q', models[3].split('/').pop().toLowerCase());
    log((await p.$$('#ds-table tbody tr[data-ds]')).length === 1, 'Search by model finds the set it was measured on');
    await p.fill('#ds-q', '');
    await p.click(`#ds-table tbody tr[data-ds]:has-text("${csvName}") [data-ds-open]`);
    await p.waitForSelector('#ds-mix');
    const mix = await p.$$eval('#ds-mix .hbar', r => r.map(x => [x.querySelector('.hb-l').textContent, Number(x.querySelector('.hb-v').textContent)]));
    log(mix.length >= 2 && mix.some(m => m[0] === 'Benign') && mix.every(m => m[1] > 0) && mix.reduce((a, m) => a + m[1], 0) === 12,
        'Details: the flow mix by class adds up to the 12 flows (' + mix.map(m => m.join(' ')).join(', ') + ')');
    log(/features\.csv/.test(await p.textContent('#ds-files')) && /Source file/.test(await p.textContent('#ds-detail')), 'Details: source file and the prepared files to download');
    log((await p.$$('#ds-detail .ds-used tbody tr')).length === 2, 'Details: the 2 sessions that used it');
    log(!!(await p.$(`#ds-table a[href="#/new/models?set=${encodeURIComponent(csvSet)}"]`)), 'Benchmark: starts a new benchmark on this set');
    await p.click(`#ds-table tbody tr[data-ds]:has-text("${csvName}") a:has-text("Compare models")`);
    await p.waitForSelector('#cmp-list input');
    log((await p.$$('#cmp-list input')).length === 4, 'Compare models: opens with the 4 models measured on this set');
    await p.goto(BASE + '/#/datasets'); await p.waitForSelector('#ds-table');
    await p.screenshot({ path: S + '/datasets.png', fullPage: true });
    const nav = await p.$$eval('.mainnav [data-nav]', a => a.map(x => x.getAttribute('data-nav')));
    log(nav.includes('datasets') && nav.includes('gpu'), 'the sidebar has Data sets and GPU & Queue');
    log(!errs.length, 'no JS errors ' + errs.join(' | '));
  } catch (e) {
    out.push('CRASH ' + e.message);
  }
  await b.close();
  console.log(out.join('\n'));
  process.exit(out.some(l => /^(FAIL|CRASH)/.test(l)) ? 1 : 0);
})();
