"""Draw simulation results as a standalone SVG line chart, with no plotting dependency.

This works on any machine, with or without Vensim installed. Variables that share units
share a panel; each distinct unit gets its own panel stacked below the previous one.
"""

from __future__ import annotations

from html import escape
import math
from pathlib import Path

import pandas as pd

from .simulate import _clean, _quiet_pysd, load_model, simulate_frame

PALETTE = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#4b5563"]
WIDTH = 860
PANEL_HEIGHT = 300
MARGIN = {"left": 72, "right": 24, "top": 56, "bottom": 44}
PANEL_GAP = 64


def _nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    if not math.isfinite(low) or not math.isfinite(high):
        return [0.0]
    if high == low:
        pad = abs(high) * 0.1 or 1.0
        low, high = low - pad, high + pad
    raw = (high - low) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    start = math.floor(low / step) * step
    ticks, value = [round(start, 10)], start
    while ticks[-1] < high - step * 1e-9:
        value += step
        ticks.append(round(value, 10))
    return ticks


def _fmt(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 1e5 or abs(value) < 1e-3:
        return f"{value:.2g}"
    return f"{value:,.6g}"


def _stocks(model_path: str | Path) -> list[str]:
    with _quiet_pysd():
        doc = load_model(model_path).doc
    return [row["Real Name"] for _, row in doc.iterrows() if row.get("Subtype") == "Integ"]


def render_results_svg(
    frame: pd.DataFrame, units: dict[str, str] | None = None, title: str = ""
) -> str:
    """Return an SVG chart of every column in frame against its time index."""
    units = units or {}
    groups: dict[str, list[str]] = {}
    for name in frame.columns:
        groups.setdefault(units.get(name, ""), []).append(name)

    times = [float(t) for t in frame.index]
    t0, t1 = times[0], times[-1] if times[-1] != times[0] else times[0] + 1
    plot_w = WIDTH - MARGIN["left"] - MARGIN["right"]
    height = MARGIN["top"] + len(groups) * PANEL_HEIGHT + (len(groups) - 1) * PANEL_GAP
    height += MARGIN["bottom"]
    colors = {name: PALETTE[i % len(PALETTE)] for i, name in enumerate(frame.columns)}

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" font-family="Helvetica, Arial, sans-serif">',
        f'<rect width="{WIDTH}" height="{height}" fill="#ffffff"/>',
    ]
    if title:
        parts.append(
            f'<text x="{MARGIN["left"]}" y="28" font-size="17" font-weight="600" '
            f'fill="#111827">{escape(title)}</text>'
        )

    def x_of(t: float) -> float:
        return MARGIN["left"] + (t - t0) / (t1 - t0) * plot_w

    for index, (unit, names) in enumerate(groups.items()):
        top = MARGIN["top"] + index * (PANEL_HEIGHT + PANEL_GAP)
        bottom = top + PANEL_HEIGHT
        values = pd.concat([pd.to_numeric(frame[n], errors="coerce") for n in names])
        values = values[values.map(lambda v: math.isfinite(v))]
        low, high = (float(values.min()), float(values.max())) if len(values) else (0.0, 1.0)
        low = min(low, 0.0) if low >= 0 and low < (high - low) * 0.5 else low
        ticks = _nice_ticks(low, high)
        y_low, y_high = ticks[0], ticks[-1] if ticks[-1] != ticks[0] else ticks[0] + 1

        def y_of(v: float, top=top, y_low=y_low, y_high=y_high) -> float:
            return top + PANEL_HEIGHT - (v - y_low) / (y_high - y_low) * PANEL_HEIGHT

        for tick in ticks:
            y = y_of(tick)
            parts.append(
                f'<line x1="{MARGIN["left"]}" x2="{WIDTH - MARGIN["right"]}" y1="{y:.1f}" '
                f'y2="{y:.1f}" stroke="#e5e7eb"/>'
            )
            parts.append(
                f'<text x="{MARGIN["left"] - 8}" y="{y + 4:.1f}" font-size="11" '
                f'text-anchor="end" fill="#6b7280">{_fmt(tick)}</text>'
            )
        for tick in _nice_ticks(t0, t1, 8):
            if t0 <= tick <= t1:
                x = x_of(tick)
                parts.append(
                    f'<text x="{x:.1f}" y="{bottom + 18}" font-size="11" text-anchor="middle" '
                    f'fill="#6b7280">{_fmt(tick)}</text>'
                )
        parts.append(
            f'<line x1="{MARGIN["left"]}" x2="{WIDTH - MARGIN["right"]}" y1="{bottom}" '
            f'y2="{bottom}" stroke="#9ca3af"/>'
        )
        if unit:
            parts.append(
                f'<text x="{MARGIN["left"]}" y="{top - 8}" font-size="11" fill="#6b7280">'
                f"{escape(unit)}</text>"
            )

        legend_x = WIDTH - MARGIN["right"]
        for name in reversed(names):
            label = escape(name)
            legend_x -= 7 * len(name) + 30
            parts.append(
                f'<rect x="{legend_x:.0f}" y="{top - 18}" width="14" height="3" '
                f'fill="{colors[name]}"/>'
                f'<text x="{legend_x + 19:.0f}" y="{top - 13}" font-size="12" '
                f'fill="#374151">{label}</text>'
            )

        for name in names:
            points, segments = [], []
            for t, v in zip(times, pd.to_numeric(frame[name], errors="coerce")):
                if v is None or not math.isfinite(v):
                    if points:
                        segments.append(points)
                    points = []
                    continue
                points.append(f"{x_of(t):.1f},{y_of(float(v)):.1f}")
            if points:
                segments.append(points)
            for segment in segments:
                parts.append(
                    f'<polyline fill="none" stroke="{colors[name]}" stroke-width="2" '
                    f'stroke-linejoin="round" points="{" ".join(segment)}"/>'
                )

    last_bottom = MARGIN["top"] + len(groups) * PANEL_HEIGHT + (len(groups) - 1) * PANEL_GAP
    time_unit = units.get("TIME STEP") or units.get("FINAL TIME") or ""
    parts.append(
        f'<text x="{MARGIN["left"] + plot_w / 2:.0f}" y="{last_bottom + 38}" font-size="12" '
        f'text-anchor="middle" fill="#374151">Time{f" ({escape(time_unit)})" if time_unit else ""}'
        "</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts)


def plot_results(
    model_path: str | Path,
    output_path: str | Path,
    variables: list[str] | None = None,
    overrides: dict[str, float] | None = None,
    final_time: float | None = None,
    time_step: float | None = None,
    title: str | None = None,
) -> dict:
    """Simulate with PySD and save an SVG chart; by default the model's stocks are drawn."""
    columns = variables or _stocks(model_path) or None
    frame = simulate_frame(model_path, overrides, columns, final_time, time_step)
    with _quiet_pysd():
        doc = load_model(model_path).doc
    units = {row["Real Name"]: str(row.get("Units") or "") for _, row in doc.iterrows()}
    units = {name: ("" if unit == "nan" else unit) for name, unit in units.items()}
    model = Path(model_path).expanduser().resolve()
    svg = render_results_svg(frame, units, title or model.stem)
    output = Path(output_path).expanduser().resolve()
    if output.suffix.lower() != ".svg":
        output = output.with_suffix(".svg")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(svg, encoding="utf-8")
    return {
        "chart": str(output),
        "model": str(model),
        "variables": list(frame.columns),
        "overrides": dict(overrides or {}),
        "final": {name: _clean(frame[name].iloc[-1]) for name in frame.columns},
    }
