# Checked explanations: statements first, and what the answers show

Date: 2026-11-11
Status: approved 2026-11-11
Builds on: docs/superpowers/specs/2026-11-10-ai-explanations-design.md (the session explainer)

## Intent

The session explainer (Apertus-8B, last step of every real-GPU session) writes free text from
a digest of the results. The RTX 3090 trials showed its typical mistake: a real number attached
to the wrong subject ("37% of tokens" when 37% was the share of time; "Decide is slowest").
Pattern checks catch known mistakes only, so each run can bring a new one.

The owner wants the model used where it adds the most and kept from getting things wrong:

1. **Statements first (option 1).** Code works out every statement a section may make. The model
   only chooses which matter, orders them and words them, and tags each sentence with the
   statements it uses. Each sentence is checked against its own statements.
2. **What the answers show (option 2).** The model reads the agents' stored answers and describes
   why they fail. Code groups and counts; the model names a pattern with a key phrase and a quote;
   code confirms both against the real answers (strict).
3. **Expert openings.** The Deployment recommendation and Optimisation guidance paragraphs become
   short openings above "Before you deploy it" and "What you can change, and what it would save":
   what matters most for this session and how the items relate. The computed items stay as they
   are, with their exact numbers.

## Goals

- No sentence states a number or a fact that is not in the statement it is tagged with.
- No pattern about the answers is shown unless the answers support it, by code's count.
- The model's value is judgement: what matters most, how things connect, what the answers show.
- Everything else from the explainer design stays: last step of the session, own process, after
  the GPU sampler stops, never changes results, written once to explanation.json, no AI label for
  users, admin-only ✦, fallback to the standard text per section.

## Non-goals

- Changing any computed number, table, checklist item, score or verdict.
- Showing a pattern supported by one or two answers.
- On-demand questions (needs a running GPU).

## Anti-goals

- A sentence that is fluent but tagged with statements that do not say what it says.
- A quote that is not word for word from a stored answer.
- A "pattern" that is just a word every answer contains (for example "flow").

## Approaches considered

How the model is asked (decided with the owner):

1. One request for all sections (as now). Fastest, about 50 s, but a long table is where the
   wrong-subject slips came from.
2. **One small request per section, model loaded once. Chosen.** Each request holds only that
   section's statements, so the model cannot borrow a number from elsewhere. About 1.5 to 2
   minutes per session, after all measuring.
3. Two passes per section (pick, then write). Most controlled but twice the time, and picking is
   what the model already does well.

How strict option 2 is (decided with the owner): **strict.** A pattern stays only when its key
phrase is found in at least half of the answers it describes, more often than in that step's
other answers, and its quote is word for word from one of them.

## Part 1: statements

`agentmeter/session/statements.py` builds, from `session_results.json` only, a list per section:

```json
{"id": "perf.3", "text": "Perceive takes 37% of each flow's time.",
 "values": [{"n": 37, "unit": "%", "of": "Perceive share of each flow's time"}],
 "subjects": ["Mistral 7B", "Perceive"]}
```

- Each statement is one fact in plain words, with the numbers it contains listed in `values`
  and the models and steps it is about listed in `subjects`.
- Formulas match what the page shows. Sources to mirror: `web/insights.js` (tabSummaries,
  sectionAnswers, keyFindings), `web/analysis.html` (renderRecDetail: points, flows per day,
  GPU size, tokens vs target, runner-up gap; renderOptimDetail: per-step shares, the estimated
  saving of shortening the slowest step, prompt reading time), and the budget verdicts already
  in `explain.py`. The page's rounding is used, so a statement reads like the page.
- Sections and their statements:
  - `summary`: the main fact of each tab (one or two each).
  - `tab.<tab>` for ranking, reliability, performance, agents, cost, recommendations: the
    statements behind that tab's headline and why line.
  - `deployment`: every "Before you deploy it" item, the score and tier, the gap to the next tier,
    the runner-up gap, one-model status.
  - `optimisation`: every "What you can change" item with its estimated saving, each step's
    budget verdict, the slowest step, the step reading the longest prompt.
- One-model sessions get a statement saying so, and no ranking statements.
- Unit tests compare statements with the page's numbers on the golden session and on a stored
  real session.

## Part 2: what the answers show

`agentmeter/session/answer_patterns.py`, from the session's `flows_<run>.json` (only when
`has_text` is true):

