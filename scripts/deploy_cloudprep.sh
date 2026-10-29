#!/usr/bin/env bash
# Build and deploy the cloud data-preparation service to Google Cloud Run (from your terminal;
# the Cloud Console is never needed). One-time setup and secrets: docs/DEPLOY.md, "Cloud data
# preparation".
#
#   CLOUD_PREP_SECRET=<same value as the control plane's> bash scripts/deploy_cloudprep.sh
#   env: GCP_REGION (asia-southeast1), CLOUD_PREP_SERVICE (agentmeter-prep)
set -euo pipefail
cd "$(dirname "$0")/.."
: "${CLOUD_PREP_SECRET:?set CLOUD_PREP_SECRET (the same value as the control plane secret)}"
REGION="${GCP_REGION:-asia-southeast1}"
SERVICE="${CLOUD_PREP_SERVICE:-agentmeter-prep}"
python3 scripts/build_web_prep.py            # the bundle must match the code being deployed
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp cloudprep/Dockerfile cloudprep/app.py cloudprep/requirements.txt "$STAGE/"
cp web/prep/agentmeter-prep.zip agentmeter/server/urlfetch.py "$STAGE/"
mkdir "$STAGE/wheels" && cp web/prep/wheels/*.whl "$STAGE/wheels/"
# --no-cpu-throttling: a job keeps running after POST /prepare has answered 202.
# --allow-unauthenticated: reachable over HTTPS, but every job must be signed by the control plane.
gcloud run deploy "$SERVICE" --source "$STAGE" --region "$REGION" \
  --allow-unauthenticated --no-cpu-throttling --cpu 2 --memory 4Gi --timeout 3600 \
  --concurrency 4 --min-instances 0 --max-instances 2 \
  --set-env-vars "CLOUD_PREP_SECRET=${CLOUD_PREP_SECRET}"
URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
echo
echo "Deployed: $URL"
curl -fsS "$URL/health" && echo
echo "Now tell the control plane where it is:"
echo "  npx supabase secrets set CLOUD_PREP_URL=$URL CLOUD_PREP_SECRET=<the same secret>"
