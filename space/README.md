---
title: AgentMeter Pilot
emoji: 📊
colorFrom: indigo
colorTo: blue
sdk: gradio
app_file: app.py
pinned: false
---

# AgentMeter — Pilot Space

Per-agent resource benchmarking of an open-source LLM in a minimal linear
Perceive → Reason → Decide → Act network-threat-detection pipeline. Mode A:
the model is pulled from the HF Hub and run **in-process on the GPU**, so
per-agent VRAM is measurable.

> This is a research benchmarking harness, not a production security product.
> The pipeline is only the test subject; the contribution is the measurement.

## Hardware

Set the Space hardware to **1× NVIDIA L4 (24 GB)**. The pilot enforces GPU and
will STOP on CPU (VRAM is a core requirement).

## Setup

1. Space **Settings → Variables and secrets** → add secret `HF_TOKEN` (a read
   token). Accept the gated model licence on the model's Hub page first.
2. Ensure these sit at the Space repo root: `app.py`, `requirements.txt`, this
   `README.md`, plus the `agentmeter/`, `configs/`, `data/` folders and `main.py`.
3. Open the app, confirm the GPU/token banners are green, click **Run pilot**.

## ⚠️ Cost

Running loads a 7B model on a paid GPU (~\$0.80/hr for the L4). **Pause the
Space** (Settings → Pause) as soon as the run finishes so you are not billed
while idle.
