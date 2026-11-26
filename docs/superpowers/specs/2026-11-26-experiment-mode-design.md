# Experiment mode: vary precision and agent count, compare in charts (design)

Status: approved 2026-11-26 by the owner, phase 1 of 3.
Mockups: `2026-11-26-experiment-mode-mockup/experiment.html` (set-up and results screens),
`one-or-two.html` (one page or two: the owner chose one page).

## Why
The owner's supervisor does not want AgentMeter to be a one-way tool (upload, run, read a report).
He wants it **dynamic**: a user changes a factor of the experiment, the system benchmarks every
choice, and charts show how performance and cost change. His two examples:

1. The **size of the model**: the same LLM at different precisions, compared across LLMs and
   across sizes.
2. The **number of agents** in the pipeline. His question: "why only 4 agents, why not 2, 6 or
   10?" The answer is that 4 (Perceive, Reason, Decide, Act: sense, think, act) is a design choice,
   not a limit. The harness is already agent-count agnostic (objective 1, `config.yaml` pipeline),
   and experiment mode shows it.

This also feeds the research questions directly. More agents re-send more context at every
hand-off (problem 3), so the cost should climb with the agent count. Whether triage accuracy climbs
with it is an open question the experiment answers. Lower precision saves memory, but with
bitsandbytes it is often slower than 16-bit, which is a finding worth showing.

## The path (three phases, this document is phase 1)
1. **Experiment mode on one GPU** (this design): a grid of models × precisions × agent counts,
   each cell measured on one RTX 3090, results compared in charts.
2. **Cost prediction** (later design): fit `time ≈ input tokens / prefill rate + output tokens /
   decode rate + overhead` per model, precision and GPU from phase 1 measurements. Sliders then
   predict any combination instantly, with a confidence range, and "Measure this" confirms it on
   the GPU. Prediction error against measurement becomes a thesis result.
3. **Several identical GPUs in parallel** (later design): the control plane rents extra RTX 3090s
   through the Vast API key it already stores. Each takes cells from the queue, runs a short
   reference benchmark first (outliers are rejected), measures, reports and destroys itself.

Phase 1 must leave room for both: every cell's results are stored per cell with its full identity
(phase 2 reads them), and cells are independent queue items (phase 3 hands them to other GPUs).

## Approaches considered
- **Agent count**
  - A. A fixed ladder of pipelines (1, 2, 3, 4, 6, 8, 10 agents), each with defined roles and
    the same final output. **Picked**: fair and repeatable, and it answers "2, 6 or 10".
  - B. Users build their own pipeline. Rejected: it was removed before because results were not
    comparable and a weak prompt spoils the readings.
  - C. A 1 to 10 slider of identical "refine" steps. Rejected: roles that do the same thing say
    little about agentic pipelines.
- **What the user varies**
  - One factor per experiment. Rejected by the owner: a free grid shows interactions (for
    example 8-bit is slower mainly when there are many agents). **Picked: a free grid**, capped.
- **Speed**
  - Bigger GPU: about 1.5 to 2 times faster with bitsandbytes, 2 to 5 times the price, and every
    stored session stops being comparable. Rejected.
  - vLLM engine: fastest, but it cannot measure per-agent working VRAM or time to first token,
    the core of objective 1. Rejected.
  - Serverless GPUs (Modal, RunPod Serverless): 3 to 4 times the hourly price, different GPU
    types, and the GPU server would need rewriting as stateless functions. Rejected.
  - Several runs at once on one multi-GPU machine: the runs share CPU, memory and disk, which
    contaminates latency. Rejected (it breaks the rule that nothing runs next to a measurement).
  - **Picked: work smarter on one RTX 3090 now** (reuse measured cells, one explanation per
    experiment, fewer flows), **parallel identical GPUs later** (phase 3).
- **2-bit** precision: bitsandbytes has no 2-bit mode; it would need another engine (HQQ, GGUF),
  so the comparison would not be like for like, and 7B models usually break at 2-bit. Dropped.
  The sizes are **4-bit, 8-bit and 16-bit**.

