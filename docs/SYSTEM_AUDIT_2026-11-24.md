# AgentMeter system audit, 24 November 2026

Four passes over the whole system: a senior SaaS design audit of every screen (desktop light and
dark, phone), a design-system token audit, a tastemaker "does it read as AI-made" scan, and a code
review of the last three days of work (`ffc84a8..HEAD`, 159 files). Evidence: screenshots of a demo
session (2 models, synthetic GPU readings) on a local server, the CSS of `web/app.html`,
`web/analysis.html` and `web/landing.html`, and the code. Labels: **Verified** (seen or measured),
**Likely** (strong inference), **Unverified** (needs a real GPU run or a real user).

## 1. The issues that matter most

| # | Area | Issue | Why it matters | Fix | Severity |
|---|------|-------|----------------|-----|----------|
| 1 | Product clarity | The session headline says "Qwen2.5 7B is more efficient than Llama 3 8B" while both score 62.3 on the efficiency score just below it (Verified). Two different "who is better" rules (head-to-head wins, and the weighted score) can disagree with no sentence explaining why. | A supervisor or user sees a contradiction on the first screen and stops trusting the numbers. | When the two disagree, add one line under the headline: "Ahead on working memory, which the score weighs lightly, so the scores tie." Same on the Leaderboard and in the PDF. | High |
| 2 | Content | The landing page describes removed or wrong features: "reuse a prepared set" (removed), "Pick one or two models" (the app allows four), "16 more from Hugging Face" (Verified). | The first page a visitor reads is out of date, which undermines everything after it. | Rewrite the three lines to match the app today. | High (quick) |
| 3 | Reliability | A preparation that crashes the GPU server is re-queued forever: re-register or stop moves running jobs back to waiting without checking the 3-attempt cap (code review). | One bad file can block every benchmark behind it, with no error shown. | Respect `MAX_ATTEMPTS` in `requeueRunning` (prep.ts). | High |
| 4 | Reliability | A preparation that stays silent for 15 minutes (a large CSV pass, IDS scoring) is re-queued while it is still running (Likely). | The same upload is prepared twice, with two notifications. | Send a keep-alive event every few minutes while a job is held (cloudprep/app.py). | High |
| 5 | Phone | On a phone, the top bar (logo, demo chip, theme, settings, five nav links) plus the demo banner fill about 40% of the first screen, and the Sessions table shows only two of its seven columns (Verified). | The app is hard to use on a phone, which is how many people check a running benchmark. | Collapse the nav into one row with a menu button. On phones, show sessions as a list of rows (title, verdict, status) instead of the table. | High |
| 6 | Design system | Two separate design systems: `analysis.html` (the embedded analysis page) defines 62 style tokens and shares only 9 with `app.html`. The landing page uses a third look: serif headlines, a beige background and a blue button, against the app's navy sidebar and lime button (Verified). | Pages feel like different products, and every visual change has to be made two or three times. | One `tokens.css` shared by the app, the analysis page, the landing page and the PDF stylesheet. Decide one brand look for landing and app. | High |
| 7 | Process | A JavaScript syntax error reached `main` code yesterday (the "score's" quote in session.js) because nothing checks the web files before a push. | One character took the whole session page down. | Add `node --check` over `web/**/*.js` to `scripts/test_all.sh` and the pre-push routine. | High (quick) |

## 2. Product and UX (design audit)

- **Session page header** (Verified). Title, four chips (Demo, 20 flows, CSV, labelled, plus a cut-off job id) and three buttons compete. The job id chip is cut off mid-string. Fix: move the job id into the ⋯ menu or a copy button, keep two buttons visible (Download PDF, New benchmark) and put "On the Leaderboard" in the menu. Medium.
- **"How to read a session" guide** sits above the tabs on first visit and pushes every result below the fold (Verified). Fix: show it as a one-line hint with "Show guide", or as a dismissible side note. Medium.
- **New benchmark upload** (Verified). The file picker is the browser's default "Choose File / No file chosen" inside a styled drop zone. It looks unfinished next to everything else. Fix: hide the native input, make the whole zone the button ("Drop a file here or browse"), and show the chosen file's name and size. Medium, quick.
- **Sidebar "New Benchmark"** is always lime, so it reads as selected even when another page is open, and it competes with each page's own lime button (Verified). Fix: style it like the other links, with a small "+" icon, and keep lime for the one primary action per page. Low, quick.
- **Leaderboard tie** (Verified). In a tie (62.3 and 62.3) one model still gets a lime "1" badge and a lime bar, while the text below says they tie. Fix: show "=" for tied ranks and give tied models the same colour treatment. Medium.
- **Footer sentence** "AgentMeter measures how efficiently LLMs run…" repeats under every page (Verified). Fix: keep it on the landing page and Help only. Low.
- **Emoji used as icons** in a few places of `app.html` and `analysis.html` (tastemaker scan). Fix: use the same line icons as the sidebar. Low.

