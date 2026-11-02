#!/usr/bin/env bash
# Deploy the CURRENT code to a staging copy: a second Supabase project (free tier allows two) and a
# Cloudflare Pages preview branch, so a change can be tried before the real site gets it.
# Nothing here touches the production project or the production site. See docs/STAGING.md.
#
#   STAGING_PROJECT_REF=<ref> STAGING_DB_URL='postgresql://...' bash scripts/deploy_staging.sh
#
# Needs: npx supabase (logged in), npx wrangler (logged in), the staging project's secrets set once
# (docs/STAGING.md step 2). Prints the staging site's address at the end.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${STAGING_PROJECT_REF:?set STAGING_PROJECT_REF to the staging Supabase project's ref}"
: "${STAGING_DB_URL:?set STAGING_DB_URL to the staging database's connection string (Project Settings → Database)}"
PAGES_PROJECT="${PAGES_PROJECT:-agentmeter}"
BRANCH="${STAGING_BRANCH:-staging}"
cd "$REPO"

echo "1/4  tests that guard a deploy (CSP headers, API documents)"
python3 scripts/csp_headers.py > /dev/null
python3 scripts/build_openapi.py > /dev/null
git diff --quiet -- web/_headers web/docs || { echo "web/_headers or web/docs changed: commit them first" >&2; exit 1; }

echo "2/4  database migrations -> staging"
npx supabase db push --db-url "$STAGING_DB_URL"

echo "3/4  Edge Function -> staging"
npx supabase functions deploy agentmeter --project-ref "$STAGING_PROJECT_REF" --no-verify-jwt --use-api

echo "4/4  site -> Pages preview branch '$BRANCH' (its config.json points at staging)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cp -r web/. "$TMP/"
python3 - "$TMP/config.json" "https://$STAGING_PROJECT_REF.supabase.co/functions/v1/agentmeter" <<'PY'
import json, sys
p, cp = sys.argv[1], sys.argv[2]
c = json.load(open(p)); c["control_plane"] = cp; c["api_base"] = ""
json.dump(c, open(p, "w"), indent=2)
PY
npx wrangler pages deploy "$TMP" --project-name "$PAGES_PROJECT" --branch "$BRANCH" --commit-dirty=true
echo
echo "Staging site: https://$BRANCH.$PAGES_PROJECT.pages.dev  (control plane: $STAGING_PROJECT_REF)"
echo "First time? Set the staging project's ALLOWED_ORIGINS to that address (docs/STAGING.md)."
