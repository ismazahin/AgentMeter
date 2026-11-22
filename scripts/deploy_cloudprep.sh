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
# Sized to stay inside the free tier: 1 vCPU, at most 1 machine, none while idle. 4 GiB because
# Cloud Run keeps files in memory (a linked file is up to 2 GB).
gcloud run deploy "$SERVICE" --source "$STAGE" --region "$REGION" \
  --allow-unauthenticated --no-cpu-throttling --cpu 1 --memory 4Gi --timeout 3600 \
  --concurrency 2 --min-instances 0 --max-instances 1 \
  --set-env-vars "CLOUD_PREP_SECRET=${CLOUD_PREP_SECRET}"

# Free-tier housekeeping (best effort, never fails the deploy): every deploy stores a new image
# (Artifact Registry: 0.5 GB free) and its source upload (Cloud Storage). Keep only the newest
# image, and delete source uploads after a day, so storage never grows past the free amount.
PROJECT="$(gcloud config get-value project 2>/dev/null)"
POLICY="$(mktemp)"; LIFE="$(mktemp)"
cat > "$POLICY" <<'JSON'
[{"name": "keep-newest", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 1}},
 {"name": "delete-older", "action": {"type": "Delete"}, "condition": {"tagState": "any"}}]
JSON
echo '{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 1}}]}' > "$LIFE"
gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy --project "$PROJECT" \
  --location "$REGION" --policy "$POLICY" --no-dry-run --quiet >/dev/null \
  && echo "Image cleanup: only the newest image is kept." || echo "WARNING: could not set the image cleanup rule (see docs/DEPLOY.md, A7)."
gcloud storage buckets update "gs://run-sources-${PROJECT}-${REGION}" --lifecycle-file "$LIFE" >/dev/null \
  && echo "Source uploads: deleted after one day." || echo "WARNING: could not set the source-upload cleanup rule (see docs/DEPLOY.md, A7)."
rm -f "$POLICY" "$LIFE"
URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
echo
echo "Deployed: $URL"
curl -fsS "$URL/health" && echo
echo "Now tell the control plane where it is:"
echo "  npx supabase secrets set CLOUD_PREP_URL=$URL CLOUD_PREP_SECRET=<the same secret>"
