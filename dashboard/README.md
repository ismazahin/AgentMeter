# AgentMeter Dashboard (Phase 10 Part A)

A single self-contained results dashboard. It **reads a precomputed
`analysis.json`** (produced by `agentmeter/analyze.py`) and displays it. It is a
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

## Not included

The live “proof-of-life” run button (Phase 10 **Part B** — Firebase-backed
single-scenario execution on a GPU) is intentionally deferred and is **not** part
of this build. This dashboard only reads and displays existing results.

## Files

| File | Purpose |
| --- | --- |
| `index.html` | The dashboard (self-contained; open via `file://`). |
| `saw.js` | SAW math module — mirrors `analyze._composite` / `_tier`; used by the live slider. |
| `sample_analysis.json` | Bundled illustrative sample conforming to the `analysis.json` schema. |