## Goals (phase 1)
- A new page, **New experiment**: upload (as today, IDS gate on), pick 1 to 4 models, tick any
  of 4-bit / 8-bit / 16-bit, tick any agent counts from 1, 2, 3, 4, 6, 8, 10. The page shows
  "N runs, about M minutes" as the user ticks, and blocks Run above **12 runs**.
- Every cell (model, precision, agent count) is benchmarked on **the same prepared flows, the
  same IDS threshold, the same GPU and the same settings**, differing only in its two factors.
- An **experiment results page**: line charts (x = agent count, one line per precision, one
  panel per model), a heatmap per measure (rows = precision, columns = agent count), and a table
  of every cell. Measures: latency per flow (mean, p95), peak VRAM, tokens per flow, re-sent
  context tokens per flow, valid answers, triage accuracy (labelled data), triage gate, energy
  and cost per 1,000 flows. Points show their 95% range.
- A cell that was already measured (a finished session with the same prepared-set hash, GPU and
  settings fingerprint for that model) is **reused**, not run again, and marked "reused".
- One **written explanation per experiment**, at the end, describing what changed and why, built
  and checked the same way as the session explanations (facts by code, LLM wording, every number
  checked). Cells skip their own explanation.
- Each cell is stored as an ordinary session, so the Leaderboard, the session page and the PDF
  work for a single cell without change. The experiment groups them.

## Non-goals (phase 1)
- Cost prediction and sliders (phase 2). Parallel GPUs (phase 3).
- 2-bit, other engines, user-built pipelines, other factors (prompt wording, temperature).
- An experiment PDF report (a CSV of the cell table is enough for now).
- Changing how a normal benchmark works: New benchmark stays as it is (4-bit, 4 agents).

## Anti-goals (a failure even if it works)
- Two cells of one experiment that differ in anything other than their precision and agent
  count (flows, IDS threshold, GPU, prompts, token budgets, decoding).
- Any cell measured while something else runs on the GPU machine.
- A chart that mixes reused and fresh cells without saying so, or a point drawn for a cell that
  failed or did not fit.
- New agent prompts that add detection intelligence. The existing guardrail in
  `agentmeter/pipeline/agents.py` holds: prompts fix the role and the output format only.

## Constraints
- One RTX 3090 (24 GB) on Vast in phase 1. No new paid services.
- 16-bit 7 to 8B models need about 15 to 17 GB of weights: they fit, larger ones do not. The
  model rule engine (`configs/model_rules.yaml`) today estimates 4-bit VRAM only; it needs a
  per-precision estimate (about 2 bytes per parameter at 16-bit, 1 at 8-bit, 0.55 at 4-bit, plus
  the same overhead and working memory) so a cell that will not fit is shown as "won't fit"
  before Run, not failed after.
- The GPU server refuses anything but 4-bit NF4 (`agentmeter/server/runtime.py` preflight,
  `agentmeter/providers/vllm_provider.py`). The HF provider already supports 8-bit
  (`agentmeter/providers/hf.py`: `load_in_8bit`) and no quantization. Experiment cells may set
  these; normal benchmarks stay 4-bit only.
- Precision and pipeline are already part of the settings fingerprint
  (`agentmeter/session/analyses.py: settings_of`), so cells are automatically separate setups on
  the Leaderboard and never pooled with each other.

## How it works

### The agent ladder
Each rung has fixed roles. The first agent reads the flow features; every agent ends with the
same final output as today: the class (a `label` agent), MITRE technique and justification (a
`verdict` agent). Priority and severity still come from the policy table, never from the model.

| Agents | Pipeline |
|---|---|
| 1 | Triage (summary, reasoning, label and verdict in one call) |
| 2 | Analyse (Perceive + Reason) → Conclude (Decide + Act) |
| 3 | Perceive → Reason → Conclude (Decide + Act) |
| 4 | Perceive → Reason → Decide → Act |
| 6 | Perceive → Enrich → Reason → Verify → Decide → Act |
| 8 | Perceive → Enrich → Hypothesise → Reason → Verify → Critique → Decide → Act |
| 10 | Perceive → Enrich → Hypothesise → Reason → Compare → Verify → Critique → Decide → Justify → Act |

