# AgentMeter control plane (Cloudflare Worker + D1 + R2)

AgentMeter **measures** LLM resource efficiency; it is not a threat-detection product. This
Worker holds accounts, settings, admin limits, run authorisation and the persisted sessions,
so the app works with the GPU off. Deploy: `docs/DEPLOY.md`, runbook A.

| | routes |
|---|---|
| auth | `POST /api/auth/login`, `/refresh`, `/logout`; `GET /api/me`; `POST /api/me/password` |
| settings | `GET`/`PUT /api/settings` (allow-list: theme, Telegram chat ID + notify on, default models / flows / Other Attack, SAW weight presets, time zone); `POST /api/settings/telegram-test` |
| admin | `GET`/`POST /api/admin/users`, `PATCH`/`DELETE /api/admin/users/:id` (max 5), `GET /api/admin/credentials` (set / not set only), `GET`/`PUT /api/admin/limits` |
| GPU backend | `GET /api/backend` (online? URL); `POST /api/runs/authorize` → short-lived run token (per-user hourly limit, queue cap) |
| sessions | `GET /api/sessions`, `/api/sessions/:id`, `/results`, `/constraints`, `/files/:name`; `DELETE /api/sessions/:id` (owner or admin); `GET /api/compare?a=&b=`, `/api/leaderboard?sort=` |
| backend → Worker (HMAC-signed) | `POST /api/backend/register`, `/heartbeat`, `/jobs`, `/jobs/:id/status`; `PUT /api/backend/sessions/:id/results`, `/summary`, `/files/:name` |

Compare, Leaderboard and the decision helper are a port of the Python reference
(`agentmeter/server/sessions.py`, `agentmeter/session/constraints.py`), checked for identical
output on fixtures exported from Python (`scripts/export_parity_fixtures.py`,
`test/parity.test.ts`). Nothing here computes a score, SAW value, rank or verdict.

```bash
npm ci --legacy-peer-deps
npx vitest run            # workerd + Miniflare (local D1/R2)
npx tsc --noEmit -p .
npm run dev               # wrangler dev --local (copy .dev.vars.example to .dev.vars first)
```
