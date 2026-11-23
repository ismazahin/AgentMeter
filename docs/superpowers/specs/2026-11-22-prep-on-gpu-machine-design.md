# Preparation on the GPU machine, never alongside a benchmark, no reuse, no schedules

Status: approved by the owner on 2026-11-22 ("yes i approve, build it") and built. Mockups in `2026-11-22-hybrid-prep-mockup/`:
`4-samebox-upload-on.png`, `4-samebox-upload-off.png`, `5-waiting.png`, `5-your-uploads.png`,
`2-session-new-benchmark.png`.

## Why

- Preparation today needs either Google Cloud Run (it asked for a MYR 120 prepayment, rejected) or
  the owner's laptop running `scripts/prep_local.py`. The owner wants neither: no spending, and no
  dependence on the laptop.
- Preparing data at the same time as a benchmark can slow the benchmark's CPU work and so change
  latency, cost per 1,000 flows and energy per flow. The owner wants current and future readings
  to be unaffected, with no exceptions.
- Every benchmark starts from a fresh upload. Reusing a prepared set is removed.
- Scheduled re-runs reuse a session's flows without an upload, so they are removed too.

## Goals

- Preparation (and the IDS traffic analysis) runs on the rented Vast.ai GPU machine. It starts with
  the instance and dies with it. No extra cost.
- Preparation and benchmarks never run at the same time on that machine.
- An upload made while a benchmark runs is saved, waits, and prepares by itself as soon as the
  benchmark ends. The user can close the page and come back.
- New benchmark offers only "Upload a file" and "Import from a link".
- "Run again", "Run another model on this set", "Benchmark this set" and "Run it again" become plain
  New benchmark links, with a note on how to compare like for like.
- No schedules anywhere.

## Non-goals

- No change to how preparation works (same code, same seed, same prepared-set hash), so sessions
  on the same file with the same flow count still group into one setup on the Leaderboard and
  Consistency.
- No change to Your data, Models and settings, or Run.

## Anti-goals

- Anything that bills money (a second instance, Cloud Run, a paid tunnel, a paid Supabase plan).
- Any overlap between preparation and a benchmark, however small.
- A way to start a second benchmark from an earlier upload (that is reuse by another name).

## Approaches considered

1. **Run the existing preparation service on the GPU machine, with one shared lock (picked).**
   `scripts/vast_up.sh` also starts `scripts/prep_local.py` (it runs `cloudprep/app.py`, opens its
   own Cloudflare quick tunnel and registers with the control plane, as on the laptop today). A
   lock on the machine lets only one of "a benchmark" or "a preparation" run at a time. Smallest
   change, and the Your data step keeps working as is.
2. Fold preparation into the GPU server's job queue as a job kind. Exclusion would be automatic, but
   the control-plane job flow (`/api/prep/jobs`) and the Your data step would need rework.
3. Run preparation alongside benchmarks at low priority on 2 cores. Rejected: it can still shift
   latency, cost and energy.
4. A second Vast.ai instance only for preparation. Rejected: costs money.

## Design

### One at a time on the GPU machine
- A lock file on the machine (`/workspace/agentmeter.busy.lock`, `flock`). The GPU server's worker
  holds it for the whole benchmark: from loading the first model to writing the results. The
  preparation service holds it for the whole preparation, including the IDS scoring and a link's
  download.
- Preparation goes ahead of queued benchmarks: while the preparation service has work waiting it
  sets a marker (`/workspace/agentmeter.prep-wanted`). The worker, before taking the lock for its
  next benchmark, waits while the marker exists. A running benchmark is never interrupted.
- Preparations themselves run one at a time, oldest first.
- The preparation service still runs at `nice -n 19`. It is no longer needed for accuracy, it only
  keeps the web server responsive.
- Idle self-destroy (`IDLE_MIN`) counts waiting and running preparations as activity.

### The waiting list (control plane)
- A preparation job gets a new state, `waiting`, before `preparing`. The upload stays in Supabase
  Storage (as today), or for a link only the link is stored.
- The control plane hands a waiting job to the preparation service. The service accepts it at once
  and reports `waiting` with a reason ("the running benchmark") until it gets the lock, then
  `preparing`.
- If the GPU machine goes away, jobs that were `waiting` or `preparing` go back to `waiting` with
  the reason "the GPU server is off". The control plane hands them out again, oldest first, when a
  preparation service registers (its first heartbeat).
- Done: a Telegram message to the uploader (the existing alerts, always on), and the stored upload
  is deleted from Storage. The prepared files stay until a benchmark starts from them, or 7 days.
