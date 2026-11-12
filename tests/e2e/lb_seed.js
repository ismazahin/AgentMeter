// Seeds a mock GPU server with finished sessions for the Leaderboard walk-throughs (no browser):
// two data sets (12 and 8 flows of the CSV sample); set A gets a 3-model and a 2-model session,
// set B a 1-model session (not enough to rank).
//   node tests/e2e/lb_seed.js [base_url]
const fs = require('fs'), path = require('path');
const BASE = process.env.LB_BASE || (require.main === module && process.argv[2]) || 'http://127.0.0.1:8766';
const CSV = path.resolve(__dirname, '..', '..', 'data', 'sample_csv', 'cicids2017_sample.csv');
const M = ['mistralai/Mistral-7B-Instruct-v0.3', 'meta-llama/Meta-Llama-3-8B-Instruct', 'Qwen/Qwen2.5-7B-Instruct', 'microsoft/Phi-3-mini-4k-instruct'];

async function j(url, opts) { const r = await fetch(BASE + url, opts); const d = await r.json(); if (!r.ok) throw new Error(url + ': ' + JSON.stringify(d)); return d; }
async function done(id) {
  for (let i = 0; i < 600; i++) {
    const s = await j('/api/jobs/' + id);
    if (s.status === 'done') return s;
    if (['failed', 'interrupted', 'stopped'].includes(s.status)) throw new Error(id + ' ' + s.status + ': ' + s.message);
    await new Promise(r => setTimeout(r, 500));
  }
  throw new Error(id + ' did not finish');
}
async function prepare(flows) {
  const fd = new FormData();
  fd.append('file', new Blob([fs.readFileSync(CSV)]), 'cicids2017_sample.csv');
  fd.append('max_flows', String(flows));
  const p = await j('/api/prepare', { method: 'POST', body: fd });
  return (await done(p.job_id)).prepared;
}
async function bench(set, models) {
  const b = await j('/api/jobs', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ run: set, models }) });
  await done(b.job_id);
  return b.job_id;
}
module.exports = async function seed() {
  const a = await prepare(12), b = await prepare(8);
  const ids = [await bench(a, [M[0], M[1], M[2]]), await bench(a, [M[0], M[3]]), await bench(b, [M[0]])];
  return { a, b, ids };
};
if (require.main === module) module.exports().then(x => console.log(JSON.stringify(x)), e => { console.error(e.message); process.exit(1); });
