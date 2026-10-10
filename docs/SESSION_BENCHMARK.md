# Benchmark sessions: user input → 4-agent pipeline → comparison

A **session** takes a prepared user run (from `scripts/ingest.py`, see
[INPUT_LAYER.md](INPUT_LAYER.md)) and runs its selected flows through the
**existing** instrumented 4-agent pipeline for **1 or 2 models**. It then scores
and compares them. It measures LLM resource efficiency, plus accuracy where labels
exist. It is not a detection product.

```
results/csv_runs/<name>/  (or pcap_runs/<name>/)
  input.json, selected_flows.csv, labels.csv        ← from the input layer
  session_scenarios.csv   flow_id (hidden) + model-visible features + held-out label
  session_config.yaml     base config + per-session overrides (models, classes, DB path)
  session.db              this session's runs / scenario_results / agent_metrics
  session_results.json    per-model metrics, SAW, accuracy, the comparison, caveats
```

Nothing is written to the locked study DB (`results/agentmeter_full_l4.db`). A
session refuses to start if its DB path resolves to a validated study DB, and its
provenance is always `non_validated`.

## What is reused (not re-implemented)

| Step | Existing code |
|---|---|
| Sequential execution, one subprocess per model, resume, GPU guard | `run/runner.run_full` → `run/worker` (`--mode sqlite`) |
| Loading flows with label isolation | `data/dataset.DatasetLoader` (label and `flow_id` are hidden columns) |
| The 4 agents and the instrumentation | `Pipeline`, `AGENT_REGISTRY`, `MetricsCollector`, `make_instrumented_hook`, `GpuProbe` |
| The model | `providers.get_provider` (mock, or hf with 4-bit NF4 from the config) |
| Accuracy and confusion | `analyze.phase7(sr, classes)` |
| SAW | `analyze._criteria` → `phase8` (`_normalise`, `_composite`, `_tier`), `sensitivity` |
| Per-agent diagnostics, statistics, provenance | `analyze.per_agent`, `analyze.statistics`, `analyze._provenance` |

**6-class runs.** The Decide agent and `normalize_class` read `classes` from the
config. A 6-class run's session config therefore lists the 5 study classes plus
`"Other Attack"`, so Decide offers it and the parser accepts it. Accuracy and
confusion are computed over those 6 classes. A 5-class run's config uses the base
config's classes unchanged, so its prompts and parsing are exactly as before.

**Scoring.**
- **Full SAW:** uses the config weights. Labelled runs only.
- **Efficiency-only SAW:** always computed, using the same targets, tiers and maths
  with accuracy weighted 0 and the other weights renormalised. It decides "which
  model is more resource-efficient", and it is the headline score for unlabelled
  (PCAP) runs.
- **Ties:** metrics within 1%, and composites within 0.5 points, are reported as
  ties.

**Relative (head-to-head) comparison.** This is added beside the absolute SAW,
which is unchanged. The absolute SAW scores each model against fixed targets from
the L4 study, so two models that both beat a target both score the maximum on it.
They can then tie even when one is clearly better. `relative_comparison` ranks the
two models directly against each other, using the same measured metrics:

| Metric | Better | Reported as | Tie when |
|---|---|---|---|
| Mean end-to-end latency per flow | lower | % lower than the other model, and a ratio | within 2%, **or** not significant (per-flow Kruskal-Wallis p ≥ 0.05) |
| Mean peak working VRAM (excludes model weights) | lower | % less, and a ratio | within 2%, or not significant; "not available" on CPU/mock |
| Mean tokens per flow | lower | % fewer, and a ratio | within 2% |
| Accuracy (labelled runs only) | higher | percentage-point difference | within 2 percentage points |

The significance check reuses the session's existing statistics. A gap that is only
timing noise is reported as a tie together with its p-value, so it never produces a
false winner.

- **Efficiency verdict (dominance rule, no weights):**
  - a model that wins at least one efficiency metric and loses none is "more
    resource-efficient";
  - if each model wins at least one, the verdict is "mixed" and the trade-off is
    written out;
  - otherwise it is a tie.
- **Accuracy** is reported beside the efficiency verdict, never folded into it. A
  "faster but less accurate" trade-off is stated explicitly.
- **Example verdict:** *"A is more resource-efficient than B: 50.0% lower latency,
  20.0% less VRAM, 10.0% fewer tokens per flow. Accuracy: A is higher by 10.0
  percentage points (A 80.0% vs B 70.0%)."*
