# AgentMeter — Data Dictionary

Plain-language reference for every table and column in AgentMeter's two SQLite
databases. Read this alongside the ER diagram. Units are given explicitly because
the columns store bare numbers (a value of `0.42` in `wall_time_s` means *0.42
seconds*).

AgentMeter stores everything in **one unified SQLite file**
(`results/agentmeter.db` by default). The tables fall into two groups — the
study *measurements* and the app *metadata* — and they are connected by real
foreign keys.

| Group | Tables | Written by |
|---|---|---|
| **Measurements** | `runs`, `scenario_results`, `agent_metrics` | `run-full` |
| **App metadata** | `session`, `weight_preset`, `note`, `tag`, `session_tag`, `hf_metadata_cache` | the web app / CRUD API |

(Legacy two-file databases are merged into this one file with
`python main.py combine-db`.)

All timestamps are **ISO-8601 UTC strings** (e.g. `2025-09-30T04:12:07+00:00`);
sorted as text they are also in chronological order.

---

## Measurement tables (in the unified `agentmeter.db`)

### `runs` — one row per benchmark execution
Primary key: `run_id`.

| Column | Type | Unit / values | Meaning |
|---|---|---|---|
| `run_id` | TEXT (PK) | id string | Unique id of one full benchmark execution. |
| `config_fingerprint` | TEXT | hash | Hash of the config used, so a resumed run cannot silently mix settings (reproducibility guard). |
| `quant_setting` | TEXT | e.g. `4-bit NF4` | Quantisation the models were loaded with. |
| `hardware_label` | TEXT | e.g. `NVIDIA L4` | GPU / hardware the run executed on. |
| `started_at` | TEXT | ISO-8601 UTC | When the run began. |
| `finished_at` | TEXT | ISO-8601 UTC / NULL | When the run ended; NULL while still running. |
| `status` | TEXT | `running` \| `complete` \| `abandoned` | Lifecycle state of the run. |
| `notes` | TEXT | free text | Optional notes. |

### `scenario_results` — one row per (model × scenario): the verdict + totals
Primary key: `(run_id, model, scenario_id)`. Foreign key: `run_id → runs.run_id`.

| Column | Type | Unit / values | Meaning |
|---|---|---|---|
| `run_id` | TEXT | → `runs` | Which run this belongs to. |
| `model` | TEXT | HF model id | Which model produced this result (e.g. `meta-llama/Meta-Llama-3-8B-Instruct`). |
| `scenario_id` | TEXT | id string | Which dataset scenario (one network-flow case) was evaluated. |
| `predicted_label` | TEXT | attack class | The class the 4-agent pipeline decided on. |
| `held_out_label` | TEXT | attack class | The **ground-truth** label, hidden from the model during evaluation. |
| `correct` | INTEGER | `1` = correct, `0` = wrong | Whether `predicted_label` matched `held_out_label`. |
| `scenario_total_time_s` | REAL | **seconds** | Total wall-clock time for the whole scenario (all 4 agents). |
| `scenario_peak_vram_mb` | REAL | **megabytes** | Peak VRAM during the scenario — the *maximum* across agents, not their sum. |
| `status` | TEXT | `complete` | Row completion marker (written atomically). |
| `completed_at` | TEXT | ISO-8601 UTC | When this scenario finished. |

### `agent_metrics` — one row per (model × scenario × agent): per-agent cost
Primary key: `(run_id, model, scenario_id, agent_name)`.
Foreign keys: `run_id → runs.run_id`, and `(run_id, model, scenario_id) → scenario_results` (checked at commit).

| Column | Type | Unit / values | Meaning |
|---|---|---|---|
| `run_id` | TEXT | → `runs` | Which run. |
| `model` | TEXT | HF model id | Which model. |
| `scenario_id` | TEXT | id string | Which scenario. |
| `agent_name` | TEXT | `perceive` \| `reason` \| `decide` \| `act` | Which of the 4 pipeline agents this row measures. |
| `wall_time_s` | REAL | **seconds** | Wall-clock time for this agent's step. |
| `ttft_s` | REAL | **seconds** / NULL | Time to first token; NULL on mock/CPU runs (never fabricated). |
| `vram_delta_mb` | REAL | **megabytes** / NULL | VRAM increase attributable to this agent; NULL on CPU. |
| `input_tokens` | INTEGER | count | Tokens in the prompt this agent sent. |
| `output_tokens` | INTEGER | count | Tokens this agent generated. |

**Reading it as a whole:** `runs` 1─< `scenario_results` 1─< `agent_metrics`. One run
has many scenario results; each scenario result is produced by four agent rows.

---

## App-metadata tables (in the unified `agentmeter.db`)

