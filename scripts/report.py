"""Render a pilot.json into a self-contained HTML report for presentation.

Lightweight by design: pure Python standard library only — no matplotlib, no
Gradio, no pandas, no server, no deployment. Charts are inline SVG. The output
is a single .html file you open in any browser (great for a projector), or
display inline in Colab.

Usage:
    python scripts/report.py                         # reads results/pilot.json
    python scripts/report.py path/to/pilot.json      # explicit input
    python scripts/report.py pilot.json -o out.html  # explicit output

In Colab:
    !python scripts/report.py results/pilot.json
    from IPython.display import HTML, display
    display(HTML(open('results/pilot_report.html').read()))
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import os
import sys

# --- palette (accessible, single base hue + warm highlight for the max) ---
C_BASE = "#4f6bed"      # bars
C_MAX = "#e8590c"       # most-expensive agent
C_INK = "#1a1a2e"
C_MUTE = "#5a6072"
C_OK_BG = "#e6f4ea"
C_OK_FG = "#1e7e34"
C_BAD_BG = "#fdecea"
C_BAD_FG = "#c0392b"
C_CARD = "#ffffff"
C_BG = "#f4f5f8"
C_BORDER = "#e2e5ec"


def _fmt(v, spec="{:.1f}"):
    if v is None:
        return "n/a"
    try:
        if isinstance(v, float) and (v != v):  # NaN
            return "n/a"
        return spec.format(v)
    except (ValueError, TypeError):
        return str(v)


def _per_agent(data: dict) -> dict:
    """Prefer the stored summary; otherwise compute means from metrics_rows."""
    if data.get("per_agent_summary"):
        return data["per_agent_summary"]
    agg: dict[str, dict] = {}
    for r in data.get("metrics_rows", []):
        a = agg.setdefault(r["agent_name"], {"w": [], "v": [], "i": [], "o": [], "t": []})
        a["w"].append(r.get("wall_time_s") or 0)
        if r.get("vram_peak_mb") is not None:
            a["v"].append(r["vram_peak_mb"])
        a["i"].append(r.get("input_tokens") or 0)
        a["o"].append(r.get("output_tokens") or 0)
        if r.get("ttft_s") is not None:
            a["t"].append(r["ttft_s"])
    mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    return {
        name: {
            "mean_wall_s": mean(a["w"]),
            "mean_vram_mb": mean(a["v"]),
            "mean_input_tokens": mean(a["i"]),
            "mean_output_tokens": mean(a["o"]),
            "mean_ttft_s": mean(a["t"]),
            "n": len(a["w"]),
        }
        for name, a in agg.items()
    }


def _hbar_chart(title: str, unit: str, rows: list[tuple[str, float]]) -> str:
    """Horizontal bar chart as inline SVG. rows = [(label, value), ...].

    The largest bar is highlighted and annotated as the most expensive agent.
    """
    rows = [(n, (0.0 if (v is None or v != v) else float(v))) for n, v in rows]
    vmax = max((v for _, v in rows), default=0.0) or 1.0
    max_name = max(rows, key=lambda r: r[1])[0] if rows else None

    label_w, val_w, pad = 92, 88, 16
    row_h, top = 46, 44
    width = 460
    bar_x = label_w + pad
    bar_full = width - bar_x - val_w
    height = top + row_h * len(rows) + 12

    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
           f'aria-label="{html.escape(title)}" font-family="inherit">']
    out.append(f'<text x="0" y="24" font-size="17" font-weight="700" fill="{C_INK}">{html.escape(title)}</text>')
    for i, (name, val) in enumerate(rows):
        y = top + i * row_h
        bw = max(2.0, bar_full * (val / vmax))
        is_max = name == max_name
        color = C_MAX if is_max else C_BASE
        out.append(f'<text x="{label_w}" y="{y+22}" font-size="14.5" text-anchor="end" '
                   f'fill="{C_INK}" font-weight="{700 if is_max else 500}">{html.escape(name)}</text>')
        out.append(f'<rect x="{bar_x}" y="{y+6}" width="{bw:.1f}" height="26" rx="5" fill="{color}"/>')
        out.append(f'<text x="{bar_x+bw+8:.1f}" y="{y+24}" font-size="14" fill="{C_MUTE}">'
                   f'{_fmt(val, "{:.1f}")} {html.escape(unit)}</text>')
        if is_max:
            out.append(f'<text x="{bar_x+6}" y="{y+24}" font-size="12" fill="#fff" '
                       f'font-weight="700">most costly</text>')
    out.append('</svg>')
    return "\n".join(out)


def build_html(data: dict, source: str) -> str:
    agents = data.get("agents") or list(_per_agent(data).keys())
    pa = _per_agent(data)
    order = [a for a in agents if a in pa]

    # header facts
    model = data.get("model_label", "?")
    quant = data.get("quantization", "none")
    gpu = data.get("gpu_name") or ("CPU" if not data.get("gpu_available") else "GPU")
    load_s = data.get("model_load_s")
    weight_vram = data.get("model_weight_vram_mb")
    dev_vram = data.get("device_vram_after_load_mb")
    n = data.get("n_scenarios", len(data.get("verdicts", [])))
    mean_scn = data.get("mean_scenario_wall_s")
    proj = data.get("projection", {}) or {}
    proj_s = proj.get("total_seconds")
    acc = data.get("accuracy")
    correct = data.get("correct")

    def card(label, value, sub=""):
        subhtml = f'<div class="cs">{html.escape(sub)}</div>' if sub else ""
        return (f'<div class="card"><div class="cl">{html.escape(label)}</div>'
                f'<div class="cv">{value}</div>{subhtml}</div>')

    proj_h = ""
    if proj_s is not None:
        proj_h = f"{proj_s/3600:.1f} h"
        proj_sub = f"{proj.get('scenarios','?')} scenarios × {proj.get('models','?')} models"
    else:
        proj_sub = ""

    model_short = str(model).split("/")[-1]  # drop the org/ prefix for the card
    cards = "\n".join([
        card("Model", f'<span class="mono">{html.escape(model_short)}</span>'),
        card("Quantization", html.escape(str(quant))),
        card("GPU", html.escape(str(gpu))),
        card("Model load", _fmt(load_s, "{:.0f}") + " s" if load_s else "n/a",
             (f"{weight_vram:,.0f} MB weights" if weight_vram else "")),
        card("Mean / scenario", _fmt(mean_scn, "{:.1f}") + " s" if mean_scn else "n/a",
             f"{n} scenarios"),
        card("Full-run projection", proj_h or "n/a", proj_sub),
    ])

    # per-agent table
    trs = []
    for a in order:
        s = pa[a]
        trs.append(
            f"<tr><td class='ag'>{html.escape(a.title())}</td>"
            f"<td>{_fmt(s.get('mean_wall_s'),'{:.2f}')}</td>"
            f"<td>{_fmt(s.get('mean_ttft_s'),'{:.2f}')}</td>"
            f"<td>{_fmt(s.get('mean_vram_mb'),'{:.0f}')}</td>"
            f"<td>{_fmt(s.get('mean_input_tokens'),'{:.0f}')}</td>"
            f"<td>{_fmt(s.get('mean_output_tokens'),'{:.0f}')}</td></tr>"
        )
    agent_table = "\n".join(trs)

    # charts
    time_chart = _hbar_chart("Mean wall time per agent", "s",
                             [(a.title(), pa[a].get("mean_wall_s")) for a in order])
    vram_chart = _hbar_chart("Mean peak VRAM per agent", "MB",
                             [(a.title(), pa[a].get("mean_vram_mb")) for a in order])

    # verdicts
    vrows = []
    for v in data.get("verdicts", []):
        ok = v.get("correct")
        cls = "ok" if ok else "bad"
        mark = "✓ correct" if ok else "✗ wrong"
        vrows.append(
            f"<tr class='{cls}'><td class='mono'>{html.escape(str(v.get('scenario_id','')))}</td>"
            f"<td>{html.escape(str(v.get('predicted_class','')))}</td>"
            f"<td>{html.escape(str(v.get('held_out_label','')))}</td>"
            f"<td class='mk'>{mark}</td></tr>"
        )
    verdict_table = "\n".join(vrows)
    acc_pct = f"{acc*100:.1f}%" if isinstance(acc, (int, float)) else "n/a"
    acc_sub = f"{correct}/{n} correct" if correct is not None else ""

    gen = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentMeter — Pilot Results</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:{C_BG}; color:{C_INK};
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 32px 24px 56px; }}
  h1 {{ font-size: 30px; margin: 0 0 2px; }}
  .sub {{ color:{C_MUTE}; margin: 0 0 24px; font-size: 15px; }}
  h2 {{ font-size: 20px; margin: 36px 0 14px; }}
  .cards {{ display:grid; grid-template-columns: repeat(auto-fit,minmax(158px,1fr)); gap:12px; }}
  .card {{ background:{C_CARD}; border:1px solid {C_BORDER}; border-radius:12px; padding:14px 16px; }}
  .cl {{ font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:{C_MUTE}; }}
  .cv {{ font-size:20px; font-weight:700; margin-top:4px; word-break:break-word; }}
  .cs {{ font-size:12px; color:{C_MUTE}; margin-top:3px; }}
  table {{ width:100%; border-collapse:collapse; background:{C_CARD};
    border:1px solid {C_BORDER}; border-radius:12px; overflow:hidden; font-size:15px; }}
  th, td {{ padding:11px 14px; text-align:right; border-bottom:1px solid {C_BORDER}; }}
  th:first-child, td:first-child {{ text-align:left; }}
  th {{ background:#eef0f6; font-size:12.5px; text-transform:uppercase; letter-spacing:.03em; color:{C_MUTE}; }}
  tr:last-child td {{ border-bottom:none; }}
  td.ag {{ font-weight:700; }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:13px; }}
  .charts {{ display:grid; grid-template-columns: 1fr 1fr; gap:24px; }}
  .panel {{ background:{C_CARD}; border:1px solid {C_BORDER}; border-radius:12px; padding:18px 20px; }}
  @media (max-width: 720px) {{ .charts {{ grid-template-columns:1fr; }} }}
  .accrow {{ display:flex; align-items:center; gap:20px; margin-bottom:14px; }}
  .bignum {{ font-size:44px; font-weight:800; color:{C_BASE}; }}
  tr.ok td.mk {{ color:{C_OK_FG}; font-weight:700; }}
  tr.bad td.mk {{ color:{C_BAD_FG}; font-weight:700; }}
  tr.ok td:first-child {{ box-shadow: inset 4px 0 0 {C_OK_FG}; }}
  tr.bad td:first-child {{ box-shadow: inset 4px 0 0 {C_BAD_FG}; }}
  .foot {{ margin-top:34px; color:{C_MUTE}; font-size:12.5px; }}
  .note {{ background:#eef2ff; border:1px solid #dfe4ff; border-radius:10px; padding:12px 16px;
    font-size:13.5px; color:#3a3f63; margin-top:14px; }}
</style></head>
<body><div class="wrap">
  <h1>AgentMeter — Pilot Results</h1>
  <p class="sub">Per-agent resource benchmarking of a linear Perceive → Reason → Decide → Act
     threat-detection pipeline. The pipeline is the test subject; the contribution is the measurement.</p>

  <div class="cards">{cards}</div>

  <h2>Per-agent cost breakdown</h2>
  <table>
    <thead><tr><th>Agent</th><th>Mean wall (s)</th><th>Mean TTFT (s)</th>
      <th>Mean VRAM (MB)</th><th>Mean in-tok</th><th>Mean out-tok</th></tr></thead>
    <tbody>{agent_table}</tbody>
  </table>
  <div class="note"><b>Read the two dimensions separately.</b> Wall time is driven mainly by how
     many tokens an agent <i>generates</i>; peak VRAM is driven by how long its <i>input context</i>
     is (KV-cache). The most expensive agent can differ between the two — that is the point of
     per-agent instrumentation.</div>

  <h2>Which agent costs the most?</h2>
  <div class="charts">
    <div class="panel">{time_chart}</div>
    <div class="panel">{vram_chart}</div>
  </div>

  <h2>Accuracy &amp; per-scenario verdicts</h2>
  <div class="accrow"><div class="bignum">{acc_pct}</div>
     <div class="sub" style="margin:0">detection accuracy vs held-out ground truth &middot; {html.escape(acc_sub)}</div></div>
  <table>
    <thead><tr><th>Scenario</th><th>Predicted</th><th>Actual (ground truth)</th><th>Result</th></tr></thead>
    <tbody>{verdict_table}</tbody>
  </table>

  <p class="foot">Generated {gen} from <span class="mono">{html.escape(os.path.basename(source))}</span>.
     Research benchmarking study — not a production security product.</p>
</div></body></html>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render pilot.json into an HTML report.")
    ap.add_argument("input", nargs="?", default="results/pilot.json", help="path to pilot.json")
    ap.add_argument("-o", "--output", default=None, help="output .html path")
    args = ap.parse_args(argv)

    if not os.path.exists(args.input):
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        return 1
    with open(args.input, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    out = args.output or os.path.join(os.path.dirname(args.input) or ".", "pilot_report.html")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(build_html(data, args.input))

    print(f"Wrote report -> {out}")
    print("Open it in a browser (double-click), or in Colab:")
    print("  from IPython.display import HTML, display")
    print(f"  display(HTML(open({out!r}).read()))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
