# AgentMeter — design brief for a front-end redesign

This brief is for a designer or coding agent (e.g. Codex) asked to redesign the AgentMeter
web interface. It explains what the system is, who uses it, every page and state, the current
visual design, and the technical rules any new design must respect. Screenshots of the current
interface are in `docs/design/current/`.

**The task:** propose and then build a clearer, more impressive interface **without changing
what the system does**. Behaviour, data, scores and wording of results stay; layout, visual
hierarchy, navigation, components and styling may change.

---

## 1. The product in one paragraph

AgentMeter is a final-year-thesis project that **measures how efficiently open-source LLMs run**
when they power a small multi-agent AI workflow. A user uploads network-traffic data (CSV or
PCAP), picks one or two LLMs, and AgentMeter runs every selected flow through a fixed
**four-agent pipeline** (Perceive → Reason → Decide → Act) on a rented GPU. It records, per
agent and per flow, **latency, GPU memory (VRAM), tokens, cost and energy**, then compares the
models head-to-head and ranks them with a weighted score (**SAW**, Simple Additive Weighting).
Every model is quantized to the same **4-bit NF4** so the comparison is fair.

### Framing rules (must stay true in any design)
- AgentMeter **measures LLM resource efficiency. It is not a threat-detection product.** The
  agents happen to analyse network traffic; accuracy is shown only as *context*.
  The footer line "AgentMeter measures how efficiently LLMs run … it isn't a threat detector"
  (or equivalent) must stay.
- Demo results (no GPU) must always be clearly marked as **not real measurements**.
- Never change any score, SAW value, ranking or verdict text logic; only how it is presented.
- The 5 **study models** are the "validated baseline"; any other model is labelled **"extra"**.

---

## 2. Who uses it

| User | Goal | What they need from the UI |
|---|---|---|
| **The researcher (owner, admin)** | Run benchmarks, manage users and models, show results to a supervisor | Fast path to "run a benchmark"; trustworthy, publication-quality results pages |
| **Supervisor / examiner** | Understand the method and trust the numbers | Clear verdict first, then evidence: how measured, statistics, caveats |
| **Invited user** (max 5 accounts, role "user") | Benchmark models on their own traffic | Simple 3-step wizard, plain language, no jargon walls |
| **Exhibition visitor** (looking over the shoulder) | Get it in 10 seconds | A striking headline result and visuals; little text |
| **SOC / ML engineer** (target audience) | Pick the cheapest model that fits a single GPU | Side-by-side numbers, cost per 1,000 flows, VRAM headroom, leaderboard |

---

## 3. Core journeys

1. **Log in** (accounts come from the admin; no public sign-up) → Home.
2. **New benchmark (3-step wizard):**
   1. **Data** — upload CSV/PCAP, *or* import from a URL, *or* reuse a prepared set. Choose
      "flows to benchmark" (budget). The server prepares the data (rule-based flow selection).
   2. **Models and settings** — see "Your data" summary (flows, label mode, class table, the
      9 flow-selection rules that fired); pick **1 or 2 models** from *Study models* and
      *More models to try* (extra models from Hugging Face, each card with size, 4-bit VRAM
      estimate, licence notes, architecture, download size); see the Settings summary; **Run**.
   3. **Run** — live progress (flows done / total, current model; a "Downloading model … X/Y GB"
      phase if a model is not on the GPU yet; a **Stop** button). On completion → the session.
3. **Read a session** (8 tabs, see §4).
4. **Compare** sessions run on the same setup; **Leaderboard** ranks models across sessions.
5. **Admin:** users, credential status (set / not set only), **Model catalog** (enable/disable,
   download at start-up, add a Hugging Face model, **Verify** = test-load at 4-bit on the GPU),
   system limits.

---

## 4. Pages, routes and what each shows

Single-page app with hash routes. Screenshots: `docs/design/current/<file>` (dark theme
unless noted; demo mode, so most efficiency numbers read "not measured" — see §6 for real data).

