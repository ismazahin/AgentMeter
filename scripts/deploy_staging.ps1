# Windows PowerShell version of scripts/deploy_staging.sh — see docs/STAGING.md.
#   $env:STAGING_PROJECT_REF = "<ref>"; $env:STAGING_DB_URL = "postgresql://..."; .\scripts\deploy_staging.ps1
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
if (-not $env:STAGING_PROJECT_REF) { throw "Set `$env:STAGING_PROJECT_REF to the staging Supabase project's ref" }
if (-not $env:STAGING_DB_URL) { throw "Set `$env:STAGING_DB_URL to the staging database's connection string" }
$Pages = if ($env:PAGES_PROJECT) { $env:PAGES_PROJECT } else { "agentmeter" }
$Branch = if ($env:STAGING_BRANCH) { $env:STAGING_BRANCH } else { "staging" }
Set-Location $Repo

Write-Host "1/4  tests that guard a deploy (CSP headers, API documents)"
python scripts/csp_headers.py | Out-Null
python scripts/build_openapi.py | Out-Null
git diff --quiet -- web/_headers web/docs
if ($LASTEXITCODE -ne 0) { throw "web/_headers or web/docs changed: commit them first" }

Write-Host "2/4  database migrations -> staging"
npx supabase db push --db-url $env:STAGING_DB_URL

Write-Host "3/4  Edge Function -> staging"
npx supabase functions deploy agentmeter --project-ref $env:STAGING_PROJECT_REF --no-verify-jwt --use-api

Write-Host "4/4  site -> Pages preview branch '$Branch' (its config.json points at staging)"
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("agentmeter-staging-" + [guid]::NewGuid())
Copy-Item -Recurse "web" $Tmp
$cfgPath = Join-Path $Tmp "config.json"
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$cfg.control_plane = "https://$($env:STAGING_PROJECT_REF).supabase.co/functions/v1/agentmeter"
$cfg.api_base = ""
$cfg | ConvertTo-Json | Set-Content $cfgPath -Encoding UTF8
try { npx wrangler pages deploy $Tmp --project-name $Pages --branch $Branch --commit-dirty=true }
finally { Remove-Item -Recurse -Force $Tmp }
Write-Host ""
Write-Host "Staging site: https://$Branch.$Pages.pages.dev  (control plane: $($env:STAGING_PROJECT_REF))"
Write-Host "First time? Set the staging project's ALLOWED_ORIGINS to that address (docs/STAGING.md)."
