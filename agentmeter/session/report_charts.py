"""Small SVG charts for the PDF report, drawn from plain numbers (no plotting library).

Each function returns an <svg> string sized by its viewBox, so it scales to its column. Colours
come from the caller (one colour per model, the app's model colours). Text is escaped.
"""
from __future__ import annotations

import math
from html import escape
from typing import Callable, Optional, Sequence

INK, MUTED, FAINT, GRID, PAPER, GOOD = "#0F1B33", "#5A657A", "#8A94A8", "#DCE2EE", "#F6F8FC", "#EEF8D6"

# The app's model colours (web/app.html --m-*, light theme), matched on the model family.
FAMILY = [("qwen", "#0B86A6"), ("llama", "#2E5BD8"), ("mistral", "#C2621A"), ("phi", "#7350D0"),
          ("gemma", "#C23D7E")]
EXTRA = ["#6B7690", "#3E8E5E", "#A2470D", "#1E4BB8"]
AGENT = ["#1E4BB8", "#4F7FE0", "#A2470D", "#E8742A", "#3E8E5E", "#7350D0"]


def model_colors(models: Sequence[str]) -> dict[str, str]:
    out, used, extra = {}, set(), iter(EXTRA)
    for m in models:
        low = str(m).lower()
        c = next((c for k, c in FAMILY if k in low and c not in used), None) or next(extra, "#6B7690")
        out[m] = c
        used.add(c)
    return out


def _t(x: float, y: float, s: str, size: float = 9, fill: str = FAINT, anchor: str = "start",
       weight: int = 400, extra: str = "") -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
            f'font-weight="{weight}"{extra}>{escape(str(s))}</text>')


def nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    if hi <= lo:
        hi = lo + (abs(lo) or 1.0)
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.5 and len(ticks) < 12:
        ticks.append(round(v, 12))
        v += step
    return ticks


def scatter(points: list[dict], *, x_label: str, y_label: str, x_fmt: Callable[[float], str],
            y_fmt: Callable[[float], str], width: int = 360, height: int = 250,
            good_corner: Optional[str] = "fast and cheap", labels: bool = True) -> str:
    """points: {label, x, y, color}. Lower-left is better on both axes."""
    pts = [p for p in points if p.get("x") is not None and p.get("y") is not None]
    if not pts:
        return ""
    L, T, R, B = 52, 12, width - 14, height - 42
    xs, ys = [p["x"] for p in pts], [p["y"] for p in pts]
    xt = nice_ticks(min(xs) * 0.9, max(xs) * 1.1)
    yt = nice_ticks(min(ys) * 0.9, max(ys) * 1.1)
    x0, x1, y0, y1 = xt[0], xt[-1], yt[0], yt[-1]
    sx = lambda v: L + (v - x0) / ((x1 - x0) or 1) * (R - L)                       # noqa: E731
    sy = lambda v: B - (v - y0) / ((y1 - y0) or 1) * (B - T)                       # noqa: E731
    o = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{escape(y_label)} against {escape(x_label)}">',
         f'<rect x="{L}" y="{T}" width="{R - L}" height="{B - T}" fill="{PAPER}"/>']
    if good_corner and len(pts) > 1:
        o.append(f'<rect x="{L}" y="{(T + B) / 2:.1f}" width="{(R - L) / 2:.1f}" height="{(B - T) / 2:.1f}" fill="{GOOD}"/>')
        o.append(_t(L + 6, B - 6, good_corner, 9, "#5B7A1C"))
    for v in xt:
        o.append(f'<line x1="{sx(v):.1f}" y1="{T}" x2="{sx(v):.1f}" y2="{B}" stroke="{GRID}"/>')
        o.append(_t(sx(v), B + 13, x_fmt(v), 9, FAINT, "middle"))
    for v in yt:
        o.append(f'<line x1="{L}" y1="{sy(v):.1f}" x2="{R}" y2="{sy(v):.1f}" stroke="{GRID}"/>')
        o.append(_t(L - 6, sy(v) + 3, y_fmt(v), 9, FAINT, "end"))
    o.append(_t((L + R) / 2, height - 8, x_label, 10, MUTED, "middle"))
    o.append(_t(12, (T + B) / 2, y_label, 10, MUTED, "middle", extra=f' transform="rotate(-90 12 {(T + B) / 2:.1f})"'))
    for p in sorted(pts, key=lambda p: p["x"]):
        cx, cy = sx(p["x"]), sy(p["y"])
        right = cx < (L + R) * 0.62
        o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{7 if labels else 6}" fill="{p["color"]}" stroke="#fff" stroke-width="1.5" opacity="0.92"/>')
        if labels:
            o.append(_t(cx + (11 if right else -11), cy + 3.5, p["label"], 10, INK, "start" if right else "end", 700))
    o.append("</svg>")
    return "".join(o)


