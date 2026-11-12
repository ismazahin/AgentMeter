# Leaderboard › Consistency (was Repeatability)

Date: 2026-11-12 · Status: approved 2026-11-12 (owner: "yes") · Mockup:
`docs/superpowers/specs/2026-11-12-per-user-separation/G-consistency.png`

## Why

The owner could not tell what Repeatability was for, even with the words explaining it. The page
answers one question: if you run the same model on the same data and GPU again, do you get the same
numbers? It now asks that question and shows the answer as pictures.

## Design (as approved)

- Tab "Consistency" (route `#/trends` kept, so old links work).
- Top: "Do you get the same result twice?", a short explanation, and three small pictures of the
  outcomes: Steady, Varies, Changed recently. Each dot is one run; the green band is "the same
  result" (10% for speed, 5% for tokens and GPU memory, the rules already used).
- Filters: Model, Data set, GPU, Measure (speed, tokens, GPU memory). No session or owner filter
  (the page is about models); a user sees their own runs, an admin everyone's together.
- Result counts that filter the cards: Steady, Varies, Changed recently, One more run for a verdict.
- One card per model on a setup with 2 or more runs: runs as dots against the band, a verdict chip,
  one plain sentence, a next step where it helps, "See every run" (the existing chart and run table
  with their CSV / LaTeX / PNG / SVG downloads), and Run it again for a model with 2 runs.
- Models run once: one line below the cards, each linking to its session to run again.
- "Repeat a run automatically" at the bottom: what it does, and the existing schedules.
- A session's Overview: "Is this result consistent?" per model, linking to its card (the session's
  dot ringed).

## Unchanged

The verdict rules (insights.js repeatSummary and drift), Telegram alerts, the route. Demo runs are
checked only when there are no real runs, as on the rest of the Leaderboard.

## Removed

The summary table, the demo checkbox, the "stable means within" choice, the separate "Runs that
moved" list (it is the "Changed recently" verdict now).
