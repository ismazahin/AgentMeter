# Analyse traffic without a GPU — implementation plan

Design: `docs/superpowers/specs/2026-11-09-gpu-free-analyse-traffic-design.md` (approved 2026-11-09).
Branch: `claude/cool-ride-mitmzl`. One commit per task; every task leaves the test suites green
(`pytest`, `controlplane: npx tsc --noEmit -p . && npx vitest run`, `scripts/test_all.sh e2e`).

The work is in four phases. Each phase ships on its own and leaves the app working:
**1** the IDS scores the pool during preparation (Python only) → **2** the cloud service and control
plane carry the new files → **3** the benchmark reuses the stored scores → **4** the web app
(Analyse traffic, Cost planner, sidebar).

---

## Phase 1 — IDS scoring of the candidate pool (Python, no deployment)

### Task 1.1 — `score_pool()` in `agentmeter/ids/stage.py`
- Add `score_pool(pool_df, features, selected_ids, labelled, model_path=None) -> (scores_df, info)`.
  It loads the model once (`load_model`), scores **every pool flow**, and returns
  `flow_id, score, label?, selected` plus an info dict with today's `ids.json` fields
  (`model`, `model_metrics_holdout`, default `threshold`, `threshold_source`, `n_flows_in`,
  `scoring_s_total`, `scoring_s_per_flow`, `training_overlap`, `features_missing`, `labelled`)
  and `scope: "pool"`.
- Constants: `IDS_SCORES_CSV = "ids_scores.csv"`, `IDS_POOL_JSON = "ids_pool.json"`,
  `IDS_IDENTITY_CSV = "ids_flows_identity.csv"`.
- Reuse `IdsModel.score` / `overlap`; do not duplicate scoring logic. `apply_ids` stays unchanged.
- Tests (`tests/test_ids_pool.py`, demo model `models/ids/demo_ids.*`): scores every pool row;
  `selected` marks exactly the prepared-set flows; labels present iff labelled; identical scores to
  `apply_ids` for the selected flows (the anti-goal "two scorings that disagree").

### Task 1.2 — Hook it into preparation (`agentmeter/server/prepare.py`, `agentmeter/ingest/run.py`)
- `process_csv` / `process_pcap` already return the pool (`"flows": pool`). Make sure every pool
  row carries the same `flow_id` the selection uses (`rules.py:492`) and the identification
  columns; add a test if a path lacks them.
- `prepare_file(..., ids=None)`: when `ids` is a model path (or `True` = the default model), after
  `finish_prepared_set` write into the run dir: `ids_scores.csv`, `ids_pool.json`, and
  `ids_flows_identity.csv` (`flow_id` + `src_ip, src_port, dst_ip, protocol, protocol_name,
  timestamp` where present). No IDS model available → no IDS files, a manifest note, no failure.
- The files are **not** in `DOWNLOADABLE` and **not** in the prepared-set hash
  (`agentmeter/session/prepared_hash.py` untouched). Add `IDS_FILES` alongside `DOWNLOADABLE`.
- `summarize()` gains an `ids` block (counts at the default τ) when the files exist.
- Tests: the prepared-set hash of a sample CSV and PCAP (`agentmeter/ingest/sample.py`) is the
  same with and without `ids`; the IDS files exist only with `ids`; a missing model is a note.

