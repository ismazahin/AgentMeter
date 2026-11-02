# A staging copy: try a change before the real site gets it

Staging is a second, separate AgentMeter: its own Supabase project (database, storage and Edge
Function) and a Cloudflare Pages **preview branch** of the same Pages project. Deploying there
never touches the production project, the production site or its users.

```
production   https://agentmeter.pages.dev          →  Supabase project <prod-ref>
staging      https://staging.agentmeter.pages.dev  →  Supabase project <staging-ref>
```

## One-time setup (about 15 minutes)

1. **Create the staging project.** supabase.com → New project (the free plan allows two active
   projects). Note its **project ref** (in the URL) and its **database connection string**
   (Project Settings → Database → Connection string → URI, with the password you chose).
2. **Give it its own secrets** — new random values, never the production ones
   (`openssl rand -hex 32` for each; see docs/SECURITY.md):
   ```bash
   npx supabase secrets set --project-ref <staging-ref> \
     ACCESS_TOKEN_SECRET=<new> RUN_TOKEN_SECRET=<new> BACKEND_SECRET=<new> PASSWORD_PEPPER=<new> \
     ALLOWED_ORIGINS=https://staging.agentmeter.pages.dev
   ```
3. **Deploy once** (below), then create the first staging admin:
   ```bash
   DATABASE_URL='<staging connection string>' PASSWORD_PEPPER='<the staging pepper>' node scripts/bootstrap-admin.mjs <admin-name>
   ```
4. Optional: a GPU backend for staging — run `scripts/serve.py` (mock mode is enough to try the
   site) with `AGENTMETER_CONTROL_PLANE_URL=https://<staging-ref>.supabase.co/functions/v1/agentmeter`
   and the staging `RUN_TOKEN_SECRET` / `BACKEND_SECRET`.

## Every time: deploy the current code to staging

```bash
STAGING_PROJECT_REF=<staging-ref> STAGING_DB_URL='<staging connection string>' bash scripts/deploy_staging.sh
```
```powershell
$env:STAGING_PROJECT_REF = "<staging-ref>"; $env:STAGING_DB_URL = "<staging connection string>"
.\scripts\deploy_staging.ps1
```

The script checks that the generated files (CSP headers, API documents) are committed, applies the
new database migrations, deploys the Edge Function and publishes the site to the `staging` branch
with a `config.json` that points at the staging project. Open
`https://staging.agentmeter.pages.dev`, try the change, then deploy to production as usual
(docs/DEPLOY.md: `git pull`, `npx supabase db push`, `npx supabase functions deploy …`,
`npx wrangler pages deploy web …`).

## Good to know

- Staging has its own users and sessions; nothing is copied from production.
- `bash scripts/test_all.sh` runs the whole test suite locally, without any account; staging is
  for seeing the real hosting (Supabase, Pages) with a change before the real users do.
- A Supabase free project pauses after a week without use; resume it from the dashboard.
