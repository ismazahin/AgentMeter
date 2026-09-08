"""AgentMeter — Phase 5 pilot as a Hugging Face Space (Gradio).

Deploy on a Space with 1x NVIDIA L4 (24 GB). Add your HF_TOKEN as a Space
secret (Settings -> Variables and secrets) and accept the gated model licence.

This app is a thin UI over agentmeter.pilot.run_pilot — it loads ONE real model
in-process on the GPU, runs the instrumented 4-agent pipeline over a few
scenarios sequentially, and shows the per-agent timing/VRAM/token table plus a
projection and pilot accuracy.

IMPORTANT: running this loads a 7B model on a paid GPU. PAUSE the Space
(Settings -> Pause) when you are done so you are not billed while idle.

Expected Space layout (all at the Space repo root):
    app.py            (this file)
    requirements.txt  (from space/requirements.txt)
    README.md         (from space/README.md, has the Space YAML header)
    agentmeter/       (the package)
    configs/          (pilot_mistral_l4.yaml etc.)
    data/             (demo_flows.csv or your real CSV)
    main.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gradio as gr

DEFAULT_CONFIG = "configs/pilot_mistral_l4.yaml"


def _gpu_banner() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            total = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            return f"✅ GPU detected: {name} ({total:.0f} GB). VRAM will be measured."
        return ("⛔ No CUDA GPU detected. The pilot enforces GPU (require_gpu: true) "
                "and will STOP — switch the Space hardware to an L4 GPU.")
    except Exception as e:
        return f"⚠️ Could not query GPU: {e}"


def _token_banner() -> str:
    return ("✅ HF_TOKEN is set." if os.environ.get("HF_TOKEN")
            else "⛔ HF_TOKEN not set. Add it under Settings → Variables and secrets "
                 "(needed for gated models).")


def run(config_path: str, n_scenarios: int, proj_scenarios: int, proj_models: int):
    from agentmeter.pilot import run_pilot

    try:
        result = run_pilot(
            config_path=config_path or DEFAULT_CONFIG,
            n=int(n_scenarios),
            proj_scenarios=int(proj_scenarios),
            proj_models=int(proj_models),
            json_path="results/pilot.json",
        )
    except Exception as e:  # surface the STOP guard and load errors cleanly
        return f"ERROR:\n{e}", None

    return result.report, result.payload.get("_json_path")


with gr.Blocks(title="AgentMeter — Pilot") as demo:
    gr.Markdown(
        "# AgentMeter — Phase 5 Pilot\n"
        "Per-agent resource benchmarking of an LLM in a linear "
        "Perceive→Reason→Decide→Act threat-detection pipeline (Mode A, in-process on GPU).\n\n"
        "**⚠️ Running loads a 7B model on a paid GPU. Pause the Space when done.**"
    )
    gr.Markdown(_gpu_banner())
    gr.Markdown(_token_banner())

    with gr.Row():
        config_in = gr.Textbox(value=DEFAULT_CONFIG, label="Pilot config path")
        n_in = gr.Slider(1, 10, value=8, step=1, label="Scenarios (pilot: 5–10)")
    with gr.Row():
        proj_scn = gr.Number(value=1000, label="Projection: scenarios")
        proj_mdl = gr.Number(value=2, label="Projection: models")

    run_btn = gr.Button("Run pilot", variant="primary")
    report_out = gr.Textbox(label="Pilot report", lines=28, show_copy_button=True)
    json_out = gr.File(label="Download pilot.json")

    run_btn.click(run, [config_in, n_in, proj_scn, proj_mdl], [report_out, json_out])


if __name__ == "__main__":
    demo.launch()
