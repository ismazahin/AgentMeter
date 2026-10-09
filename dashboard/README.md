# AgentMeter Validation-baseline page (read-only)

The read-only view of the **locked 5-model L4 study** (linked from the benchmark
service as "Validation baseline"; served at `/` by `scripts/serve.py`). It **reads a
precomputed `analysis.json`** (produced by `agentmeter/analyze.py`) and displays it. It is a
pure **view layer** — it runs no models, touches no pipeline / instrumentation /
detection code, and does no measurement of its own.

## What it shows

- **SAW ranking table** — rank, model, accuracy, latency, total device VRAM,
  tokens, composite, and a colour-coded tier badge. Re-ranked **live** from the
  weight sliders below it.
- **VRAM finding** — the `notes.vram_finding` line, shown verbatim.
- **Per-agent diagnostics** — mean wall time and marginal working memory
  (`vram_delta`, explicitly *not* total device VRAM) per agent, with a model
  selector.
- **Live sensitivity** — four weight sliders (accuracy / latency / vram /
  tokens) renormalised to sum 1.0, recomputing the composite and re-ranking the
  table in-browser. Preset buttons cover `default` plus each configured
  `sensitivity.weight_sets` entry.
- **Source banner** — states whether you're viewing the bundled **SAMPLE** or a
  **user-loaded** file, so illustrative numbers are never mistaken for real ones.

All scoring/re-ranking is delegated to [`saw.js`](./saw.js), which mirrors
`analyze._composite` / `_tier` exactly (guaranteed by `tests/test_saw_parity.py`).

### Tabs (presentation view)

A simple top nav switches between two views (everything still reads from the
one loaded `analysis.json`; no server or GPU needed to view precomputed results):

- **Overview** — headline **Highlights** cards (Composite Health Score, Accuracy,
  Latency per scenario as labelled bar charts, best model emphasised), the SAW
  ranking table, decision support, Hugging Face model reference info (external
  context via `/api/model-metadata`, never part of the score), the live weight sliders
  and the VRAM finding.
- **Detailed Analysis** — the study content for a supervisor: per-agent
  diagnostics (with the *reason dominates latency* / *decide dominates marginal
  working memory* findings highlighted per row), the sensitivity table (rank
  under each weight set, with rank-stability of the #1 model called out), the
  Kruskal-Wallis + Dunn statistics for latency and peak VRAM (read-only),
  accuracy detail (per-model, plus per-class and confusion matrices when present
  in the JSON), a **Resource cost per attack class** panel (from the
  `per_class` section) — mean/median latency, VRAM and tokens per attack class,
  by model and metric, framing *where resource cost concentrates by class* (a
  measurement view, not a per-class detection ranking), and a **Chapter 4 —
  analytical expansion** group (from the `advanced` section, all read-only):
  a **Pareto frontier** (accuracy vs mean latency and vs mean tokens, front
  marked), **latency stability (CoV)**, **prefill vs decode** split (mean TTFT vs
  wall−TTFT), **output throughput** (tokens/s), and **misclassification resource
  cost** (latency/tokens when right vs wrong — a resource comparison only, never a
  detection ranking).
A **Glossary** of the key terms sits at the end of Detailed Analysis.

Dropping the canonical `results/analysis/analysis.json` at `dashboard/analysis.json`
makes it auto-load when the page is served by `scripts/serve.py`; otherwise use
**Load results.json**. Running the benchmark on your own data is the service's job
(`/service`); reproducing the locked study is docs/REPRODUCE_BASELINE.md.

## Run it — simplest (no server)

Open the file directly in a browser:

```bash
# macOS
open dashboard/index.html
# Linux
xdg-open dashboard/index.html
# Windows
start dashboard/index.html
```

Or just double-click `dashboard/index.html`. It works fully offline over
`file://` — SAW table, sliders/presets, and per-agent chart all function.

It opens on the **bundled SAMPLE** (amber banner). To view your real results,
click **“Load analysis.json”** and pick your file, e.g.
`results/analysis/analysis.json` from an `analyze` run. The banner turns green to
confirm real data.

## Run it — local server (optional)

Only needed if you want the page to **auto-load** an `analysis.json` sitting next
to it. Browsers block the auto-`fetch` under `file://` (the harmless console
message you may see) — the **file picker always works** regardless. Over HTTP the
auto-load works:

```bash
cp results/analysis/analysis.json dashboard/analysis.json
cd dashboard
python -m http.server 8000
# then open http://localhost:8000/
```

## The sample data

`sample_analysis.json` lets the dashboard render immediately. Its
accuracy / composite / rank / tier and the “VRAM normalises to 1.0” finding are
the **locked real L4 results**. The normalised latency/token values are *solved*
so that under the default weights the composites reproduce those locked values
exactly — but their raw **magnitudes and all per-agent numbers are ILLUSTRATIVE
placeholders** (see the `notes.SAMPLE` field) until you load a real
`analysis.json`. Regenerate it with:

```bash
python scripts/gen_sample_analysis.py
```

## Files

| File | Purpose |
| --- | --- |
| `index.html` | The dashboard (self-contained; open via `file://`). |
| `saw.js` | SAW math module — mirrors `analyze._composite` / `_tier`; used by the live slider. |
| `sample_analysis.json` | Bundled illustrative sample conforming to the `analysis.json` schema. |
