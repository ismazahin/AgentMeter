# AgentMeter web front-end (static)

This directory is the whole front-end: `index.html` (the Prepare and Benchmark pages and the
results), `report.js` (a copy of `dashboard/report.js`, kept identical by
`tests/test_frontend_split.py`) and `config.json`. There is no build step and there are no
server functions.

- `config.json` → `api_base`: the GPU backend's URL. When it's empty, the page talks to its own
  origin (that's how `scripts/pull_eval_server.py` serves it at `/service`).
- Every API call, upload and download goes from the browser **straight to `api_base`**. Nothing
  passes through the static host, so its request-body limits (Vercel functions: ~4.5 MB) don't
  apply.
- The backend must list this site's origin in `AGENTMETER_ALLOWED_ORIGINS`, or the browser
  blocks the calls (CORS).

Deploy: see docs/DEPLOY.md ("Front-end on Vercel / Cloudflare Pages").
