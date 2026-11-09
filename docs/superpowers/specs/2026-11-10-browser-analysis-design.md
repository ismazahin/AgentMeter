# Analyse traffic in the uploader's browser — design

Date: 2026-11-10 · Status: for approval · Replaces the "self-starting preparation service" half of
`2026-11-10-one-data-step-and-autostart-design.md` (its "one data step" half still stands).

## Intent and why

Analyse traffic must work without a rented GPU **and** without the owner's PC being on. The
preparation code already runs in the browser (Pyodide: `web/prep/prep-worker.js`, the same Python,
byte-identical files). Adding the IDS scoring there, and storing the result in Supabase, removes
the service, the tunnel, the secrets and the autostart: each user's own computer does the work.

## Goals

1. Analyse traffic prepares **and** scores in the uploader's browser by default, and stores the
   analysis (the 3 prepared-set files + `ids_scores.csv`, `ids_pool.json`, `ids_flows_identity.csv`)
   in Supabase, exactly as a cloud job does today, so everything after it (the analysis page, the
   Data sets list, "Benchmark these alerts", stored-score reuse on the GPU) is unchanged.
2. Browser scores equal scikit-learn's for the same model (to float rounding), tested.
3. One data step (from the earlier design): Analyse traffic is the only upload; New benchmark,
   Data sets, Repeatability, GPU & Queue leave the sidebar as already agreed; Cost planner joins.

## Non-goals

- Retraining or changing the IDS; scikit-learn in the browser (its version cannot load the 1.9.1 file).
- Autostart, self-registration (dropped: not needed).

## Anti-goals

- A second scorer that can silently disagree with scikit-learn (a test pins equality, incl. missing values).
- A browser tab that dies on a huge file without saying why (limits are checked first).

## Constraints

- **A link import cannot run in the browser** (other sites block cross-origin downloads): links
  still need the preparation service (`prep_local.py` or Cloud Run), when one is configured.
- Browser memory (WebAssembly): the browser path takes files up to a set size (designer's call:
  200 MB, and the existing pool caps of 500,000 CSV rows / 20,000 PCAP flows); larger files go to
  the service when one runs, else the page says so.
- The model download (a compact export of the forest) is fetched once per browser and cached by
  its sha256. Its size is printed when exported; Supabase's free plan stores files ≤ 50 MB.

## Approaches considered

1. **The browser (chosen):** free, no PC on, scales per user; slower on big files; one-time download.
2. The owner's PC with autostart: works only while the PC is on; setup per machine. Kept as an
   optional helper for links and very large files.
3. Rejected: GitHub Actions (terms forbid serverless use); free hosts without a card (≈ 512 MB RAM,
   below the 630 MB measured for 100k flows); scikit-learn in Pyodide (version mismatch).

## Design

### The portable forest (`agentmeter/ids/forest.py`)
- `export_forest(clf) -> dict of arrays`: every tree's nodes concatenated — `feature` (int32, −2 =
  leaf), `threshold` (float64), `left`, `right` (int32, global indices), `missing_left` (bool),
  `p_attack` (float64, the leaf's attack fraction), `roots` (int32, one per tree). Saved as
  `ids_rf.forest.npz` (compressed) next to the model; its metadata (`ids_rf.json`) gains
  `forest_sha256` and `forest_bytes`.
- `PortableForest.predict_attack(X)`: vectorised walk, all rows per tree at once; NaN follows
  `missing_left`; returns the mean of `p_attack` over trees = scikit-learn's `predict_proba[:, 1]`.
- `scripts/train_ids.py` writes the export; `scripts/export_ids_forest.py <ids_rf.joblib>` exports
  an existing model (the owner's current one) without retraining.
- `load_model` stays for servers; `load_portable(npz, meta, hashes?)` gives the same interface
  (`features`, `threshold`, `identity` incl. the **joblib's** sha256, `score`, `overlap`), so
  `score_pool` / `write_ids_analysis` run unchanged in the browser. The stored-score reuse on the
  GPU (sha256 match) therefore works for browser analyses too.
- Tests: equality with `predict_proba` on the hold-out sample and on rows with NaN / inf; the demo
  model and a freshly trained one; export size reported.

### Control plane
- Admin › IDS model accepts the fourth file `ids_rf.forest.npz` (checked against `forest_sha256`).
- `GET /api/ids-model/browser` (any signed-in user): the model's metadata and short-lived download
  URLs of `ids_rf.forest.npz`, `ids_rf.json`, `ids_rf.hashes.npy`; 404 `no_ids_model` when none.
- A browser analysis is a prep job too: `POST /api/prep/jobs {where: "browser", filename, size,
  max_flows}` creates the row (`source_kind = 'browser'`, status `running`) and returns upload URLs
  for the six output files; `POST /api/prep/jobs/:id/finish {set_name, files}` (the job's owner)
  checks the files are stored and finishes it like a service's `done` (same `analysisFacts`).
  The addresses file stays owner-only. A browser job with no news for 30 minutes is failed.

### Browser (`web/prep/prep-worker.js`, `web/js/analyse.js`)
- The worker gets the model files (cached in Cache Storage by sha256), loads them with
  `load_portable`, and calls `prepare_file(..., ids=model)`; it returns all six files.
- Analyse traffic: a file ≤ the browser limit → browser (default); a link, or a larger file → the
  service if one is configured, else a clear message. Progress shows the one-time downloads
  (Python ≈ 40 MB, the model) and each step. Leaving the page stops a browser analysis (said so).
- No IDS model stored: the set is prepared and stored without an analysis, as today.

### One data step (unchanged from the earlier design)
Sidebar Home · Analyse traffic (with the Data sets list) · Sessions · Leaderboard (Ranking · Compare
· Repeatability) · Cost planner; GPU chip → queue panel; redirects for `#/new`, `#/datasets`,
`#/trends`, `#/gpu`.

## Left to the builder
- npz vs a flat binary format if Pyodide loading is slow; Cache Storage details.
- The browser size limit's exact value after a timing check in Chromium.
- Tests: pytest (forest equality, export, portable loader, bundle), vitest (browser job, model
  route, admin 4th file), e2e (analyse_flow in the browser path with the demo model's export).
