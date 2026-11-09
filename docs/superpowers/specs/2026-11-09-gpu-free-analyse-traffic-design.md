# Analyse traffic without a GPU — design

Date: 2026-11-09 · Status: approved 2026-11-09

## Intent and why

AgentMeter is meant to be usable without renting a GPU; the GPU should only be needed for the
step that truly needs it, running the LLM agents. Today, with the GPU off, the app is a
**viewer** of past results: nothing new can be done. Four sidebar pages work only half or not
at all (New benchmark, Data sets, Repeatability, GPU & Queue), and the IDS gate (a CPU Random
Forest) runs on the GPU server only because it is part of the benchmark job
(`agentmeter/session/benchmark.py:prepare_session` → `agentmeter/ids/stage.py:apply_ids`).

This change moves the IDS off the GPU server and makes **Analyse traffic** the GPU-free starting
point: upload a CSV or PCAP → the IDS detections → the alert queue → the projected LLM cost per
model (from earlier measured sessions). From there, when a GPU is on, **"Benchmark LLMs on these
alerts"** starts a session on the same flows (decision **A**: one flow, detect → triage →
measure). The sidebar is reorganised so that no main page needs a GPU.

## Goals

1. With no GPU rented, a signed-in user can upload a CSV/PCAP (file or URL) and get:
   the IDS's flagged/cleared counts, the alert list ordered by IDS score, recall/precision/FPR
   when labels exist, and a projected LLM time/cost/energy per model for these alerts.
2. The IDS threshold τ can be changed on the page and the results update at once (no server).
3. "Benchmark LLMs on these alerts" starts a session that uses **the same IDS scores** and τ
   (no re-scoring on the GPU when the model is the same).
4. Sidebar: Home · Analyse traffic · Sessions · Leaderboard (tabs: Ranking · Compare ·
   Repeatability) · Cost planner; Account: Settings · Admin; Help. GPU status is a header chip
   whose panel shows the queue.
5. Costs nothing to host: the IDS runs on the existing cloud preparation service on the **free
   Hugging Face Space** (CPU Basic, 2 vCPU, 16 GB; `scripts/deploy_cloudprep_hf.py`).

## Non-goals

- No change to the LLM pipeline, the measurements, SAW scoring, or the locked study.
- No new IDS model or training changes (`scripts/train_ids.py` unchanged).
- No IDS in the browser (Pyodide) and none in the Supabase function (see Approaches).
- Not a production detection product: no live capture, no alerting on detections.

## Anti-goals (a failure even if it works)

- Two IDS scorings of the same flows that can disagree (cloud vs GPU server).
- Changing the prepared-set hash of existing sets, or breaking existing sessions/compare.
- Any page in the main sidebar that is empty or broken when the GPU is off.
- Presenting IDS output as an LLM triage verdict: priority/severity/MITRE stay LLM-session
  results (the policy table maps a *class*; the binary IDS has none). **Decided: the IDS stays
  binary** — no multiclass model in this change.
- Showing one user's IP addresses to anyone else (see "Identification columns").

## Constraints

- The IDS model loader refuses a scikit-learn version other than the one it was trained with
  (`agentmeter/ids/model.py:115`; currently 1.9.1). The cloud service must install exactly that.
- The model files live in the control plane (Admin › IDS model; `GET /api/backend/ids-model`,
  used by `scripts/vast_ids.py`). The cloud service must fetch them the same way (signed).
- The free Space sleeps after 48 h idle; the control plane already wakes it (`wakePrep`).
  The first analysis after sleep waits ~1 min — the page must say so.
- Browser upload cap 90 MB (`service.max_upload_mb`); larger inputs use URL import.
- Candidate pool caps already exist: CSV ≤ 500,000 rows (stratified), PCAP ≤ 20,000 flows.
- The prepared set holds ≤ 500 rule-selected flows (`MAX_SELECTED`); its hash
  (`agentmeter/session/prepared_hash.py`) covers features + labels only.
- The app now lives in `web/app.html` (the landing page is `web/index.html`), and the Data sets
  page has Details / Benchmark this set / Rename / Delete backed by the control plane's
  `datasets` table (migration `20261109000000`, `GET /api/datasets`,
  `POST /api/datasets/rename|delete`, run-token kind `delete_set`). Analyse traffic must keep
  these actions, ownership rules and audit entries.

## Approaches considered

1. **IDS in the data-preparation step, on the cloud service (chosen).** The same Python
   (`agentmeter.ids`) runs where preparation already runs; the GPU server's own prepare uses
   the same code. Reuses the most, one scoring path, results identical by construction.
   Cost: scikit-learn pinned on the Space and a slightly longer build. Free on the HF Space.
2. **IDS in the browser.** Export the forest to arrays, score with numpy in Pyodide. Free and
   offline, but a 100-tree depth-20 forest may be tens of MB per user and it is a second
   scoring implementation that must agree exactly. Rejected.
3. **IDS in the Supabase function.** Cannot load a scikit-learn model; CPU-time limits too tight
   for 100k flows. Rejected.

Hosting: free HF Space (chosen) over Cloud Run (needs a billing card).

## The design (decided parts)

### Where the IDS runs and what it scores
- The cloud preparation job gains an optional IDS stage. After the candidate pool is built and
  before rule selection writes the prepared set, it scores **every flow in the candidate pool**
  (not only the ≤ 500 selected), so the analysis describes the uploaded traffic, not the
  benchmark sample.
