# AgentMeter Dashboard (Phase 10 Part A)

A single self-contained results dashboard. It **reads a precomputed
`analysis.json`** (produced by `agentmeter/analyze.py`) and displays it. It is a
pure **view layer** — it runs no models, touches no pipeline / instrumentation /
detection code, and does no measurement of its own.

## What it shows

- **SAW ranking table** — rank, model, accuracy, latency, total device VRAM,
  tokens, composite, and a colour-coded tier badge. Re-ranked **live** from the
  weight sliders below it.
- **VRAM finding** — the `notes.vram_finding` line, shown verbatim.
- **Per-agent diagnostics** — mean wall time and marginal working memory
  (`vram_delta`, explicitly *not* total device VRAM) per agent, with a model
  selector.
- **Live sensitivity** — four weight sliders (accuracy / latency / vram /
  tokens) renormalised to sum 1.0, recomputing the composite and re-ranking the
  table in-browser. Preset buttons cover `default` plus each configured
  `sensitivity.weight_sets` entry.
- **Source banner** — states whether you're viewing the bundled **SAMPLE** or a
  **user-loaded** file, so illustrative numbers are never mistaken for real ones.

All scoring/re-ranking is delegated to [`saw.js`](./saw.js), which mirrors
`analyze._composite` / `_tier` exactly (guaranteed by `tests/test_saw_parity.py`).

### Tabs (presentation view)

A simple top nav switches between three views (everything still reads from the
one loaded `analysis.json`; no server or GPU needed to view precomputed results):

- **Overview** — headline **Highlights** cards (Composite Health Score, Accuracy,
  Latency per scenario as labelled bar charts, best model emphasised), the SAW
  ranking table, the live weight sliders, the VRAM finding, and the "Add a model"
  admin panel.
