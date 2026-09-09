"""AgentMeter — interactive local model-comparison demo (Gradio, CPU-only).

Reads STRICTLY from saved pilot JSON files (no live inference, no GPU). Pick any
two models produced by the pilot and compare them side by side:
  (1) per-agent wall time and VRAM,
  (2) per-agent token usage,
  (3) accuracy,
  (4) an overall "which model is more resource-efficient" summary.

Never fabricates numbers. If a file is missing or a metric is absent, it says so
rather than filling anything in.

Run locally:
    pip install gradio          # one-time (the only extra dependency)
    python scripts/compare_app.py            # reads ./results/*.json
    python scripts/compare_app.py --results-dir path/to/jsons --share

Data notes (from how the pilot measures):
  - Per-agent `vram_peak_mb` is the clean torch peak-allocated DELTA around each
    agent (peak reset before the node), valid regardless of resident weights.
  - `model_weight_vram_mb` is the resident weight footprint (torch allocator).
  - Device-level NVML `device_vram_after_load_mb` can be contaminated for a
    second model if the first was not fully freed, so it is NOT used here.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from typing import Any, Optional

import pandas as pd

AGENT_ORDER_FALLBACK = ["perceive", "reason", "decide", "act"]


# --------------------------------------------------------------------------
# Loading (strict; no fabrication)
# --------------------------------------------------------------------------
def _is_single_pilot(d: dict) -> bool:
    return isinstance(d, dict) and "per_agent_summary" in d and "model_label" in d


def discover_payloads(results_dir: str) -> dict[str, dict]:
    """Return {model_label: payload} from every pilot JSON in results_dir.

    Includes single-model files and expands any combined file's member models.
    """
    payloads: dict[str, dict] = {}
    for path in sorted(glob.glob(os.path.join(results_dir, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        if _is_single_pilot(data):
            payloads.setdefault(str(data["model_label"]), data)
        elif isinstance(data, dict) and data.get("kind") == "agentmeter_pilot_combined":
            for m in data.get("models", []):
                if _is_single_pilot(m):
                    payloads.setdefault(str(m["model_label"]), m)
    return payloads


# --------------------------------------------------------------------------
# Metric extraction helpers
# --------------------------------------------------------------------------
def _agents(payload: dict) -> list[str]:
    return list(payload.get("agents") or list(payload.get("per_agent_summary", {}).keys()))


def _agent_val(payload: dict, agent: str, key: str) -> Optional[float]:
    s = payload.get("per_agent_summary", {}).get(agent)
    if not s or key not in s:
        return None
    v = s[key]
    if isinstance(v, float) and v != v:  # NaN
        return None
    return v


def _mean_total_tokens_per_scenario(payload: dict) -> Optional[float]:
    tot = payload.get("per_scenario_totals") or {}
    vals = [(r.get("input_tokens", 0) + r.get("output_tokens", 0)) for r in tot.values()]
    return sum(vals) / len(vals) if vals else None


def _mean_peak_vram_per_scenario(payload: dict) -> Optional[float]:
    tot = payload.get("per_scenario_totals") or {}
    vals = [r.get("vram_mb") for r in tot.values() if r.get("vram_mb") is not None]
    return sum(vals) / len(vals) if vals else None


def _fmt(v, spec="{:.2f}", dash="—"):
    return dash if v is None else spec.format(v)


# --------------------------------------------------------------------------
# Comparison builders (return DataFrames + markdown; pure, testable)
# --------------------------------------------------------------------------
def per_agent_table(pa: dict, pb: dict, label_a: str, label_b: str) -> pd.DataFrame:
    agents = _agents(pa) or AGENT_ORDER_FALLBACK
    rows = []
    for ag in agents:
        rows.append({
            "Agent": ag.title(),
            f"{label_a} wall(s)": _fmt(_agent_val(pa, ag, "mean_wall_s")),
            f"{label_b} wall(s)": _fmt(_agent_val(pb, ag, "mean_wall_s")),
            f"{label_a} VRAM(MB)": _fmt(_agent_val(pa, ag, "mean_vram_mb"), "{:.0f}"),
            f"{label_b} VRAM(MB)": _fmt(_agent_val(pb, ag, "mean_vram_mb"), "{:.0f}"),
        })
    return pd.DataFrame(rows)


def per_agent_tokens_table(pa: dict, pb: dict, label_a: str, label_b: str) -> pd.DataFrame:
    agents = _agents(pa) or AGENT_ORDER_FALLBACK
    rows = []
    for ag in agents:
        rows.append({
            "Agent": ag.title(),
            f"{label_a} in-tok": _fmt(_agent_val(pa, ag, "mean_input_tokens"), "{:.0f}"),
            f"{label_b} in-tok": _fmt(_agent_val(pb, ag, "mean_input_tokens"), "{:.0f}"),
            f"{label_a} out-tok": _fmt(_agent_val(pa, ag, "mean_output_tokens"), "{:.0f}"),
            f"{label_b} out-tok": _fmt(_agent_val(pb, ag, "mean_output_tokens"), "{:.0f}"),
        })
    return pd.DataFrame(rows)


def long_metric_df(pa: dict, pb: dict, label_a: str, label_b: str, key: str) -> pd.DataFrame:
    """Long-format df for a grouped bar chart: columns Agent, Model, Value."""
    agents = _agents(pa) or AGENT_ORDER_FALLBACK
    rows = []
    for ag in agents:
        for label, p in ((label_a, pa), (label_b, pb)):
            v = _agent_val(p, ag, key)
            if v is not None:
                rows.append({"Agent": ag.title(), "Model": label, "Value": round(v, 3)})
    return pd.DataFrame(rows)


def accuracy_table(pa: dict, pb: dict, label_a: str, label_b: str) -> pd.DataFrame:
    def acc(p):
        a = p.get("accuracy")
        return None if a is None else a * 100
    return pd.DataFrame([
        {"Metric": "Accuracy (%)", label_a: _fmt(acc(pa), "{:.1f}"), label_b: _fmt(acc(pb), "{:.1f}")},
        {"Metric": "Correct / N",
         label_a: f"{pa.get('correct','—')}/{pa.get('n_scenarios','—')}",
         label_b: f"{pb.get('correct','—')}/{pb.get('n_scenarios','—')}"},
    ])


def efficiency_summary(pa: dict, pb: dict, label_a: str, label_b: str) -> str:
    """Data-driven resource-efficiency verdict. Resource dimensions only:
    latency, resident weight VRAM, mean peak activation VRAM, tokens/scenario.
    Accuracy is reported separately (it is not a resource cost)."""
    def cmp_lower(a, b):
        if a is None or b is None:
            return None
        if a < b:
            return label_a
        if b < a:
            return label_b
        return "tie"

    lat_a, lat_b = pa.get("mean_scenario_wall_s"), pb.get("mean_scenario_wall_s")
    wv_a, wv_b = pa.get("model_weight_vram_mb"), pb.get("model_weight_vram_mb")
    pv_a, pv_b = _mean_peak_vram_per_scenario(pa), _mean_peak_vram_per_scenario(pb)
    tk_a, tk_b = _mean_total_tokens_per_scenario(pa), _mean_total_tokens_per_scenario(pb)
    acc_a, acc_b = pa.get("accuracy"), pb.get("accuracy")

    dims = [
        ("Latency (mean s/scenario)", lat_a, lat_b, cmp_lower(lat_a, lat_b), "{:.2f}"),
        ("Resident weight VRAM (MB)", wv_a, wv_b, cmp_lower(wv_a, wv_b), "{:.0f}"),
        ("Peak activation VRAM (MB/scenario)", pv_a, pv_b, cmp_lower(pv_a, pv_b), "{:.0f}"),
        ("Tokens per scenario", tk_a, tk_b, cmp_lower(tk_a, tk_b), "{:.0f}"),
    ]

    lines = ["### Which model is more resource-efficient?", ""]
    lines.append(f"| Resource dimension | {label_a} | {label_b} | Winner (lower) |")
    lines.append("|---|---|---|---|")
    wins = {label_a: 0, label_b: 0}
    for name, a, b, winner, spec in dims:
        if winner in wins:
            wins[winner] += 1
        w = winner if winner else "n/a"
        lines.append(f"| {name} | {_fmt(a, spec)} | {_fmt(b, spec)} | {w} |")
    lines.append("")

    if wins[label_a] or wins[label_b]:
        if wins[label_a] > wins[label_b]:
            overall = label_a
        elif wins[label_b] > wins[label_a]:
            overall = label_b
        else:
            overall = None
        if overall:
            lines.append(f"**More resource-efficient overall: {overall}** "
                         f"(wins {max(wins.values())} of {wins[label_a]+wins[label_b]} "
                         f"comparable resource dimensions).")
        else:
            lines.append("**Resource efficiency is split** — each model wins some dimensions "
                         "(see table); the right choice depends on which resource you weight most.")
    else:
        lines.append("_Not enough comparable resource data to declare a winner._")

    # Accuracy reported separately.
    if acc_a is not None and acc_b is not None:
        if acc_a == acc_b:
            acc_line = f"Accuracy is tied ({acc_a*100:.1f}%)."
        else:
            better = label_a if acc_a > acc_b else label_b
            acc_line = (f"On **accuracy** (a separate axis from resource cost), "
                        f"**{better}** leads: {label_a} {acc_a*100:.1f}% vs {label_b} {acc_b*100:.1f}%.")
        lines += ["", acc_line]
    lines += ["", "_Small pilot (per-model n from the files); treat as indicative, not statistically "
              "significant. Both models run at the same quantization for fairness._"]
    return "\n".join(lines)


def header_md(pa: dict, pb: dict, label_a: str, label_b: str) -> str:
    def facts(p):
        return (f"quant={p.get('quantization','?')}, GPU={p.get('gpu_name','?')}, "
                f"load={_fmt(p.get('model_load_s'),'{:.0f}')}s, "
                f"weights={_fmt(p.get('model_weight_vram_mb'),'{:.0f}')}MB, "
                f"n={p.get('n_scenarios','?')}")
    return (f"## {label_a}  vs  {label_b}\n\n"
            f"- **{label_a}** — {facts(pa)}\n"
            f"- **{label_b}** — {facts(pb)}\n")


# --------------------------------------------------------------------------
# Gradio UI (gradio imported lazily so the compute code is testable without it)
# --------------------------------------------------------------------------
def build_ui(results_dir: str):
    import gradio as gr

    payloads = discover_payloads(results_dir)
    labels = list(payloads.keys())

    with gr.Blocks(title="AgentMeter — Model Comparison") as demo:
        gr.Markdown("# AgentMeter — Model Comparison\n"
                    "Reads only from saved pilot JSON files in "
                    f"`{results_dir}` — no live inference, no GPU.")

        if len(labels) < 2:
            gr.Markdown(
                f"### ⚠️ Need at least two pilot files to compare.\n\n"
                f"Found **{len(labels)}** in `{results_dir}`: "
                f"{', '.join(labels) if labels else '(none)'}.\n\n"
                "Put the per-model `pilot_*.json` files there (or `pilot_combined.json`) "
                "and restart. Nothing is fabricated for a missing model."
            )
            return demo

        default_b = labels[1] if len(labels) > 1 else labels[0]
        with gr.Row():
            dd_a = gr.Dropdown(labels, value=labels[0], label="Model A")
            dd_b = gr.Dropdown(labels, value=default_b, label="Model B")

        header = gr.Markdown()
        gr.Markdown("### 1 & 2. Per-agent wall time and VRAM")
        t_agent = gr.Dataframe(interactive=False)
        with gr.Row():
            p_wall = gr.BarPlot(x="Agent", y="Value", color="Model", title="Mean wall time per agent (s)",
                                y_title="seconds", tooltip=["Agent", "Model", "Value"])
            p_vram = gr.BarPlot(x="Agent", y="Value", color="Model", title="Mean peak VRAM per agent (MB)",
                                y_title="MB", tooltip=["Agent", "Model", "Value"])
        gr.Markdown("### Per-agent token usage")
        t_tokens = gr.Dataframe(interactive=False)
        p_tokens = gr.BarPlot(x="Agent", y="Value", color="Model", title="Mean output tokens per agent",
                              y_title="tokens", tooltip=["Agent", "Model", "Value"])
        gr.Markdown("### 3. Accuracy")
        t_acc = gr.Dataframe(interactive=False)
        gr.Markdown("### 4. Resource-efficiency summary")
        md_summary = gr.Markdown()

        def refresh(a, b):
            if a not in payloads or b not in payloads:
                miss = [x for x in (a, b) if x not in payloads]
                msg = f"### ⚠️ Missing data for: {', '.join(miss)} — not shown (never fabricated)."
                empty = pd.DataFrame()
                return (msg, empty, empty, empty, empty, empty, empty, msg)
            pa, pb = payloads[a], payloads[b]
            return (
                header_md(pa, pb, a, b),
                per_agent_table(pa, pb, a, b),
                long_metric_df(pa, pb, a, b, "mean_wall_s"),
                long_metric_df(pa, pb, a, b, "mean_vram_mb"),
                per_agent_tokens_table(pa, pb, a, b),
                long_metric_df(pa, pb, a, b, "mean_output_tokens"),
                accuracy_table(pa, pb, a, b),
                efficiency_summary(pa, pb, a, b),
            )

        outputs = [header, t_agent, p_wall, p_vram, t_tokens, p_tokens, t_acc, md_summary]
        dd_a.change(refresh, [dd_a, dd_b], outputs)
        dd_b.change(refresh, [dd_a, dd_b], outputs)
        demo.load(refresh, [dd_a, dd_b], outputs)

    return demo


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AgentMeter model comparison demo (Gradio).")
    ap.add_argument("--results-dir", default="results", help="folder with pilot_*.json files")
    ap.add_argument("--share", action="store_true", help="create a public Gradio link")
    ap.add_argument("--port", type=int, default=7860)
    args = ap.parse_args(argv)

    if not os.path.isdir(args.results_dir):
        print(f"ERROR: results dir not found: {args.results_dir}")
        return 1
    demo = build_ui(args.results_dir)
    demo.launch(server_port=args.port, share=args.share)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