1. **Groups (code).** Per model and step: cut off at the budget, empty, unreadable label; per
   model: each mix-up pair (true class answered as another). A group needs at least 3 answers.
   At most 4 groups per session: the largest first.
2. **Request (model), one per group.** It gets the group's answers (each cut to 300 characters,
   IP addresses and ports removed, at most 8 per group) and up to 8 answers of the same step
   that are not in the group, for contrast. It returns JSON: `pattern` (one or two sentences),
   `key_phrase` (2 to 6 words), `quote` (from one of the group's answers).
3. **Checks (code), all required:**
   - the quote is found word for word (ignoring case and spacing) in one of the group's answers;
   - the key phrase appears in at least half of the group's answers;
   - the key phrase appears in less than half as many of the contrast answers (so it is a
     pattern of this group, not of every answer);
   - the pattern sentence passes the style rules and contains no number at all (the counts
     under it are added by code).
4. **Shown.** A new card "What the answers show" on Output Reliability. Each kept pattern:
   the sentence, "found in 6 of 9 cut-off Perceive answers" (code's count), and the quote in a
   quote block. Up to 4 patterns. No card when none passes or the session has no answer text.

## Part 3: the requests and the checks for sections

- `configs/explainer.yaml` keeps the model order (Apertus-8B, then gpt-oss-20b) and gains
  `answer_patterns: {max_groups: 4, min_group: 3, sample: 8, contrast: 8}`.
- The explainer process loads the model once and answers a list of requests in order (one per
  section, then one per answer group), writing one reply per request. The time limit covers the
  whole list (8 minutes). A request that fails or times out leaves its section to the fallback.
- Section request: the section's statements with their ids, the section's word limits, the rules
  (style, no reassurance, budget direction), and the instruction to return
  `{"sentences": [{"text": "...", "uses": ["perf.3", "perf.1"]}, ...]}`.
- Checks per sentence (all required):
  - every id in `uses` exists in this section's statements;
  - every number in the sentence matches a value of one of its `uses`, with the same rounding
    and unit (this replaces the "anywhere in the results" check);
  - every step or model named in the sentence is named in one of its `uses`;
  - the existing style, reassurance, one-model, budget-direction, quote-mark and repeat checks.
- A sentence that fails is dropped. A section keeps its passing sentences if they meet its
  minimum length, else it is retried once with the reasons, then falls back to the standard text.
- `explanation.json` keeps, per section, the sentences with their `uses`, so an admin can see
  which statements each sentence came from. The admin-only ✦ tooltip shows them.

## Expert openings

- `deployment` becomes the opening above "Before you deploy it": two to four sentences on what
  matters most for this session and how the checks relate. It replaces the computed one-line
  headline of the Deployment recommendation panel, as today.
- `optimisation` becomes the opening above "What you can change, and what it would save": which
  change to make first, what it costs (a bigger budget costs time and tokens) and what it fixes.
- The computed points table, checklist and per-step table stay underneath, unchanged.

## Testing

- Unit: statements match the page on the golden and a real session; each sentence check with a
  passing and a failing case (number from another statement, step not in its statements, unknown
  id); each pattern check (quote not word for word, phrase in under half, phrase as common in the
  contrast answers, number in the sentence); group building (sizes, at most 4, IPs removed).
- The test-only explainer returns tagged sentences (one deliberately tagged with the wrong
  statement) and patterns (one with a made-up quote), so the mock e2e checks the fallbacks and
  the new card.
- On a rental: `scripts/explainer_trial.py` prints each section's sentences with their `uses`,
  the dropped sentences with reasons, and the kept and dropped patterns.

## Left to the builder

- Statement wording and ids, within the sources listed.
- How sentences are joined into a paragraph (for a tab: the first sentence is the headline, the
  next one or two the why line).
- The card's layout, within the app's existing tokens (no AI label for users).

## Calls made by Claude (owner to check)

- Groups need at least 3 answers, at most 4 groups, 8 answers sampled per group.
- The contrast rule (the key phrase must be at most half as common in the same step's other
  answers), so a pattern is specific to the group.
- The pattern card sits on Output Reliability only.
- The overall time limit for the explainer becomes 8 minutes.
- For a tab, the first sentence is the headline and the next one or two the why line.
- Statements carry `subjects`, so a sentence may only name models and steps its statements name.
- A pattern sentence contains no numbers; code adds the counts.
