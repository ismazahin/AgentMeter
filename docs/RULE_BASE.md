# The rule-base: three stages

AgentMeter **measures LLM resource efficiency** (latency, VRAM, tokens, and accuracy
when the input is labelled). It is not a threat-detection product. Every decision it
makes about inputs and results comes from explicit, declared rules that you can read.
They run in three stages. Each stage reads only the output of the stage before it, and
no later stage can change an earlier stage's result.

| stage | question | rules live in | engine | runs |
|---|---|---|---|---|
| 1 | Which flows go to the models? | `configs/flow_rules.yaml` | `agentmeter/ingest/rules.py` | Prepare (before any model runs) |
| 2 | Which model is more efficient? | `config.yaml` → `scoring:`, plus the fixed dominance rule | `agentmeter/session/scoring.py`, `agentmeter/session/relative.py`, `agentmeter/session/brief.py` | after the benchmark |
| 3 | What should a user know about these models beyond what was measured? | `configs/recommendation_rules.yaml` | `agentmeter/session/stage3.py` | after stage 2, same job |

## Stage 1: flow selection (input layer)

- **Inputs:** the candidate pool of flows from an uploaded CSV or PCAP (already bounded
  and sampled across the whole file; see [INPUT_LAYER.md](INPUT_LAYER.md)).
- **Rules:** ordered instances of a fixed set of operator types (`per_group`,
  `top_percentile`, `threshold`, `typical_band`, `random_fill`) and the
  `class_balance` constraint (labelled CSV only). Each rule has a quota and a reserve,
  and the whole set shares a budget (`max_flows`, ≤ 500). They use only statistical
  flow properties. "Notable" means statistically unusual in this capture. It never
  means "malicious".
- **Outputs:** the selected flows (`flows.csv`) and `selection_audit.json`. The audit
  lists every rule, whether it fired, and how many flows it admitted. This is shown in
  the service's Prepare summary and in the PDF ("Flow selection (rule-base)").

## Stage 2: scoring and the measured verdict

- **Inputs:** the per-flow measurements the 4-agent pipeline recorded for each model.
- **Rules:**
  - *Absolute SAW* (`config.yaml` → `scoring:`): Direct-Rating weights (declared
    subjective), fixed targets from the L4 study, the Healthy/Degraded/Critical tier
    cut-offs, and the sensitivity weight sets.
  - *Head-to-head dominance rule* (`relative.py`, 2-model sessions): for each metric,
    a winner or a tie (within 2 %, or 2 pp for accuracy). Latency and VRAM winners must
    also be significant (Kruskal-Wallis p < 0.05). One model wins ≥ 1 efficiency metric
    and loses none → "more resource-efficient". Each wins one → "mixed". Otherwise →
    "tie". Accuracy is reported beside the verdict and is never folded into it.
  - *Recommendation text* (`brief.py`, a line-for-line mirror of `dashboard/report.js`,
    checked by a parity test).
- **Outputs:** `session_results.json` keys `per_model`, `phase8` (SAW table, ranks,
  tiers), `sensitivity`, `statistics`, `comparison`, `relative_comparison` (with the
  verdict), and `phase7`/`per_class` for labelled runs.

## Stage 3: recommendation context (external model metadata)

- **Inputs:**
  - the stage-2 results, read from a **copy** (`measured.*` facts: peak VRAM, latency,
    whether this model is the efficiency winner, and the verdict);
  - the GPU the session ran on (`env.*`);
  - public model metadata from the Hugging Face Hub API
    (`https://huggingface.co/api/models/<id>`) for the session's 1–2 models: gated,
    licence, parameter count, downloads, likes, last modified (`hf.*`). It is fetched
    through `agentmeter/server/hf_metadata.py`, which caches results in the app
    database and works offline. `AGENTMETER_HF_METADATA=off` disables the network
    call, and the tests run with it off.
  - derived facts: VRAM headroom, age in months, the other model's size, and whether
    this model is the smaller one.
- **Rules:** `configs/recommendation_rules.yaml`. Each rule has an `id`, a
  `description`, `when` (a list of conditions that must all hold), a `note` template and
  its `sources`. Rules are evaluated once per model. Operators: `present`, `truthy`,
  `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `in`, `not_in`, `matches`. `$name` refers to
  `constants`. When a field is missing (metadata unavailable, or no VRAM reading on a
  CPU/mock run), the result is **no_data**, never a silent "no".

  | id | fires when | source |
  |---|---|---|
  | `gated_access` | the model is gated on HF | huggingface |
  | `restrictive_licence` | the licence matches the non-commercial/custom-licence pattern | huggingface |
  | `vram_headroom` | a peak-VRAM reading and the GPU's total VRAM both exist (the note reports the headroom) | measured, session_gpu, huggingface |
  | `model_age` | last modified > `stale_after_months` (12) before the fetch | huggingface |
  | `efficiency_consistent_with_size` | this model won on efficiency and is also the smaller model | measured, huggingface |
  | `low_adoption` | downloads (last 30 days) < `low_downloads` (10 000) | huggingface |

- **Outputs**, in `session_results.json`:
  - `model_context`: per model, the metadata fields used, `source_url` and
    `fetched_at`, or `status: "unavailable"` with a reason;
  - `recommendation_stage3`: the measured head-to-head verdict, quoted unchanged,
    then the fired notes (rule id, model, note, sources, URL, fetched_at), then an
    audit row for every rule × model (condition, `fired` / `not_fired` / `no_data`,
    detail).

  The service results page shows these as the "Recommendation — stage 3" card. The
  PDF shows the verdict first, then the notes and a **Rules fired** table (rule,
  model, condition, result, source, fetched at). The session analysis view shows them
  in its session panel.
- **Hard rule:** stage 3 never changes a score, a rank, a SAW value or the verdict. It
  runs after they are fixed and only reads a deep copy of them. If metadata is
  unavailable, or a rule file is broken, the result records the reason and the job
  still finishes. `tests/test_stage3.py` builds the same session with the metadata
  present, absent, altered and garbage. It asserts that every scored key
  (`per_model`, `comparison`, `relative_comparison`, `phase8`, `sensitivity`,
  `statistics`, `phase7`, `per_agent`, …) is identical in all four cases.

## Viewing a session's full analysis

After a benchmark job finishes, the Benchmark page lists it with **View full
analysis**. The link opens `/?session=<job id>`, which is the same Overview + Detailed
analysis page as the Validation baseline, rendered from that job's
`session_results.json`. It is labelled **Session analysis (non-validated)** and shows
the sample size, models, input, hardware, stage-3 notes and caveats. On unlabelled input
(PCAP), the accuracy panels are hidden and say "accuracy not measured for unlabelled
input". The Validation baseline (`/` with no `session`) is unchanged.
