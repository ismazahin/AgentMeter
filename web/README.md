# AgentMeter web front-end (static)

This directory is the whole front-end: `index.html` (the page: its markup and styles) with the app's
code in `js/` — loaded in this order and sharing one global scope: `core.js` (helpers, access to the
backend and the control plane, routing), `home.js`, `wizard.js` (New benchmark), `session.js`,
`analysis.js` (Repeatability, charts, exports), `compare.js` (Compare models, Data sets, GPU & Queue,
Leaderboard), `settings.js` (Settings, Admin, login, profile) and `boot.js` (start-up) — `analysis.html` with `saw.js`, `report.js` and `insights.js` (the only copies: the GPU backend
serves these same files) and `config.json`. There is no build step and there are no
server functions.

- `config.json` → `api_base`: the GPU backend's URL. When it's empty, the page talks to its own
  origin (that's how `scripts/serve.py` serves it at `/`).
- A session's Detailed-analysis tab embeds the backend's `/analysis?session=<id>&embed=1`
  page in an iframe (it posts only its height back).
- Every API call, upload and download goes from the browser **straight to `api_base`**. Nothing
  passes through the static host, so its request-body limits (Vercel functions: ~4.5 MB) don't
  apply.
- The backend must list this site's origin in `AGENTMETER_ALLOWED_ORIGINS`, or the browser
  blocks the calls (CORS).

Deploy: see docs/DEPLOY.md ("Front-end on Vercel / Cloudflare Pages").