| Route | Page | Screenshot | Contents |
|---|---|---|---|
| `#/login` | Login | `01-login.jpg`, `01-login-light.jpg` | Username, password, Remember me; forced password change on first login |
| `#/` | Home | `02-home-empty.jpg`, `02b-home-with-sessions.jpg` | "Start a new benchmark" call to action; latest session verdict with two bar charts (latency, working VRAM); recent sessions table |
| `#/new` | New benchmark, step 1 | `03-new-step1-data.jpg`, `14-new-step1-light.jpg` | Tabs: Upload / URL / Reuse; flows budget; "Other attack" option; Prepare |
| `#/new/models?prep=…` | Step 2 | `04-new-step2-models.jpg` | Your data (summary, class table, rules); Models (study + "More models to try" cards); Settings; Run |
| `#/new/run/<job>` | Step 3 | `05-new-step3-running.jpg` | Phase heading, progress bar and text, Stop |
| `#/session/<id>/<tab>[/<sub>]` | Session | `06-session-1…8-*.jpg`, `13-session-overview-light.jpg`, `16-phone-session.jpg` | Title "A vs B", chips (GPU, flows, input, labelled, job id, owner), Compare + Download PDF, 8 tabs: |
| | → Overview | `06-session-1-overview.jpg` | Verdict headline, 4 metric tiles (latency, VRAM, tokens, accuracy), per-model table, "Good to know" caveat chips |
| | → Model Ranking | `06-session-2-ranking.jpg` | SAW scores, weighting presets (sliders, view-only re-rank), statistics sub-tabs (embedded analysis page) |
| | → Accuracy | `06-session-3-accuracy.jpg` | Accuracy by class, confusion details (context only) |
| | → Performance | `06-session-4-performance.jpg` | Latency distribution (p50/p95/p99, warm-up), effect size + CI, Kruskal-Wallis |
| | → Agent Analysis | `06-session-5-agents.jpg` | Per-agent table per model (latency share, tokens in/out, VRAM), pipeline timeline (Gantt) for one flow, agentic overhead |
| | → Cost & Energy | `06-session-6-cost.jpg` | Cost per 1,000 flows, energy per flow, GPU memory table |
| | → Recommendations | `06-session-7-recommendations.jpg` | Measured verdict, "Does a model fit your limits?" form, model context from Hugging Face (licence, VRAM headroom, age) |
| | → Data & Exports | `06-session-8-data.jpg` | Download PDF/JSON/CSV, prepared set, delete session |
| `#/sessions` | Sessions | `07-sessions.jpg` | Filterable list (status, input type) |
| `#/compare` | Compare | `08-compare.jpg` | Pick same-setup sessions; merged tiles and tables |
| `#/leaderboard` | Leaderboard | `09-leaderboard.jpg` | Tabs "By setup" (ranked table per GPU + data set) and "Overall" (wins, win rate) |
| `#/settings` | Settings | `10-settings.jpg` | Theme, Telegram notifications, defaults (flows, models), SAW presets, backend info |
| `#/profile` | Profile | `11-profile.jpg` | Account, logged-in devices (log out others), change password |
| `#/admin` | Admin (admin only) | `12-admin.jpg` | Users, credentials status, Model catalog table, System limits |
| — | GPU offline view | — | Shown when no GPU server is online; stored sessions stay browsable |
| — | Phone | `15-phone-home.jpg`, `16-phone-session.jpg` | Must work at 390 px wide with no horizontal scroll |

Header (every page): logo, main nav (Home, New benchmark, Sessions, Compare, Leaderboard),
**GPU badge** (see §5), profile button (name + role chip → menu: Profile, Settings, Admin, Log out).

---

## 5. States the design must handle

| State | Where | Today |
|---|---|---|
| **Real GPU online** | header badge | green "Real GPU: NVIDIA GeForce RTX 3090" |
| **Demo mode** (no GPU; simulated) | header badge + page banner + chips on results | amber badge, amber banner "Demo mode: no GPU, so results are simulated" |
| **GPU offline** | header badge; Run disabled; New benchmark explains | red badge "GPU offline"; history still browsable |
| Admin vs user | nav, Admin page | Admin link + "Admin" role chip only for admins |
| Running / queued / downloading model / stopped / failed / done | run page, sessions list | status chips (Running, Done, Demo, Failed, Interrupted, Stopped) |
| Single-model run | session | head-to-head parts say "no second model" |
| Extra (non-baseline) model | everywhere a model name appears | small **"extra"** tag |
| Gated model | model cards, admin | "licence verification required" chip |
| Model not offered | picker note | "N more listed models are not available here: <model>: <reason>" |
| Empty states | home, sessions, compare, leaderboard | short message + action |
| Errors | forms | inline error box under the form |
| Light / dark / system theme | everywhere | user setting; both must be designed |

---

