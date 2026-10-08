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
| Mean peak working VRAM | lower | % less, and a ratio | within 2%, or not significant; "not available" on CPU/mock |
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

## The service web flow (`/service`)

`python scripts/pull_eval_server.py`, then open `http://<host>:8000/service`. The
analysis dashboard of the locked study stays at `/`, labelled "Validation
baseline", and links to the service with **Run a benchmark**.

1. **Upload.** Choose a `.csv`, `.pcap` or `.pcapng` file, set the number of flows
   to benchmark and choose whether to keep "Other Attack".
   - The file goes to `POST /api/ingest`, which saves it under `results/uploads/` and
     runs the existing ingestion into `results/csv_runs|pcap_runs/<name>_<stamp>/`.
     Uploading the same file twice creates two separate runs.
   - A bad file is rejected with its reason.
2. **Validation summary + models.** The page shows:
   - rows read, usable, dropped and excluded (or packets and flows for a PCAP);
   - whether the run is labelled, meaning accuracy plus efficiency, or unlabelled,
     meaning efficiency only;
   - the class distribution and how many of each class were selected;
   - the feature-match status, and the flow-selection rules with which ones fired.

   You pick at most 2 of the 5 study models: a 3rd checkbox is disabled, and the API
   enforces the same limit. The summary is re-read from the run's files by
   `GET /api/runs/<kind>/<name>`, so reloading the page keeps it.
3. **Run + progress.**
   - **Run** posts to `/api/jobs`, and the page then polls `GET /api/jobs/<id>`
     every 2 seconds. It shows the status, a progress bar, flows done out of the
     total, the current model, and the queue position while queued.
   - The job id is in the URL (`#/job/<id>`, also kept in localStorage). Reloading,
     or reopening the link later, re-attaches to the same job.
   - A failed or interrupted job offers **Resume**.
4. **Results.** The page renders the server's `session_results.json`; it computes
   nothing itself:
   - the head-to-head verdict, with the absolute SAW statement under it;
   - a per-model table;
   - a head-to-head table;
   - accuracy by class and confusion matrices, for labelled runs only;
   - per-agent time and tokens;
   - the caveats;
   - downloads: the results JSON, plus SAW and per-agent CSVs through the existing
     `report.js`.

   **Recent jobs** lists past jobs so you can reopen any result.

**No GPU.** On a server without a GPU, `GET /api/service/config` switches the
service to **demo mode** with the mock provider, and the page says so on every view.
On a GPU host it uses `hf` with `configs/run_full_l4.yaml` (4-bit NF4). An operator
can force the mode with `AGENTMETER_SERVICE_PROVIDER=mock|hf`. Run the server as
**one process**, because the single-job lock lives in that process.

**Checks.**
- `tests/test_service_api.py` (pytest) covers the API contract.
- `tests/e2e/service_flow.js` is a Playwright walk-through against a live server:
  `node tests/e2e/service_flow.js http://127.0.0.1:8766`. It covers:
  - upload → summary → the 2-model cap → run;
  - re-attach after a reload;
  - results, and reopening from Recent jobs;
  - PCAP efficiency only, and a bad file rejected;
  - no horizontal scroll at 375px.

## PDF benchmark report

**Download PDF report** on the results page calls `GET /api/jobs/<id>/report.pdf`.
For a job that hasn't finished it returns **409** `not_ready`. It builds a 1–2 page
A4 report with reportlab (`agentmeter/session/pdf_report.py`).

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

