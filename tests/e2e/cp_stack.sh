#!/usr/bin/env bash
# Run the control-plane walk-through locally (no Supabase account needed):
#   the control plane's dev server (controlplane/dev-server.ts: the Edge Function's routes on
#   PGlite, served under /functions/v1/agentmeter like Supabase, with Storage-like direct uploads)
#   -> the first admin -> Pages-like static server for web/ (applies web/_headers, incl. the CSP)
#   -> tests/e2e/cp_flow.js, which starts / stops / restarts the mock GPU backend itself.
#     bash tests/e2e/cp_stack.sh [out_dir]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$(mktemp -d)}"; mkdir -p "$OUT"
STATE="$OUT/controlplane-data"; rm -rf "$STATE"
export WP=8787 PP=8080 BP=8766
for port in $WP $PP $BP; do
  if curl -s -o /dev/null -m 2 "http://127.0.0.1:$port/"; then echo "port $port is in use (an earlier run?) — stop it first" >&2; exit 2; fi
done
export DEV_RUN_SECRET="dev-run-secret-$(openssl rand -hex 12)" DEV_BACKEND_SECRET="dev-backend-secret-$(openssl rand -hex 12)"
CP_URL="http://127.0.0.1:$WP/functions/v1/agentmeter"
cd "$REPO/controlplane"
ACCESS_TOKEN_SECRET="dev-access-$(openssl rand -hex 16)" RUN_TOKEN_SECRET="$DEV_RUN_SECRET" BACKEND_SECRET="$DEV_BACKEND_SECRET" \
PASSWORD_PEPPER="dev-pepper-$(openssl rand -hex 12)" ALLOW_HTTP_BACKEND=1 ALLOWED_ORIGINS="http://127.0.0.1:$PP" BACKEND_OFFLINE_AFTER_S=12 \
AGENTMETER_ADMIN_USER=admin AGENTMETER_ADMIN_PASSWORD="admin-password-123" DATA_DIR="$STATE" PORT="$WP" \
  setsid node --experimental-transform-types --no-warnings dev-server.ts > "$OUT/controlplane.log" 2>&1 &
WPID=$!
python "$REPO/tests/e2e/pages_server.py" "$PP" "$CP_URL" "http://127.0.0.1:$BP" > "$OUT/pages.log" 2>&1 &
PPID2=$!
cleanup() {
  kill $PPID2 2>/dev/null || true
  kill -- -"$WPID" 2>/dev/null || true
}
trap cleanup EXIT
for _ in $(seq 1 60); do curl -fsS "$CP_URL/api/health" >/dev/null 2>&1 && break; sleep 1; done
export CP_URL
cd "$REPO"
node tests/e2e/cp_flow.js "$OUT"