## 6. Real data to design with (from a real RTX 3090 session)

Demo screenshots show placeholders; real sessions look like this (session
`job_20261021_053653_…`, 20 flows, Qwen2.5-3B vs Llama-3.2-3B, both "extra"):

| | Qwen/Qwen2.5-3B-Instruct | meta-llama/Llama-3.2-3B-Instruct |
|---|---|---|
| Mean latency / flow | 10.80 s (p95 12.99 s) | 9.89 s (p95 10.86 s) |
| Working VRAM (excl. weights) | 157 MB | 243 MB |
| Total peak VRAM | 3,128 MB | 3,276 MB |
| Tokens / flow | 3,401 | 3,359 |
| Accuracy (context) | 20.0 % | 10.0 % |
| Cost / 1,000 flows | $0.77 | $0.70 |
| Energy / flow | 0.51 Wh | 0.50 Wh |
| SAW (full) | 46.87 "Critical" | 43.0 "Critical" |

- **Verdict:** "Qwen2.5-3B-Instruct is more resource-efficient than Llama-3.2-3B-Instruct: 35.5 % less VRAM (tie on latency, tokens)." Latency tie: Kruskal-Wallis p = 0.168.
- **Per agent (Qwen):** perceive 3.26 s (30 %), reason 4.06 s (38 %), decide 0.27 s (2.5 %), act 3.20 s (30 %).
- **Agentic overhead:** 303 hand-off tokens per flow = 8.9 % of all tokens.
- **Caveats shown as chips:** Fair A-vs-B, GPU ≠ study GPU (L4), Exploratory run, Balanced sample, Small sample (20 flows).
- **Model catalog:** 5 study models plus 16 extra models (Qwen2.5 1.5B/3B/14B, Phi-3.5/Phi-4 mini, Llama-3.2-3B, Gemma-2-2B, Mistral-NeMo, SmolLM2, Granite 3.1, Falcon3, OLMo 2, Yi-1.5, Aya-Expanse, OLMoE mixture-of-experts, Falcon3-Mamba state-space). Admin "Verify" results: e.g. "✓ verified · RTX 3090 · weights 1,969 MB · 100 % 4-bit", or "✕ failed: only 4 % of the weights loaded as 4-bit".

---

## 7. The current visual design

- **Style:** dark-first, flat cards on a near-black background, one amber accent.
- **Font:** Manrope (Google Fonts, 400/500/600/700); monospace for model ids and job ids.
- **Tokens** (CSS custom properties on `:root`, redefined for dark under
  `prefers-color-scheme: dark` and `[data-theme="dark"]`):
  - light: `--bg #F6F7F9`, `--surface #FFF`, `--text #12161C`, `--muted #5B6470`, `--border #D9DEE5`
  - dark: `--bg #12161C`, `--surface #161B22`, `--surface-2 #1A2029`, `--text #E6E9ED`, `--muted #9AA3AE`, `--border #262D37`
  - `--accent #E8A33D` (amber), `--success`, `--danger`, `--series-1/2` (model A amber, model B blue),
    agent colours `--ag-perceive` blue, `--ag-reason` purple, `--ag-decide` amber, `--ag-act` teal
  - `--radius 8px`, `--radius-lg 12px`; content max width 1180 px; 44 px touch targets
- **Components in use:** sticky header with nav and pill badge; page head (h1 + muted lead);
  `.card` (bordered, 20 px padding) for almost every block; metric tiles; chips/pills (status,
  caveats, tags); tables in `.table-wrap` (horizontal scroll); `details/summary` disclosures for
  "Show full verdict", "How this was measured"; tab bar + pill sub-tabs; horizontal bar charts
  (`.hbar`, CSS only); a Gantt timeline; form fields; info "i" tooltips; progress bar.
- **Charts:** hand-made CSS/SVG only; the Ranking/Statistics views embed `web/analysis.html`
  (a second page with its own charts) in an iframe.

### What the owner and testers find weak (observations, not prescriptions)
- Everything is a card of the same weight: no clear visual hierarchy between the verdict and the
  supporting detail; pages read as long stacks of boxes.
- Text-heavy: many explanatory sentences, notes and caveats compete with the numbers.
- The headline result (who is more efficient, by how much) is not visually striking enough for a
  demo or exhibition.
