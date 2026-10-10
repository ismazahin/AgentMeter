#!/usr/bin/env bash
# Phase 47 — run the control-plane walk-through locally (no Cloudflare account needed):
#   Worker in `wrangler dev` (local D1 + R2 in a temp dir) -> bootstrap the admin -> Pages-like
#   static server for web/ (applies web/_headers, incl. the CSP) -> tests/e2e/cp_flow.js, which
#   starts / stops / restarts the mock GPU backend itself.
#     bash tests/e2e/cp_stack.sh [out_dir]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$(mktemp -d)}"; mkdir -p "$OUT"
STATE="$OUT/wrangler-state"; rm -rf "$STATE"
export WP=8787 PP=8080 BP=8766
for port in $WP $PP $BP; do
  if curl -s -o /dev/null -m 2 "http://127.0.0.1:$port/"; then echo "port $port is in use (an earlier run?) — stop it first" >&2; exit 2; fi
done
export DEV_RUN_SECRET="dev-run-secret-$(openssl rand -hex 12)" DEV_BACKEND_SECRET="dev-backend-secret-$(openssl rand -hex 12)"
PEPPER="dev-pepper-$(openssl rand -hex 12)"
cd "$REPO/worker"
cat > .dev.vars <<V
ACCESS_TOKEN_SECRET=dev-access-$(openssl rand -hex 16)
RUN_TOKEN_SECRET=$DEV_RUN_SECRET
BACKEND_SECRET=$DEV_BACKEND_SECRET
PASSWORD_PEPPER=$PEPPER
ALLOW_HTTP_BACKEND=1
ALLOWED_ORIGINS=http://127.0.0.1:$PP
BACKEND_OFFLINE_AFTER_S=12
V
npx wrangler d1 migrations apply agentmeter --local --persist-to "$STATE" > "$OUT/migrations.log" 2>&1
PASSWORD_PEPPER="$PEPPER" AGENTMETER_ADMIN_PASSWORD="admin-password-123" WRANGLER_PERSIST_TO="$STATE" \
  node scripts/bootstrap-admin.mjs admin --local
setsid npx wrangler dev --local --persist-to "$STATE" --ip 127.0.0.1 --port "$WP" > "$OUT/wrangler.log" 2>&1 &
WPID=$!
python "$REPO/tests/e2e/pages_server.py" "$PP" "http://127.0.0.1:$WP" "http://127.0.0.1:$BP" > "$OUT/pages.log" 2>&1 &
PPID2=$!
cleanup() {
  kill $PPID2 2>/dev/null || true
  kill -- -"$WPID" 2>/dev/null || true          # the whole group: npx -> wrangler -> workerd
  rm -f "$REPO/worker/.dev.vars"
}
trap cleanup EXIT
for _ in $(seq 1 60); do curl -fsS "http://127.0.0.1:$WP/api/health" >/dev/null 2>&1 && break; sleep 1; done
cd "$REPO"
node tests/e2e/cp_flow.js "$OUT"