def histograms(models: list[dict], *, edges: list[float], x_fmt: Callable[[float], str],
               width: int = 520, row_h: int = 58) -> str:
    """models: {label, color, bins:[count], marks:[{label, x}]}. One small histogram per model, shared x axis."""
    if not models or len(edges) < 2:
        return ""
    L, R = 92, width - 12
    lo, hi = edges[0], edges[-1]
    sx = lambda v: L + (v - lo) / ((hi - lo) or 1) * (R - L)                         # noqa: E731
    ymax = max((max(m["bins"] or [0]) for m in models), default=1) or 1
    height = row_h * len(models) + 26
    o = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="Latency distribution per model">']
    for i, m in enumerate(models):
        top = i * row_h + 6
        base = top + row_h - 14
        o.append(_t(4, top + 18, m["label"], 10, INK, "start", 600))
        o.append(f'<line x1="{L}" y1="{base}" x2="{R}" y2="{base}" stroke="{GRID}"/>')
        bw = (R - L) / len(m["bins"])
        for j, c in enumerate(m["bins"]):
            if not c:
                continue
            h = (row_h - 22) * c / ymax
            o.append(f'<rect x="{L + j * bw + 0.6:.1f}" y="{base - h:.1f}" width="{max(bw - 1.2, 0.8):.1f}" height="{h:.1f}" fill="{m["color"]}" rx="1"/>')
        for k in m.get("marks") or []:
            x = sx(k["x"])
            o.append(f'<line x1="{x:.1f}" y1="{top + 4}" x2="{x:.1f}" y2="{base}" stroke="{INK}" stroke-dasharray="2 2" stroke-width="0.8"/>')
            near_end = x > R - 24
            o.append(_t(x - 2 if near_end else x + 2, top + 10, k["label"], 8, MUTED, "end" if near_end else "start"))
    axis_y = row_h * len(models) + 6
    for v in nice_ticks(lo, hi):
        if lo <= v <= hi:
            o.append(_t(sx(v), axis_y + 12, x_fmt(v), 9, FAINT, "middle"))
    o.append("</svg>")
    return "".join(o)


def lines(series: list[dict], *, y_fmt: Callable[[float], str], x_label: str, y_label: str = "",
          width: int = 520, height: int = 200, y_min: Optional[float] = None) -> str:
    """series: {label, color, points:[(x, y)], faint_points:[(x, y)]?}. A line chart with a shared x axis."""
    allp = [p for s in series for p in (s.get("points") or []) + (s.get("faint_points") or []) if p[1] is not None]
    if not allp:
        return ""
    L, T, R, B = 52, 10, width - 12, height - 34
    xs, ys = [p[0] for p in allp], [p[1] for p in allp]
    xt = nice_ticks(min(xs), max(xs), 5)
    yt = nice_ticks(min(ys) if y_min is None else y_min, max(ys) * 1.05)
    x0, x1, y0, y1 = xt[0], xt[-1], yt[0], yt[-1]
    sx = lambda v: L + (v - x0) / ((x1 - x0) or 1) * (R - L)                       # noqa: E731
    sy = lambda v: B - (v - y0) / ((y1 - y0) or 1) * (B - T)                       # noqa: E731
    o = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{escape(y_label or "value")} over {escape(x_label)}">']
    for v in yt:
        o.append(f'<line x1="{L}" y1="{sy(v):.1f}" x2="{R}" y2="{sy(v):.1f}" stroke="{GRID}"/>')
        o.append(_t(L - 6, sy(v) + 3, y_fmt(v), 9, FAINT, "end"))
    for v in xt:
        o.append(_t(sx(v), B + 13, f"{v:g}", 9, FAINT, "middle"))
    o.append(_t((L + R) / 2, height - 6, x_label, 9.5, MUTED, "middle"))
    for s in series:
        for x, y in s.get("faint_points") or []:
            if y is not None:
                o.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="1.6" fill="{s["color"]}" opacity="0.35"/>')
        pts = [(sx(x), sy(y)) for x, y in s.get("points") or [] if y is not None]
        if len(pts) > 1:
            o.append(f'<polyline fill="none" stroke="{s["color"]}" stroke-width="1.8" points="' +
                     " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '"/>')
    o.append("</svg>")
    return "".join(o)


def stacked(parts: list[dict], *, height: int = 22) -> str:
    """parts: {label, value, color}. One 100% stacked bar (HTML, so labels wrap and fit)."""
    tot = sum(p["value"] or 0 for p in parts) or 1
    segs = []
    for p in parts:
        w = 100 * (p["value"] or 0) / tot
        lab = escape(p["label"]) if w >= 9 else ""
        segs.append(f'<div style="width:{w:.2f}%;background:{p["color"]}">{lab}</div>')
    return f'<div class="stack" style="height:{height}px">{"".join(segs)}</div>'
