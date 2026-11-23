#!/usr/bin/env bash
# Data preparation with a waiting list, end to end and locally (no Supabase account needed):
#   the control plane's dev server (CLOUD_PREP_SECRET set) <- the preparation service
#   (cloudprep/app.py, as it runs on the GPU machine: it registers and PULLS waiting uploads, one at a
#   time and never while a benchmark holds the machine's lock) -> Pages-like static server for web/
#   (applies web/_headers, incl. the CSP) -> the walk-through, which starts the mock GPU backend itself
#   (same machine, same lock as the service).
#     bash tests/e2e/cloudprep_stack.sh [out_dir]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$(mktemp -d)}"; mkdir -p "$OUT"
STATE="$OUT/controlplane-data"; rm -rf "$STATE"
export WP=8787 PP=8080 BP=8766 CPP=8790
for port in $WP $PP $BP $CPP; do
  if curl -s -o /dev/null -m 2 "http://127.0.0.1:$port/"; then echo "port $port is in use (an earlier run?) — stop it first" >&2; exit 2; fi
done
export DEV_RUN_SECRET="dev-run-secret-$(openssl rand -hex 12)" DEV_BACKEND_SECRET="dev-backend-secret-$(openssl rand -hex 12)"
PREP_SECRET="dev-cloud-prep-$(openssl rand -hex 12)"
CP_URL="http://127.0.0.1:$WP/functions/v1/agentmeter"
export AGENTMETER_BUSY_LOCK="$OUT/busy.lock"          # the service and the mock GPU server share it
CLOUD_PREP_SECRET="$PREP_SECRET" PORT="$CPP" PULL_S=2 AGENTMETER_CONTROL_PLANE_URL="$CP_URL" PYTHONPATH="$REPO" \
  setsid python "$REPO/cloudprep/app.py" > "$OUT/cloudprep.log" 2>&1 &
CPID=$!
cd "$REPO/controlplane"
ACCESS_TOKEN_SECRET="dev-access-$(openssl rand -hex 16)" RUN_TOKEN_SECRET="$DEV_RUN_SECRET" BACKEND_SECRET="$DEV_BACKEND_SECRET" \
PASSWORD_PEPPER="dev-pepper-$(openssl rand -hex 12)" ALLOW_HTTP_BACKEND=1 ALLOWED_ORIGINS="http://127.0.0.1:$PP" BACKEND_OFFLINE_AFTER_S=12 \
CLOUD_PREP_SECRET="$PREP_SECRET" \
AGENTMETER_ADMIN_USER=admin AGENTMETER_ADMIN_PASSWORD="admin-password-123" DATA_DIR="$STATE" PORT="$WP" \
  setsid node --experimental-transform-types --no-warnings dev-server.ts > "$OUT/controlplane.log" 2>&1 &
WPID=$!
python "$REPO/tests/e2e/pages_server.py" "$PP" "$CP_URL" "http://127.0.0.1:$BP" > "$OUT/pages.log" 2>&1 &
PPID2=$!
cleanup() {
  kill $PPID2 2>/dev/null || true
  kill -- -"$WPID" 2>/dev/null || true
  kill -- -"$CPID" 2>/dev/null || true
}
trap cleanup EXIT
for _ in $(seq 1 60); do curl -fsS "$CP_URL/api/health" >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:$CPP/health" >/dev/null 2>&1 && break; sleep 1; done
# what scripts/prep_local.py does on the GPU machine: register the service's address (signed)
PREP_REG() { PYTHONPATH="$REPO" python - "$CP_URL" "$PREP_SECRET" "$1" <<'PY'
import sys, importlib.util
spec = importlib.util.spec_from_file_location("pl", "scripts/prep_local.py"); pl = importlib.util.module_from_spec(spec); spec.loader.exec_module(pl)
cp, secret, what = sys.argv[1:4]
st, d = pl.tell_control_plane(cp, secret, "register", {"stopping": True} if what == "stop" else {"url": what, "version": "e2e"})
print("prep register:", st, d); sys.exit(0 if st == 200 else 1)
PY
}
cd "$REPO"
PREP_REG "http://127.0.0.1:$CPP"
export CP_URL PREP_SECRET CPP
export -f PREP_REG
# CLOUDPREP_FLOWS picks the walk-throughs (default: analyse_flow); each gets its own folder.
for f in ${CLOUDPREP_FLOWS:-analyse_flow}; do node "tests/e2e/$f.js" "$OUT/$f"; done
