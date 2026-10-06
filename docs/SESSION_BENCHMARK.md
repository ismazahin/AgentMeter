# Benchmark sessions: user input → 4-agent pipeline → comparison

A **session** takes a prepared user run (from `scripts/ingest.py`, see
[INPUT_LAYER.md](INPUT_LAYER.md)) and runs its selected flows through the
**existing** instrumented 4-agent pipeline for **1 or 2 models**. It then scores
and compares them. It measures LLM resource efficiency, plus accuracy where labels
exist. It is not a detection product.

```
results/csv_runs/<name>/  (or pcap_runs/<name>/)
  input.json, selected_flows.csv, labels.csv        ← from the input layer
  session_scenarios.csv   flow_id (hidden) + model-visible features + held-out label
  session_config.yaml     base config + per-session overrides (models, classes, DB path)
  session.db              this session's runs / scenario_results / agent_metrics
  session_results.json    per-model metrics, SAW, accuracy, the comparison, caveats
```

Nothing is written to the locked study DB (`results/agentmeter_full_l4.db`). A
session refuses to start if its DB path resolves to a validated study DB, and its
provenance is always `non_validated`.

## What is reused (not re-implemented)

| Step | Existing code |
|---|---|
| Sequential execution, one subprocess per model, resume, GPU guard | `run/runner.run_full` → `run/worker` (`--mode sqlite`) |
| Loading flows with label isolation | `data/dataset.DatasetLoader` (label and `flow_id` are hidden columns) |
| The 4 agents and the instrumentation | `Pipeline`, `AGENT_REGISTRY`, `MetricsCollector`, `make_instrumented_hook`, `GpuProbe` |
| The model | `providers.get_provider` (mock, or hf with 4-bit NF4 from the config) |
| Accuracy and confusion | `analyze.phase7(sr, classes)` |
| SAW | `analyze._criteria` → `phase8` (`_normalise`, `_composite`, `_tier`), `sensitivity` |
| Per-agent diagnostics, statistics, provenance | `analyze.per_agent`, `analyze.statistics`, `analyze._provenance` |

**6-class runs.** The Decide agent and `normalize_class` read `classes` from the
config. A 6-class run's session config therefore lists the 5 study classes plus
`"Other Attack"`, so Decide offers it and the parser accepts it. Accuracy and
confusion are computed over those 6 classes. A 5-class run's config uses the base
config's classes unchanged, so its prompts and parsing are exactly as before.

**Scoring.**
- **Full SAW:** uses the config weights. Labelled runs only.
- **Efficiency-only SAW:** always computed, using the same targets, tiers and maths
  with accuracy weighted 0 and the other weights renormalised. It decides "which
  model is more resource-efficient", and it is the headline score for unlabelled
  (PCAP) runs.
- **Ties:** metrics within 1%, and composites within 0.5 points, are reported as
  ties.

**Relative (head-to-head) comparison.** This is added beside the absolute SAW,
which is unchanged. The absolute SAW scores each model against fixed targets from
the L4 study, so two models that both beat a target both score the maximum on it.
They can then tie even when one is clearly better. `relative_comparison` ranks the
two models directly against each other, using the same measured metrics:

| Metric | Better | Reported as | Tie when |
|---|---|---|---|
| Mean end-to-end latency per flow | lower | % lower than the other model, and a ratio | within 2%, **or** not significant (per-flow Kruskal-Wallis p ≥ 0.05) |
| Mean peak working VRAM | lower | % less, and a ratio | within 2%, or not significant; "not available" on CPU/mock |
| Mean tokens per flow | lower | % fewer, and a ratio | within 2% |
| Accuracy (labelled runs only) | higher | percentage-point difference | within 2 percentage points |

The significance check reuses the session's existing statistics. A gap that is only
timing noise is reported as a tie together with its p-value, so it never produces a
false winner.

- **Efficiency verdict (dominance rule, no weights):**
  - a model that wins at least one efficiency metric and loses none is "more
    resource-efficient";
  - if each model wins at least one, the verdict is "mixed" and the trade-off is
    written out;
  - otherwise it is a tie.
- **Accuracy** is reported beside the efficiency verdict, never folded into it. A
  "faster but less accurate" trade-off is stated explicitly.
