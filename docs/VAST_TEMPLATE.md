# A GPU that sets itself up: the Vast.ai template "AgentMeter"

Once set up, renting a GPU is: pick an offer → template **AgentMeter** → **Rent**. The instance
clones AgentMeter, installs it, downloads the IDS model (Admin › IDS model), starts the backend
and a tunnel, and registers with the control plane. After about 5 minutes the site shows the GPU
online. No PowerShell, no SSH, nothing copied by hand. (`scripts\vast_start.ps1` keeps working as
the manual way.)

Vast.ai's screens change from time to time: if a button below has another name, look for the same
setting (template image, launch mode, on-start script, environment variables).

## One-time setup

### 1. Upload the IDS model (the site)

Admin → **IDS model** → choose `ids_rf.joblib`, `ids_rf.json` and `ids_rf.hashes.npy` from
`models\ids\` → **Upload and use**. The upload is checked (the .joblib must match the sha256 in its
.json). To replace it later (a retrained model), upload the new three files the same way.

### 2. Put the settings in Vast (once)

Vast.ai → **Account** (or Settings) → **Environment Variables**: add each variable below. They are
given to every instance you rent. Values are the same as in your `backend.env` and `.env`.

| Variable | Value |
|---|---|
| `HF_TOKEN` | your Hugging Face token (`.env`) |
| `AGENTMETER_CONTROL_PLANE_URL` | `https://<project-ref>.supabase.co/functions/v1/agentmeter` |
| `AGENTMETER_RUN_TOKEN_SECRET` | = the control plane's `RUN_TOKEN_SECRET` (`backend.env`) |
| `AGENTMETER_BACKEND_SECRET` | = the control plane's `BACKEND_SECRET` (`backend.env`) |
| `VAST_API_KEY` | optional but recommended: lets an idle instance destroy itself |
| `IDLE_MIN` | optional, default 60: idle minutes before it destroys itself |
| `PRELOAD_STUDY` | optional: `1` downloads all 5 study models at start-up (slower start) |

Keep these in the **account's** environment variables, not in a template you might share, and
protect the Vast account (strong password, two-factor). Anyone who controls the account can read
them; they are the same secrets `vast_start.ps1` copies to every instance today.

### 3. Create the template (once)

Vast.ai → **Templates** → **New template** (or edit a copy of the PyTorch template you use now):

- **Image:** the same PyTorch + CUDA ≥ 12.1 image you rent today (e.g. Vast's PyTorch image).
- **Launch mode:** SSH (keep SSH, so you can still log in and watch).
- **Disk:** at least 100 GB (models, ~16 GB each).
- **On-start script:** paste this one line:
  ```
  cd /workspace && (git -C AgentMeter pull -q --ff-only || git clone -q -b claude/cool-ride-mitmzl https://github.com/ismazahin/AgentMeter.git AgentMeter); bash AgentMeter/scripts/vast_onstart.sh
  ```
- **Visibility:** private. Save it as **AgentMeter**.

## Every rental

1. Search offers (e.g. RTX 3090 / 4090 / L4), choose the **AgentMeter** template, **Rent**.
2. Wait about 5 minutes (longer the first time models download). The site's GPU badge turns
   online; Admin › Problems shows a start-up error if one happened.
3. Run benchmarks. When finished, wait for the sessions to show **Done** in Sessions (results are
   kept in Supabase), then destroy the instance — or let `IDLE_MIN` do it.

To watch the start-up: Vast's **Connect** → SSH, then `tmux attach -t am` (detach: Ctrl+B, D),
or read `/workspace/agentmeter-onstart.log`.

## What the on-start script does (`scripts/vast_onstart.sh`)

checks the required variables (and says which is missing) → clones / updates the code → writes
the settings to `/root/agentmeter.env` (readable by root only) → starts `scripts/vast_up.sh` in
tmux session `am`, which installs, checks the GPU, downloads the models marked for start-up,
**downloads the IDS model** (`scripts/vast_ids.py`, signed with `AGENTMETER_BACKEND_SECRET`,
checked by sha256 and scikit-learn version), starts the backend and the tunnel. It never rents
or destroys anything except the idle self-destroy you opt into with `VAST_API_KEY`.
