# One data step for everything, and a preparation service that starts by itself — design

Date: 2026-11-10 · Status: approved 2026-11-10 · Builds on `2026-11-09-gpu-free-analyse-traffic-design.md`

## Intent and why

Today there are two ways to put data into AgentMeter that do the same first step in two places:
**New benchmark** (step 1 Data, disabled when the GPU is off; the prepared set lives on the GPU
server's disk and is lost with the rental) and **Analyse traffic** (the same preparation on the
owner's computer, kept in Supabase, plus the IDS scores). The owner wants **one** way, so that
preparing data works whether the GPU is busy, off or free, and is then reusable for benchmarks.

For that, the preparation service on the owner's PC (`scripts/prep_local.py`) must be up whenever
the PC is on, without anyone remembering to start it, and without the interactive
`supabase secrets set` prompt it needs today.

## Goals

1. **One entry for data:** Analyse traffic is the only upload. It prepares, scores with the IDS,
   and lists the data sets (the Data sets list moves into it). A benchmark always starts from a
   prepared set: "Benchmark these alerts" / "Benchmark this set" / "Run again" → models → run.
2. **New benchmark leaves the sidebar;** `#/new` with no set redirects to Analyse traffic; the
   models and run steps (`#/new/models?set=…`, `#/new/run/…`) stay as they are.
3. **The service starts when the owner logs in to Windows,** runs in the background, writes a
   log, restarts its tunnel if it drops, and needs no click and no prompt.
4. **The service registers itself** with the control plane (its tunnel address), signed with the
   secret it already has, and keeps a heartbeat; the site knows whether it is up.

## Non-goals

- A button on the website that starts the service (browsers cannot start programs on a PC).
- Preparing in the browser as a fallback (decided: the service is required, so every set has IDS
  results). The browser preparation code stays in the repo but is no longer offered.
- Running the service as a Windows service (admin rights) or on another OS's init system.

## Anti-goals

- Anything that asks the owner a question at start-up (it must start unattended).
- A data set that can be benchmarked but was never scored when an IDS model was stored.
- Sets prepared earlier on the GPU server becoming unusable: they stay listed and benchmarkable.

## Decided

| Question | Decision |
|---|---|
| Combine New benchmark and Analyse traffic | Yes: one data step (Analyse traffic), benchmark from a set |
| Service not running | The upload is refused with how to start it; no browser fallback (a) |
| How the service starts | Automatically at Windows log-in (2) |

## Approaches considered

- **Starting at log-in:** (1) a desktop shortcut to double-click — visible but must be remembered;
  (2) automatic at log-in — chosen. Within (2), *designer's call:* a shortcut in the user's
  Startup folder running `pythonw` (no window, **no admin rights**), rather than Task Scheduler
  (`schtasks /sc onlogon` needs admin rights) or a Windows service (admin, and harder to undo).
- **Telling the control plane where the service is:** (a) `supabase secrets set` on every start
  (today: needs the CLI, logged in, and answers an interactive prompt); (b) **self-registration
  (chosen):** like the GPU server (`backend.ts: register/heartbeat`), signed with
  `CLOUD_PREP_SECRET`; (c) a named Cloudflare tunnel with a fixed address — needs a domain on
  Cloudflare; still supported (`--tunnel-token/--hostname`), and it registers the same way.

## Design

### Self-registration (control plane)
- `POST /api/prep/register` and `POST /api/prep/heartbeat`, HMAC-signed with
  `CLOUD_PREP_SECRET` (`verifySigned`, as the service's job reports). Body: `{url, version}`;
  only `https://` URLs (plus `http://127.0.0.1` when `ALLOW_HTTP_BACKEND` is set, for tests).
- Stored in the existing `backends` table as the row `id='prep'` (no migration). The service
  is **online** when its last heartbeat is within 3 minutes (designer's call).
- `CLOUD_PREP_URL` (env) becomes the fallback: the registered, online address wins.
  `cloudPrepConfigured` = a secret and (an online registered URL or `CLOUD_PREP_URL`).
- `/api/backend` reports `cloud_prep` from that, so Analyse traffic shows "not running" truthfully.
- `CLOUD_PREP_SECRET` is set once (it already is); `prep_local.py` no longer runs the CLI.

### `scripts/prep_local.py`
- On start: the service, the tunnel, then register; a heartbeat every 60 s; if cloudflared
  exits or the public address stops answering, restart the tunnel and register the new address;
  if the service exits, restart it. Ctrl+C / stop ends both.
- `--background`: no console output, log to `results/prep_local.log` (rotated at 5 MB).
- `install` / `uninstall` / `status` sub-commands: `install` writes
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AgentMeter prep.cmd` running
  `pythonw scripts\prep_local.py --background` from the repo folder, and starts it now;
  `uninstall` removes it and stops a running copy; `status` says whether it runs and its address.
- The secret file and the `--tunnel-token/--hostname` options stay.

### Web app
- Sidebar: Home · Analyse traffic · Sessions · Leaderboard (tabs Ranking · Compare ·
  Repeatability) · Cost planner; Account; Help; GPU status chip → queue panel (as already shown).
- Analyse traffic gains the Data sets list (with its Details / Benchmark this set / Rename /
  Delete). `#/datasets` → `#/analyse`; `#/trends` → Leaderboard › Repeatability; `#/gpu` → the
  queue panel; `#/new` without a set → `#/analyse`.
- When the service is not running: "Start your computer's preparation service (it starts by
  itself after `python scripts/prep_local.py install`)".

## Left to the builder
- Heartbeat interval and the online window (60 s / 3 min suggested); log rotation details.
- Where the queue panel opens (popover or side sheet) and its contents (today's GPU & Queue page).
- Tests: vitest for register/heartbeat/online; pytest for prep_local (registration, restart logic
  with a fake cloudflared, install/uninstall writing the Startup file in a temp folder); e2e
  updates for the redirects and the Data sets list inside Analyse traffic.
