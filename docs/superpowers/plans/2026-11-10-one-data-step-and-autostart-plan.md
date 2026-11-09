# One data step + a self-starting preparation service — plan

Design: `docs/superpowers/specs/2026-11-10-one-data-step-and-autostart-design.md` (approved 2026-11-10).
Branch `claude/cool-ride-mitmzl`; one commit per task; suites green after each.

## A. Control plane: the service registers itself
1. `prep.ts`: `POST /api/prep/register`, `POST /api/prep/heartbeat` (HMAC, `CLOUD_PREP_SECRET`,
   `verifySigned`); row `backends.id='prep'` {url, version, registered_at, last_heartbeat_at}.
2. `cloudPrepUrl(env)`: the registered URL when its heartbeat is < 180 s old, else `CLOUD_PREP_URL`.
   Every use of `env.CLOUD_PREP_URL` / `cloudPrepConfigured` goes through it (dispatch, wake,
   create/start checks, `/api/backend.cloud_prep`).
3. vitest: signed only; https only (http allowed with `ALLOW_HTTP_BACKEND`); online window;
   dispatch goes to the registered URL; env fallback; `/api/backend` reflects it.

## B. `scripts/prep_local.py`
1. Register + heartbeat thread (signed POST, retries); no `supabase secrets set` by default
   (`--set-secret` keeps the old CLI step for the very first set-up of the secret).
2. Supervisor loop: restart the service / tunnel when they exit; re-register a new tunnel address.
3. `--background`: log to `results/prep_local.log` (RotatingFileHandler 5 MB × 3), no prompts.
4. `install` / `uninstall` / `status`: the Startup-folder `.cmd` (pythonw, repo folder),
   start/stop a running copy via a pid file; non-Windows: print what to do instead.
5. pytest: signing matches the control plane; restart logic with fake processes; install/uninstall
   in a temp Startup folder; status.

## C. Web app
1. Data sets list inside Analyse traffic (move `#ds-controls`/`#ds-body`; `loadDatasets` from
   `loadAnalyse`); `#/datasets` → `#/analyse`.
2. Sidebar: drop New benchmark, Data sets, Repeatability, GPU & Queue; add Cost planner.
   `#/new` without `set`/`prep`/`again` → `#/analyse`; models/run steps unchanged.
3. Leaderboard tab "Repeatability" (`#/trends` → `#/leaderboard?tab=repeatability`).
4. GPU chip → a panel with the GPU & Queue content (`#/gpu` opens it); Admin keeps the full queue.
5. "Not running" notice wording; Ctrl+K search entries; How to use.

## D. Tests and docs
- e2e: analyse_flow covers the list, redirects and sidebar; existing flows that open `#/new`,
  `#/datasets`, `#/trends`, `#/gpu` updated.
- DEPLOY.md A7 (install once), README feature list, `web/README.md`, OpenAPI regenerated.