New roles (format and role only, no detection hints):
- **Enrich**: restate what the protocol, ports and timing fields mean in plain words.
- **Hypothesise**: list the two or three behaviours the flow could be.
- **Compare**: weigh the listed behaviours against each other.
- **Verify**: check the reasoning against the feature values and point out contradictions.
- **Critique**: name the weakest step of the reasoning so far.
- **Justify**: write the one-line justification for the chosen label.
- **Conclude**: give the label line and the verdict in one reply.

Rungs 1, 2 and 3 need agents that produce both the label and the verdict in one reply; the label
is parsed from it as `make_verdict` already does for a pipeline without a verdict agent.

**Hand-off rule (one rule for every rung):** each agent reads the outputs of all earlier agents
(the full history). This is how most agent frameworks pass context, and it makes the re-sent
context grow with the number of agents, which is the effect to measure. The experiment's 4-agent
rung therefore differs slightly from the normal benchmark's pipeline (where Act reads only
Reason), and it has its own prompt version, so the two are never pooled. *(Decided by the owner,
2026-11-26: every agent reads all earlier outputs.)*

Token budgets: the existing per-agent `max_new_tokens` apply by role; new roles get the Reason
budget. Decoding stays as configured for normal sessions.

### Precision
- **4-bit**: today's setting (bitsandbytes NF4, double quantization, fp16 compute).
- **8-bit**: bitsandbytes LLM.int8 (`load_in_8bit`).
- **16-bit**: no quantization, the weights' own fp16/bf16.

### The experiment and its cells
- The control plane gets an **experiments** table: id, owner, title, prepared set, models,
  precisions, agent counts, status, and per cell: model, precision, agent count, session id,
  state (`waiting`, `reused`, `running`, `done`, `failed`, `wont_fit`).
- **Planning** (on create): the grid is expanded, cells that will not fit the GPU are marked
  `wont_fit`, cells that match a finished session are marked `reused`, and the rest become jobs.
- **Jobs**: one GPU-server job per (precision, agent count), holding every model of the grid that
  still needs that combination. This keeps today's job shape (one session, several models run one
  after another in their own worker process), so the queue, the waiting list, the GPU lock, the
  run grants, live telemetry and Stop all work unchanged. Each job carries the experiment id and a
  config override (quantization block, pipeline rung); it writes an ordinary session.
- Job order: by precision, then by agent count, so the same model and precision run back to back.
- **Loading once per model and precision** (the speed-up promised for phase 1) is the last step
  of phase 1: a worker mode that loads a model once and runs several rungs, each into its own
  session. The first version may load per job; the design must not block this.
- Cells skip the per-session explainer. When every cell is final, one experiment explanation runs
  (the checked explainer, with facts built per cell and per factor by code). If it cannot run or
  times out, the results page shows code-built sentences instead.

### Settled details (builder's calls)
- **Flows**: the upload's flow count, as in New benchmark; the page suggests 10 to 20 and shows
  the sample-size advisor's note. Every cell uses the same flows (only those the IDS flags).
- **The 12-run cap** counts cells that will be run; reused and won't-fit cells do not count.
- **Reuse** matches experiment cells only. Normal benchmarks use the normal pipeline's prompt
  version, so they never match an experiment rung, even at 4 agents and 4-bit.
- **Stop** on the experiment stops the running job and cancels the waiting ones; finished cells
  keep their results and the experiment shows as stopped (partial).
- **Telegram**: one message when the experiment finishes, not one per cell.
- **Who**: anyone who may run a benchmark; experiments are owner-only like sessions.

### Estimate before Run
Per cell: flows × seconds per flow from earlier sessions of that model on this GPU
(`INSIGHTS.runEstimate`), scaled by agent count (tokens grow with the rung) and by a precision
factor measured once and stored; plus model loading. Unknown history gives a range, labelled
as rough. The page shows runs, minutes and the GPU cost at the configured hourly price.

