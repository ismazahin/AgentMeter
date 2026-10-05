# `agentmeter/` — package layout

Modules are grouped into subpackages **by role**, so it is clear at a glance which
file does what. Behaviour is unchanged — this is organisation only. Every module is
still importable as `from agentmeter import <name>` (re-exported in `__init__.py`).

```
agentmeter/
├── config.py            Configuration loader (reads config .yaml). Used everywhere.
│
├── db/                  ← DATABASES (the .db files live under results/)
│   ├── storage.py         Study/run RESULTS DB — tables runs, scenario_results,
│   │                      agent_metrics. Written during `run-full`.
│   └── appdb.py           APP-METADATA DB (results/agentmeter_app.db): sessions,
│                          weight presets, notes, tags, HF-metadata cache. SEPARATE
│                          from the study DB — never opens the locked study DB.
│
├── data/                ← DATASET
│   ├── dataset.py         Loads scenarios, strips the label (data isolation), builds
│   │                      the feature-only prompt the model sees.
│   └── dataprep.py        `build-dataset`: makes the balanced 300-scenario CSV from
│                          the raw CIC-IDS2017 files.
│
├── pipeline/            ← THE 4-AGENT PROCESS (Perceive → Reason → Decide → Act)
│   ├── pipeline.py        The linear agent chain (LangGraph).
│   ├── agents.py          The four agents + registry.
│   └── instrument.py      Per-agent instrumentation: wall time, TTFT, VRAM, tokens.
│
├── providers/           ← MODEL BACKENDS
│   ├── base.py            Provider interface.
│   ├── mock.py            Mock provider (CPU, no GPU) — for dev/tests.
│   └── hf.py              Hugging Face in-process provider (real LLM on GPU).
│
├── run/                 ← EXECUTION (running the benchmark)
│   ├── runner.py          `run-full` orchestrator: sequential per-model, resumable.
│   ├── worker.py          One model per subprocess (clean VRAM isolation).
│   ├── pilot.py           Pilot run (1 model, few scenarios).
│   └── measure.py         `measure-vram`: per-model weight footprint (load-only).
│
├── analysis/            ← ANALYSIS (read-only over a results DB → analysis.json)
│   ├── analyze.py         Phase 7 accuracy + Phase 8 SAW + sensitivity + stats
│   │                      (Kruskal-Wallis/Dunn) + provenance (validated vs user run).
│   ├── analyze_by_class.py  Per-attack-class resource breakdown (additive).
│   └── analyze_advanced.py  Pareto, CoV, prefill/decode, throughput, misclass (additive).
│
├── server/             ← WEB / API layer (Flask; served by scripts/pull_eval_server.py)
│   ├── crud_api.py        REST routes for sessions/presets/notes/tags (over appdb).
│   ├── pull_eval.py       Pull ONE extra HF model + on-demand eval (separate DB).
│   ├── local_sessions.py  Read-only discovery of result JSON under results/.
│   ├── hf_metadata.py     Hugging Face Hub metadata (external CONTEXT only, cached).
│   └── config_builder.py  Build a user config → configs/user/ (never the locked one).
│
├── ingest/             ← PCAP INPUT LAYER (service pivot; optional deps, not re-exported)
│   ├── pcap.py            Validate a .pcap/.pcapng upload + capture stats.
│   ├── flows.py           CICFlowMeter (Python port) → 78 CIC-IDS2017 feature columns.
│   ├── feature_map.py     Explicit CIC-IDS2017 ↔ extractor mapping + gap report.
│   ├── rules.py           Rule engine: bounded representative flow selection + audit.
│   ├── run.py             validate → extract → select → results/pcap_runs/<name>/.
│   └── sample.py          Deterministic synthetic capture for tests/demos.
│
└── util/               ← UTILITIES
    ├── envtools.py        Load tokens from .env (names only, never values).
    ├── env_check.py       Environment / dependency check (`check-env`).
    └── vast_shutdown.py   Vast.ai instance self-destroy (cost safety).
```

## Where the database lives

**Phase 26: one unified SQLite file** holds every table (path set by
`storage.sqlite_path` in the config, default `results/agentmeter.db`). The study
results tables and the app-metadata tables now live together, so the whole schema
is one connected database — one file to open, back up and diagram.

| Code file | Tables (all in the ONE file) |
|---|---|
| `db/storage.py` | `runs`, `scenario_results`, `agent_metrics` (the measurements) |
| `db/appdb.py`   | `session`, `weight_preset`, `note`, `tag`, `session_tag`, `hf_metadata_cache` (app metadata) |
| `db/combine.py` | `combine-db`: merge a legacy study DB + app DB into one unified file |

Custom/user runs may still be written to their own `results/user_runs/<name>.db`,
and the on-demand model **pull** writes its own `results/pulls/…` file. `.db` files
are gitignored — they live on your machine, not in the repo.

**Migrating from the old two-file layout:** copy your files as backups, then
`python main.py combine-db --study results/agentmeter_full_l4.db --app results/agentmeter_app.db --out results/agentmeter.db`.
The sources are not modified; ids are preserved so all foreign keys survive.

## Relationships (see the ER diagram)

- **Measurements:** `runs` 1─<`scenario_results` and `runs` 1─<`agent_metrics` (FK `run_id`);
  `agent_metrics` 1─<`scenario_results` on the composite `(run_id, model, scenario_id)`
  (`DEFERRABLE INITIALLY DEFERRED`, checked at commit — both tables are written in one atomic
  transaction per scenario).
- **App metadata:** `weight_preset` 1─<`session` (FK `session.preset_id`, nullable, `ON DELETE SET NULL`);
  `session` 1─<`note`; `session` N─M `tag` via `session_tag`.
- **Link between the two groups (now a real FK):** `session.source_run_id` → `runs.run_id`
  (`ON DELETE SET NULL`), recorded from an imported analysis' `run_ids`. It is nullable; if the analysis
  refers to a run not present in the file, it is stored as NULL rather than rejected.
- `hf_metadata_cache.model_id` matches a benchmarked `model` name. It is **not** a DB FK (no single-column
  unique `model` parent exists), so it stays a documented lookup — `check-integrity` verifies coverage.

### Design notes (be ready to defend these)

- **`model` is stored as a repeated string, not a lookup table — on purpose.** The study is a fixed set
  of models over an immutable dataset; rows are written once and **never updated**, so the classic update
  anomaly cannot arise. The per-model lookup is provided *virtually* by `check-integrity` and by
  `hf_metadata_cache`.
- **Foreign keys.** `PRAGMA foreign_keys = ON`; `run_id` is a real enforced FK from both child tables to
  `runs`, and the composite `agent_metrics → scenario_results` FK is deferred to commit. Real FKs also
  enforce `session.preset_id` and `session.source_run_id`. The file uses **WAL** journalling so a reader
  (the dashboard) and a writer proceed without blocking.
- **Cross-reference verification.** Run `python main.py check-integrity` (read-only) to report any
  `session.source_run_id`/`preset_id` that doesn't resolve and per-model HF-metadata coverage.
- **`hf_metadata_cache` is hybrid relational + document.** The full cleaned metadata stays in the
  `payload` JSON (source of truth), and the fields worth querying (`params_b`, `downloads`, `likes`,
  `license`, `pipeline_tag`) are **promoted into typed columns** refreshed on every write, so they can be
  filtered/sorted in SQL. `analysis_json`/`summary` on `session` follow the same principle.
- **Timestamps** are ISO-8601 UTC strings (`datetime.now(timezone.utc).isoformat(...)`), sorted with
  SQLite's `datetime()` — lexicographic order equals chronological order.
