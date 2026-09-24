#!/usr/bin/env bash
# AgentMeter — one-click wrapper for the EXISTING full run (no new run logic).
set -euo pipefail
cd "$(dirname "$0")"

echo "============================================================"
echo "  AgentMeter - FULL RUN (5 models x 300 scenarios, sequential)"
echo "  Command: python main.py --config configs/run_full_l4.yaml run-full"
echo
echo "  Requires a CUDA GPU (require_gpu is enforced - no CPU fallback)."
echo "  This INCURS GPU COST. Results persist to"
echo "  results/agentmeter_full_l4.db and the run is resumable"
echo "  (re-run this to continue an interrupted run)."
echo "============================================================"
echo

python main.py --config configs/run_full_l4.yaml run-full

echo
read -n 1 -r -p "Done. Press any key to close..." _ || true
echo
