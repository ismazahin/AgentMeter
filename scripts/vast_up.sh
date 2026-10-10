#!/usr/bin/env bash
# Phase E/47 — bring the AgentMeter GPU backend up on a fresh Vast.ai instance, in ONE command.
#
#   export HF_TOKEN=hf_...                       # needs access to Llama-3-8B + gemma-2-9b (gated)
#   export AGENTMETER_WORKER_URL=https://agentmeter-control-plane.<you>.workers.dev
#   export AGENTMETER_RUN_TOKEN_SECRET=...       # = the Worker secret RUN_TOKEN_SECRET
#   export AGENTMETER_BACKEND_SECRET=...         # = the Worker secret BACKEND_SECRET
#   export AGENTMETER_ALLOWED_ORIGINS=https://agentmeter.pages.dev   # your front-end origin(s)
#   bash scripts/vast_up.sh                      # quick tunnel (random trycloudflare.com URL)
#   CF_TUNNEL_TOKEN=eyJ... CF_TUNNEL_HOSTNAME=gpu.example.org bash scripts/vast_up.sh   # named tunnel
#
# The backend REGISTERS its public URL with the control plane by itself (and heartbeats every
# 60 s): the front-end gets the URL from the Worker — nothing to paste. Users log in at the
# front-end; the backend only accepts requests carrying a run token the Worker issued.
#
# Optional:  VAST_API_KEY=...  IDLE_MIN=45  -> destroy the instance after 45 idle minutes
#            AGENTMETER_DATA_DIR=/workspace  (persistent disk: models, jobs, prepared sets)
#            VERIFY=all  (load all 5 models in 4-bit as a smoke test; default: Phi-3 only)
#            PORT=8000   SKIP_INSTALL=1
#
# Steps: check env -> install deps -> check CUDA/GPU -> download + verify the 5 models
# (refuses on gated-access errors) -> start ONE server process in REAL mode (it refuses
# to start without GPU/models/control-plane secrets — no mock fallback) -> start Cloudflare
# Tunnel -> write its URL to $LOGS/public_url -> the backend registers it with the Worker.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
PORT="${PORT:-8000}"
DATA="${AGENTMETER_DATA_DIR:-/workspace}"
LOGS="$DATA/agentmeter-logs"
mkdir -p "$LOGS"
export HF_HOME="${HF_HOME:-$DATA/hf}"           # model weights on the persistent disk
export PYTHONUNBUFFERED=1

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. environment ------------------------------------------------------------------
say "1/6 checking settings"
[ -n "${HF_TOKEN:-}" ] || die "HF_TOKEN is not set (needed for the gated Llama-3 / gemma-2 weights)."
for v in AGENTMETER_WORKER_URL AGENTMETER_RUN_TOKEN_SECRET AGENTMETER_BACKEND_SECRET; do
  [ -n "${!v:-}" ] || die "$v is not set — the backend only runs jobs the control plane authorised (docs/DEPLOY.md)."
done
case "$AGENTMETER_WORKER_URL" in https://*) ;; *) die "AGENTMETER_WORKER_URL must start with https://";; esac
[ "${#AGENTMETER_RUN_TOKEN_SECRET}" -ge 16 ] || die "AGENTMETER_RUN_TOKEN_SECRET is shorter than 16 characters."
[ "${#AGENTMETER_BACKEND_SECRET}" -ge 16 ] || die "AGENTMETER_BACKEND_SECRET is shorter than 16 characters."
if [ -z "${AGENTMETER_ALLOWED_ORIGINS:-}" ]; then
  echo "WARNING: AGENTMETER_ALLOWED_ORIGINS is not set: only the backend's own /service page will work"
  echo "         (a Vercel / Cloudflare Pages front-end will be blocked by CORS). Set it to e.g."
  echo "         https://agentmeter.vercel.app and re-run."
fi
echo "data dir: $DATA   HF_HOME: $HF_HOME   logs: $LOGS"
# the tunnel URL is written here once known; the backend re-reads it on every heartbeat
export AGENTMETER_PUBLIC_URL_FILE="$LOGS/public_url"
rm -f "$AGENTMETER_PUBLIC_URL_FILE"
[ -n "${CF_TUNNEL_HOSTNAME:-}" ] && echo "https://$CF_TUNNEL_HOSTNAME" > "$AGENTMETER_PUBLIC_URL_FILE"

# --- 2. dependencies ------------------------------------------------------------------
if [ "${SKIP_INSTALL:-0}" != "1" ]; then
  say "2/6 installing Python dependencies"
  python3 -m pip install -q --upgrade pip
  python3 -m pip install -q -r requirements.txt -r requirements-gpu.txt -r requirements-pcap.txt
  python3 -m pip install -q --no-deps cicflowmeter==0.2.0
else
  say "2/6 skipping install (SKIP_INSTALL=1)"
fi

# --- 3. GPU ---------------------------------------------------------------------------
say "3/6 checking CUDA / GPU"
command -v nvidia-smi >/dev/null || die "nvidia-smi not found: this is not a GPU instance."
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
python3 - <<'PY' || die "PyTorch cannot see the GPU (CUDA). Pick a Vast template with CUDA >= 12.1 + PyTorch."
import torch, sys
ok = torch.cuda.is_available()
print("torch", torch.__version__, "CUDA", torch.version.cuda, "available:", ok)
if ok: print("GPU:", torch.cuda.get_device_name(0), round(torch.cuda.get_device_properties(0).total_memory / 2**30, 1), "GB")
sys.exit(0 if ok else 1)
PY