### Task 1.3 — Import carries the IDS files (`import_prepared`)
- Accept optional `ids_scores.csv` and `ids_pool.json` (never `ids_flows_identity.csv`: the GPU
  server doesn't need IPs). Check `ids_scores.csv`'s `selected` flow ids equal `features.csv`'s
  flow ids; reject otherwise. Raise `MAX_IMPORT_BYTES` only for these two files with their own cap
  (e.g. 40 MB for 500,000 rows) so the prepared-set cap keeps its meaning.
- Tests: round trip prepare → import keeps the files; mismatched ids rejected; identity file ignored.

## Phase 2 — Cloud service and control plane

### Task 2.1 — Hugging Face Space runs the IDS (`cloudprep/`)
- `cloudprep/requirements.txt`: add `scikit-learn==1.9.1` and `joblib` (match `requirements.txt`).
- `/prepare` accepts an optional `ids: {sha256, files: {"ids_rf.joblib": url, "ids_rf.json": url,
  "ids_rf.hashes.npy": url?}}`. The service downloads the files into a cache dir keyed by sha256
  (reused across jobs), loads them with `load_model` (which checks sha256 and sklearn version), and
  calls `prepare_file(..., ids=path)`. It still holds no key: the URLs are single-use, signed by
  the control plane.
- `outputs` may include upload URLs for `ids_scores.csv`, `ids_pool.json`,
  `ids_flows_identity.csv`; the service uploads the ones it produced and lists them in `done.files`.
- Progress events: a new phase `ids` ("scoring N flows with the IDS").
- `scripts/build_web_prep.py` / the browser bundle are unaffected (the browser never scores).
- Tests (`tests/test_cloudprep*.py` if present, else new): a signed job with the demo model's
  files served from a local HTTP server produces the three files; a bad sha256 fails the job with
  a clear code (`ids_model_invalid`); no `ids` → today's behaviour.

### Task 2.2 — Control plane hands the model to the Space (`supabase/functions/agentmeter/src/prep.ts`, `ids.ts`)
- `PREP_FILES` stays the prepared-set list; add `PREP_IDS_FILES = ["ids_scores.csv",
  "ids_pool.json", "ids_flows_identity.csv"]` with content types.
- `dispatch()`: when an IDS model is committed (`storedMeta`), add `ids` with signed download URLs
  (reuse the code behind `backendIdsModel`) and upload URLs for the IDS files.
- Events `done`: accept the IDS files when listed; check each is in storage (as for the others).
- `GET /api/prep/jobs/:id/files/:n`: serve `ids_scores.csv` and `ids_pool.json` to whoever may read
  the job today; serve `ids_flows_identity.csv` **only to the job's owner** (not admins), 403
  `owner_only` otherwise.
- Tests (`controlplane/test/prep.test.ts`, `ids.test.ts`): dispatch body has `ids` iff a model is
  stored; owner-only file rule; done with an unknown file still rejected.

### Task 2.3 — Analyses as data sets, deletion, audit (`datasets.ts`, migration)
- Migration `supabase/migrations/20261110000000_agentmeter_analyses.sql`: add to `prep_jobs`
  `ids_json TEXT` (the `ids_pool.json` summary for listings) — no new table: an analysis **is**
  a prep job with IDS files, and its data set row is the existing `datasets` table.
- Link a prep job to its `datasets` row when it finishes (key = `set_name`), so the Data sets list
  (soon Analyse traffic) shows analyses that have not been benchmarked yet. Ownership: the prep
  job's owner, consistent with `datasets` rules.
- `POST /api/datasets/delete` also deletes the prep job's stored files, including
  `ids_flows_identity.csv`; audit entry `dataset.delete` notes `identity_file_deleted: true`.
- Tests: listing shows an unbenchmarked analysis to its owner; delete removes the identity file.

### Task 2.4 — Deploy and verify on the Space (manual, documented)
- `docs/DEPLOY.md`: the Space now installs scikit-learn and needs nothing else; the first build is
  slower. Redeploy with `python scripts/deploy_cloudprep_hf.py`.
- Verify: upload the IDS hold-out CSV (or a sample) through the control plane with the GPU off;
  the job finishes with the three IDS files. Record timing for 100k flows in the doc.

## Phase 3 — The benchmark reuses the stored scores

### Task 3.1 — `prepare_session` (`agentmeter/session/benchmark.py`)
- Before calling `apply_ids`: if the run dir has `ids_scores.csv` + `ids_pool.json`:
  - the GPU server's IDS model sha256 equals `ids_pool.json`'s → take the selected flows'
    scores from the file, flag at τ, write today's `ids_results.csv` / `ids.json` (same fields,
    plus `scores_source: "prepared set"`), no re-scoring;
  - it differs or no model → `SessionError` ("this data set was scored by IDS <name/sha>; this
    server has <…>; upload the same model in Admin › IDS model").
- No IDS files → today's path unchanged (older sets).
- Tests: reuse path gives identical `ids_results.csv` to re-scoring; mismatch refused; old sets
  behave as before; downstream session scoring tests unchanged (`tests/test_session_benchmark.py`).

## Phase 4 — Web app (`web/app.html`, `web/js/*.js`)

### Task 4.1 — Analyse traffic page (replaces Data sets)
- New view `analyse` (`#/analyse`, `#/analyse/<set>`) in a new `web/js/analyse.js`; reuse the
  upload/link widgets from `wizard.js` (move, don't copy). Analyses always run on the cloud
  service (designer's call: the browser and GPU-server preparation paths stay available in
  "Benchmark this set" flows but produce no IDS files).
- While running: the prep-job progress, including "Waking the preparation service (about a
  minute after a quiet spell)".
- Results (all in the browser from `ids_scores.csv`): τ slider (default `ids_pool.json.threshold`);
  flagged / cleared / flagged share; alert table sorted by score with paging (owner: IPs, ports,
  protocol from the identity file; others: flow id only); with labels: recall, precision, FPR,
  recall per attack class; "N of these alerts are in the benchmark sample".
- Projected LLM cost for the flagged count (Task 4.3's function).
- "Benchmark LLMs on these alerts": enabled when a GPU backend is online; imports the set to the
  backend (`/api/prepared/import`, with the two IDS files) and opens model choice with the IDS
  gate on at the page's τ.
- The list view keeps Data sets' Details / Benchmark this set / Rename / Delete menu.
- `#/datasets` redirects to `#/analyse`.

### Task 4.2 — Benchmark entry points (wizard)
- `#/new` with no set → redirects to `#/analyse` (upload starts there). `#/new/models?set=…` (from
  an analysis or "Benchmark this set") keeps working; the IDS threshold step reads τ from the
  analysis when present.

### Task 4.3 — Cost planner (`#/planner`, `web/js/planner.js`)
- `projectCost({models, gpu, flowsPerDay | alertsPerDay})` in `web/insights.js` (pure; unit-tested
  with the existing insights tests): per model, the most recent finished session on the same GPU
  and pipeline/prompt version gives latency, cost, energy per flow; output per day with the source
  session shown. No matching session → "not measured on this GPU yet".
- Page: model picker, GPU picker (GPUs seen in stored sessions), volume input, table.

### Task 4.4 — Sidebar, Leaderboard tabs, GPU chip
- Sidebar: Home · Analyse traffic · Sessions · Leaderboard · Cost planner; Settings · Admin; How to use.
- Leaderboard gets tabs Ranking · Compare · Repeatability (move the Repeatability view in;
  `#/trends` redirects to `#/leaderboard?tab=repeatability`).
- GPU & Queue → a panel opened from the header GPU chip (status + queue); full queue in Admin.
  `#/gpu` opens the panel.
- Update How to use and the Ctrl+K search entries (`web/js/home.js`).

### Task 4.5 — End-to-end and docs
- `tests/e2e/app_flow.js` / `cp_flow.js`: GPU off → Analyse traffic upload → results → τ change
  updates counts; GPU on → Benchmark these alerts → session shows `scores_source`.
- Docs: `README.md` (feature list), `docs/IDS.md` (the IDS runs at preparation; the GPU reuses the
  scores), `docs/SESSION_BENCHMARK.md`, `web/README.md`; regenerate OpenAPI (`scripts/build_openapi.py`).

---

## Order and checkpoints
1. Phase 1 (1.1 → 1.3): reviewable Python change; nothing deployed.
2. Phase 2 (2.1 → 2.4): needs a Space redeploy and a Supabase migration + function deploy (staging
   first: `docs/STAGING.md`). **Checkpoint:** an analysis completes with the GPU off.
3. Phase 3: needs a GPU rental to verify for real (mock provider covers the tests).
4. Phase 4: the visible change. **Checkpoint:** walk through the new sidebar with you before
   removing the old routes.

## Risks to watch
- Space memory/time on 500,000-row CSVs (scores + identity file ≈ tens of MB): measure in 2.4;
  if slow, cap the scored pool and say so on the page.
- Supabase free-plan 50 MB per stored file: `ids_flows_identity.csv` for 500k rows may approach it;
  gzip it if needed (measure in 2.4).
- sklearn version drift: the Space, the GPU server and `requirements.txt` must all pin the same.
