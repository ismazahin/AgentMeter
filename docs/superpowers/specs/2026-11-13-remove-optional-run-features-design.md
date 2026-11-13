# Remove load scaling, repeat check and custom pipelines (design)

Date: 2026-11-13. Status: waiting for approval.

## Why

The owner will not use three optional run features, and none of them changes a score, a rank or a
verdict:

- **Load scaling**: after the measured flows, extra flows with 1, 2 and 4 in flight at once.
- **Repeat check**: the first 10 flows answered a second time, to see whether answers repeat.
- **Build my own** (custom pipeline): a user-defined chain of agents instead of the default
  Perceive, Reason, Decide, Act.

They make the New benchmark page longer and the code larger. The owner chose to delete them from
the whole system, the GPU server included, not only to hide them on the website.

## Goals

- Nobody can start a run with any of the three, from the website, a preset, Run again, a scheduled
  repeat or the API.
- The code that runs and analyses them is deleted, with its tests.
- Every run uses the default four agents.

## Non-goals

- The IDS gate stays as it is. It is a different kind of run and is decided separately.
- The Consistency menu (Leaderboard › Consistency) stays. It compares repeated sessions over time
  and never used the repeat check.
- The "What if one agent were removed?" estimate stays. Only its hint to try Build my own goes.
- Model load time and the per-flow timeline (run/extras.py `model_load`, `flow_times`) stay. They are
  recorded on every run and feed other charts.

## Anti-goals

- An old session must never fail to open. Sessions already recorded with these features keep
  their measured numbers.
- Stored schedules and saved presets must keep working. A stored option for a deleted feature is
  ignored, never an error that stops a scheduled run.

## Approaches

1. **Hide on the website only.** Smallest change and easy to undo, but the code stays. Not chosen.
2. **Delete from the website and the GPU server.** Chosen by the owner. Larger and touches the
   measuring code, so it needs the full test suite and a check that old sessions still open.

## What changes

### GPU server

- `agentmeter/run/load_test.py`: deleted. `worker.py`, `runner.py` and `session/benchmark.py` lose
  their `load_test` and `repeat_check` parameters and the steps they ran. `providers/hf.py` loses
  any code only load scaling used.
- `agentmeter/run/extras.py`: the `repeat_check` kind and `REPEAT_FLOWS` go. `model_load` and
  `flow_times` stay.
- `agentmeter/pipeline/spec.py`: the custom-pipeline parts go (normalize, apply_to_config, custom
  agents). Whatever the default pipeline still needs, such as finding the label agent, moves into
  the remaining pipeline code. `make_custom_agent` and `pipeline.custom` in `pipeline.py` and
  `agents.py` go.
- `server/jobs.py`, `server/jobs_api.py`, `server/schedules.py`: the `load_test`, `repeat_check`
  and `pipeline` fields are no longer accepted for new runs. If a request or stored schedule still
  carries them, they are dropped silently. The OpenAPI file is rebuilt (`scripts/build_openapi.py`).
- `session/analyses.py`: the `load_scaling` analysis and the repeat part of the reliability analysis
  go. The settings fingerprint keeps reading `pipeline.custom` from a stored config, so an old
  custom session still sits in its own Leaderboard setup and is never compared as default.

### Website

- New benchmark: the Agents section (Default pipeline / Build my own) and the Optional extras fold
  go. The Settings summary keeps its Agents line, always "Perceive → Reason → Decide → Act".
  Presets and Run again stop saving and restoring the three options.
- Session page: the load scaling section, the "same answer twice" line of Consistency of answers,
  the "This session's pipeline" card and the "Other pipelines on these flows" card go, along with
  `INSIGHTS.pipelineCompare`. The remove-an-agent card loses its Build my own sentence.
- The reproducibility package's run.json stops listing the three options.
- `web/app.html` and `web/analysis.html` lose the related markup, CSS and help text.

### Tests and docs

- Deleted: `tests/test_custom_pipeline.py`, `tests/e2e/pipeline_flow.js`, and the parts of other
  tests that only checked these features.
- Updated: `workflow_flow.js`, `insights_flow.js`, `test_session_benchmark.py`,
  `test_session_trends.py`, `test_service_api.py`, and a new test that a job carrying the old
  fields still runs as a default run.
- The How to use page and any README section that describes the features are updated.

## Old sessions

- A session recorded with **load scaling** or the **repeat check** opens with all its main
  measurements. The extra section is simply not shown any more (its files stay on disk, unread).
- A session recorded with a **custom pipeline** opens with its timings, tokens and answers per
  agent. Anything that needed the custom agents' definitions, such as the hand-offs between agents,
  is left out for that session. It keeps its own Leaderboard setup group.

## Left to the builder

- The exact split of `pipeline/spec.py` between what is deleted and what the default pipeline still
  needs.
- Wording of any remaining help text, following the house style (no em dashes, semicolons or clause
  colons on screen).

## Done when

- None of the three can be started from any route, and the website shows no trace of them.
- The full Python suite, the control-plane tests and the browser walk-throughs that run on this
  machine pass.
- An old session of each kind (load scaling, repeat check, custom pipeline) is seeded in a test and
  opens without errors.