# --- 4. models ------------------------------------------------------------------------
say "4/6 models: access check, download to $HF_HOME, 4-bit NF4 load test"
python3 scripts/vast_models.py --verify "${VERIFY:-microsoft/Phi-3-mini-4k-instruct}" \
  || die "models are not ready (see the message above) — the server will not start in real mode."
export HF_HUB_OFFLINE=1        # from here on nothing is fetched: a missing model fails loudly

# --- 5. server (ONE process, REAL provider, loopback only — the tunnel is the way in) ---
say "5/6 starting the AgentMeter backend (provider REAL) on 127.0.0.1:$PORT"
if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  die "something already listens on port $PORT (an earlier run?). Stop it: pkill -f serve.py"
fi
GUARD=()
if [ -n "${VAST_API_KEY:-}" ] && [ -n "${IDLE_MIN:-}" ]; then
  GUARD=(--auto-destroy --idle-timeout "$IDLE_MIN")
  echo "cost guard: the instance DESTROYS itself after $IDLE_MIN idle minutes with no job (disk is lost)."
else
  echo "cost guard: off (set VAST_API_KEY and IDLE_MIN to destroy the instance when idle)."
fi
nohup python3 scripts/serve.py --provider real --host 127.0.0.1 --port "$PORT" \
  ${GUARD[@]+"${GUARD[@]}"} > "$LOGS/server.log" 2>&1 &
echo $! > "$LOGS/server.pid"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$(cat "$LOGS/server.pid")" 2>/dev/null; then
    tail -n 30 "$LOGS/server.log" >&2; die "the server exited (see above / $LOGS/server.log)."
  fi
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/health" | python3 -c 'import json,sys; h=json.load(sys.stdin); assert h["provider"]=="real", h; print("health:", h["provider"], h["gpu"]["name"], "| models on disk:", sum(h["models_local"].values()), "/ 5")' \
  || die "the server is up but not in REAL mode — refusing to expose it."

# --- 6. Cloudflare Tunnel ---------------------------------------------------------------
say "6/6 Cloudflare Tunnel"
if ! command -v cloudflared >/dev/null; then
  arch="$(uname -m)"; case "$arch" in x86_64) a=amd64;; aarch64|arm64) a=arm64;; *) die "unknown arch $arch";; esac
  curl -fsSL -o /usr/local/bin/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$a"
  chmod +x /usr/local/bin/cloudflared
fi
cloudflared --version
if [ -n "${CF_TUNNEL_TOKEN:-}" ]; then
  # (a) NAMED tunnel: created once in the Cloudflare dashboard with a public hostname on your
  # domain pointing at http://localhost:$PORT. The hostname never changes between sessions.
  nohup cloudflared tunnel --no-autoupdate run --token "$CF_TUNNEL_TOKEN" > "$LOGS/tunnel.log" 2>&1 &
  echo $! > "$LOGS/tunnel.pid"
  sleep 5
  [ -n "${CF_TUNNEL_HOSTNAME:-}" ] || die "set CF_TUNNEL_HOSTNAME (the public hostname of the named tunnel) so the backend can register it."
  URL="https://$CF_TUNNEL_HOSTNAME"
else
  # (b) QUICK tunnel: no account needed; a NEW random https://*.trycloudflare.com URL each time.
  nohup cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > "$LOGS/tunnel.log" 2>&1 &
  echo $! > "$LOGS/tunnel.pid"
  URL=""
  for _ in $(seq 1 60); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOGS/tunnel.log" | head -n1 || true)"
    [ -n "$URL" ] && break
    sleep 1
  done
  [ -n "$URL" ] || { tail -n 20 "$LOGS/tunnel.log" >&2; die "no trycloudflare URL appeared."; }
fi
echo "$URL" > "$AGENTMETER_PUBLIC_URL_FILE"

# --- registration: the backend picks the URL up within ~5 s and registers with the Worker ---
REG=""
for _ in $(seq 1 30); do
  if curl -fsS "$AGENTMETER_WORKER_URL/api/health" >/dev/null 2>&1; then REG=ok; break; fi
  sleep 1
done
[ -n "$REG" ] || echo "WARNING: the Worker at $AGENTMETER_WORKER_URL did not answer /api/health — check the URL; the backend keeps retrying."

cat <<EOF

============================================================================
 AgentMeter backend is UP — provider REAL on $(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)
   backend URL : $URL
   health      : $URL/health
   logs        : $LOGS/server.log   $LOGS/tunnel.log

 Registered with the control plane: $AGENTMETER_WORKER_URL
   The front-end finds this backend through the Worker — nothing to paste. Log in and
   the Run button turns on (GPU online). Check: grep "control plane" $LOGS/server.log

 Stop billing when done:  destroy the instance in the Vast console (or: vastai destroy instance <id>).
 Stopping (pausing) still bills for disk.  Stop only the backend: bash scripts/vast_down.sh
============================================================================
EOF
