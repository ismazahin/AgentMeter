# Defensible results: accuracy statistics, confusion matrix, realistic SOC projection, verdict agreement — design

Date: 2026-11-10 · Status: approved 2026-11-10 (owner: "do everything except 2"; the non-LLM baseline
is deferred) · From the priority ranking (items 1, 3, 4, 5).

All four are additive: they read what a session already stores, add keys to session_results.json
or compute in the browser, and never change a score, SAW value, rank or verdict
(tests/test_scored_keys_golden.py).

1. **Accuracy statistics** (`accuracy_stats`, session/analyses.py): per model, accuracy with a 95%
   Wilson interval; per model pair, an exact McNemar test on the flows both answered (paired:
   same flows), Holm-corrected across pairs, with a plain verdict ("differ beyond chance" /
   "no evidence of a difference"). Shown as a card in the session's accuracy section.
2. **Confusion matrix** (display only): the stored `per_model[m].accuracy.confusion` as a grid per
   model, true class × answered class, the diagonal marked, the largest off-diagonal cells named.
3. **Realistic SOC projection** (browser, insights.js): from each model's measured triage rates,
   the analyst queue per 10,000 flows at a benign share the user picks (default 99%):
   escalations, missed attacks, and the share of escalations that are real attacks.
4. **Verdict agreement, visible**: the existing check (Act names the class Decide chose) as a bar
   per model, plus up to 5 example flows where they disagree (from the stored agent texts).

Deferred: the non-LLM baseline (needs a decision on how its model is trained).
