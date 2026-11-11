#!/usr/bin/env bash
# Restart ONLY the AgentMeter server on a running rental, e.g. after `git pull`, with the same
# settings scripts/vast_up.sh used. The Cloudflare tunnel and its address are left alone, so the
# website reconnects within seconds and no new quick tunnel is requested (those are rate-limited).
#
#   cd /workspace/AgentMeter && git pull origin claude/cool-ride-mitmzl && bash scripts/vast_restart_server.sh
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f /venv/main/bin/activate ] && . /venv/main/bin/activate
set -a
for f in /root/backend.env /root/hf.env /root/agentmeter.env; do [ -f "$f" ] && . "$f"; done
set +a
DATA="${AGENTMETER_DATA_DIR:-/workspace}"
LOGS="$DATA/agentmeter-logs"
PORT="${PORT:-8000}"
export HF_HOME="${HF_HOME:-$DATA/hf}" PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1
export AGENTMETER_PUBLIC_URL_FILE="$LOGS/public_url"
export AGENTMETER_CONTROL_PLANE_URL="${AGENTMETER_CONTROL_PLANE_URL:-${AGENTMETER_WORKER_URL:-}}"
export AGENTMETER_ALLOWED_ORIGINS="${AGENTMETER_ALLOWED_ORIGINS:-https://agentmeter.pages.dev}"
# the IDS model vast_up.sh used: copied by vast_start.ps1, or fetched to $DATA/ids
if [ -z "${AGENTMETER_IDS_MODEL:-}" ]; then
  for m in /root/ids/ids_rf.joblib "$DATA"/ids/*.joblib; do
    if [ -f "$m" ]; then export AGENTMETER_IDS_MODEL="$m"; break; fi
  done
fi
GUARD=()
if [ -n "${VAST_API_KEY:-}" ] && [ -n "${IDLE_MIN:-}" ]; then GUARD=(--auto-destroy --idle-timeout "$IDLE_MIN"); fi

if pgrep -f "scripts/serve.py" >/dev/null; then
  echo "stopping the running server ..."
  pkill -f "scripts/serve.py" || true
  for _ in $(seq 1 20); do pgrep -f "scripts/serve.py" >/dev/null || break; sleep 1; done
fi
mkdir -p "$LOGS"
nohup python3 scripts/serve.py --provider real --host 127.0.0.1 --port "$PORT" \
  ${GUARD[@]+"${GUARD[@]}"} > "$LOGS/server.log" 2>&1 &
echo $! > "$LOGS/server.pid"
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  if ! kill -0 "$(cat "$LOGS/server.pid")" 2>/dev/null; then
    tail -n 30 "$LOGS/server.log" >&2; echo "ERROR: the server exited (see above)." >&2; exit 1
  fi
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null || { echo "ERROR: the server did not answer on port $PORT." >&2; exit 1; }
echo "server restarted on code $(git log --oneline -1)"
if pgrep -f "cloudflared tunnel" >/dev/null && [ -s "$AGENTMETER_PUBLIC_URL_FILE" ]; then
  echo "tunnel kept: $(cat "$AGENTMETER_PUBLIC_URL_FILE") (the website reconnects within about 10 s)"
else
  echo "WARNING: no tunnel is running. Start one: see docs/DEPLOY.md, or run bash /root/am_start.sh"
fi