- New outputs next to `manifest.json` / `features.csv` / `labels.csv`:
  - `ids_scores.csv` — `flow_id, score, label?, selected` for the whole pool
    (`selected` = in the prepared set). Labels only when the input had them.
  - `ids_flows_identity.csv` — `flow_id` + the identification columns (`src_ip, src_port,
    dst_ip, protocol, protocol_name, timestamp`) for the whole pool (see below).
  - `ids.json` — model identity (sha256, sklearn version, threshold rule), default τ, pool size,
    scoring time, training-overlap check, features missing (same fields as today's `ids.json`).
- These files are **outside** the prepared-set hash; existing sets keep their hash.
- Analyses are prep jobs: they live in the control plane like prep jobs today (`prep.ts`),
  with the two new files added to the job's files.

### Identification columns (decided: stored, owner only)
- Today the control plane strips identification columns from what it stores (Phase 47).
  **Decided:** for analyses only, they are stored in a separate file, `ids_flows_identity.csv`,
  readable **only by the analysis's owner** (not other users; admins only through the existing
  delete path, never by reading). The prepared set and its stored copy stay stripped as today,
  so the prepared-set hash and session sharing are unchanged.
- Deleting the analysis (data set) deletes this file. The audit log records its deletion.
- Other viewers of the same analysis/data set see flow ids and features only.

### The Analyse traffic page (browser; no GPU)
- Upload (file or URL) → progress (including "waking the preparation service") → results.
- τ slider (default = model's τ). Everything below recomputes in the browser from
  `ids_scores.csv`: flagged/cleared counts, flagged share, alert list ordered by score
  (with source/destination IP, port and protocol for the owner), and with labels: recall,
  precision, FPR, recall per attack class.
- Projected LLM cost per model for the flagged alerts: alerts × measured per-flow latency /
  cost / energy from stored sessions (see Cost planner). Shown as a projection, with the
  session(s) and GPU it comes from.
- "Benchmark LLMs on these alerts" (enabled when a GPU backend is online) opens model choice for
  this prepared set with the IDS gate on at the page's τ. **Decided: today's semantics** — the
  LLMs get the flagged flows among the ≤ 500 rule-selected flows. The page says
  "N of these alerts are in the benchmark sample" (N recomputed with τ).
- The list of past analyses replaces the Data sets page (each shows which sessions used it),
  keeping its Rename / Delete / Benchmark this set actions.

### The benchmark uses the stored scores
- `prepare_session` (`agentmeter/session/benchmark.py`) reads the set's `ids_scores.csv` /
  `ids.json` when present and the GPU server's IDS model has the **same sha256**: it takes the
  flagged flows among the selected ones at τ, without re-scoring, and writes today's
  `ids_results.csv` / `ids.json` into the run folder so all downstream analysis is unchanged.
- If the server's model differs or is missing: refuse with a clear message (anti-goal: no two
  disagreeing scorings). Sets with no stored scores (older sets) keep today's behaviour.

### Sidebar and pages
- Workspace: Home · **Analyse traffic** · Sessions · **Leaderboard** (tabs Ranking · Compare ·
  Repeatability) · **Cost planner**. Account: Settings · Admin. Help: How to use.
- Removed from the sidebar: New benchmark (its upload becomes Analyse traffic; model choice
  is reached from an analysis or a session's "benchmark again"), Data sets (→ Analyse traffic),
  Repeatability (→ Leaderboard tab), GPU & Queue (→ header chip panel; full queue in Admin).
  Old hash routes (`#/new`, `#/datasets`, `#/trends`, `#/gpu`) redirect to their new home.
- **Cost planner**: choose models, a GPU (from GPUs seen in stored sessions) and flows per day
  (or alerts per day); shows projected time, cost, energy per day per model from stored session
  metrics on the same pipeline/prompt version. Browser-only.

## Left to the builder

- Exact fields in stored session summaries used for projections, and which session is "the"
  source when several match (suggestion: most recent on the same GPU + pipeline; show it).
- How the cloud service authenticates its model download (reuse the backend-secret flow of
  `vast_ids.py` or a signed URL handed in the `/prepare` request — the latter fits the service's
  "never holds a key" rule better).
- Model caching on the Space between jobs (by sha256).
- Alert-list paging for large pools; chart choices.
- Tests: pytest for the stage and the benchmark reuse path; vitest for `prep.ts` changes;
  e2e update for the new routes.

## Decisions log

| Question | Decision |
|---|---|
| Approach | IDS in the data-preparation step on the cloud service |
| Hosting | Free Hugging Face Space (CPU Basic) |
| Analysis ↔ session | One flow (A): analysis first, "Benchmark LLMs on these alerts" next |
| Class without a GPU | Binary IDS only; priority/severity appear after an LLM benchmark |
| Benchmark sample | Today's semantics: flagged ∩ ≤ 500 rule-selected flows |
| IP addresses | Stored for the owner only, in a separate file; prepared set stays stripped |

Calls made by the designer (check these): scoring the whole candidate pool, IDS files outside the
prepared-set hash, refusing a benchmark when the GPU server's IDS model differs, past analyses
replacing the Data sets page, Repeatability as a Leaderboard tab, the GPU queue in a header-chip
panel and Admin.
