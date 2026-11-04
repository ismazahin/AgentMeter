#!/usr/bin/env bash
# The start-up script of the Vast.ai template "AgentMeter" (docs/VAST_TEMPLATE.md): a freshly
# rented GPU sets itself up with nothing copied by hand. On Vast's vastai/pytorch images it is
# named by the environment variable
#
#   PROVISIONING_SCRIPT=https://raw.githubusercontent.com/ismazahin/AgentMeter/claude/cool-ride-mitmzl/scripts/vast_onstart.sh
#
# (run after /workspace and the environment variables are ready; the template's On-start field
# runs too early on those images). It is self-contained: it clones the code itself. By hand:
#   bash AgentMeter/scripts/vast_onstart.sh   (from /workspace)
#
# Settings come from the template's / account's environment variables (never from this repo):
#   required  HF_TOKEN  AGENTMETER_CONTROL_PLANE_URL  AGENTMETER_RUN_TOKEN_SECRET  AGENTMETER_BACKEND_SECRET
#   optional  VAST_API_KEY + IDLE_MIN (default 60: the instance destroys itself after that many idle
#             minutes)  AGENTMETER_ALLOWED_ORIGINS (default https://agentmeter.pages.dev)
#             PRELOAD_STUDY=1  AGENTMETER_BRANCH (default claude/cool-ride-mitmzl)
#
# The IDS model is downloaded from the control plane (Admin › IDS model) by scripts/vast_up.sh.
# Everything runs in tmux session "am" and is logged to /workspace/agentmeter-onstart.log:
#   ssh in, then `tmux attach -t am` to watch; the site shows the GPU online when it is ready.
set -uo pipefail

LOG=/workspace/agentmeter-onstart.log
mkdir -p /workspace
exec > >(tee -a "$LOG") 2>&1
echo "==> AgentMeter on-start $(date -u +%FT%TZ)"

# Vast passes template variables to the container; an SSH-launch instance keeps them in
# /etc/environment only, so read that too (values are never printed).
if [ -f /etc/environment ]; then set -a; . /etc/environment 2>/dev/null || true; set +a; fi

missing=()
for v in HF_TOKEN AGENTMETER_CONTROL_PLANE_URL AGENTMETER_RUN_TOKEN_SECRET AGENTMETER_BACKEND_SECRET; do
  [ -n "${!v:-}" ] || missing+=("$v")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "ERROR: missing environment variables: ${missing[*]}"
  echo "Set them in the Vast template (or Account › Environment variables); see docs/VAST_TEMPLATE.md."
  exit 1
fi

BRANCH="${AGENTMETER_BRANCH:-claude/cool-ride-mitmzl}"
cd /workspace
if [ -d AgentMeter/.git ]; then
  git -C AgentMeter fetch -q origin "$BRANCH" && git -C AgentMeter checkout -q "$BRANCH" \
    && git -C AgentMeter pull -q --ff-only origin "$BRANCH" || echo "warning: could not update the code; using what is there"
else
  git clone -q -b "$BRANCH" https://github.com/ismazahin/AgentMeter.git AgentMeter || { echo "ERROR: git clone failed"; exit 1; }
fi
cd /workspace/AgentMeter
echo "code: $(git log --oneline -1)"

# Everything vast_up.sh needs, in a file only root can read (tmux starts a fresh shell).
umask 077
ENVF=/root/agentmeter.env
{
  for v in HF_TOKEN AGENTMETER_CONTROL_PLANE_URL AGENTMETER_RUN_TOKEN_SECRET AGENTMETER_BACKEND_SECRET \
           VAST_API_KEY PRELOAD_STUDY AGENTMETER_IDS_MODEL CF_TUNNEL_TOKEN CF_TUNNEL_HOSTNAME; do
    [ -n "${!v:-}" ] && printf "export %s=%q\n" "$v" "${!v}"
  done
  printf "export AGENTMETER_ALLOWED_ORIGINS=%q\n" "${AGENTMETER_ALLOWED_ORIGINS:-https://agentmeter.pages.dev}"
  printf "export IDLE_MIN=%q\n" "${IDLE_MIN:-60}"
} > "$ENVF"

RUN=/root/agentmeter-run.sh
cat > "$RUN" <<'EOS'
[ -f /venv/main/bin/activate ] && . /venv/main/bin/activate   # PyTorch on Vast's images
. /root/agentmeter.env
cd /workspace/AgentMeter
bash scripts/vast_up.sh 2>&1 | tee -a /workspace/agentmeter-onstart.log
EOS
chmod 700 "$RUN"

command -v tmux >/dev/null || { apt-get update -qq && apt-get install -y -qq tmux; } || true
if command -v tmux >/dev/null; then
  tmux has-session -t am 2>/dev/null && { echo "tmux session 'am' already runs: not starting twice"; exit 0; }
  tmux new-session -d -s am "bash $RUN; exec bash"
  echo "started in tmux session 'am' (ssh in, then: tmux attach -t am)"
else
  echo "tmux unavailable: running in the background, log $LOG"
  nohup bash "$RUN" >/dev/null 2>&1 &
fi