## 3. Design system

**Score: about 55 / 100.** The tokens exist (62 CSS variables, model colours, Path A palette), but they are not enforced.

| Category | Found | Target |
|----------|-------|--------|
| Font sizes | 27 different, including half pixels (12.5, 13.5, 14.5 px) | 7 steps, for example 11, 12, 13, 14, 16, 20, 28 |
| Font weights | 7 (400, 500, 600, 650, 700, 750, 800) | 3 (400, 600, 700) |
| Corner radii | 24 values | 3 tokens: small 4, medium 8, pill |
| Spacing | 161 different padding, margin and gap values | a 4 px scale as tokens (4, 8, 12, 16, 24, 32) |
| Shadows | 15 | 2 levels (raised, overlay) |
| Hard-coded colours | 23 hex values outside the token block, 13 in JavaScript | none outside tokens |
| Inline styles in JavaScript | 210 | classes only, so dark mode and print follow the tokens |
| Shared tokens, app and analysis page | 9 of 62 | all |

Priority actions: 1) one shared token file, 2) a type scale and spacing scale as tokens, then replace values file by file, 3) move inline styles in `session.js` and `analysis.js` into classes.

## 4. Code (review of the last three days)

1. **Waiting list ignores the retry cap on restart** (prep.ts `requeueRunning`). See issue 3. Important.
2. **Old sessions still show the retired L4 caveats** on the embedded analysis page (`analysis.html` line 3859 prints them raw; only `session.js` filters them). Important, small fix.
3. **Long silent preparation re-queued while running** (prep.ts housekeeping and cloudprep Reporter). See issue 4. Important.
4. Report fact ids are cut to 24 characters, so two long model names can share ids (card_facts.py). Nit.
5. The golden scored-keys test now filters the retired caveats from the new output too, so a reintroduced caveat would not be caught for `session.caveats`. Nit.

Wider observations:
- `web/js/session.js` (1,855 lines) and `web/analysis.html` (4,058 lines, its own CSS and render code inside an iframe) are the two largest and most fragile files. Merging the analysis page's views into the app's own modules would remove the iframe, the duplicate styles and the duplicate render logic.
- Two Python tests have failed for days (`test_listings_and_stored_results_need_a_read_token`, `test_the_bundle_is_current`). A suite that is always a little red hides new failures. Fix them or mark them with a reason.
- The full Python suite takes about an hour single-threaded. `pytest-xdist` would cut that to about 15 minutes.

## 5. PDF report

- The new reading paragraphs and the "why" part have only been tested with the test-only fake writer. **Unverified** on the real explainer model. First real GPU run: read it before showing a supervisor.
- With four models, some detail tables run long (per class: 20 rows, class by agent: 20 rows). Consider showing the top rows and the rest in the CSV.
- PDFs of sessions finished before today keep the old layout. An admin "Rebuild PDF" button would refresh them from the stored results (the GPU is not needed for the layout, only for new explanations).

## 6. What could not be verified

Real GPU behaviour and the real explainer's wording, keyboard navigation and screen-reader behaviour in depth, performance with hundreds of sessions, and the control-plane login flow on a phone.

## 7. Status

**Needs follow-up.** The core product is coherent and far more honest than most benchmark tools, but the headline contradiction, the stale landing copy, the waiting-list bugs, the phone layout and the split design system should be addressed before the system is shown widely.

## 8. Follow-up (24 November 2026, same day)

Fixed before the mock presentation: the headline-vs-score sentence (session page, PDF) and "=1" for
ties on the Leaderboard; the waiting-list retry cap on restart and a keep-alive for long
preparations; the stale landing and Help text; the retired L4 wording on the analysis page (caveats
and banner); `scripts/check_web.py` (every web script parses, `web/_headers` matches the inline
scripts) in `test_all.sh` and pytest; the upload box, the session id chip, the one-line reading
guide and the sidebar accent; the phone navigation (one scrollable row) and the session list on
phones (compact rows); the two long-failing tests; `scripts/rebuild_pdfs.py` to rebuild stored
sessions' PDFs; half-pixel font sizes and the 650/750 weights in the app styles.

Left for after the presentation: one shared token file for the app, the analysis page, the landing
page and the PDF (the design-system merge), the spacing and radius scales, moving inline styles out
of the JavaScript, and folding the embedded analysis page into the app.