- **Caveats:** the relative view adds two notes to the session caveats.
  - Both models ran on the same hardware and the same flows, so the comparison is
    like-for-like.
  - On non-L4 hardware such as an A100, absolute numbers aren't comparable with the
    locked study. Only the A-vs-B relation is.

`session_results.json` keeps the dashboard's `analysis.json` keys (`phase7` for
labelled runs only, `phase8`, `sensitivity`, `per_agent`, `statistics`,
`provenance`). It adds `session`, `per_model`, `comparison` (absolute) and
`relative_comparison` (head-to-head, `null` unless there are 2 models), plus caveats.

## CPU smoke test (mock provider)

```
python scripts/ingest.py data/sample_csv/cicids2017_sample.csv --max-flows 15
python scripts/benchmark.py results/csv_runs/cicids2017_sample --models mock-a mock-b --provider mock
```

## Running real models on Google Colab (A100)

1. **Runtime.** Choose Runtime → Change runtime type → **A100 GPU**.
2. **Install.**
   ```
   !git clone -b claude/cool-ride-mitmzl https://github.com/ismazahin/agentmeter.git
   %cd agentmeter
   !pip install -q -r requirements.txt -r requirements-gpu.txt
   # PCAP uploads only:
   !pip install -q -r requirements-pcap.txt && pip install -q --no-deps cicflowmeter==0.2.0
   ```
3. **Hugging Face token.** Only gated models (Llama 3, Gemma 2) need one. Accept each
   model's licence on its Hub page first, then:
   ```python
   import os
   from google.colab import userdata
   os.environ["HF_TOKEN"] = userdata.get("HF_TOKEN")   # Colab Secrets
   ```
4. **Prepare the input.** Upload the CSV or PCAP, or mount Drive, then:
   ```
   !python scripts/ingest.py /content/Tuesday-WorkingHours.pcap_ISCX.csv --max-flows 100 --other-attack
   ```
5. **Benchmark two models.** Execution is sequential, one model fully finishes before
   the next loads, and each runs in its own process:
   ```
   !python scripts/benchmark.py results/csv_runs/Tuesday-WorkingHours.pcap_ISCX \
       --models mistralai/Mistral-7B-Instruct-v0.3 Qwen/Qwen2.5-7B-Instruct \
       --base-config configs/run_full_l4.yaml
   ```
   `configs/run_full_l4.yaml` is read only, never modified. It supplies the
   provider settings:
   - `provider: hf`
   - **4-bit NF4** (`load_in_4bit`, `bnb_4bit_quant_type: nf4`, double quant)
   - `require_gpu: true`, so there is no silent CPU fallback
   - the 4 agents with their token budgets
   - `cleanup_model_cache_after: true`, which deletes each model's download after
     its worker finishes so Colab's disk doesn't fill up

   The session copies that file and overrides only the models, dataset, classes and
   DB path. Models can be any of the 5 study models, which are marked `canonical`,
   or any other Hugging Face model id, such as a pulled model.
6. **Timing.**
   - The L4 study averaged about 20 s per flow per model: roughly 9 hours for 300
     flows × 5 models.
   - An A100 is usually faster, but measure first with `--max-flows 20`.
   - A session costs about (flows × 2 models × seconds per flow).
7. **Disconnects.**
   - If a session was interrupted, re-running the same `benchmark.py` command resumes
     it: completed flows are skipped and nothing is duplicated.
   - Re-running a session that already finished measures again as a new run in the
     same `session.db`, and `session_results.json` then reflects the latest run.
   - The existing runner refuses to resume on a different GPU type, so a session
     never mixes readings from two GPUs. `--fresh` restarts it.
   - Copy `results/csv_runs/<name>/` to Drive to keep it.

**Caveats** (each one is also written into `session_results.json`):
- **Non-validated:** sessions are exploratory and never merged into the locked study.
- **Hardware:** A100 numbers are not comparable with the L4 study's numbers.
- **Balanced sample:** CSV runs use class-balanced selection, so accuracy is a
  per-class view, not real-world prevalence.
- **PCAP features** are approximate.
- **CPU and mock runs** have no VRAM readings, so the VRAM criterion can't separate
  the models there.

## Running sessions as background jobs (web service)

`agentmeter/server/jobs.py` wraps the same `run_session` as a persistent
background **job**, so a run outlives the browser tab and the server process. The
way a benchmark is computed does not change.

