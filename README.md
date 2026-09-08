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
| 5 | **Pilot run** (1 model, 5-10 scenarios) | ✅ code ready — run on GPU Space |

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