- Each upload can start exactly one benchmark. The control plane marks the job `used` when the
  benchmark is created and refuses a second one. Used and expired uploads leave "Your uploads".
- Cancel: allowed while `waiting`. It deletes the stored upload.
- Limits on the free Supabase plan: files up to 50 MB, 1 GB in all. The upload limit shown is
  50 MB. Bigger data comes in through Import from a link, which stores only the link while waiting.
  If Storage is full, the upload is refused with "Too many uploads are waiting. Try again later."

### Website
- Upload step: the reuse tab and its panel go, and so does the "Data sets" help line. One status
  line under the form: "Prepared on the GPU server. If a benchmark is running, your upload waits
  and prepares as soon as it ends." When the preparation service is offline: "The GPU server is
  off", a warning, and Continue disabled.
- Below the form, "Your uploads": the user's uploads not yet benchmarked, with Waiting, Preparing
  or Ready, and Open. It replaces the old "Your analyses" list.
- Step 2 (Preparing) in the waiting state (mockup `5-waiting.png`): "Waiting for the running
  benchmark to finish, then preparing.", why it waits, the upload's details, what is running now,
  its place, the Telegram note, and "Cancel this upload". It moves on by itself when preparing
  starts.
- Continue no longer starts anything on the owner's computer: `anLaunchService`, `anWatchService`,
  the `agentmeter-prep://` links and the install hint go. `wakeCloud` (the Hugging Face nudge) goes.
- The four rerun buttons become `#/new` links named "New benchmark", with the note "To compare like
  for like, upload the same file and pick the same number of flows."
- Schedules: the Overview "Repeat on a schedule" card, the Consistency "Repeat a run automatically"
  card and the scheduled-run markers on the Consistency chart go.

### GPU server (Python)
- The worker takes the shared lock around each benchmark and honours the preparation marker.
- `agentmeter/server/schedules.py`, its routes, its tests and its OpenAPI entries are removed. Old
  `schedules.json` files are ignored.
- `/api/prepared/import` stays (the hand-off from the preparation service uses it). Routes used
  only by the reuse tab are removed if nothing else calls them.

### Removed files and docs
- `scripts/deploy_cloudprep.sh`, `scripts/deploy_cloudprep.ps1`, `scripts/deploy_cloudprep_hf.py`.
- `docs/DEPLOY.md`: the Cloud Run (A7) and Hugging Face sections. A short section says the
  preparation service runs on the GPU machine and needs `CLOUD_PREP_SECRET` in the Vast template.
- `prep_local.py`: the Windows `install`, `uninstall`, `start`, `stop` commands and the protocol
  handler. Running it in the foreground (what the GPU machine does) stays.
- `prep.ts`: the `CLOUD_PREP_URL` fallback. Only the registered service is used.

### Tests
- The lock: a benchmark and a preparation never overlap, a waiting preparation goes before the next
  queued benchmark, a running benchmark is not interrupted.
- The waiting list: waiting, preparing, done, cancel, re-queue after the service disappears, one
  benchmark per upload, the 7-day expiry, Storage full.
- Remove `tests/test_schedules.py` and schedule steps in e2e flows. Update e2e flows that used the
  reuse tab, the data-set list or the laptop launcher. The upload step has exactly two tabs.

## Left to the builder
- How the worker waits on the marker (poll interval, at most a few seconds).
- What "Running now" shows when the running benchmark belongs to another user (default: "A
  benchmark of N models, started M minutes ago", no owner or models named).
- Whether the browser (Pyodide) preparation path, used only without a control plane, stays.
  Default: leave it untouched.

## Calls made while building (for the owner to check)
- **The service pulls, the control plane does not push.** A signed `POST /api/prep/claim` hands out
  the oldest waiting job with fresh single-use URLs only once the service holds the machine, so a
  long wait never outlives a URL. The control plane nudges the service (`POST /nudge`) when a job
  starts waiting; the service also looks every minute.
- **Order.** By the time a job joined the list (milliseconds), so a job a dead machine dropped keeps
  its place.
- **Retries.** A job whose machine goes away (new address, stopping, or 15 minutes with no news) goes
  back on the list; after the third try it fails.
- **Limits.** At most 3 unfinished uploads per user; waiting files use at most 800 MB of Storage.
- **One benchmark per upload.** Marked when the benchmark is authorised. If that authorisation's
  token expired without a session (the click failed), one retry is allowed after 5 minutes.
- **Data sets page.** It lived inside the reuse tab, so it is gone too (rename, delete and the flow
  mix of old sets). Sessions keep everything they measured.
- **Single-server mode** (no control plane, local use): the GPU server prepares uploads as a job in
  its own queue, which never overlapped a benchmark either; the browser path stays for tests.

