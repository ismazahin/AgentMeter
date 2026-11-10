# AI-written explanations for each session

Date: 2026-11-10
Status: design, waiting for the owner's approval

## Intent

Today every explanation in the app is built by fixed code. The numbers change from session to
session, but the sentences follow the same templates, so two sessions read alike even when they
found different things. The owner wants explanations and recommendations that adapt to what each
session actually found.

An open-source LLM, run on the session's rented GPU as the last step of the session, reads the
finished analysis and writes the interpretive text. It does not measure, score or rank anything.

## Goals

- Each session gets its own wording for the parts of the app that interpret results.
- The text is written once, at the end of the session, and stored with it. Everyone who opens the
  session reads the same text, and it stays available after the rented GPU is destroyed.
- The LLM reads only the analysis results. No raw flows, no internet, no other sessions.
- The text never uses `;`, `:` or a dash as punctuation (owner's style rule, see Checks).

## Non-goals

- Changing any number, table, chart, score, rank, verdict or the setup fingerprint.
- Answering user questions on demand (chat). That needs a running GPU.
- Rewriting the ~35 per-question answers under each section. They are short factual lines and
  already change with the data.
- Putting the AI text in the PDF report (can come later).
- Writing text for sessions that finished before this feature, or for demo (mock) runs.

## Anti-goals (failure even if it "works")

- A number in the AI text that is not in the results.
- AI text that silently replaces the computed text without being labelled as AI-written.
- The LLM sharing the GPU with a measured model, or running before every model has released it.
- A session that fails or is not scored because the explanation step failed.

## Approaches considered

1. **LLM replaces every explanation in the app.** Most adaptive, but the most text to check
   (about 2,000 words, 1 to 3 minutes) and a mistake shows up everywhere.
2. **LLM writes the interpretation, computed facts stay fixed.** Chosen by the owner.
3. **LLM writes one summary card, everything else stays fixed.** Does not meet the goal, because
   the explanations across the tabs would stay static.

Also decided with the owner: write once and store (not on demand), and run it as the last step
inside the session job (not as a separate queued job).

## What the LLM writes (sections)

All sections come from one call, returned as labelled pieces so each is checked on its own.

| Section key | Where it shows | Replaces |
|---|---|---|
| `summary` | New "Summary" card at the top of Overview, about 120 words | nothing (new) |
| `tab.<tab>.h`, `tab.<tab>.s` for overview, ranking, reliability, performance, agents, cost, recommendations | Each tab's large headline and its one "why" line | `INSIGHTS.tabSummaries(res)[tab].h` and `.s` |
| `deployment` | Deployment recommendation panel, above the computed points table and checklist | the one-line headline from `REPORT.recommendation` (the caveat stays computed) |
| `optimisation` | Optimisation guidance panel, above the computed per-step table and numbered changes | the one-line `REPORT.optimisationHint` text |

The model ranking chips, points table, "before you deploy" checklist, per-step table, all other
tables and charts stay computed.

If a section is missing or fails a check, that section alone shows the existing computed text.

## Where it runs

Inside the benchmark job, in `agentmeter/session/benchmark.py` `run_session`, after
`session_results.json` and `flows_<run>.json` are written. Every measured model has already exited
its worker process and released the GPU by then.

1. Telemetry `explain` event, so the progress page shows "Writing the explanation".
2. Build a digest of the results (see Input).
3. Start a separate subprocess that loads the explainer model, sends one request, writes the raw
   reply and exits. A separate process guarantees its GPU memory is freed. Time limit: 5 minutes,
   then the process is killed.
4. Check each section (see Checks). If any section fails, send one retry for the failed sections
   only, with the reason. Sections that still fail are dropped.
5. Write `<run_dir>/explanation.json`.
6. The job is then marked done as today. Any exception in steps 1 to 5 is logged, recorded in
   `explanation.json` as `status: "failed"` with the reason, and the session still finishes.

The existing GPU clean check before the next benchmark's first model still applies.

Skipped (no LLM, `status: "skipped"` with the reason) when: the provider is mock, the explainer
is turned off in config, or the GPU has less free memory than the model needs.

## Model and engine

- Configured in a new `configs/explainer.yaml`: `enabled`, `model`, `engine` (`hf` or `vllm`),
  `reasoning` (`low`), `max_new_tokens`, `timeout_s`, `min_free_vram_gb`, and an ordered
  `fallback` list used when the first model does not fit the GPU.
- Default: `openai/gpt-oss-20b` (Apache 2.0) with low reasoning. Fallback:
  `swiss-ai/Apertus-8B-Instruct` (Apache 2.0). Neither family is in `configs/model_catalog.yaml`,
  so neither can be benchmarked in this app. The explainer is never added to the catalog.
- Downloaded at rental start by the model store, like study models, so the user does not wait
  for a download.
- **Must be confirmed before building** (plan step 1): a timing test on an RTX 3090 rental with one
  stored session. gpt-oss-20b ships in MXFP4. If neither vLLM nor transformers runs it in about
  16 GB on Ampere, it would load at full size and not fit, and the default becomes Apertus-8B.
  The test records load time, writing speed, total time and the text quality for the owner.

## Input (digest)

Built by a new `agentmeter/session/explain.py` `build_digest(results)` from `session_results.json`
only, compact JSON under about 4,000 tokens:

- models with their everyday names (`INSIGHTS.nick` rules, ported to Python)
- input type, number of flows, labelled or not, GPU name, demo or real
- per model: mean and p95 latency, tokens per flow, working and total peak VRAM, valid answer
  rate, accuracy when labelled, efficiency score and tier, cost and energy per 1,000 flows when
  measured
- head-to-head verdict and which differences are beyond chance
- weights, targets, and per criterion the points earned
- per model, per agent: time share, prompt and answer tokens, working memory, slowest step
- reliability: failures by agent, mix-ups (top three), triage queue when present
- GPU state (normal or not), warm-up, drift
- the fixed explanations already computed (tab headlines), so the LLM knows the facts it must not
  contradict

Nothing else is sent: no flow rows, no prompts or agent answers, no IP addresses.

## Prompt

A system prompt in `agentmeter/session/explain_prompt.md` (versioned, its hash stored in the
output). It tells the model:

- write only from the digest, never add a number that is not in it, round as the digest does
- write for a SOC lead deciding which model to deploy, plain words, short sentences
- every section has a word limit (summary 120, tab headline 20, tab why line 30, deployment 120,
  optimisation 120)
- do not use semicolons, colons or dashes as punctuation, use full stops and commas
- name models by the everyday names given, never repeat the full Hugging Face id
- return JSON with exactly the section keys listed above

## Checks (per section)

1. **Numbers.** Every number in the text (with its unit) must match a value in the digest, after
   the same rounding. Section fails otherwise.
2. **Style.** No `;`, no `:`, no `—`, `–`, or ` - ` (a dash with spaces). Hyphens inside words stay
   allowed (`4-bit`, `gpt-oss-20b`, `per-agent`), and so does `:` inside a time like `12:30` if
   the digest has one.
3. **Names.** Only the session's models, by their everyday names.
4. **Length.** Within 1.5 times the word limit.
5. **Not empty, valid JSON, expected keys.**

## Output: `explanation.json`

```json
{
  "status": "ok | partial | failed | skipped",
  "reason": "only when not ok",
  "model": "openai/gpt-oss-20b",
  "engine": "hf",
  "prompt_sha256": "...",
  "digest_sha256": "...",
  "generated_at": "ISO time",
  "seconds": 41.2,
  "tokens": {"input": 3120, "output": 640},
  "sections": {"summary": "...", "tab.ranking.h": "...", "...": "..."},
  "dropped": {"tab.cost.s": "number 0.42 not in the results"}
}
```

`session_results.json` is not changed, so its hash and the reproducibility package are unchanged.

## Storage and API

- Benchmark server: `GET /api/jobs/<id>/explanation` returns the file, `404` with
  `{"status": "none"}` for sessions without one.
- Control plane: `session_upload` adds `explanation.json` to the files it uploads, so it is stored
  with the session and survives the rental. The app reads it from the same place as the other
  session files.
- The reproducibility zip includes it when present.
- The embedded analysis page (Deployment recommendation, Optimisation guidance) gets the
  explanation the same way it gets the results today: in the `agentmeter-embed-results` message
  from the app (control plane), or by fetching `/api/jobs/<id>/explanation` itself (single server).

## What the user sees

- Progress page: one more step, "Writing the explanation", with its own short progress line.
  (The app walk-through that counts seven steps is updated to eight.)
- Overview: a Summary card at the top. Below the text, in small muted type:
  "Written by gpt-oss-20b from this session's results. The numbers in the tabs are the measured
  values."
- Tab headlines and why lines use the AI text when present, each with a small "AI" marker whose
  tooltip carries the same line as above.
- Deployment recommendation and Optimisation guidance show the AI paragraph first, then the
  computed details as now.
- When there is no AI text (old session, demo, failed or skipped), everything looks as it does
  today. No empty card.

## Testing

- Unit: `build_digest` on the golden session fixture; every check, with passing and failing
  examples (numbers, each forbidden symbol, hyphenated words allowed, names, length); the
  fallback of a single failed section.
- A test-only explainer (`AGENTMETER_TEST_EXPLAINER=1`) that returns canned JSON, including one
  section that breaks a rule, so the mock e2e can check the Summary card, the AI headlines, the
  fallback for the bad section, and the markers.
- e2e: the observability and insights walk-throughs still pass; the explanation step never
  appears in the GPU timeline's measured windows.
- Manual: the timing test on a real rental (plan step 1).

## Left to the builder

- The exact digest field names and the number-matching tolerance.
- How the subprocess hands its reply back (file or stdout).
- CSS for the Summary card and the AI marker, within the app's existing tokens.

## Calls made by Claude (owner to check)

- Default model gpt-oss-20b with Apertus-8B as fallback, pending the timing test.
- The ~35 per-question answers stay computed.
- The AI text is not in the PDF in this version.
- Demo runs get no AI text (a test-only fake exists for automated tests).
- Hyphens inside words are allowed, dashes used as punctuation are not.
- Time limit 5 minutes, one retry for failed sections, then computed text for those sections.
- The embedded analysis page receives the explanation with the results (same message or fetch).
- The progress page gains one step.
