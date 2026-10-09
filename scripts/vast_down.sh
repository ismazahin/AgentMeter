#!/usr/bin/env bash
# Phase E — stop the AgentMeter backend + tunnel started by scripts/vast_up.sh.
# This does NOT stop Vast billing: destroy the instance in the Vast console for that
# (or `vastai destroy instance <id>`), see docs/DEPLOY.md "Stop billing".
set -uo pipefail
LOGS="${AGENTMETER_DATA_DIR:-/workspace}/agentmeter-logs"
for name in tunnel server; do
  f="$LOGS/$name.pid"
  if [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then
    kill "$(cat "$f")" && echo "stopped $name (pid $(cat "$f"))"
  else
    echo "$name: not running"
  fi
  rm -f "$f"
done
echo "Jobs, prepared sets and model weights stay on disk until the instance is DESTROYED."