- **Detailed Analysis** — the study content for a supervisor: per-agent
  diagnostics (with the *reason dominates latency* / *decide dominates marginal
  working memory* findings highlighted per row), the sensitivity table (rank
  under each weight set, with rank-stability of the #1 model called out), the
  Kruskal-Wallis + Dunn statistics for latency and peak VRAM (read-only),
  accuracy detail (per-model, plus per-class and confusion matrices when present
  in the JSON), and a **Resource cost per attack class** panel (from the
  `per_class` section) — mean/median latency, VRAM and tokens per attack class,
  by model and metric, framing *where resource cost concentrates by class* (a
  measurement view, not a per-class detection ranking).
- **Sessions** — data management (CRUD) served by the app, stored in a **separate
  metadata DB** (`results/agentmeter_app.db`), never the locked study DB. Import an
  `analysis.json` as a named **session** (its summary is extracted once and kept
  read-only — the study numbers are never edited), open a session to render it
  across the other tabs, rename or delete it, and attach **notes**. The
  **Saved weight presets** panel (under the Overview sensitivity sliders) saves the
  live weights as a named preset and loads them back; the four builtins
  (default/equal/accuracy_heavy/efficiency_heavy) are load-only. Requires the
  server (same-origin); over `file://` it is read-only.
- **Settings** — **status only**. It shows each token as *set* / *not set* and
  the cost-safety modes (auto-destroy, idle-timeout) read from the server's
  `GET /settings-status`. It has **no input fields for any token** — secrets live
  only in the server-side `.env` and never pass through the browser. Over
  `file://` (no server) it shows "status unavailable".

When you open the dashboard **from the pull/eval server**, dropping the canonical
`results/analysis/analysis.json` at `dashboard/analysis.json` makes it auto-load
(the server serves that file); otherwise use **Load analysis.json**.

## Run it — simplest (no server)

Open the file directly in a browser:

```bash
# macOS
open dashboard/index.html
# Linux
xdg-open dashboard/index.html
# Windows
start dashboard/index.html
```

Or just double-click `dashboard/index.html`. It works fully offline over
`file://` — SAW table, sliders/presets, and per-agent chart all function.

It opens on the **bundled SAMPLE** (amber banner). To view your real results,
click **“Load analysis.json”** and pick your file, e.g.
`results/analysis/analysis.json` from an `analyze` run. The banner turns green to
confirm real data.

## Run it — local server (optional)

Only needed if you want the page to **auto-load** an `analysis.json` sitting next
to it. Browsers block the auto-`fetch` under `file://` (the harmless console
message you may see) — the **file picker always works** regardless. Over HTTP the
auto-load works:

```bash
cp results/analysis/analysis.json dashboard/analysis.json
cd dashboard
python -m http.server 8000
# then open http://localhost:8000/
```

## The sample data

`sample_analysis.json` lets the dashboard render immediately. Its
accuracy / composite / rank / tier and the “VRAM normalises to 1.0” finding are
the **locked real L4 results**. The normalised latency/token values are *solved*
so that under the default weights the composites reproduce those locked values
exactly — but their raw **magnitudes and all per-agent numbers are ILLUSTRATIVE
placeholders** (see the `notes.SAMPLE` field) until you load a real
`analysis.json`. Regenerate it with:

```bash
python scripts/gen_sample_analysis.py
```

## Add a model (admin, Phase 11)

The **“Add a model (admin)”** panel pulls **one extra** Hugging Face model and
runs it through the **same harness** as the locked 5-model study, for a
side-by-side comparison — **without touching that study**. The run happens on a
**Vast.ai GPU instance** via [`scripts/pull_eval_server.py`](../scripts/pull_eval_server.py),
which **serves this dashboard and the API from one origin** (so the API calls are
same-origin — no CORS, no tunnel needed):

```bash
pip install -r requirements.txt -r requirements-gpu.txt   # flask + flask-cors
export HF_TOKEN=...                                        # for gated models
python scripts/pull_eval_server.py                         # binds 0.0.0.0:8000
```

It prints the exact URL to open (`http://<instance-ip>:<mapped-port>/`, using the
instance's Vast.ai port mapping). Open **that** URL, leave the **Server base URL**
field **empty** (same-origin), enter an `org/Model-Name`, and click **Pull &
evaluate**. A progress bar tracks *validating → pulling → running scenario k/300 →
analyzing → done*. On completion the pulled model appears in the SAW table as a
**striped, `exploratory`-badged row**, ranked with the same weights but clearly
**not part of the validated set**.

Viewing this file over `file://` instead? Set the **Server base URL** to the
instance address (e.g. `http://203.0.113.7:8000`) — the server enables permissive
CORS as a fallback so that still works. The base URL is configurable and never
hard-coded (see [`pull-config.js`](./pull-config.js); the value is remembered in
your browser). An optional `--ngrok` flag opens a public tunnel if the mapped port
is not directly reachable.

### Tokens (.env) — fill once, never commit

Fill tokens **once** in a local `.env` instead of pasting them each run:

```bash
cp .env.example .env    # then edit .env and fill the slots you need
```

The server loads `.env` on startup (via `python-dotenv`, with a builtin parser
fallback) into the environment; existing environment values always win. **`.env`
(and `.env.local`) are gitignored — only the empty `.env.example` template is
committed.** No token value is ever hard-coded or written to logs (the startup
banner shows each token as `set` / `not set`, never its value).

| Var | Needed for | Required? |
| --- | --- | --- |
| `HF_TOKEN` | pulling **gated/private** HF models (Llama, Gemma…) | Only for gated/private models — **public models, including the canonical 5, need none** |
| `VAST_API_KEY` | the server's self-destroy (`--auto-destroy` / idle) | Only if you use auto-destroy |
| `VAST_INSTANCE_ID` | identify this instance to destroy | Optional — falls back to Vast's own env vars |
| `GITHUB_TOKEN` | `git clone` a **private** repo on the box | Only for a manual private clone (not auto-wired) |

If `HF_TOKEN` is missing when you pull a gated model, the server logs a clear
`set HF_TOKEN` message and keeps running — it never crashes or prints the token.

### Cost safety — destroy the instance, don't leave it billing

A rented Vast.ai GPU bills for every minute it is **alive**. **Destroy ≠
stop/pause** — a paused instance *still bills for storage*, so we **destroy**.
Three layers of protection, outermost first:

1. **Scheduled end / max-duration on the rental (ALWAYS set this).** When you rent
   the instance on Vast.ai, set a scheduled end / maximum duration. This is the
   **outer safety net** that kills the box even if every line of code below fails.
   **Do not skip it** — it is the only layer that survives a wedged process.
2. **`--idle-timeout MINUTES` self-destroy** (default 30). A background watchdog
   destroys the instance after that many minutes with **no activity and no running
   job** — catching a forgotten server, a demo that never happened, or a wedged
   process. It **never** fires while a run is in progress. `0` disables it.
3. **`--auto-destroy` after a completed run.** Once a run's results are written to
   the pull DB **and** the merged analysis JSON is flushed to disk, the instance
   destroys itself as the **very last** step (logged `results persisted; destroying
   instance`). Clean automatic shutdown after a demo.

Both self-destroy layers are gated on `--auto-destroy` (**default OFF** — while
developing/testing, nothing self-destroys). They **degrade safely**: with
`VAST_API_KEY` or the instance id missing, the server logs a warning and keeps
running rather than crashing (destroy becomes a no-op — rely on layer 1).

**Rent safely (demo):**

```bash
# On Vast.ai: set a scheduled end / max-duration when you create the instance. (layer 1)
export VAST_API_KEY=...          # your Vast.ai API key (never hard-coded)
export VAST_INSTANCE_ID=...      # or pass --instance-id; else read from Vast env vars
export HF_TOKEN=...
python scripts/pull_eval_server.py --auto-destroy --idle-timeout 30   # layers 2 + 3
```

While **developing**, run with neither flag (`python scripts/pull_eval_server.py`)
so the box never self-destroys — and still set layer 1 on the rental.

Integrity guarantees (enforced in [`agentmeter/pull_eval.py`](../agentmeter/pull_eval.py),
verified by `tests/test_pull_eval.py`):

- **Size guard** — a model larger than **8B params** (or not a causal-LM text
  model) is rejected from HF metadata **before any download**.
- **Separate DB** — each pull writes `results/pulls/agentmeter_pull_<slug>.db`;
  the locked study DB is **never** written.
- **Canonical read-only** — the validated results, weights, tiers and
  **statistics** (Kruskal-Wallis + Dunn) are copied read-only; the pulled model
  is appended as `exploratory:true` / `validated:false` and **excluded** from the
  study's statistics.
- **Same harness** — reuses `runner.run_full` (subprocess-per-model VRAM
  isolation, sequential, 4-bit NF4, checkpoint/resume) — nothing is
  reimplemented.
- **Single-job lock** — only one pull/eval runs at a time.

## Files

| File | Purpose |
| --- | --- |
| `index.html` | The dashboard (self-contained; open via `file://`). |
| `saw.js` | SAW math module — mirrors `analyze._composite` / `_tier`; used by the live slider. |
| `pull-config.js` | Admin pull/eval endpoint config (ngrok base URL placeholder — never hard-coded). |
| `sample_analysis.json` | Bundled illustrative sample conforming to the `analysis.json` schema. |
