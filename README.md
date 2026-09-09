# AgentMeter

Benchmarking open-source LLM **resource efficiency** in agentic network
threat-detection pipelines.

> The agentic pipeline is only the **test subject**. The contribution is the
> **measurement harness**, the **per-agent instrumentation** (wall time, TTFT,
> peak VRAM, tokens), and the **comparison methodology**.

## Status

Built incrementally, phase by phase. Currently: **Phase 0 (skeleton + env check)**.

| Phase | Deliverable | State |
|-------|-------------|-------|
| 0 | Skeleton, `config.yaml`, deps, env check | ✅ |
| 1 | Dataset loader + data isolation + prompt builder | ✅ |
| 2 | Minimal 4-agent LangGraph pipeline (linear) | ✅ |
| 3 | Per-agent instrumentation (time, tokens, VRAM) | ✅ |
| 4 | HF in-process model adapter (Mode A) | ✅ (code + offline smoke) |
| 5 | **Pilot run** (1 model, 5-10 scenarios) | ✅ code ready — run on Colab (T4) or HF Space (L4) |

**Running the pilot (needs a GPU):**
- **Google Colab (free T4, 15 GB):** open `notebooks/agentmeter_pilot_colab.ipynb`
  in Colab, set a T4 runtime, run the cells. Uses **4-bit NF4** quantization
  (`configs/pilot_colab_t4.yaml`) because a 7-8B model does not fit in fp16 on a T4.
  Quantization is a declared methodology change — see the notebook's notice.
- **HF Space (L4, 24 GB):** see `space/SPACE_SETUP.md`; uses fp16
  (`configs/pilot_mistral_l4.yaml`). Incurs GPU cost — pause the Space after.

**Presenting the results (CPU-only, no GPU, no deps):**
```bash
python scripts/report.py results/pilot.json     # -> results/pilot_report.html
```
Open the HTML in any browser (great on a projector): a per-agent cost table,
time & VRAM bar charts (which agent costs most), accuracy, and per-scenario
verdicts. In Colab it renders inline (last notebook cell).

**Comparing two models side by side (interactive, local, CPU-only):**
```bash
pip install -r requirements-demo.txt     # gradio + pandas (one-time)
python scripts/compare_app.py            # reads ./results/*.json, opens in browser
```
Pick any two models produced by the pilot (`pilot_<model>.json` or
`pilot_combined.json`) and compare per-agent wall time & VRAM, per-agent tokens,
accuracy, and a resource-efficiency summary. Reads strictly from saved JSON —
never fabricates; if a model file is missing it says so.

Phases 6-10 are intentionally **not** built yet.

## Design rules (hard constraints)

- Pipeline is a **minimal linear chain**: Perceive → Reason → Decide → Act.
  No governance, correlation, alerting, or retry/self-correction loops.
- **Sequential execution only** — never run models in parallel (contaminates
  resource readings).
- **Mode A** only: pull the model from the HF Hub and run it **in-process** on
  the GPU so VRAM is measurable. No Inference Endpoints.
- All config (model, dataset, weights, thresholds, run mode) lives in
  `config.yaml`. Nothing hard-coded.
- If a GPU is required (`run.require_gpu: true`, Phase 5) and `torch.cuda` is
  unavailable, the harness **stops** — no silent CPU fallback.

## Setup

Phases 0-3 need **no** Hugging Face account, token, or GPU.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # CPU / mock deps
python main.py check-env               # Phase 0 environment check
```

GPU dependencies (Phases 4-5, install on the GPU host only):

```bash
pip install -r requirements-gpu.txt
```

## Configuration

See [`config.yaml`](config.yaml). Key switches:

- `model.provider`: `mock` (Phases 0-3, no GPU) or `hf` (Phase 4+, in-process GPU).
- `run.require_gpu`: `false` for Phases 0-3, `true` for the Phase 5 pilot.
- `dataset.limit`: pilot uses 5-10 scenarios.
- `scoring.weights`: SAW weights (Accuracy 40 / Latency 25 / VRAM 20 / Token 15).