- Few real visualisations: most results are tables; metric tiles use emoji icons.
- The session page has 8 tabs; the information architecture feels flat.
- The amber demo banner and chips dominate demo pages.
- Model pickers and the admin catalog are long lists of similar cards/rows (21 models).
- Tables (per-model, admin catalog) get wide and cramped; long model ids wrap awkwardly.
- It looks like a competent internal tool, not a polished product.

---

## 8. Hard technical constraints

1. **Static, no build step.** The front-end is `web/index.html` (≈3,000 lines: HTML, one
   `<style>`, inline `<script>`s), `web/analysis.html` (embedded analysis page), `web/report.js`
   (must stay byte-identical to `dashboard/report.js`; `tests/test_frontend_split.py`),
   `web/saw.js`, `web/config.json`. Deployed to **Cloudflare Pages** as plain files.
2. **Strict Content-Security-Policy** (`web/_headers`, generated): `script-src 'self'` plus the
   SHA-256 of each inline script. **After any change to an inline `<script>`, run
   `python scripts/csp_headers.py`**, or the page will not run (`tests/test_control_plane.py`
   fails when `_headers` is stale). No `eval`, no inline event handlers (`onclick=`), no scripts
   from CDNs. You *may* add your own static `.js`/`.css` files under `web/` (same origin).
   Styles: `'self' 'unsafe-inline'` + Google Fonts; fonts from `fonts.gstatic.com`.
3. **Keep the DOM contract.** JavaScript and the Playwright walk-throughs address elements by
   `id`, `data-*` attributes and routes (list in Appendix A). Keep them, or update the JS and
   `tests/e2e/*.js` together. Keep the hash routes.
4. **Keep behaviour and text of results.** Verdicts, scores, caveats, numbers and their units are
   computed by the backend/JS; restyle them, don't rewrite their meaning.
5. **Accessibility:** keyboard-usable tabs and menus (ARIA roles are in place), visible focus,
   contrast AA in both themes, `prefers-reduced-motion` respected, 44 px touch targets.
6. **Responsive:** no horizontal page scroll at 390 px (tested), tables may scroll inside
   their own container.
7. **Both themes** (light, dark, system) must be designed; colours via the CSS tokens.
8. **Security UX stays:** no secret values are ever shown (credentials show set / not set);
   the access token stays in memory; demo results always labelled.

---

## 9. How to run it locally

```bash
pip install -r requirements.txt flask
# A) standalone demo (no login): http://127.0.0.1:8766/#/new
AGENTMETER_TEST_MODEL_REPOS=tests/fixtures/model_repos.json AGENTMETER_TEST_DISK_FREE_GB=200 \
  python scripts/serve.py --provider mock --port 8766
# B) the full multi-user app (login, admin) with a local control plane: needs Node 22 (cd controlplane && npm ci)
bash tests/e2e/cp_stack.sh /tmp/cp-out        # runs the control-plane walk-through (82 checks)
```
Walk-throughs (Playwright, must stay green): `tests/e2e/cp_flow.js` (via `cp_stack.sh`),
`tests/e2e/model_picker_flow.js`, `tests/e2e/app_flow.js`, `tests/e2e/service_flow.js`,
`tests/e2e/session_analysis.js`. Python tests: `python -m pytest -q`.
Log in as `admin` / `admin-password-123` in the local stack.

---

## 10. What we would like from you

1. **First, a proposal** (no code yet): 2–3 visual directions (mood, type, colour, layout
   principles) with one recommended, plus the new information architecture (especially the
   session page and Home) and a component list. Show the Overview tab and Home in each
   direction (mock-ups or HTML).
2. **Then implement incrementally**, page by page (Home → Session → New benchmark → the rest),
   keeping every walk-through green and regenerating the CSP after each script change.
3. Design for the **exhibition moment**: the verdict and the key numbers should be readable from
   two metres away; detail stays one click deeper.

---

## Appendix A — DOM hooks used by the walk-throughs

Element ids:

