# Build and deploy the cloud data-preparation service to Google Cloud Run, from PowerShell
# (Windows). Same as scripts/deploy_cloudprep.sh. One-time setup: docs/DEPLOY.md, section A7.
#
#   $env:CLOUD_PREP_SECRET = "<same value as the control plane's>"
#   .\scripts\deploy_cloudprep.ps1
#   optional: $env:GCP_REGION (asia-southeast1), $env:CLOUD_PREP_SERVICE (agentmeter-prep)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not $env:CLOUD_PREP_SECRET) { throw 'Set it first:  $env:CLOUD_PREP_SECRET = "<the same value as the control plane secret>"' }
$Region = if ($env:GCP_REGION) { $env:GCP_REGION } else { "asia-southeast1" }
$Service = if ($env:CLOUD_PREP_SERVICE) { $env:CLOUD_PREP_SERVICE } else { "agentmeter-prep" }

python scripts/build_web_prep.py            # the bundle must match the code being deployed
if ($LASTEXITCODE -ne 0) { throw "building the preparation bundle failed" }

$Stage = Join-Path ([System.IO.Path]::GetTempPath()) ("agentmeter-prep-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path (Join-Path $Stage "wheels") | Out-Null
try {
    Copy-Item cloudprep/Dockerfile, cloudprep/app.py, cloudprep/requirements.txt, web/prep/agentmeter-prep.zip, agentmeter/server/urlfetch.py -Destination $Stage
    Copy-Item web/prep/wheels/*.whl -Destination (Join-Path $Stage "wheels")
    # --no-cpu-throttling: a job keeps running after POST /prepare has answered 202.
    # --allow-unauthenticated: reachable over HTTPS, but every job must be signed by the control plane.
    # Sized to stay inside the free tier: 1 vCPU, at most 1 machine, none while idle. 4 GiB because
    # Cloud Run keeps files in memory (a linked file is up to 2 GB).
    gcloud run deploy $Service --source $Stage --region $Region `
        --allow-unauthenticated --no-cpu-throttling --cpu 1 --memory 4Gi --timeout 3600 `
        --concurrency 2 --min-instances 0 --max-instances 1 `
        --set-env-vars "CLOUD_PREP_SECRET=$($env:CLOUD_PREP_SECRET)"
    if ($LASTEXITCODE -ne 0) { throw "gcloud run deploy failed" }

    # Free-tier housekeeping (best effort, never fails the deploy): keep only the newest image
    # (Artifact Registry: 0.5 GB free) and delete source uploads after a day.
    $Project = (gcloud config get-value project 2>$null)
    $Policy = Join-Path $Stage "cleanup-policy.json"
    $Life = Join-Path $Stage "lifecycle.json"
    Set-Content -Path $Policy -Encoding ascii -Value '[{"name": "keep-newest", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 1}}, {"name": "delete-older", "action": {"type": "Delete"}, "condition": {"tagState": "any"}}]'
    Set-Content -Path $Life -Encoding ascii -Value '{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 1}}]}'
    gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy --project $Project --location $Region --policy $Policy --no-dry-run --quiet | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "Image cleanup: only the newest image is kept." } else { Write-Host "WARNING: could not set the image cleanup rule (see docs/DEPLOY.md, A7)." }
    gcloud storage buckets update "gs://run-sources-$Project-$Region" --lifecycle-file $Life | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "Source uploads: deleted after one day." } else { Write-Host "WARNING: could not set the source-upload cleanup rule (see docs/DEPLOY.md, A7)." }
} finally {
    Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue
}
$Url = gcloud run services describe $Service --region $Region --format "value(status.url)"
Write-Host ""
Write-Host "Deployed: $Url"
try { Invoke-RestMethod "$Url/health" | ConvertTo-Json -Compress | Write-Host } catch { Write-Host "health check: $_" }
Write-Host "Now tell the control plane where it is:"
Write-Host "  npx supabase secrets set CLOUD_PREP_URL=$Url CLOUD_PREP_SECRET=<the same secret>"
