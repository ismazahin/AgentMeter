# Preparation on the GPU machine, no reuse, no schedules

Status: proposed, waiting for the owner's approval. Mockups: `2026-11-22-hybrid-prep-mockup/`
(`4-samebox-upload-on.png`, `4-samebox-upload-off.png`, `2-session-new-benchmark.png`).

## Why

- Preparation today needs either Google Cloud Run (it asked for a MYR 120 prepayment, rejected) or
  the owner's laptop running `scripts/prep_local.py`. The owner wants neither: no spending, and no
  dependence on the laptop.
- The owner wants every benchmark to start from a fresh upload. Reusing a prepared set is removed.
- Scheduled re-runs reuse a session's flows without an upload, so they are removed too.

## Goals

- Preparation (and the IDS traffic analysis) runs on the rented Vast.ai GPU machine, as a second,
  low-priority process. It starts with the instance and dies with it. No extra cost.
- New benchmark offers only "Upload a file" and "Import from a link".
- "Run again", "Run another model on this set", "Benchmark this set" and "Run it again" become
  plain New benchmark links, with a note on how to compare like for like.
- No schedules anywhere.

## Non-goals

- No change to how preparation works (same code, same seed, same prepared-set hash), so old and new
  sessions still group into the same setup on the Leaderboard and Consistency when the same file and
  flow count are used.
- No change to the steps after upload (Your data, Models and settings, Run).

## Anti-goals

- Anything that bills money (a second instance, Cloud Run, a paid tunnel).
- Preparation slowing a running benchmark enough to change its numbers.

## Approaches considered

1. **Run the existing preparation service on the GPU machine (picked).** `scripts/vast_up.sh` also
   starts `scripts/prep_local.py` (which runs `cloudprep/app.py`, opens its own Cloudflare quick
   tunnel and registers with the control plane, as on the laptop today). The control plane already
   prefers a registered online service. Smallest change, and the Your data step keeps working as is.
2. Fold preparation into the GPU server's Flask app with its own worker lane. Fewer processes, but
   the control-plane job flow (`/api/prep/jobs`) and the Your data step would need rework.
3. A second Vast.ai instance only for preparation, destroyed with the GPU one. Costs money and needs
   a watcher holding the Vast.ai API key. Rejected by the owner's no-spending rule.

## Design

### GPU machine
- `scripts/vast_up.sh` starts `prep_local.py` in the background after the GPU server is up, with
  `nice -n 19` and `taskset` pinning it to at most 2 CPU cores (fewer if the machine has under 6),
  so the benchmark keeps the rest. Its log goes to `/workspace/agentmeter-prep.log`.
- The service secret: `CLOUD_PREP_SECRET` comes from the Vast template environment, like the
  other secrets. `vast_onstart.sh` adds it to the required list and to `agentmeter.env`.
- Idle self-destroy (`IDLE_MIN`) also counts a running preparation as activity, so the instance
  never destroys itself mid-preparation.
- A session that ran while a preparation was running alongside it gets a recorded flag
  (`prep_overlap: true`) and a small note in its "How this was measured" details.

### Control plane
- `prep.ts` uses only the registered service. The `CLOUD_PREP_URL` fallback is removed.
- `backend.ts` reports `prep_service.online` as today. The web app treats "GPU server offline" and
  "preparation service offline" as one state: the GPU server is off.
- Schedules have no control-plane part beyond `runs.ts` passing them through, which is removed.

### Website
- Upload step: the reuse tab, its panel (your analyses, all data sets, saved sets, prepared on this
  server) and the "Data sets" help line go. The preparation note becomes the one-line status in the
  mockup ("Prepared on the GPU server." or "The GPU server is off.").
- Continue no longer starts anything on the owner's computer: `anLaunchService`, `anWatchService`,
  the `agentmeter-prep://` links and the install hint go. When the service is offline, Continue is
  disabled and the warning in the mockup shows.
- `wakeCloud` (the Hugging Face nudge) goes.
- The four rerun buttons become `#/new` links named "New benchmark", with the note "To compare like
  for like, upload the same file and pick the same number of flows."
- Schedules: the Overview "Repeat on a schedule" card, the Consistency "Repeat a run automatically"
  card and the scheduled-run markers on the Consistency chart go.
- Deep links that preselect a set (`#/new/models?set=…` from outside the wizard) stop being offered.
  The wizard's own hand-off from Your data to Models stays.

### GPU server (Python)
- `agentmeter/server/schedules.py`, its routes, its tests and its OpenAPI entries are removed. Old
  `schedules.json` files are ignored.
- The reuse-only routes stay for now if the wizard's own flow still uses them (the builder checks
  `/api/prepared/import`, which the hand-off from the preparation service uses).

### Removed files and docs
- `scripts/deploy_cloudprep.sh`, `scripts/deploy_cloudprep.ps1`, `scripts/deploy_cloudprep_hf.py`.
- `docs/DEPLOY.md`: the Cloud Run (A7) and Hugging Face sections. A short section says the
  preparation service runs on the GPU machine and needs `CLOUD_PREP_SECRET` in the Vast template.
- `prep_local.py`: the Windows `install`, `uninstall`, `start`, `stop` commands and the protocol
  handler. Running it in the foreground (what the GPU machine does) stays.

### Tests
- Remove `tests/test_schedules.py` and schedule steps in e2e flows. Update e2e flows that used the
  reuse tab, the data-set list or the laptop launcher. Add a test that the upload step has exactly
  two tabs, and one that `vast_up.sh` starts the preparation service at low priority.

## Left to the builder
- Exact core count for `taskset` on unusual machines (minimum 1).
- Whether the browser (Pyodide) preparation path, used only without a control plane, stays as is.
  Default: leave it untouched.
