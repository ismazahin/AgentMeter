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
└── util/               ← UTILITIES
    ├── envtools.py        Load tokens from .env (names only, never values).
    ├── env_check.py       Environment / dependency check (`check-env`).
    └── vast_shutdown.py   Vast.ai instance self-destroy (cost safety).
```

## Where the databases live

The Python **code** that manages a DB is under `db/`, but the actual **`.db` files**
live under `results/` (path set by `storage.sqlite_path` in the config, not hard-coded):

| Code file | Database file | Role |
|---|---|---|
| `db/storage.py` | `results/agentmeter_full_l4.db` | **Locked study** results (read-only baseline) |
| `db/storage.py` | `results/user_runs/<name>.db` | Custom/user runs (flagged `non_validated`) |
| `db/appdb.py`   | `results/agentmeter_app.db`   | App metadata: sessions, presets, notes, tags, HF cache |

`.db` files are gitignored — they live on your machine, not in the repo.

## Relationships (see the ER diagram)

- **Study DB:** `runs` 1─<`scenario_results` and `runs` 1─<`agent_metrics` (FK `run_id`);
  `scenario_results` 1─<`agent_metrics` on the composite key `(run_id, model, scenario_id)` —
  enforced at the application level, atomic per scenario (both tables written in one transaction),
  not a DB-level FK. The study DB schema is **locked** and never migrated.
- **App DB:** `weight_preset` 1─<`session` (FK `session.preset_id`, nullable, `ON DELETE SET NULL` —
  records which saved weighting a session was scored with); `session` 1─<`note`; `session` N─M `tag`
  via `session_tag`. `session.source_run_id` is a **weak, unenforced** reference to a `runs.run_id` in
  the *separate* study DB, recorded from an imported analysis' `run_ids`; it may be NULL and may point
  at a run not present locally. `hf_metadata_cache.model_id` is likewise a **weak, unenforced** lookup
  key matching a benchmarked model name — not a DB FK. The app layer never opens the study DB to
  resolve either cross-DB reference.
