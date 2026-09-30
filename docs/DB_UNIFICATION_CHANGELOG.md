# AgentMeter — database work changelog (context handoff)

This document is a complete record of the database-layer changes made across
Phases 23–26, so another Claude Code session (or a reviewer) has full context.

**Branch:** `claude/cool-ride-mitmzl` (all work pushed here).
**Repo default branch:** `claude/agentmeter-phases-0-5-3ftnmx`.
**Test status:** `python -m pytest` → **177 passed**.

---

## 1. The headline change

AgentMeter used to store data in **two separate SQLite files**:

- `results/agentmeter_full_l4.db` — the study **measurements** (the thesis numbers).
- `results/agentmeter_app.db` — the app/dashboard **metadata**.

It now uses **one unified SQLite file** — `results/agentmeter.db` — holding all
nine tables in one connected schema with real foreign keys. The old two-file
layout is fully migrated with a new `combine-db` command; the two originals are
never modified (kept as backups).

The measurements themselves are unchanged; analysis is read-only and produces
identical numbers.

---

## 2. Schema — the nine tables (all in `results/agentmeter.db`)

**Measurement tables** (written by `run-full`, via `db/storage.py`):

- `runs` — PK `run_id`.
- `scenario_results` — PK `(run_id, model, scenario_id)`; FK `run_id → runs`.
- `agent_metrics` — PK `(run_id, model, scenario_id, agent_name)`;
  FK `run_id → runs`; **composite FK `(run_id, model, scenario_id) → scenario_results`,
  `DEFERRABLE INITIALLY DEFERRED`** (checked at commit — the four agent rows and the
  scenario_results parent are written in one atomic transaction, agent rows first).
- Secondary indexes `idx_sr_model`, `idx_am_model` (per-model query speed).

**App-metadata tables** (written by the dashboard, via `db/appdb.py`):

- `session` — PK `id`; **FK `preset_id → weight_preset(id)` ON DELETE SET NULL**;
  **FK `source_run_id → runs(run_id)` ON DELETE SET NULL** (this is the FK that ties
  the two groups into one schema).
- `weight_preset` — PK `id`; 4 built-in presets seeded read-only.
- `note` — PK `id`; FK `session_id → session(id)` ON DELETE CASCADE.
- `tag` — PK `id`; `name` unique (case-insensitive).
- `session_tag` — PK `(session_id, tag_id)`; FKs to `session` and `tag`.
- `hf_metadata_cache` — PK `model_id`; JSON `payload` is the source of truth, plus
  **promoted queryable columns** `params_b, downloads, likes, license, pipeline_tag`
  (re-projected on every write; backfilled from payload on migration).

**The one non-FK link:** `hf_metadata_cache.model_id` matches a benchmarked `model`
name but is NOT a DB foreign key (there is no single-column unique `model` parent).
It stays a documented lookup, verified by `check-integrity`.

All timestamps are ISO-8601 UTC strings. `PRAGMA foreign_keys = ON`. The file uses
**WAL** journalling so the dashboard can read while a run writes; both stores
checkpoint the WAL on `close()`.

---

## 3. New CLI commands (`main.py`)

- **`python main.py combine-db --study <study.db> --app <app.db> --out <unified.db> [--overwrite]`**
  Merge a legacy study DB + app DB into one unified file. Preserves every id (so all
  FKs survive), sets a dangling `source_run_id` to NULL (it is a real FK now), does
  not duplicate built-in presets, and runs `PRAGMA foreign_key_check` (aborts if it
  fails). **Never modifies the two sources.**

- **`python main.py check-integrity [--db <study>] [--app-db <app>]`**
  Read-only. Reports any `session.source_run_id`/`preset_id` that doesn't resolve and
  per-model HF-metadata coverage. Defaults to the unified/known study DB. Exit 0 when
  clean, 1 on a hard error (a dangling `preset_id`).

---

## 4. Behaviour changes to be aware of

- **`session.source_run_id` is now a real FK.** On import, if the analysis references
  a run not present in the DB, `create_session` stores NULL rather than rejecting the
  import (degrade-to-null). Previously it was an unenforced "weak" string.
- **The AppStore locked-DB guard was removed.** `AppStore` may now open the same file
  as the study tables, and it ensures BOTH schemas exist in the file (study first, so
  the `source_run_id` FK is valid).
