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
| 2b | Does a measured model fit *my* limits? (fit scoring) | `configs/constraint_rules.yaml` | `agentmeter/session/constraints.py` | after the benchmark; re-evaluated on demand with the user's limits |
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

### Stage 2b: decision helper (fit scoring against the user's limits)

- **Inputs:** the measured numbers of each model (mean and p95 latency per flow, the
  total peak VRAM = loaded weights + working memory, cost per 1,000 flows) and the limits the user
  enters on the session's Recommendation tab (or passes in the query string of
  `GET /api/jobs/<id>/constraints` and `/report.pdf`).
- **Rules:** `configs/constraint_rules.yaml`, written in the same style as the other two
  rule files. `inputs` declares the limits the user can set (label, unit, default
  `null` = not set). Each rule has an `id`, a `description`, a measured `field`, an `op`
  (`le`, `lt`, `ge`, `gt`), the `limit` input it compares with, and a `note` template.

  | id | checks | field |
  |---|---|---|
  | `mean_latency` | mean latency per flow ≤ max mean latency | `measured.mean_latency_s` |
  | `p95_latency` | p95 latency per flow ≤ max p95 latency | `measured.p95_latency_s` |
  | `peak_vram` | **total** peak VRAM (weights + working memory) ≤ max total peak VRAM; a session without a total-peak record gives `no_data`, never `meets` | `measured.peak_vram_mb` (= `memory.total_peak_mb`) |
  | `cost_per_1k_flows` | cost per 1,000 flows ≤ budget | `measured.cost_per_1k_flows_usd` |

- **Results per model:**
  - **meets**: every limit that is set holds;
  - **fails**: one or more limits do not hold, and the failing rule ids are listed;
  - **no_data**: nothing failed, but a set limit had no measurement (VRAM on a CPU/mock
    run, or cost without a GPU price);
  - **no_constraints**: no limit was set.

  Unset limits are skipped and shown as `not_set`.
- **Outputs:** `session_results.json` → `decision_helper` is the evaluation with the rule
  file's defaults (none set). The Recommendation tab re-evaluates it with the user's
  limits, and the PDF's "Decision helper" section shows the limits that were in its
  download link.
- **Never changes a score, rank, SAW value or the verdict.** It reads a copy of the
  scored payload (`tests/test_phase46.py`, `tests/test_scored_keys_golden.py`).

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
  | `custom_licence` | the HF licence id is a custom or non-OSI one (`other`, `llama*`, `gemma`, `deepseek`, `qwen-research`, `tongyi-qianwen`, `cc-by-nc*`, the OpenRAIL family); the note states the licence id and advises checking its terms. OSI-approved licences (Apache-2.0, MIT, BSD, GPL, MPL …) and open content licences (CC-BY, CC0) never fire | huggingface |
  | `vram_headroom` | a peak-VRAM reading and the GPU's total VRAM both exist (the note reports the headroom) | measured, session_gpu, huggingface |
  | `model_age` | last modified > `stale_after_months` (12) before the fetch | huggingface |
  | `efficiency_consistent_with_size` | the more efficient model is also the smaller one; the note states this as co-occurrence, not as a cause | measured, huggingface |
  | `low_adoption` | downloads (last 30 days) < `low_downloads` (10 000) | huggingface |

- **Checking the rules against live metadata (no benchmark):**
  ```bash
  export HF_TOKEN=hf_...            # optional; used for gated models and rate limits, never printed
  python main.py stage3-check       # the 5 models of configs/run_full_l4.yaml
  python main.py stage3-check --models Qwen/Qwen2.5-7B-Instruct --json results/stage3_check.json
  ```
  It always fetches live, with no cache, and writes nothing unless you pass `--json`. It
  ignores `AGENTMETER_HF_METADATA`. Per model, it prints the fields the rules use
  (licence + `license_name`, gated, params, last_modified, downloads) and every rule's
  result: `fired` with its note, `not_fired` with the failed condition, or `no_data`
  with the reason. Without a benchmark, the measured rules (`vram_headroom`,
  `efficiency_consistent_with_size`) always report `no_data`. It exits 1 when no
  model's metadata could be fetched.
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

## Viewing a session

Every finished session has its own page in the app (`#/session/<id>`), with these tabs:

- **Summary:** the measured verdict, the per-model table, cost and energy, and caveats.
- **Detailed analysis:** statistical depth (p50/p95/p99, warm-up check, Cliff's delta
  and bootstrap CIs), plus the former dashboard's Overview and Detailed views. These are
  embedded from `/analysis?session=<id>&embed=1`, rendered from the session's results.
- **Agents:** the per-agent breakdown, agentic overhead, failures, and the per-flow
  timeline.
- **Recommendation:** the stage-2 verdict, the stage-2b decision helper and the stage-3
  model context.
- **Downloads:** the PDF, the results JSON, the CSVs and the prepared-set files.

On unlabelled input (PCAP), the accuracy panels are hidden and say "accuracy not measured
for unlabelled input". The locked Validation baseline is not in the app's navigation; it
stays reachable by direct URL at `/baseline`, and reproducing it is CLI-only
([REPRODUCE_BASELINE.md](REPRODUCE_BASELINE.md)).
