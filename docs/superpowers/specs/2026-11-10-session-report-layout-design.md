# Session pages as short reports — design

Date: 2026-11-10 · Status: for approval · Visual direction: tastemaker (existing AgentMeter
"research paper" style; `.tastemaker/decisions.log`).

## Intent and why

The readers are a **SOC analyst** choosing a model for triage and **someone self-hosting
AgentMeter** checking it on their GPU. After opening a session they should think two things at
once: *"this has a detailed breakdown"* and *"it's easy to understand"*. Today each subtab is a
stack of ~3-9 cards of equal weight, so the answer and a footnote look alike, the depth is
invisible until scrolled past, and the same verdict is worded differently on four tabs.

## Decided (owner)

- **Layout C, report-style** (picked from three prototypes, `layout-A/B/C.png`): every analysis
  is open on the page, one after another, with an **"On this page" contents list** on the left
  that follows the reader.
- **Keep the subtabs (option 2):** each subtab is its own short report with its own contents list.
  Tabs without subtabs (Overview, Output Reliability, Agent Analysis, Cost & Energy, Data &
  Exports) are one report each.
- **Takeaways** (earlier decision, A + C) stay at the top of each report as its **Summary**.

## Goals

1. Every subtab and single-pane tab reads as a report: Summary, then one **section** per analysis.
2. Every section has the same anatomy:
   ```
   [question as heading, serif]          e.g. "Does it slow down during the run?"
   [one-line answer, plain words]         e.g. "No: the last flows were as fast as the first."
   [the chart / table, unchanged]
   ▸ How to read this                     (the card's definitions, collapsed)
   ```
3. A contents list per report: its section questions in order, the current one highlighted while
   scrolling, a click scrolls to it. Sections include the embedded analysis-page parts shown on
   that subtab (EMBED_PARTS).
4. One wording of the verdict: Overview states it; other tabs link to it instead of rephrasing.
5. Everyday model names and plain statistics wording everywhere in headings and answer lines
   (INSIGHTS.nick / plainText), including Overview's figure strip and the GPU-health notice.

## Non-goals
- Changing what is computed, any score, rank or verdict logic (golden test stays green).
- Merging subtabs into one page per tab (owner chose to keep them).
- The PDF report (separate follow-up).

## Anti-goals
- A section with a heading and no answer line where the data allows one.
- A contents list that breaks the app's hash routing (`#/session/<id>/<tab>/<sub>`) or the
  back button.
- Long pages that are slow: no new data fetching; the same cards, rearranged.

## Constraints
- Cards are rendered as HTML strings in `web/js/session.js` (32 card functions) and
  `web/js/analysis.js` (15); embedded parts come from `web/analysis.html` in an iframe.
- Accessibility: the contents list is a `<nav aria-label="On this page">`, the current item has
  `aria-current`, sections are headings in order; keyboard and screen readers work.
- Phones: the contents list becomes an "On this page" menu at the top of the report.
- Both light and dark themes (existing tokens only).

## Approaches considered
- **A. Question-led layers** (answer, one main chart, the rest as an expandable numbered list):
  clearest, but hides depth behind clicks.
- **B. Dashboard grid** (a tile per analysis): biggest first impression, hardest to read.
- **C. Report-style** (chosen): everything open with a contents list: thorough and research-like.
- Within C: **one report per tab** (subtabs removed) vs **a report per subtab** (chosen: shorter
  pages, the existing subtab navigation stays).

## The design

### Section wrapper (one component)
`section({id, q, answer, body, how})` returns:
`<section class="rsec" id="sec-<id>"><h3>q</h3><p class="rsec-a">answer</p>body<details>How to read this…</details></section>`.
Each card function's current `<div class="card"><h2>Title</h2><p class="muted small">intro</p>…`
is converted to it: the h2 becomes the question, the intro becomes the answer when no computed
answer exists, the "What each column means / How this is tested" details become "How to read this".