### Results page
- Header: title, the flows (count, file, IDS threshold), the GPU, status per cell.
- **Charts**: for each measure, a small-multiple line chart (panel per model, x = agent count,
  line per precision) with 95% ranges, and a heatmap toggle. Live: points appear as cells finish.
- **What changed**: the experiment explanation, then code-built facts such as "From 4 to 10
  agents, Qwen 7B (4-bit) needed 2.6 times the time per flow; triage accuracy moved from 72% to
  75%, a gap the test calls a tie".
- **Cell table**: every cell with its measures, a link to its session, and "reused" or
  "won't fit" where so. CSV download.
- Comparisons between two cells use the same tests as sessions (Kruskal-Wallis, Dunn, ties for
  non-significant gaps).

### Where it shows in the app
- Sidebar: **New experiment** under New benchmark.
- Sessions list: an experiment is one row that expands to its cells.
- The Leaderboard needs no change: each cell is its own setup.

## Build order (phase 1)
1. Agent ladder: role prompts, registry entries, the full-history hand-off, prompt version,
   unit tests for every rung (mock provider).
2. Precision: 8-bit and 16-bit through the HF provider for experiment jobs; per-precision VRAM
   estimate in the rule engine; preflight accepts experiment overrides only.
3. GPU server: job config overrides (quantization, rung), the experiment id on the session, the
   per-cell explainer skip.
4. Control plane: experiments table, create (plan, reuse, wont_fit, enqueue), read, list,
   owner-only access, the cell state updates as sessions finish.
5. Web: New experiment page (grid, live run count and estimate, 12-run cap), results page (charts,
   heatmaps, cell table, CSV), sessions list grouping.
6. Experiment explanation (facts per cell and factor, the checked explainer, fallback sentences).
7. Load once per model and precision (worker runs several rungs).
8. Tests and the end-to-end flow on the mock provider; one real run on the RTX 3090.

## What is left to the builder
- Exact role prompt wording (format-only guardrail, reviewed against the existing four).
- The precision speed factor's first value before any 8-bit or 16-bit history exists.
- Chart library details, colours and layout within the existing design tokens and the mockup.
- Names of tables, routes and fields.

## Decided with the owner
- **Hand-off rule**: every agent reads all earlier outputs (the owner's choice, 2026-11-26). It is
  how most agent frameworks pass context and shows the growing re-sent context (problem 3).

## Changes decided after approval (2026-11-26)
- **One page, not a separate experiment mode.** New benchmark gets an optional block "Compare model
  sizes and number of agents". Untouched, it is a standard benchmark (4-bit, 4 agents). With more
  than one size or number of agents ticked, it becomes an experiment. The session page shows a
  **Size & agents** tab for any run of an experiment. There is no separate New experiment page.
- **Every benchmark uses the full-history hand-off** (prompt version `triage-2`), including the
  standard 4-agent pipeline, so all new readings follow one rule. Older sessions stay viewable but
  are separate setups (the owner said not to worry about them).
- **Data preparation stays on the Vast machine**, in the waiting list, never alongside a benchmark
  (Hugging Face Spaces that run code now need a paid plan; a CPU-core split was considered and not
  needed).

## Built (phase 1)
- `agentmeter/pipeline/agents.py`: role framework, `LADDERS`, full-history hand-off.
- `agentmeter/session/experiment.py`: sizes, plan (cap 12), variant block, memory estimate.
- `POST /api/jobs` with `precisions[]` and `agents[]`: one job per combination, each an ordinary
  session with `session.variant` and `session.experiment`; cells skip the per-session explainer;
  Stop cancels the waiting runs; `GET /api/experiments/<id>`; won't-fit cells are skipped.
- Control plane: one Telegram message per experiment (its last run).
- Web: the compare block, the watch page names each run and follows the experiment, the Size &
  agents tab (charts, heatmap, what changed, every run, CSV).
- Not yet: reuse of already-measured cells, loading a model once for several pipeline sizes, the
  LLM-written experiment explanation (the tab's "What changed" is built from the measurements).
