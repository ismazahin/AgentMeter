# A "Start" button for the preparation service — design

Date: 2026-11-10 · Status: for approval · Replaces the autostart half of
`2026-11-10-one-data-step-and-autostart-design.md`; `2026-11-10-browser-analysis-design.md` is not
pursued now (kept for later). The "one data step" sidebar still stands.

## Intent and why

The owner runs AgentMeter on one PC only. Instead of typing `python scripts/prep_local.py` in a
terminal, Analyse traffic offers a **Start** button; the browser asks "Open AgentMeter prep?"
(with "Always allow"), and the service starts in the background on that PC.

## Goals

1. One-time `python scripts/prep_local.py install` (no admin rights) registers the link types
   `agentmeter-prep://start` and `agentmeter-prep://stop` for the Windows user;
   `uninstall` removes them; `status` says whether it runs and where.
2. Analyse traffic, when the service is not running, shows **Start the preparation service**
   (opens `agentmeter-prep://start`), then waits until it is online (up to 2 minutes) and enables
   the upload; while running, a small **Stop** link.
3. The service starts unattended: no console window, no prompt, a log in `results/prep_local.log`,
   one copy at a time (a second Start does nothing), and it restarts its tunnel if it drops.
4. It tells the control plane its address itself (signed, like the GPU server); the page knows
   truthfully whether it runs.

## Non-goals
- Starting it from another computer or a phone (by design it runs on this PC only).
- Autostart at log-in; the browser analysis.

## Anti-goals
- A link any website could use to do something harmful: the link only starts or stops this
  service (no arguments are taken from it), so the worst another site can do is start or stop it.
- Any prompt or console window when started from the button.

## Design

### Control plane (as in the autostart design)
- `POST /api/prep/register` and `POST /api/prep/heartbeat`, HMAC-signed with `CLOUD_PREP_SECRET`
  (`verifySigned`), body `{url, version}`, `https://` only (http with `ALLOW_HTTP_BACKEND` for tests);
  stored in `backends` as `id='prep'` (no migration).
- `cloudPrepUrl(env)`: the registered URL when its heartbeat is < 180 s old, else `CLOUD_PREP_URL`;
  every use of the URL and of `cloudPrepConfigured` goes through it; `/api/backend.cloud_prep` too.
- `POST /api/prep/register` also accepts `{stopping: true}` to go offline at once on Stop.

### `scripts/prep_local.py`
- Default (terminal) run as today, but registers itself instead of `supabase secrets set`
  (`--set-secret` keeps the CLI step for the first set-up of the secret).
- `--background`: no output, rotating log, a pid file (`results/prep_local.pid`): a second start
  exits when the first is alive; heartbeat every 60 s; restart the tunnel / service if they exit.
- `start` / `stop` (what the links run); `install` / `uninstall` / `status`.
- `install` writes `HKCU\Software\Classes\agentmeter-prep` (URL Protocol) with the command
  `"<pythonw>" "<repo>\scripts\prep_local.py" link "%1"`; `link` accepts only `…://start` or
  `…://stop` and ignores anything else. Non-Windows: prints that the button is Windows-only.
- The secret: `.cloudprep_secret` as today; the control plane must have the same one, set once
  with `python scripts/prep_local.py --set-secret` (or the previous run already did).

### Web (`web/js/analyse.js`)
- Not running → the notice with the **Start** button and "First time? Run
  `python scripts/prep_local.py install` once on this PC." After a click: "Starting…" and a poll
  of `/api/backend` every 3 s; online → upload enabled; 2 minutes without → "It did not start:
  see results/prep_local.log, or run `python scripts/prep_local.py` in a terminal."
- Running → "Preparation service running · Stop".

## Left to the builder
- Heartbeat/online timings (60 s / 180 s); log rotation; how `stop` reaches the running copy
  (pid file + terminate).
- Tests: vitest (register/heartbeat/online/stopping/fallback/dispatch target); pytest (signing,
  link parsing, single instance, install/uninstall writing the registry through an injectable
  writer, background start/stop with fake processes); e2e (button shown when offline; online after
  a registered heartbeat).
