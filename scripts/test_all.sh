#!/usr/bin/env bash
# The full AgentMeter test suite in one command (CPU only: no GPU, no tokens, no accounts).
#
#   bash scripts/test_all.sh            # everything
#   bash scripts/test_all.sh e2e        # only the browser walk-throughs (mock backend)
#   bash scripts/test_all.sh stacks     # only the control-plane and cloud-prep stacks
#   bash scripts/test_all.sh unit       # only pytest + the control-plane vitest
#
# Needs: Python with requirements.txt + requirements-dev.txt + flask (+ requirements-pcap.txt for
# PCAP flows), Node 22 with Playwright (require('playwright') must resolve; set PLAYWRIGHT_PATH
# otherwise) and `npm ci` in controlplane/. Optional: PYODIDE_DIST = a local copy of the Pyodide
# 0.28.3 "full" files for prep_flow (without it Pyodide loads from its CDN).
#
# Each browser flow runs against a FRESH mock backend on port 8766 (an empty results folder), as it
# expects. Logs and screenshots go to $OUT (default: a new temp folder). Exit code 1 if anything failed.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${OUT:-$(mktemp -d -t agentmeter-tests-XXXX)}"; mkdir -p "$OUT"
WHAT="${1:-all}"
PY="${PYTHON:-python}"
cd "$REPO"
FAILED=0; SUMMARY=()
note() { SUMMARY+=("$1"); echo "$1"; }

SERVE_PID=""
stop_backend() { [ -n "$SERVE_PID" ] && kill -- -"$SERVE_PID" 2>/dev/null; SERVE_PID=""; sleep 1; }
start_backend() {   # start_backend VAR=value ...  -> a fresh mock backend on :8766
  stop_backend
  local results; results="$(mktemp -d)"
  env "$@" AGENTMETER_RESULTS_DIR="$results" setsid "$PY" scripts/serve.py --provider mock --port 8766 > "$OUT/serve.log" 2>&1 < /dev/null &
  SERVE_PID=$!
  for _ in $(seq 60); do curl -s -o /dev/null 127.0.0.1:8766/health && return 0; sleep 1; done
  echo "the mock backend did not start (see $OUT/serve.log)" >&2; return 1
}
trap stop_backend EXIT

flow() {            # flow <name> [VAR=value ...]
  local name=$1; shift
  if ! start_backend "$@"; then note "$name: backend did not start"; FAILED=1; return; fi
  timeout 900 node "tests/e2e/$name.js" http://127.0.0.1:8766 "$OUT/$name" > "$OUT/$name.out" 2>&1
  local code=$? pass fail
  pass=$(grep -c '^PASS' "$OUT/$name.out"); fail=$(grep -cE '^(FAIL|CRASH)' "$OUT/$name.out")
  if [ "$code" -ne 0 ] || [ "$fail" -ne 0 ] || [ "$pass" -eq 0 ]; then
    FAILED=1; note "✗ $name: $pass passed, $fail failed (exit $code) — $OUT/$name.out"
    grep -E '^(FAIL|CRASH)' "$OUT/$name.out" | head -5 | sed 's/^/    /'
  else note "✓ $name: $pass passed"; fi
}

python3 scripts/csp_headers.py > /dev/null      # web/_headers must match the inline scripts

if [ "$WHAT" = all ] || [ "$WHAT" = e2e ]; then
  flow insights_flow
  flow pipeline_flow
  flow service_flow AGENTMETER_MAX_UPLOAD_MB=1 AGENTMETER_MAX_CSV_ROWS=100000
  flow session_analysis
  flow app_flow
  flow compare_many_flow
  flow datasets_gpu_flow AGENTMETER_MOCK_LATENCY_S=0.2
  flow workflow_flow
  flow telemetry_flow AGENTMETER_MOCK_LATENCY_S=0.05
  flow prep_flow
  flow observability_flow AGENTMETER_TEST_SYNTHETIC_GPU=1 AGENTMETER_IDLE_SAMPLE_S=1 AGENTMETER_MOCK_LATENCY_S=0.02
  flow model_picker_flow AGENTMETER_TEST_MODEL_REPOS=tests/fixtures/model_repos.json AGENTMETER_TEST_DISK_FREE_GB=200
  flow ids_flow
  stop_backend
fi

if [ "$WHAT" = all ] || [ "$WHAT" = stacks ]; then
  for stack in cp_stack cloudprep_stack; do
    timeout 900 bash "tests/e2e/$stack.sh" "$OUT/$stack" > "$OUT/$stack.out" 2>&1
    code=$?
    if [ "$code" -ne 0 ]; then FAILED=1; note "✗ $stack: exit $code — $(tail -1 "$OUT/$stack.out")"
    else note "✓ $stack: $(tail -1 "$OUT/$stack.out")"; fi
  done
  # the Start button: no fixed service address, the service registers itself
  PREP_REGISTERED=1 CLOUDPREP_FLOWS=prep_button_flow timeout 600 bash tests/e2e/cloudprep_stack.sh "$OUT/prep_button" > "$OUT/prep_button.out" 2>&1
  code=$?
  if [ "$code" -ne 0 ]; then FAILED=1; note "✗ prep_button: exit $code — $(tail -1 "$OUT/prep_button.out")"
  else note "✓ prep_button: $(tail -1 "$OUT/prep_button.out")"; fi
fi

if [ "$WHAT" = all ] || [ "$WHAT" = unit ]; then
  (cd controlplane && npx vitest run > "$OUT/vitest.out" 2>&1); code=$?
  line=$(grep -E "Tests " "$OUT/vitest.out" | tail -1 | sed 's/^ *//')
  if [ "$code" -ne 0 ]; then FAILED=1; note "✗ vitest: $line"; else note "✓ vitest: $line"; fi
  timeout 1800 "$PY" -m pytest -p no:warnings > "$OUT/pytest.out" 2>&1; code=$?
  line=$(grep -E "passed|failed|error" "$OUT/pytest.out" | tail -1)
  if [ "$code" -ne 0 ]; then FAILED=1; note "✗ pytest: $line"; else note "✓ pytest: $line"; fi
fi

echo; echo "Summary ($OUT):"; printf '  %s\n' "${SUMMARY[@]}"
exit $FAILED
