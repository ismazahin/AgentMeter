# Leaderboard: remove Compare models, add "Why this order"

Date: 2026-11-12 · Status: approved 2026-11-12 (owner: "yes") · Mockup:
`docs/superpowers/specs/2026-11-12-per-user-separation/F-why-this-order.png`

## Why

The owner found Compare models adds little next to the new By setup panel, and wants the
ranking to explain itself: anyone viewing a setup should see the core reason the models are in
that order.

## Approaches considered

1. Keep Compare models as a tab. Rejected: it repeats By setup.
2. Fold Compare into By setup (ranges, "could be chance", all measures, why not counted).
   Shown as mockup E; set aside, since most models run once per setup, so the ranges would
   mostly be empty, and the owner preferred a plain explanation of the order.
3. **Remove Compare models; add "Why this order" under the By setup ranking (chosen).**

## Design

- Leaderboard tabs: By setup, Overall, Repeatability. `#/compare` addresses (the session page's
  Compare button, "Open in Compare" in the analysis, old links) open By setup with that
  session's setup selected. The control plane and GPU server keep their compare routes.
- "Why this order", shown under the ranking when it ranks by SAW score and the setup has 2 or
  more ranked models:
  - one line on the score: the points each measure is worth (from the weights the scores
    were computed with, for example speed 42, GPU memory 33, tokens 25);
  - a verdict written by the page from the points, not by a language model: the measure that
    decided 1st over 2nd, by how many points, what the 2nd model won back, and the net gap;
  - per model: points and the measured value for speed, GPU memory and tokens, the most points
    per measure highlighted, a stacked bar of where the points come from, and the score;
  - a note that cost and energy are not part of the score and can be ranked on with Rank by.
- Points per measure are stored with each new session (`saw_eff_points` and `saw_eff_weights`
  in the session summary's per-model metrics: weight times the normalised value, times 100; the
  three add up to the stored score). They are kept only when they add up to the stored score
  within 0.15. A setup's points are the flow-weighted mean over its sessions, the same way the
  score is combined, so the three still add up to the score shown.
- Older sessions without stored points: the measured values and the weights, with a note that
  the points split starts with new sessions. No verdict sentence is invented for them.
- Hidden for one-model setups (the Not enough notice stays) and when ranking by one measure.

## Non-goals

No change to how scores or ranks are computed. No statistics on repeated runs (Repeatability
covers runs over time).