### `session` — a saved analysis view in the dashboard
Primary key: `id`. Foreign key: `preset_id → weight_preset.id` (`ON DELETE SET NULL`).

| Column | Type | Unit / values | Meaning |
|---|---|---|---|
| `id` | INTEGER (PK) | autoincrement | Session id. |
| `name` | TEXT | free text | User-given name for the saved session. |
| `created_at` / `updated_at` | TEXT | ISO-8601 UTC | Create / last-update time. |
| `source_filename` | TEXT | filename | The analysis file this session was imported from. |
| `summary` | TEXT | JSON | Small extracted summary (top model, tiers…) — read-only projection of the analysis. |
| `analysis_ref` | TEXT | path/name | Original reference of the imported analysis. |
| `analysis_json` | TEXT | JSON | Full stored copy of the imported analysis (read-only source of truth). |
| `source_run_id` | TEXT | → `runs.run_id` (FK) | The study run this analysis came from; a real FK (`ON DELETE SET NULL`), nullable. Stored as NULL if the analysis refers to a run not present in the file. |
| `preset_id` | INTEGER | → `weight_preset.id` / NULL | Which saved weighting the session was scored with. |

### `weight_preset` — saved SAW scoring weight sets
Primary key: `id`.

| Column | Type | Unit / values | Meaning |
|---|---|---|---|
| `id` | INTEGER (PK) | autoincrement | Preset id. |
| `name` | TEXT | free text | Preset name (e.g. `accuracy_heavy`). |
| `w_accuracy` | REAL | weight ≥ 0 | Weight on accuracy in the composite score. |
| `w_latency` | REAL | weight ≥ 0 | Weight on latency. |
| `w_vram` | REAL | weight ≥ 0 | Weight on VRAM. |
| `w_tokens` | REAL | weight ≥ 0 | Weight on token cost. |
| `created_at` / `updated_at` | TEXT | ISO-8601 UTC | Timestamps. |
| `is_builtin` | INTEGER | `1` = built-in (read-only), `0` = user | Built-in presets are load-only; they cannot be edited or deleted. |

### `note` — free-text notes attached to a session
Primary key: `id`. Foreign key: `session_id → session.id` (`ON DELETE CASCADE`).

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER (PK) | Note id. |
| `session_id` | INTEGER | Which session the note belongs to. |
| `body` | TEXT | The note text. |
| `created_at` / `updated_at` | TEXT | Timestamps (ISO-8601 UTC). |

### `tag` — labels for organising sessions
Primary key: `id`. `name` is unique (case-insensitive).

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER (PK) | Tag id. |
| `name` | TEXT (unique, nocase) | Tag label. |
| `created_at` | TEXT | Timestamp. |

### `session_tag` — which tags are on which sessions (many-to-many)
Primary key: `(session_id, tag_id)`. FKs to `session` and `tag` (both `ON DELETE CASCADE`).

| Column | Type | Meaning |
|---|---|---|
| `session_id` | INTEGER | → `session.id`. |
| `tag_id` | INTEGER | → `tag.id`. |
| `created_at` | TEXT | When the tag was attached. |

### `hf_metadata_cache` — cached Hugging Face Hub metadata (external context)
Primary key: `model_id`. Hybrid relational + document: `payload` is the source of
truth; the other columns are a queryable projection refreshed on every write.

| Column | Type | Unit / values | Meaning |
|---|---|---|---|
| `model_id` | TEXT (PK) | HF model id | The model this cached metadata describes. |
| `fetched_at` | REAL | Unix epoch seconds | When the metadata was fetched. |
| `status` | TEXT | `ok` \| `unavailable` | Whether the fetch succeeded. |
| `payload` | TEXT | JSON | Full cleaned metadata dict (source of truth). |
| `params_b` | REAL | billions of params | Model size, promoted from payload. |
| `downloads` | INTEGER | count | HF download count. |
| `likes` | INTEGER | count | HF likes. |
| `license` | TEXT | e.g. `apache-2.0` | Model licence. |
| `pipeline_tag` | TEXT | e.g. `text-generation` | HF task tag. |

---

## Relationships at a glance

- **Measurements:** `runs` 1─< `scenario_results` 1─< `agent_metrics` (linked by
  `run_id`, and the composite `(run_id, model, scenario_id)`).
- **App metadata:** `weight_preset` 1─< `session` 1─< `note`; `session` N─M `tag`
  via `session_tag`.
- **Link between the groups (real FK):** `session.source_run_id` → `runs.run_id`
  (`ON DELETE SET NULL`; nullable). `hf_metadata_cache.model_id` matches a `model`
  name but is a documented lookup, not a DB FK. Run `python main.py check-integrity`
  to verify these on demand.
