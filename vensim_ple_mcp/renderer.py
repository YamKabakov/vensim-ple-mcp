"""Render a faithful, presentation-friendly SVG preview from a Vensim sketch."""

from __future__ import annotations

from html import escape
from pathlib import Path
import re

from .parser import _read, parse_sketch


def _attachment(source, target) -> tuple[float, float]:
    dx, dy = target.x - source.x, target.y - source.y
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return source.x, source.y
    width = max(source.width, 7)
    height = max(source.height, 7)
    scales = []
    if abs(dx) > 1e-9:
        scales.append(width / abs(dx))
    if abs(dy) > 1e-9:
        scales.append(height / abs(dy))
    scale = min(scales)
    return source.x + dx * scale, source.y + dy * scale


def _curve(source, target, controls) -> str:
    start = _attachment(source, target)
    end = _attachment(target, source)
    if controls:
        x, y = controls[0]
        return f"M {start[0]:.0f} {start[1]:.0f} Q {x:.0f} {y:.0f} {end[0]:.0f} {end[1]:.0f}"
    return f"M {start[0]:.0f} {start[1]:.0f} L {end[0]:.0f} {end[1]:.0f}"


LOOP_LABEL = re.compile(r"^[RBU]\d+$")


def _sign_position(source, target, controls) -> tuple[float, float]:
    """A point near the arrowhead, nudged to the side, for the link's polarity sign."""
    start = _attachment(source, target)
    end = _attachment(target, source)
    t = 0.8
    if controls:
        cx, cy = controls[0]
        x = (1 - t) ** 2 * start[0] + 2 * (1 - t) * t * cx + t**2 * end[0]
        y = (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * cy + t**2 * end[1]
        dx, dy = end[0] - cx, end[1] - cy
    else:
        x = start[0] + (end[0] - start[0]) * t
        y = start[1] + (end[1] - start[1]) * t
        dx, dy = end[0] - start[0], end[1] - start[1]
    length = max((dx * dx + dy * dy) ** 0.5, 1)
    return x - dy / length * 10, y + dx / length * 10


def _label(name: str, x: float, y: float, width: float, css_class: str) -> str:
    words = name.replace("_", " ").split()
    if len(words) <= 2 or width > 80:
        return f'<text class="{css_class}" x="{x:.0f}" y="{y + 4:.0f}">{escape(" ".join(words))}</text>'
    split = (len(words) + 1) // 2
    return (
        f'<text class="{css_class}" x="{x:.0f}" y="{y - 4:.0f}">'
        f'<tspan x="{x:.0f}">{escape(" ".join(words[:split]))}</tspan>'
        f'<tspan x="{x:.0f}" dy="15">{escape(" ".join(words[split:]))}</tspan></text>'
    )


def render_svg(model_path: str | Path, output_path: str | Path) -> Path:
    source, text = _read(model_path)
    objects, arrows, views = parse_sketch(text)
    if not objects:
        raise ValueError(f"no sketch objects found in {source}")
    object_map = {obj.object_id: obj for obj in objects}
    max_x = max(obj.x + obj.width for obj in objects)
    max_y = max(obj.y + obj.height for obj in objects)
    width = max(960, int(max_x + 80))
    height = max(640, int(max_y + 70))
    title = views[0] if views else source.stem.replace("_", " ")
    title_object = next(
        (
            obj for obj in objects
            if obj.kind == "comment" and obj.name and not LOOP_LABEL.match(obj.name)
            and not obj.name[0].isdigit()
        ),
        None,
    )
    if title_object is not None:
        title = title_object.name

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<defs>",
        '<filter id="shadow" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="3" stdDeviation="4" flood-opacity=".12"/></filter>',
        '<marker id="arrow" markerUnits="userSpaceOnUse" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto"><path d="M0,0 L9,3.5 L0,7 Z" fill="#475569"/></marker>',
        '<marker id="flow-arrow" markerUnits="userSpaceOnUse" markerWidth="12" markerHeight="9" refX="11" refY="4.5" orient="auto"><path d="M0,0 L12,4.5 L0,9 Z" fill="#0f766e"/></marker>',
        "<style>",
        ".title{font:700 22px Inter,Arial,sans-serif;fill:#0f172a;text-anchor:middle}.node-label{font:600 13px Inter,Arial,sans-serif;fill:#1e293b;text-anchor:middle}.flow-label{font:600 12px Inter,Arial,sans-serif;fill:#0f766e;text-anchor:middle}.small-label{font:500 12px Inter,Arial,sans-serif;fill:#334155;text-anchor:middle}.info{fill:none;stroke:#64748b;stroke-width:1.8;marker-end:url(#arrow)}.pipe{fill:none;stroke:#0f766e;stroke-width:4;marker-end:url(#flow-arrow)}.pipe.tail{marker-end:none}.sign{font:700 14px Inter,Arial,sans-serif;fill:#334155;text-anchor:middle;paint-order:stroke;stroke:#fff;stroke-width:3}.loop{font:700 13px Inter,Arial,sans-serif;text-anchor:middle}",
        "</style></defs>",
        f'<rect width="100%" height="100%" rx="18" fill="#f8fafc"/><rect x="20" y="20" width="{width - 40}" height="{height - 40}" rx="16" fill="#fff" stroke="#e2e8f0"/>',
        f'<text class="title" x="{width / 2:.0f}" y="52">{escape(title)}</text>',
    ]
    signs: list[str] = []  # drawn last, so node boxes don't cover them
    for arrow in arrows:
        source_obj = object_map.get(arrow.source_id)
        target_obj = object_map.get(arrow.target_id)
        if not source_obj or not target_obj:
            continue
        if arrow.kind == "physical":
            css_class = "pipe" if arrow.head else "pipe tail"
        else:
            css_class = "info"
        svg.append(f'<path class="{css_class}" d="{_curve(source_obj, target_obj, arrow.control_points)}"/>')
        if arrow.kind == "information" and arrow.polarity:
            sx, sy = _sign_position(source_obj, target_obj, arrow.control_points)
            signs.append(f'<text class="sign" x="{sx:.0f}" y="{sy + 5:.0f}">{arrow.polarity}</text>')

    for obj in objects:
        x, y, w, h = obj.x, obj.y, obj.width, obj.height
        if obj.kind == "comment":
            if obj is title_object or not obj.name:
                continue
            if LOOP_LABEL.match(obj.name):
                colour = {"R": "#b45309", "B": "#1d4ed8"}.get(obj.name[0], "#64748b")
                svg.append(
                    f'<circle cx="{x:.0f}" cy="{y:.0f}" r="15" fill="#fff" stroke="{colour}" '
                    f'stroke-width="2" stroke-dasharray="70 10"/>'
                    f'<text class="loop" x="{x:.0f}" y="{y + 5:.0f}" fill="{colour}">{escape(obj.name)}</text>'
                )
            else:
                svg.append(f'<text class="small-label" x="{x:.0f}" y="{y + 4:.0f}">{escape(obj.name)}</text>')
            continue
        if obj.kind == "level":
            svg.append(
                f'<rect x="{x - w:.0f}" y="{y - h:.0f}" width="{2*w:.0f}" height="{2*h:.0f}" rx="8" fill="#dbeafe" stroke="#1d4ed8" stroke-width="2" filter="url(#shadow)"/>'
            )
            svg.append(_label(obj.name, x, y, w, "node-label"))
        elif obj.kind == "flow":
            svg.append(_label(obj.name, x, y, w, "flow-label"))
        elif obj.kind == "valve":
            svg.append(
                f'<path d="M{x-8:.0f},{y-7:.0f} L{x:.0f},{y:.0f} L{x-8:.0f},{y+7:.0f} Z M{x+8:.0f},{y-7:.0f} L{x:.0f},{y:.0f} L{x+8:.0f},{y+7:.0f} Z" fill="#14b8a6" stroke="#0f766e"/>'
            )
        elif obj.kind == "cloud":
            svg.append(
                f'<path d="M{x-13:.0f},{y+4:.0f} C{x-16:.0f},{y-5:.0f} {x-6:.0f},{y-10:.0f} {x:.0f},{y-5:.0f} C{x+7:.0f},{y-12:.0f} {x+18:.0f},{y-4:.0f} {x+13:.0f},{y+5:.0f} Z" fill="#ccfbf1" stroke="#0f766e"/>'
            )
        else:
            svg.append(
                f'<rect x="{x - w:.0f}" y="{y - h:.0f}" width="{2*w:.0f}" height="{2*h:.0f}" rx="{min(h, 14):.0f}" fill="#f1f5f9" stroke="#94a3b8"/>'
            )
            svg.append(_label(obj.name, x, y, w, "small-label"))
    svg.extend(signs)
    svg.append("</svg>\n")
    output = Path(output_path).expanduser().resolve()
    if output.suffix.lower() != ".svg":
        output = output.with_suffix(".svg")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(svg), encoding="utf-8")
    return output