| Endpoint | Purpose |
|---|---|
| `POST /api/jobs` | Create a job from `{"run": "csv_runs/<name>", "models": [..≤2], "provider": "mock"/"hf", "base_config": "run_full_l4.yaml"}`. Returns **202** with `job_id` immediately |
| `GET /api/jobs/<id>` | Status, live progress (`completed` / `total` flows, per model, current model), `result_ready`, `result_url` |
| `GET /api/jobs/<id>/result` | `session_results.json` once the job is `done`. Before that it returns **409** `not_ready` |
| `GET /api/jobs` | Recent jobs, newest first, plus the job currently running |
| `POST /api/jobs/<id>/resume` | Re-queue an `interrupted` or `failed` job |
| `POST /api/jobs` with `{"after_prepare": "<prepare job id>", "models": [...]}` | Phase 46: queue the benchmark **behind** a data-preparation job (the wizard's "Run when data is ready"); the single worker runs the preparation first and the benchmark then takes its prepared set. A failed preparation fails the benchmark with `prepare_failed` |
| `GET /api/jobs/<id>/constraints?max_mean_latency_s=…` | Decision helper: each measured model against your limits (`configs/constraint_rules.yaml`); the same query string on `/report.pdf` puts the limits in the PDF |
| `GET /api/sessions` (`?status=Done\|Demo\|Running\|Failed&input=CSV\|PCAP`) | Phase 46: benchmark sessions for Home / Sessions (needs a read token with a control plane; the app reads the Worker copy) |
| `GET /api/sessions/<id>` | One session's summary and identity (prepared-set hash, GPU, settings fingerprint) |
| `GET /api/compare?a=<id>&b=<id>` | Side-by-side efficiency metrics plus the like-for-like check |
| `GET /api/leaderboard?sort=latency\|vram\|tokens\|cost\|energy` | Models ranked within (prepared-set hash, GPU) groups (needs a read token with a control plane; the app reads the Worker copy) |

- **Status:** `queued → running → done | failed | interrupted`.
- **One job at a time (single-job lock):** a single worker thread runs jobs in order,
  for GPU and VRAM safety. A job created while another is running stays `queued`,
  and its `queue_position` shows how many jobs are ahead of it.
- **Persistence:** each job is stored as `results/jobs/<job_id>.json`, written
  atomically. When the server restarts, it re-reads the store:
  - a job that was `running` when the process died becomes **`interrupted`**;
  - `queued` jobs start again in their original order.
- **Resume:** `run_full` continues the incomplete run in that session's
  `session.db`. Completed flows are skipped and no rows are duplicated.
- **Progress** is read live from the run's own `session.db`, so it stays correct
  after a restart.
- **Errors** return `{"error", "code"}`, with codes `too_many_models`, `bad_models`,
  `run_missing` (404), `no_gpu` (503), `not_ready` (409), `not_resumable` (409),
  `not_found` (404) and `bad_request`.
- **No GPU:** a job that needs one (hf provider) is rejected when it is created.
  It never falls back to the CPU.
- **Path safety:** runs are named, never given as filesystem paths, and base configs
  must be files in `configs/`.
- **Locked study:** jobs only ever write to the prepared run's own `session.db`
  (non_validated), never the locked study.

## The app (`/`)

`python scripts/serve.py`, then open `http://<host>:8000/`. Phase 46 makes the service and
the old analysis dashboard **one app with one header**. The header holds Home, New
benchmark, Sessions, Compare and Leaderboard, plus a GPU status chip ("Real GPU: <name>"
or "Demo mode (no GPU)") and Settings. The same static files run on Vercel / Pages
(`web/`, see DEPLOY.md). `/service` is an alias, and the old step URLs redirect to the
wizard.

- **Home:** the latest finished session as head-to-head bars (mean latency and peak
  VRAM) with **Open session**, a **New benchmark** call to action, and a table of recent
  sessions (input type, flows, GPU, more-efficient model, status Done / Demo / Running /
  Failed, Open / Watch). With no sessions, it shows a one-line empty state. It never
  shows the locked study's data.
- **New benchmark:** one wizard in 3 steps.
  1. **Data.** The tabs are *Upload a file*, *Import from a link* and *Reuse a prepared
     set* (pick one prepared on this server, enter its id, or re-upload the downloaded
     files), plus the flow count and the Other Attack opt-in.
  2. **Models and settings.** The data is prepared in the background while you choose.
     The data card shows its progress, then the summary (sampling, class table, rules)
     and the prepared-set downloads.
  3. **Run.** The progress bar starts with "Preparing your data" when the run was queued
     before the data was ready (`after_prepare`), then shows benchmark progress. It
     opens the session when done.

  Prepare and benchmark jobs are never shown as separate concepts.
- **Sessions:** every benchmark run, filterable by status and input type. A session
  page has chips for the GPU, flows and input type, with **Download PDF** and **Compare
  with another session**, and five tabs:
  - **Summary:** the measured verdict, the per-model table (latency, p95, tokens, peak
    VRAM, cost per 1,000 flows, energy per flow, accuracy as context), the head-to-head,
    cost and energy, and caveats.
  - **Detailed analysis:** statistical depth (p50/p95/p99 with a histogram, the warm-up
    check, Cliff's delta and bootstrap 95% CIs beside the Kruskal-Wallis p, and a
    small-sample note), plus the former dashboard's Overview and Detailed views. Those
    are embedded from `/analysis?session=<id>&embed=1`, which follows the app's theme and
    has no second header.
  - **Agents:** per agent, the latency and token shares, working VRAM (excludes model weights),
    agentic overhead (an estimate: hand-off tokens), failures (empty output, hit token cap, unparseable
    label), and a Gantt-style timeline of one flow.
  - **Recommendation:** the stage-2 verdict and the resource hint, the decision helper
    (your limits → meets / fails / no_data), and the stage-3 model context.
  - **Downloads:** the PDF, `session_results.json`, the SAW and per-agent CSVs, and the
    prepared-set files.
- **Compare:** pick 2 finished sessions to see every efficiency metric side by side. The
  validity check sits at the top: same prepared-set hash? same GPU? same settings
  (provider, quantisation, generation, agents, token caps, classes)? If anything
  differs, the page lists what differs and labels the comparison **not like-for-like**,
  with no best-value marks.
- **Leaderboard:** models ranked across sessions, but only inside a (prepared-set hash,
  GPU) group. Each row shows the sessions and flows behind it (flow-weighted means), and
  can be sorted by latency, VRAM, tokens, cost per 1,000 flows or energy per flow. A
  group whose sessions used different settings is flagged.
- **Settings:** theme System / Dark / Light, kept in this browser. Accounts come later.
- The **Validation baseline** (the locked study) is not in the navigation. It stays at
  `/baseline` by direct URL, and reproducing it is CLI-only (REPRODUCE_BASELINE.md).

**No GPU.** Without a GPU the backend runs in **demo mode** (mock provider). The chip
and a notice say so, and mock runs show cost and energy as "not measured". An operator
can force the mode with `AGENTMETER_SERVICE_PROVIDER=mock|hf`. Run the server as **one
process**, because the single-job lock lives in that process.

## Phase 46 analyses (additive)

All of these are added to `session_results.json` after scoring, by
`agentmeter/session/analyses.py`. They read the same DB rows and a copy of the scored
payload. `tests/test_scored_keys_golden.py` re-scores a committed session and checks
that every scored key is byte-identical to the pre-Phase-46 output.

| key | what | where shown |
|---|---|---|
| `agents` | per agent: latency, tokens in/out and their share of the flow, working VRAM (excludes model weights), failures (`empty_output`, `hit_token_cap`, Decide's `unparseable_label`; no timeouts or retries exist by design); agentic overhead **(an estimate)** = earlier agents' outputs re-sent in later prompts (`pipeline/agents.py` `HANDOFFS`), counted from the recorded output-token numbers, not by re-tokenising the prompts; a per-flow timeline | Agents tab, PDF |
| `latency_distribution` | p50/p95/p99, a 20-bin histogram on shared edges, and the warm-up check (median of the first 5 flows vs the rest, flagged above 1.2×) | Detailed analysis, PDF |
| `effect_sizes` | Cliff's delta with Romano magnitude, and a percentile-bootstrap 95% CI (2,000 resamples, seed 46) of mean(A) − mean(B) for latency, VRAM and tokens, beside the existing Kruskal-Wallis p; a note under 20 flows per model | Detailed analysis, PDF |
| `cost_energy` | cost per 1,000 flows = mean latency × 1,000 / 3,600 × GPU $/h, using the live Vast instance price (`VAST_API_KEY` + `VAST_INSTANCE_ID`, server-side only) or `config.yaml` `pricing.gpu_usd_per_hour`, with source and fetched_at; energy = GPU board power sampled from the parent process (NVML, or nvidia-smi; `energy.sample_interval_s`, default 0.5 s) and integrated over each model's window → Wh per flow and per 1,000 flows, reported **whole board** and **net of idle**. Idle board power is sampled for `energy.idle_sample_s` (default 5 s) right before each model's worker starts, while the GPU is idle (`run_full`'s `before_model` hook). Net = whole-board energy − idle W × window, a lower bound on the model's own share (the GPU may still be leaving a high-power state). Mock/CPU: "not measured" | Summary, Leaderboard, Compare, PDF |
| `memory` | per model: **working VRAM** (excludes model weights: the allocator peak above the memory held before each agent call, mean and highest), **model weights** (torch allocated right after load), **total peak** = weights + working = the larger of torch's peak reserved memory over all agent calls and the NVML device-used peak sampled during the run (CUDA context included). The worker records the absolute allocator peaks at the point where it already reads the per-call peak, after the call's timer has stopped, so nothing is added to a timing. They go in a `model_memory` table in the session DB. Sessions from before this change have no total peak: shown as "no_data", and the decision helper's VRAM limit gives `no_data` | Summary (GPU memory), Home bars, Compare, Leaderboard, PDF |
| `decision_helper` | stage-2 fit scoring with `configs/constraint_rules.yaml` (RULE_BASE.md) | Recommendation tab, PDF |
| `session_identity` | prepared-set hash (selected_flows.csv + labels.csv), GPU, settings + fingerprint | Compare, Leaderboard |

**What is not recorded, and why.** The pipeline makes one model call per agent with no
retries and no timeouts, by design. Retries are therefore always 0, and timeouts are
"not applicable". The inter-agent context size is not logged separately. It is
estimated from the recorded output tokens of the agents whose outputs are re-sent;
re-tokenisation can differ by a few tokens. GPU power is sampled outside the measured
worker process, so it adds nothing to the measured latency. Per-flow energy comes from
1-second `completed_at` stamps, so a model's window edges are accurate to about a second.

**Checks.**
- `tests/test_service_api.py` and `tests/test_phase46.py` (pytest) cover the API
  contract and every new computation.
- The Playwright walk-throughs run against a live server: `tests/e2e/app_flow.js` (the
  whole app), `service_flow.js` (the wizard's data paths), `session_analysis.js` (the
  embedded analysis, CSV and PCAP), `split_flow.js` (front-end and backend on different
  origins) and `baseline_page.js`. Usage: `node tests/e2e/<file> http://127.0.0.1:8766`.
  Each starts from a fresh results directory: job creation is rate-limited per client.

## PDF benchmark report

**Download PDF** on the session page calls `GET /api/jobs/<id>/report.pdf`.
For a job that hasn't finished it returns **409** `not_ready`. It builds a 1–2 page
A4 report with reportlab. Since Phase 46 it also has the Agents, Cost and energy, Decision helper and Statistical depth sections (`agentmeter/session/pdf_report.py`).

The report contains:
- **Header:** run name, generation time, the session run id and the job id. A
  **DEMO** banner appears when the run used the mock provider.
- **Input:**
  - source type and file; labelled (accuracy + efficiency) or unlabelled
    (efficiency only);
  - rows available and flows selected;
  - the class set: "scored" for labelled runs, "chosen from, not scored" for
    unlabelled runs;
  - feature match and selection mode.
- **Flow selection (rule-base):** every rule, its type, whether it fired, and how
  many flows it admitted. This comes from the run's own `selection_audit.json`.
- **Models evaluated,** with the provider, hardware and quantisation.
- **Per-metric comparison:** the head-to-head winner for each metric (latency,
  VRAM, tokens, plus accuracy when labelled), and a per-model table with the SAW
  composites.
- **Verdict:** the session's own head-to-head sentence plus the absolute SAW
  statement, quoted verbatim.
- **Recommendation:** the efficiency verdict in plain language, then the
  accuracy caveat. A low-accuracy run is stated to be "NOT an endorsement as a
  threat detector". An unlabelled run says accuracy was not measured. A resource-
  only optimisation hint follows.
- **Accuracy by class:** labelled runs only.
- **Caveats:** non-validated, balanced sample, the hardware note, the mock note
  when it applies, and the scope statement.

- **Recommendation — stage 3:** the measured head-to-head verdict, then the
  context notes from Hugging Face model metadata, and a **Rules fired** table. These
  notes never change a score or the verdict; see [RULE_BASE.md](RULE_BASE.md).

**Values are never recomputed.** Every number comes from `session_results.json`
(plus `input.json` and `selection_audit.json` for the input and rule-base
sections).

**Same logic as the dashboard.** The recommendation text comes from
`agentmeter/session/brief.py`, a line-for-line Python mirror of `report.js`'s
`recommendation()` and `optimisationHint()`. `tests/test_pdf_report.py` runs
`report.js` under node and asserts the outputs are identical. When the head-to-head
is a tie and the SAW top is a tie too, no "best starting point" model is named,
because rank 1 inside a tie would be arbitrary.

**Dependencies:** `reportlab` is in `requirements.txt`. The tests also need
`pypdf`, which is in `requirements-dev.txt`.