- **Example verdict:** *"A is more resource-efficient than B: 50.0% lower latency,
  20.0% less VRAM, 10.0% fewer tokens per flow. Accuracy: A is higher by 10.0
  percentage points (A 80.0% vs B 70.0%)."*
- **Caveats:** the relative view adds two notes to the session caveats.
  - Both models ran on the same hardware and the same flows, so the comparison is
    like-for-like.
  - On non-L4 hardware such as an A100, absolute numbers aren't comparable with the
    locked study. Only the A-vs-B relation is.

`session_results.json` keeps the dashboard's `analysis.json` keys (`phase7` for
labelled runs only, `phase8`, `sensitivity`, `per_agent`, `statistics`,
`provenance`). It adds `session`, `per_model`, `comparison` (absolute) and
`relative_comparison` (head-to-head, `null` unless there are 2 models), plus caveats.

## CPU smoke test (mock provider)

```
python scripts/ingest.py data/sample_csv/cicids2017_sample.csv --max-flows 15
python scripts/benchmark.py results/csv_runs/cicids2017_sample --models mock-a mock-b --provider mock
```

## Running real models on Google Colab (A100)

1. **Runtime.** Choose Runtime → Change runtime type → **A100 GPU**.
2. **Install.**
   ```
   !git clone -b claude/cool-ride-mitmzl https://github.com/ismazahin/agentmeter.git
   %cd agentmeter
   !pip install -q -r requirements.txt -r requirements-gpu.txt
   # PCAP uploads only:
   !pip install -q -r requirements-pcap.txt && pip install -q --no-deps cicflowmeter==0.2.0
   ```
3. **Hugging Face token.** Only gated models (Llama 3, Gemma 2) need one. Accept each
   model's licence on its Hub page first, then:
   ```python
   import os
   from google.colab import userdata
   os.environ["HF_TOKEN"] = userdata.get("HF_TOKEN")   # Colab Secrets
   ```
4. **Prepare the input.** Upload the CSV or PCAP, or mount Drive, then:
   ```
   !python scripts/ingest.py /content/Tuesday-WorkingHours.pcap_ISCX.csv --max-flows 100 --other-attack
   ```
5. **Benchmark two models.** Execution is sequential, one model fully finishes before
   the next loads, and each runs in its own process:
   ```
   !python scripts/benchmark.py results/csv_runs/Tuesday-WorkingHours.pcap_ISCX \
       --models mistralai/Mistral-7B-Instruct-v0.3 Qwen/Qwen2.5-7B-Instruct \
       --base-config configs/run_full_l4.yaml
   ```
   `configs/run_full_l4.yaml` is read only, never modified. It supplies the
   provider settings:
   - `provider: hf`
   - **4-bit NF4** (`load_in_4bit`, `bnb_4bit_quant_type: nf4`, double quant)
   - `require_gpu: true`, so there is no silent CPU fallback
   - the 4 agents with their token budgets
   - `cleanup_model_cache_after: true`, which deletes each model's download after
     its worker finishes so Colab's disk doesn't fill up

   The session copies that file and overrides only the models, dataset, classes and
   DB path. Models can be any of the 5 study models, which are marked `canonical`,
   or any other Hugging Face model id, such as a pulled model.
6. **Timing.**
   - The L4 study averaged about 20 s per flow per model: roughly 9 hours for 300
     flows × 5 models.
   - An A100 is usually faster, but measure first with `--max-flows 20`.
   - A session costs about (flows × 2 models × seconds per flow).
7. **Disconnects.**
   - If a session was interrupted, re-running the same `benchmark.py` command resumes
     it: completed flows are skipped and nothing is duplicated.
   - Re-running a session that already finished measures again as a new run in the
     same `session.db`, and `session_results.json` then reflects the latest run.
   - The existing runner refuses to resume on a different GPU type, so a session
     never mixes readings from two GPUs. `--fresh` restarts it.
   - Copy `results/csv_runs/<name>/` to Drive to keep it.

**Caveats** (each one is also written into `session_results.json`):
- **Non-validated:** sessions are exploratory and never merged into the locked study.
- **Hardware:** A100 numbers are not comparable with the L4 study's numbers.
- **Balanced sample:** CSV runs use class-balanced selection, so accuracy is a
  per-class view, not real-world prevalence.
- **PCAP features** are approximate.
- **CPU and mock runs** have no VRAM readings, so the VRAM criterion can't separate
  the models there.