```
adm-count adm-creds adm-model-add-go adm-model-id adm-model-note adm-models-error adm-models-mode
adm-models-table adm-new-go adm-new-pass adm-new-user adm-users-table agents-flow agents-gantt
agents-overhead-0 agents-table-0 analysis-frame banner cmp-a cmp-accuracy-table cmp-error cmp-go
cmp-list cmp-mode cmp-none cmp-overview-table cmp-performance-table cmp-table cmp-tiles
cmp-validity cp-logout cp-menu cp-profile-btn cp-role cp-user ctab-accuracy ctab-performance dh-go
dh-max_mean_latency_s dh-max_peak_vram_mb dh-pdf dh-table flt-input glossary-panel home-empty
home-latest home-sessions-table lb-by lb-demo lb-overall-empty lb-overall-head lb-overall-table
lb-setup-panel lb-sort lb-table-0 lbtab-overall lbtab-setup lim-save lim-sph login-error login-form
login-go login-pass login-remember login-user mc-note mcost-table nav nav-admin ov-title
pre-accuracy-0 pre-latency-0 pre-name-0 pre-tokens-0 pre-vram-0 preset-save-btn prof-devices-table
prof-role prof-username pull-btn pwc-go pwc-new pwchange-form saw-body session-body session-chips
session-compare session-tabs session-watch sessions-table set-account set-flows set-models
set-notify set-password set-preset-add set-save set-tg set-theme-dark set-theme-light
set-theme-system set-tz sliders spanel-overview spanel-performance spanel-recommendations
ssub-context ssub-guide ssub-scores ssub-statistics ssub-weighting stab-accuracy stab-agents
stab-data stab-performance stab-ranking stab-recommendations stats-effects stats-effects-card
stats-percentiles stats-small svc-backend-badge svc-bench-id svc-bench-id-btn svc-bench-id-error
svc-cav-detail svc-caveats svc-class-table svc-cost-card svc-counts svc-demo svc-dl-json svc-dl-pdf
svc-downloads svc-downloads-main svc-drop svc-env svc-evaluate svc-extra svc-extra-count
svc-extra-models svc-extra-note svc-file svc-full-verdict svc-headline svc-imp-features
svc-imp-labels svc-imp-manifest svc-import-btn svc-import-error svc-import-form svc-limits
svc-max-flows svc-memory-table svc-models svc-models-table svc-offline-notice svc-other-attack
svc-preset svc-preset-table svc-preset-weights svc-progress-text svc-progress-bar svc-recent-sets
svc-relative-table svc-run svc-run-mode svc-sampling svc-set-id-value svc-sets-table svc-stage3
svc-stop svc-stored-table svc-summary-card svc-tiles svc-upload-btn svc-upload-error
svc-upload-form svc-url svc-verdict svc-weights-card svc-weights-used tab-src-reuse tab-src-url
view-compare view-config view-detailed view-home view-login view-new view-offline view-sessions
view-settings wiz-data-chip wiz-data-error wiz-data-status wiz-downloads wiz-offline
wiz-step-data wiz-step-models wiz-step-run wiz-steps
```

Data attributes: `data-tab data-sub data-nav data-view data-open data-pane data-part data-set
data-model data-enable data-dlstart data-remove data-verify data-user data-device data-revoke
data-dl data-cav data-study`. Classes the tests read: `.extra-tag`, `.agents-model`, `.lb-table`.

Regenerate this list after changes:
`grep -ohE "#[a-zA-Z][a-zA-Z0-9_-]+" tests/e2e/*.js | sort -u`.

## Appendix B — API the page calls (unchanged by a redesign)

Control plane (Supabase Edge Function): `/api/auth/*`, `/api/me`, `/api/me/devices`, `/api/me/password`,
`/api/settings`, `/api/settings/telegram-test`, `/api/sessions`, `/api/compare/*`,
`/api/leaderboard`, `/api/backend`, `/api/admin/users`, `/api/admin/credentials`,
`/api/admin/limits`, `/api/admin/models`, `/api/runs/authorize`.
GPU backend: `/health`, `/api/service/config`, `/api/prepare`, `/api/prepared/*`, `/api/jobs`,
`/api/jobs/<id>` (+ `/stop`, `/report.pdf`, downloads), `/api/models/catalog`,
`/api/models/verify`, `/analysis` (embedded).

## Appendix C — Where things are

| Path | What |
|---|---|
| `web/index.html` | the whole app (markup, CSS, JS) |
| `web/analysis.html` | embedded analysis/charts page (Ranking, Statistics) |
| `web/_headers` | generated CSP — `python scripts/csp_headers.py` |
| `tests/e2e/` | Playwright walk-throughs |
| `docs/SESSION_BENCHMARK.md` | how a benchmark works, API table |
| `docs/RULE_BASE.md` | flow selection, model suitability, recommendation rules |
| `docs/DEPLOY.md` | Cloudflare + Vast.ai deployment |
| `docs/design/current/` | screenshots of the current interface |