### Questions and answers per subtab (headings decided here; answers computed where the data allows)
| Report | Sections (question headings) |
|---|---|
| Overview | Summary (verdict) · Key numbers · Can these timings be trusted? (GPU health, run quality) · Key findings · How each model scores on each criterion · Per model · Your notes · Repeat on a schedule |
| Ranking › Scores | Summary · Which model ranks first? (score table) · Highlights |
| Ranking › Weighting | How much does each criterion count? · What if I weight it differently? |
| Ranking › Robustness | How sure is the ranking? · How far can the weights move before it changes? |
| Ranking › Trade-offs | Which models are never beaten on two costs at once? |
| Ranking › Model Guide | What is known about each model? · Questions · Glossary |
| Output Reliability | Did every flow get a usable answer? · How did the IDS and the LLM split the work? · Are the accuracy differences real? · Which classes does each model mix up? · What would an analyst have to do? (triage) · What would this look like in a real SOC? · Are the answers consistent? · Do the explanations match the verdict? · Where do answers fail? |
| Performance › Comparison | Which model is faster, and by how much? · Per attack class |
| Performance › Latency | How fast is each model? · Does it slow down during the run? · How predictable is it? · Where does the time go inside one call? |
| Performance › Capacity | How many flows can one GPU handle? · How much does the first flow cost? |
| Performance › What drives it | Which flows are slow, and why? · Does it scale with parallel flows? |
| Performance › GPU & Throughput | How much GPU memory does it need? · Was the GPU busy or idle? · Throughput |
| Performance › Statistics | Is the difference real? · How many flows are needed to tell models apart? · Is each model's result the same every time? |
| Agent Analysis | Which agent takes the most time? · What do agents pass to each other? · What if one agent were removed? · What are the prompts made of? · Are the token budgets right? · Time per agent by attack class · One flow, step by step · Explore every flow |
| Cost & Energy | What does it cost per 1,000 flows? · How energy-efficient is each model? · GPU over the run · How much does an idle GPU cost? · What would my volume cost? · Where is it cheapest to run? · Cost per usable answer |
| Recommendations › Your limits | Which model should I deploy? (measured verdict) · Best model for my priority · Deployment fit · Does a model fit my limits? |
| Recommendations › Model context | What is known about these models? (stage 3, never changes a score) |
| Recommendations › Deployment guidance | What should I do next? · How to optimise |
| Data & Exports | Download the report and results · The prepared flows · Delete this session |

Answer lines: computed in `web/insights.js` (one small function per section, plain words, short
model names, no em dashes) where the stored results support one sentence; otherwise the card's
intro, rewritten plainly. A section never shows an empty answer line.

### Contents list ("On this page")
- Built from the report's sections after render (headings in DOM order, embedded parts included).
- Wide screens (≥ 1100 px): a sticky left column (about 220 px) beside the report.
- Narrower: a compact "On this page" select/menu above the Summary.
- Scroll-spy with IntersectionObserver; click scrolls smoothly (respecting reduced motion) and
  records the section in the URL as `?sec=<id>` without a new history entry, so links and reload
  return to it.
- Embedded parts: the iframe already reports its height; its parts are listed with their titles
  and scrolling to one posts a message the embedded page answers with the part's offset.

### Typography and rhythm (tokens unchanged)
- Report width: text max 74 ch; charts and tables use the full column.
- Section heading: serif 22 px; answer line 16 px `--text`; body as today; "How to read this"
  14 px `--muted-2`.
- Sections separated by a hairline (`--border`), 28 px apart; the Summary has the `--rule` top.

### Overview and repeated verdicts
- The figure strip uses short names and one number format ("33% faster", "95%", "2 of 4 pass").
- The GPU-health notice: one line per model ("Qwen2.5 7B: GPU slowed down in 11% of samples"),
  the slowdown causes in "How to read this".
- Ranking/Performance/Recommendations show their own answers; where they restate the overall
  verdict, they link "See the verdict on Overview" instead of a second wording.

## Left to the builder
- The exact answer-line sentence for each section (rules above), and which sections have none
  computable (use the rewritten intro).
- Section ids, the select widget on phones, the postMessage format for embedded parts.
- Tests: walk-throughs updated for section ids; a check that every report has a contents list
  whose items match its section headings; a unit test per new answer function.