- **Analysis provenance now treats `results/agentmeter.db` as validated** (alongside
  the historical `agentmeter_full_l4.db`). Provenance is a *label only* — it is
  appended last and never enters any calculation; analysis opens the DB read-only.
  User-run and pull DBs remain `non_validated`.
- **Default DB path** (`appdb.DEFAULT_APP_DB`) is now `results/agentmeter.db`.

---

## 5. Files changed

**New files**
- `agentmeter/db/combine.py` — the merge logic (`combine_databases`).
- `agentmeter/analysis/integrity.py` — the read-only cross-reference checker.
- `docs/DATA_DICTIONARY.md` — every table/column/type/unit/meaning.
- `docs/dbeaver_readable_views.sql` — readable SELECTs for browsing in DBeaver.
- `docs/fix_preset_fk.sql` — one-off: add the real `preset_id` FK to an old migrated
  app DB (superseded by `combine-db`, kept for reference).
- `tests/test_combine.py`, `tests/test_integrity.py`.

**Modified**
- `agentmeter/db/appdb.py` — unified schema, guard removed, WAL, real `source_run_id`
  FK + degrade-to-null, HF promoted columns + migration/backfill, `preset_id` FK,
  `set_session_preset`, `_run_exists`, checkpoint on close.
- `agentmeter/db/storage.py` — composite FK, model indexes, `SCHEMA` alias, WAL,
  checkpoint on close.
- `agentmeter/analysis/analyze.py` — provenance validated-set includes the unified DB.
- `agentmeter/server/crud_api.py` — `POST /sessions` accepts `preset_id`;
  `PATCH /sessions/<id>` handles `name` and/or `preset_id`.
- `main.py` — `combine-db` and `check-integrity` subcommands.
- `agentmeter/__init__.py` — re-export `combine`, `integrity`.
- `agentmeter/README.md`, `docs/DATA_DICTIONARY.md` — single-file model.
- `tests/test_crud.py`, `tests/test_integrity.py` — updated for the unified model.

---

## 6. What was NOT changed (invariants)

- The study **measurements are byte-for-byte unchanged**; no re-run, no recompute.
- Analysis is **read-only** (`sqlite3.connect("file:...?mode=ro")`); it cannot alter
  the DB. Re-running analysis gives the same accuracy/latency/VRAM/SAW numbers.
- `pull_eval` still isolates a pulled model into its own `results/pulls/*.db`, and
  `config_builder` still refuses to overwrite the locked study *config* — both left
  intact.

---

## 7. Migration already performed (on the user's machine)

```
copy results\agentmeter_full_l4.db results\agentmeter_full_l4.backup.db
copy results\agentmeter_app.db     results\agentmeter_app.backup.db
python main.py combine-db --study results\agentmeter_full_l4.db --app results\agentmeter_app.db --out results\agentmeter.db
python main.py check-integrity --db results\agentmeter.db --app-db results\agentmeter.db   # RESULT: OK
```
Result: `results/agentmeter.db` = 1 run, 1500 scenario_results, 6000 agent_metrics,
5 hf_metadata_cache, 2 session, 4 weight_preset — full 5-model × 300-scenario study.

**Remaining action for the user:** point the config's `storage.sqlite_path` at
`results/agentmeter.db` so the app + analysis use the one file going forward.

---

## 8. Commit list (this arc, newest first)

- `8cf073f` Phase 26: treat the unified agentmeter.db as the validated study baseline
- `7c69b62` Phase 26: unify the study and app databases into one SQLite file
- `ad55fd2` docs: add script to add the real preset_id FK to migrated app DBs
- `ddd85a5` docs: add data dictionary + readable DBeaver queries
- `82198c7` Phase 25: check-integrity defaults to the locked study DB when present
- `809f066` Phase 25: DB integrity hardening — composite FK, HF hybrid columns, checker
- `03dfd98` Phase 24: connect weight_preset to session via nullable preset_id FK
- `36877db` Phase 23: connected ERD + safe cross-DB reference (session.source_run_id)

## 9. ER diagram (not in the repo)

Published artifact (single unified database, 9 tables, real FKs):
https://claude.ai/artifact/KqT6KqfNC2AR5Ftz9oEMYz
