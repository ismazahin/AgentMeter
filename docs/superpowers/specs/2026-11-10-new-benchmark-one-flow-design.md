# New Benchmark: one flow from upload to run — design

Date: 2026-11-10 · Status: approved 2026-11-10 · Supersedes the "one data step" parts of
`2026-11-10-one-data-step-and-autostart-design.md` (Analyse traffic no longer a separate menu).
Builds on `2026-11-09-gpu-free-analyse-traffic-design.md` (the IDS at preparation) and
`2026-11-10-start-button-design.md` (the preparation service and its Start button).

## Intent
One sidebar menu, **New Benchmark**, for the whole journey (the owner's words):
1. user uploads a data set, 2. the system prepares it, 3. the prepared data and its details are
shown, 4. the user chooses models and other settings, 5. the user runs the benchmark (GPU busy:
queued). Preparation never needs the GPU; running does.

## Steps (`#/new…`)
| Step | Route | Content |
|---|---|---|
| ① Upload | `#/new` | file or link, flows for the benchmark sample, "Group other attacks as Other Attack"; tab *Reuse a prepared set* = your earlier analyses + the Data sets list (Details / Benchmark this set / Rename / Delete). Service not running → the Start button (Start-button design). |
| ② Preparing | `#/new/prep/<job>` | live progress of the preparation job (the service on the owner's PC) |
| ③ Your data | `#/new/prep/<job>` (done) | what is in it (file, rows, flows kept, labels, class mix) + the IDS analysis (threshold slider, alerts, recall / precision / false-positive rate, per class, projected LLM cost). **Continue** → ④ |
| ④ Models & settings | `#/new/models?set=…&ids=τ` | today's models page; IDS gate at the chosen threshold |
| ⑤ Run | `#/new/run/<job>` | today's run page |

GPU states: **off** → Continue (③) and Run (④) disabled with "To run a benchmark, the GPU server
must be running first."; **busy** → Run queues ("N jobs ahead") and the run page says "You'll get
a Telegram message when it finishes" (or links to Settings when no Telegram chat is set);
**free** → starts at once.

## Decided (owner)
One menu; steps as above; GPU off = disabled with that message; busy = queue + Telegram.

## Designer's calls
- Preparation always goes through the preparation service (every set gets IDS results and is kept
  in Supabase); preparing on the GPU server or in the browser is no longer offered.
- The Data sets list lives in ①'s *Reuse a prepared set* tab; the Data sets menu goes.
- Sidebar: Home · New Benchmark · Sessions · Leaderboard (tabs Ranking · Compare · Repeatability)
  · Cost planner; Account; Help. The GPU chip opens the GPU & Queue page (no longer in the
  sidebar). Redirects: `#/analyse` → `#/new`, `#/analyse/<job>` → `#/new/prep/<job>`,
  `#/datasets` → `#/new?src=reuse`, `#/trends` → Leaderboard › Repeatability.
- No queue when the GPU is off (owner: disabled instead).

## Non-goals
A waiting list in Supabase for when the GPU is off; inline model choice on step ③.

## Tests
e2e: the analyse walk-through rewritten for the five steps (GPU off: ①–③ work, Continue disabled
with the message; GPU on: ④–⑤ with stored scores); the Start-button flow on ①; redirects;
the queued message.
