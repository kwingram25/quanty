"""Server-rendered SVG charts. Hover (crosshair + tooltip) is added by static/chart.js from the
JSON each chart embeds; the chart reads fully without it."""

import json
import math
import time
from html import escape

from markupsafe import Markup

SLOTS = 8  # categorical colors (--series-1..8 in style.css), assigned in fixed order


def color_slots(names: list[str]) -> dict[str, int]:
    """Entity -> slot, by sorted name, so a strategy keeps its color whatever else is shown.
    Past eight, the rest share the last slot (and are told apart by the legend and tooltip)."""
    return {n: min(i, SLOTS - 1) + 1 for i, n in enumerate(sorted(names))}


def _nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks = []
    t = start
    while t <= hi + step * 0.5:
        ticks.append(round(t, 10))
        t += step
    return ticks


def _json(data: object) -> str:
    """JSON safe inside <script> (which is not HTML-decoded): only "</" needs guarding."""
    return json.dumps(data).replace("</", "<\\/")


def _hm(ms: int, seconds: bool = False) -> str:
    return time.strftime("%H:%M:%S" if seconds else "%H:%M", time.gmtime(ms / 1000))


def _money(v: float) -> str:
    return f"{'−' if v < -0.005 else ''}${abs(v):,.2f}"


def step_chart(
    series: dict[str, list[tuple[int, float]]],
    slots: dict[str, int],
    *,
    label: str,
    width: int = 760,
    height: int = 260,
) -> Markup:
    """Cumulative values that change only at events (realized P&L), as step-after lines on one axis."""
    pts = [p for s in series.values() for p in s]
    if not pts:
        return Markup('<p class="empty">No realized P&amp;L yet.</p>')
    t0, t1 = min(p[0] for p in pts), max(p[0] for p in pts)
    if t1 - t0 < 60_000:
        t0, t1 = t0 - 30_000, t1 + 30_000
    ticks = _nice_ticks(min(0.0, *(p[1] for p in pts)), max(0.0, *(p[1] for p in pts)))
    y0, y1 = ticks[0], ticks[-1]
    direct = len(series) <= 4
    pad_l, pad_r, pad_t, pad_b = 64, (130 if direct else 16), 12, 28
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b

    def x(t: int) -> float:
        return pad_l + (t - t0) / (t1 - t0) * pw

    def y(v: float) -> float:
        return pad_t + (y1 - v) / (y1 - y0) * ph

    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">']
    for v in ticks:
        cls = "baseline" if abs(v) < 1e-9 else "grid"
        out.append(f'<line class="{cls}" x1="{pad_l}" x2="{pad_l + pw}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        out.append(
            f'<text class="tick" x="{pad_l - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{_money(v)}</text>'
        )
    for t in (t0, (t0 + t1) // 2, t1):
        anchor = "start" if t == t0 else "end" if t == t1 else "middle"
        when = _hm(t, seconds=t1 - t0 < 600_000)
        out.append(f'<text class="tick" x="{x(t):.1f}" y="{height - 8}" text-anchor="{anchor}">{when}</text>')
    labels = []
    for name, s in sorted(series.items()):
        d = [f"M{x(t0):.1f},{y(0):.1f}"]
        for t, v in s:
            d.append(f"H{x(t):.1f}V{y(v):.1f}")
        d.append(f"H{x(t1):.1f}")
        out.append(f'<path class="line s{slots[name]}" d="{"".join(d)}"/>')
        labels.append((y(s[-1][1]), name, s[-1][1]))
    if direct:  # end labels, nudged apart so they never overlap
        last = -1e9
        for yy, name, v in sorted(labels):
            yy = max(yy, last + 16)
            last = yy
            out.append(
                f'<text class="direct" x="{pad_l + pw + 8}" y="{yy + 4:.1f}">'
                f"{escape(name)} {_money(v)}</text>"
            )
    data = {
        "t0": t0,
        "t1": t1,
        "x0": pad_l,
        "pw": pw,
        "w": width,
        "top": pad_t,
        "ph": ph,
        "series": [{"name": n, "slot": slots[n], "pts": s} for n, s in sorted(series.items())],
    }
    out.append("</svg>")
    out.append(f'<script type="application/json" class="chart-data">{_json(data)}</script>')
    return Markup("".join(out))


def sparkline(
    points: list[tuple[int, float]], *, label: str, unit: str = "ms", width: int = 140, height: int = 28
) -> Markup:
    if len(points) < 2:
        return Markup('<span class="muted">–</span>')
    t0, t1 = points[0][0], points[-1][0]
    lo, hi = 0.0, max(v for _, v in points) or 1.0
    xs = [(t - t0) / (t1 - t0 or 1) * (width - 4) + 2 for t, _ in points]
    ys = [height - 3 - (v - lo) / (hi - lo) * (height - 6) for _, v in points]
    path = "M" + "L".join(f"{a:.1f},{b:.1f}" for a, b in zip(xs, ys, strict=True))
    data = {
        "t0": t0,
        "t1": t1,
        "x0": 2,
        "pw": width - 4,
        "w": width,
        "top": 3,
        "ph": height - 6,
        "unit": unit,
        "series": [{"name": label, "slot": 1, "pts": points}],
    }
    return Markup(
        f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">'
        f'<path class="line s1" d="{path}"/></svg>'
        f'<script type="application/json" class="chart-data">{_json(data)}</script>'
    )
