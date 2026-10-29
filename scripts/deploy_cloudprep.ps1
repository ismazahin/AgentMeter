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
    gcloud run deploy $Service --source $Stage --region $Region `
        --allow-unauthenticated --no-cpu-throttling --cpu 2 --memory 4Gi --timeout 3600 `
        --concurrency 4 --min-instances 0 --max-instances 2 `
        --set-env-vars "CLOUD_PREP_SECRET=$($env:CLOUD_PREP_SECRET)"
    if ($LASTEXITCODE -ne 0) { throw "gcloud run deploy failed" }
} finally {
    Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue
}
$Url = gcloud run services describe $Service --region $Region --format "value(status.url)"
Write-Host ""
Write-Host "Deployed: $Url"
try { Invoke-RestMethod "$Url/health" | ConvertTo-Json -Compress | Write-Host } catch { Write-Host "health check: $_" }
Write-Host "Now tell the control plane where it is:"
Write-Host "  npx supabase secrets set CLOUD_PREP_URL=$Url CLOUD_PREP_SECRET=<the same secret>"
