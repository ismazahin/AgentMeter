# AgentMeter control plane (Supabase: Edge Function + Postgres + Storage)

AgentMeter **measures** LLM resource efficiency; it is not a threat-detection product. The
control plane holds accounts, settings, admin limits, run authorisation and the persisted
sessions, so the app works with the GPU off. Deploy: `docs/DEPLOY.md`, runbook A.

Where things are:

| | |
|---|---|
| `supabase/functions/agentmeter/src/` | every route (`app.ts`), runtime-neutral |
| `supabase/functions/agentmeter/index.ts` | the Supabase Edge Function entry (Deno, postgres.js, Storage) |
| `supabase/migrations/` | the Postgres schema and the private Storage bucket |
| `controlplane/` (here) | Node tooling: tests, local dev server, first-admin bootstrap, the one-time copy from Cloudflare |

| | routes (under `https://<project-ref>.supabase.co/functions/v1/agentmeter`) |
|---|---|
| auth | `POST /api/auth/login`, `/refresh`, `/logout`; `GET /api/me`; `POST /api/me/password` |
| settings | `GET`/`PUT /api/settings` (allow-list: theme, Telegram chat ID + notify on, default models / flows / Other Attack, SAW weight presets, time zone); `POST /api/settings/telegram-test` |
| admin | `GET`/`POST /api/admin/users`, `PATCH`/`DELETE /api/admin/users/:id` (max 5), `GET /api/admin/credentials` (set / not set only), `GET`/`PUT /api/admin/limits` |
| GPU backend | `GET /api/backend` (online? URL); `POST /api/runs/authorize` → short-lived run token (per-user hourly limit, queue cap) |
| sessions | `GET /api/sessions`, `/api/sessions/:id`, `/results`, `/constraints`, `/files/:name`; `DELETE /api/sessions/:id` (owner or admin); `GET /api/compare?a=&b=`, `/api/leaderboard?sort=` |
| backend → control plane (HMAC-signed) | `POST /api/backend/register`, `/heartbeat`, `/jobs`, `/jobs/:id/status`; `PUT /api/backend/sessions/:id/results`, `/summary`, `/files/:name`; large files: `POST .../upload-url` (single-use Storage URL), then `POST .../commit` |

Compare, Leaderboard and the decision helper are a port of the Python reference
(`agentmeter/server/sessions.py`, `agentmeter/session/constraints.py`), checked for identical
output on fixtures exported from Python (`scripts/export_parity_fixtures.py`,
`test/parity.test.ts`). Nothing here computes a score, SAW value, rank or verdict.

```bash
npm ci
npx vitest run                    # on PGlite (Postgres in-process; nothing to install)
TEST_DATABASE_URL=postgres://... npx vitest run   # the same, through postgres.js on a real server
npx tsc --noEmit
npm run dev                       # local control plane on http://127.0.0.1:8787/functions/v1/agentmeter
```
