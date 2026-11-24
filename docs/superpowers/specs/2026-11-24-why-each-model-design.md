# "Why each model performed this way" in the PDF report (design)

Status: approved 2026-11-24 (why part), extended the same day to every section (below).
Mockup: `2026-11-24-why-mockup/` (`why.html`, `why-page-1.png`, `why-page-2.png`, `why-mockup.pdf`).
Builds on: `2026-11-24-session-report-design.md` (the full PDF report) and
`2026-11-11-checked-explanations-design.md` (the checked explainer).

## Why
The full PDF report restates the website: one answer line and a table per card. The owner's
supervisor reads the report and needs to understand **why each model performed the way it did**,
not only which one won. Today nothing in the report connects a result to its cause.

## Goals
- A new part right after the 4-page front: **Why each model performed this way**. About one page
  per model plus a short comparison table ("Where the models part ways").
- Each result is traced to a **measured cause** in this session, and where the session cannot show
  the cause, to a **likely reason from the model's design** (layers, key-value heads, vocabulary,
  parameters, context window).
- Likely reasons are always labelled: highlighted text, a **D** reference, and hedged wording
  ("probably", "most likely", "usually"). Measured facts carry numbered references to an evidence
  list beside each model, naming the value and the report section it comes from.
- **Voice: technical journalism** (the owner's pick, "style 2"), the register of AnandTech or Ars
  Technica hardware reviews. Third person only, no first person. It opens with what one would
  expect, then what happened, then the reason. Sentences vary in length. Each number is given once.

## Non-goals
- No new measurement. No change to any score, rank, verdict or statistical rule.
- The website does not show this part yet (the text is stored, so it can follow later).
- No paid AI service, nothing sent outside the GPU machine.

## Anti-goals (a failure even if it works)
- A cause the data does not support. A design fact stated as if it were measured.
- Text that reads as AI-written: the owner rejected the first draft for its staccato rhythm,
  repeated frames ("A likely reason is…" in every paragraph), stating the obvious, tidy contrast
  lines and marketing subtitles.
- A likely reason that points the wrong way (for example "more key-value heads" offered for the
  model that used LESS memory).

## Approaches considered
- **A. The existing checked explainer writes it, with code-written text as the fallback.
  Picked by the owner.** Code works out every fact. The explainer model (configs/explainer.yaml,
  Apertus 8B, then gpt-oss-20b) chooses, orders and words them, tagging each sentence with the facts
  it used, and every sentence is checked as today. A section that fails is asked once more, then
  replaced by code-written text. About one extra minute of GPU time per session.
- B. Code writes every sentence from templates. No GPU time and fully predictable, but formulaic
  and poor at joining several causes into one explanation.
- A paid AI service was ruled out (cost, and session data would leave the machine).

## The facts (code, `agentmeter/session/why.py`)
Built from session_results.json, the same way `statements.py` builds today's statements, as a list
per model and one for the pair: `{id, text, values, subjects, kind: "measured" | "design", ref}`.

**Measured** (each with the report section it comes from):
- Time: latency per flow and its p-value against the other model; the share and seconds of each
  agent; output tokens per agent; decode speed (tokens per second, `advanced.throughput`); prefill
  against decode; which agent accounts for most of a latency gap between two models (per-agent
  difference).
- Memory: working memory mean and highest, weights, total peak; input tokens per flow (so a reader
  sees whether prompt length could explain a memory gap).
- Tokens: input against output; the share of each prompt that is flow features, system text and
  hand-offs (`prompt_composition`); tokens the SAME feature text became for each model (each model's
  own tokenizer counted it, so this is measured, not a design guess).
- Slow flows: p95 against p50; warm-up; drift over the run; GPU state; and, when the per-flow data
  holds it, whether the slowest flows were those with the longest answers. Only stated when code
  can compute it from this session.
- Statistics: which gaps passed the test, which are ties, which are not tested.

**Design** (likely reasons): read from each benchmarked model's `config.json` in the **local model
download** on the GPU machine when the session ends (gated models such as Llama need no token,
since the file is already there). Fields: layers, attention heads, key-value heads, head size,
vocabulary size, context window, model type, and parameter count. Derived: attention cache per token
(layers × key-value heads × head size). Stored additively in session_results.json under
`model_design`. A model whose file is missing simply has no design facts.

**The direction rule.** A design fact may be offered as a likely reason for a result only when it
points the same way (for example, more cache per token for the model that used more working
memory). When the design would predict the opposite of what happened, it may only appear as
"would usually…, but here…" (as with the vocabulary in the mockup). Code decides which design facts
are allowed for which result, per session.

## The explainer section (`explain.py`, a new prompt `explain_why_prompt.md`)
- New sections: `why.compare` (the comparison table's two text columns) and `why.<model>` per model,
  with sub-headings Time, Memory, Tokens, and The slow flows when there is something to say.
- The prompt states the voice with two short example paragraphs in style 2, third person, and a
  list of phrases to avoid ("delve", "notably", "it is worth noting", "crucial", "plays a key role",
  "showcases", "robust", "seamless", "moreover", "furthermore", "in summary", "overall", and any
  first person).
- Checks, on top of today's (every number and model or step name must come from the cited facts):
  1. a sentence that cites a design fact must contain a hedge word (likely, probably, usually,
     may, suggests) and is shown highlighted with its D reference;
  2. a sentence that cites only measured facts must not contain a hedge about those facts;
  3. no first person, no phrase from the avoid list, no semicolons or em dashes;
  4. the same opening may not start more than one paragraph in a section.
- Fallback: code-written text for that section, in the same structure (plain, shorter, still
  referenced). The "Who wrote this" box says which sections came from the explainer and which from
  code.

## In the PDF
Placed after the front, before the session tabs. Layout as in the mockup: the comparison table,
then one block per model with its story on the left and the evidence list on the right (measured
references numbered, design references D1, D2…), then the design facts side by side, then "Who
wrote this, how it was checked, what it cannot do". With 3 or 4 models, one block per model and the
comparison table gets one result column per model. A single-model session gets one block, compared
with the score targets, and no comparison table.

## Testing
- `why.py`: facts on the report fixtures (tests/fixtures/report), including the direction rule
  (a design fact pointing the wrong way is never offered as a reason).
- Checks: hedge required on design sentences, avoid-list, first person, reference mapping.
- The fake explainer (AGENTMETER_TEST_EXPLAINER=1) produces the section, so the PDF part renders in
  the browser flows; a test with the explainer failing shows the code-written fallback.
- A real run on the GPU before release, read by the owner, because the voice can only be judged on
  the real model's output.

## Left to the builder
Exact wording of the code fallback, the prompt's example paragraphs, sizes and spacing on the page,
and which sub-headings appear when a model has little to say.

## Calls I made (the owner can overrule)
- Design facts come from the local model download, not the Hugging Face website.
- The direction rule above.
- The website does not show the new part yet.
- Semicolons, em dashes and colons stay out of this section too. The style-2 sample read well
  without them.


## Extension (approved 2026-11-24): a reading paragraph under every section
Mockup: `2026-11-24-section-explanations-mockup/`. The owner wants every analysis in the report to
say what its readings mean and what impact they have, in detail and in plain words, so the report is
not "filled with graphs" without explanation. The general explanation of the system (method page,
glossary, methodology, method notes) was removed in the same round; this is its replacement, about
this session only.

- **Shape.** Each section keeps its table or chart, then ONE paragraph of 2 to 5 sentences in the
  style-2 voice. No fixed labels such as "What this shows" or "Impact": the paragraph opens with what
  stands out in that section, uses its own numbers, and ends with the consequence for a real use
  (a batch job against an analyst waiting, a service that restarts often, the number of GPUs).
- **Facts.** Code builds a fact list per section (`agentmeter/session/card_facts.py`), from the
  stored results and the web's own derived views (web/insights.js through QuickJS). Besides the
  measured facts, a section may carry a few **context facts**: general, always-true statements that
  let a sentence state an impact without inventing one ("a model kept in memory pays its load time
  once", "the GPU is billed by the hour whatever it is doing"). They carry no numbers.
- **Requests.** One request per session tab, asking for every section of that tab at once
  (`{"cards": {"<section>": [{"text", "uses"}]}}`); sections that fail the checks are asked again
  together in one more request, then fall back to the web's one-line answer as today. Roughly 7 to 9
  requests per session instead of one per section, so the extra GPU time stays at a few minutes.
- **Checks.** As in the why part: every number and model or step name from the cited facts, design
  facts only in hedged sentences, the avoid-list, no first person, no semicolons, em dashes or colons.
  A paragraph whose sentences all fail is dropped as a whole, never shown half-written.
- **Where.** Stored in explanation.json under `cards` (and `why`), read by the PDF report when it is
  built. The website can show the same paragraphs later.
