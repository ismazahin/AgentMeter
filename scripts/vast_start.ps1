# AgentMeter - start the GPU backend on a freshly rented Vast.ai instance, from a Windows laptop.
#
# AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product.
#
#   powershell -ExecutionPolicy Bypass -File scripts\vast_start.ps1
#
# It asks for the instance's "Direct SSH Connect" line (Vast console -> Instances -> >_), then:
#   1. copies your two secret files to /root on the instance over SSH (scp, encrypted):
#        %USERPROFILE%\.agentmeter\backend.env  -> /root/backend.env  (AGENTMETER_*_SECRET)
#        <repo>\.env                            -> /root/hf.env       (HF_TOKEN, VAST_API_KEY)
#      Nothing is printed: the values never appear on screen, in this script or in git.
#   2. logs in and runs scripts/vast_up.sh inside tmux session "am" (it keeps running if the
#      laptop disconnects; reconnect and run:  tmux attach -t am).
# Starting the instance is what costs money; this script never rents or destroys anything.
param(
  [string]$SshLine = "",
  [string]$Key = "$env:USERPROFILE\.ssh\vast_ed25519",
  [string]$BackendEnv = "$env:USERPROFILE\.agentmeter\backend.env",
  [string]$HfEnv = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env"),
  [string]$WorkerUrl = "https://agentmeter-control-plane.ismazahin.workers.dev",
  [string]$AllowedOrigins = "https://agentmeter.pages.dev",
  [string]$Repo = "https://github.com/ismazahin/AgentMeter.git",
  [string]$Branch = "claude/cool-ride-mitmzl",
  [int]$IdleMin = 10,
  [switch]$PreloadStudy          # also download all 5 study models before starting (slower start)
)
$ErrorActionPreference = "Stop"

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

foreach ($f in @($Key, $BackendEnv, $HfEnv)) {
  if (-not (Test-Path $f)) { Fail "file not found: $f" }
}
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) { Fail "ssh is not installed (Windows: Settings -> Optional features -> OpenSSH Client)." }

if (-not $SshLine) {
  Write-Host "Paste the 'Direct SSH Connect' line from Vast (e.g. ssh -p 12345 root@1.2.3.4 -L 8080:localhost:8080):"
  $SshLine = Read-Host
}
$m = [regex]::Match($SshLine, '-p\s+(\d+)\s+root@([A-Za-z0-9\.\-]+)')
if (-not $m.Success) { Fail "could not read the port and address from: $SshLine" }
$Port = $m.Groups[1].Value
$HostAddr = $m.Groups[2].Value
Write-Host "instance: root@$HostAddr port $Port"

# Vast gives every rental a new host key: accept a NEW one, but still refuse a CHANGED one.
$opts = @("-i", $Key, "-o", "StrictHostKeyChecking=accept-new")

# The script that runs on the instance. Written with LF line endings (bash rejects CRLF).
$remote = @"
[ -f /venv/main/bin/activate ] && . /venv/main/bin/activate   # PyTorch lives here (before set -u)
set -euo pipefail
sed -i 's/\r`$//' /root/backend.env /root/hf.env
chmod 600 /root/backend.env /root/hf.env /root/am_start.sh
set -a; . /root/backend.env; . /root/hf.env; set +a
export AGENTMETER_WORKER_URL='$WorkerUrl'
export AGENTMETER_ALLOWED_ORIGINS='$AllowedOrigins'
export IDLE_MIN='$IdleMin'
export PRELOAD_STUDY='$(if ($PreloadStudy) { "1" } else { "0" })'
cd /workspace
if [ -d AgentMeter/.git ]; then
  cd AgentMeter && git fetch -q origin '$Branch' && git checkout -q '$Branch' && git pull -q --ff-only origin '$Branch'
else
  git clone -q '$Repo' AgentMeter && cd AgentMeter && git checkout -q '$Branch'
fi
echo "code: `$(git log --oneline -1)"
bash scripts/vast_up.sh
"@
$tmp = Join-Path $env:TEMP "am_start.sh"
[IO.File]::WriteAllText($tmp, ($remote -replace "`r`n", "`n"))

Write-Host "1/2 copying the secret files (values are not shown)..."
& scp @opts -P $Port $BackendEnv "root@${HostAddr}:/root/backend.env"
if ($LASTEXITCODE -ne 0) { Fail "copying backend.env failed. Is the instance Running? Try again in a few seconds." }
& scp @opts -P $Port $HfEnv "root@${HostAddr}:/root/hf.env"
if ($LASTEXITCODE -ne 0) { Fail "copying .env failed." }
& scp @opts -P $Port $tmp "root@${HostAddr}:/root/am_start.sh"
if ($LASTEXITCODE -ne 0) { Fail "copying the start script failed." }
Remove-Item $tmp -ErrorAction SilentlyContinue

Write-Host "2/2 starting the backend in tmux session 'am' (about 5 min; models download on first use, or now if marked "download at start-up")..."
Write-Host "    Leave this window open. To detach without stopping it: Ctrl+B, then D."
& ssh @opts -t -p $Port "root@$HostAddr" "tmux new-session -A -s am 'bash /root/am_start.sh; exec bash'"
