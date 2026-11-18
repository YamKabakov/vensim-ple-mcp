"""Generate portable Vensim .mdl files from a small declarative JSON specification."""

from __future__ import annotations

from pathlib import Path

from .layout import Scene, build_scene
from .model import ModelSpec


FOOTER = r"""///---\\\
:L<%^E!@
5:Time
19:100,0
24:0
25:0
26:0
23:0
15:0,0,0,0,0,0
27:0,
34:0,
42:0
72:0
73:0
95:0
96:0
97:0
77:0
78:0
102:1
108:1
93:0
94:0
92:0
91:0
90:0
87:0
75:
43:
103:8,8,8,3,8
105:0,0,0,0,0,0,0,0,0,0
104:Arial|12||30-41-59|51-65-85|-1--1--1|71-85-105|219-234-254|248-250-252
"""


def _equation_block(name: str, expression: str, unit: str, note: str = "") -> str:
    clean_note = note.replace("~", "-").replace("|", "/")
    return f"{name} = {expression}\n\t~\t{unit}\n\t~\t{clean_note}\t|\n"


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _sketch(spec: ModelSpec, scene: Scene) -> str:
    lines = [
        r"\\\---/// Sketch information - do not modify anything except names",
        "V300  Do not put anything below this section - it will be ignored",
        "*Main Model",
        (
            f"$148-163-184,0,{spec.theme.font}|{spec.theme.font_size}||{spec.theme.text}|"
            f"{spec.theme.shape}|{spec.theme.arrow}|{spec.theme.fill}|{spec.theme.background}|"
            "96,96,100,0"
        ),
    ]
    next_id = 1
    title_id = next_id
    next_id += 1
    title_half = max(60, len(spec.title) * 6)
    lines.append(
        f"12,{title_id},0,{scene.width // 2},42,{title_half},16,8,135,0,18,-1,0,0,0,"
        "-1--1--1,0-0-0,||B|0-0-0,0,0,0,0,0,0"
    )
    lines.append(spec.title)

    # Vensim attaches a flow's name to its valve only when the name record directly
    # follows the valve record, so emit each valve and its label as a pair.
    labels = {shape.attached_to: shape for shape in scene.shapes.values() if shape.kind == "flow"}
    ordered_shapes = []
    for shape in sorted(
        scene.shapes.values(),
        key=lambda shape: ({"level": 0, "valve": 1, "cloud": 2}.get(shape.kind, 3), shape.y, shape.x),
    ):
        if shape.kind == "flow":
            continue
        ordered_shapes.append(shape)
        if shape.kind == "valve":
            ordered_shapes.append(labels[shape.key])
    for shape in ordered_shapes:
        shape.object_id = next_id
        next_id += 1

    for shape in ordered_shapes:
        x, y = int(round(shape.x)), int(round(shape.y))
        w, h = int(round(shape.width / 2)), int(round(shape.height / 2))
        if shape.kind == "cloud":
            lines.append(f"12,{shape.object_id},48,{x},{y},10,8,0,3,0,0,-1,0,0,0,0,0,0,0,0,0")
        elif shape.kind == "valve":
            direction = 4 if shape.vertical else 1
            lines.append(
                f"11,{shape.object_id},0,{x},{y},6,8,34,3,0,0,{direction},0,0,0,0,0,0,0,0,0"
            )
        elif shape.kind == "level":
            lines.append(f"10,{shape.object_id},{shape.name},{x},{y},{w},{h},3,3,0,0,0,0,0,0,0,0,0,0,0,0")
        elif shape.kind == "flow":
            lines.append(f"10,{shape.object_id},{shape.name},{x},{y},{w},{h},40,3,0,0,-1,0,0,0,0,0,0,0,0,0")
        else:
            lines.append(f"10,{shape.object_id},{shape.name},{x},{y},{w},{h},8,3,0,0,-1,0,0,0,0,0,0,0,0,0")

    for route in scene.routes:
        source = scene.shapes[route.source]
        target = scene.shapes[route.target]
        arrow_id = next_id
        next_id += 1
        if route.kind == "physical":
            # Pipes run from the valve: shape 4 ends in an arrowhead, 100 has none.
            shape_code = 4 if route.head else 100
            midpoint = (int((source.x + target.x) / 2), int((source.y + target.y) / 2))
            lines.append(
                f"1,{arrow_id},{source.object_id},{target.object_id},{shape_code},0,0,22,0,192,0,"
                f"-1--1--1,,1|({midpoint[0]},{midpoint[1]})|"
            )
            continue
        polarity = ord(route.polarity) if route.polarity in ("+", "-") else 0
        if route.control is None:
            shape_code, point = 0, (0, 0)
        else:
            # Vensim's handle lies on the arc; the layout's control is a quadratic Bezier
            # control point, whose curve passes through the midpoint below.
            shape_code = 1
            point = (
                int(0.25 * source.x + 0.5 * route.control[0] + 0.25 * target.x),
                int(0.25 * source.y + 0.5 * route.control[1] + 0.25 * target.y),
            )
        lines.append(
            f"1,{arrow_id},{source.object_id},{target.object_id},{shape_code},0,{polarity},0,0,192,0,"
            f"-1--1--1,,1|({point[0]},{point[1]})|"
        )
    lines.append(FOOTER.rstrip("\n"))
    return "\n".join(lines) + "\n"


def generate_text(spec: ModelSpec) -> tuple[str, Scene]:
    scene = build_scene(spec)
    chunks = ["{UTF-8}\n"]
    for level in spec.levels:
        chunks.append(_equation_block(level.name, level.equation, level.unit, level.note))
    for flow in spec.flows:
        chunks.append(_equation_block(flow.name, flow.equation, flow.unit, flow.note))
    for variable in spec.variables:
        chunks.append(_equation_block(variable.name, variable.equation, variable.unit, variable.note))
    sim = spec.simulation
    chunks.extend(
        [
            _equation_block("INITIAL TIME", _fmt(sim.initial_time), sim.time_unit),
            _equation_block("FINAL TIME", _fmt(sim.final_time), sim.time_unit),
            _equation_block("TIME STEP", _fmt(sim.time_step), sim.time_unit),
            _equation_block("SAVEPER", "TIME STEP", sim.time_unit),
        ]
    )
    chunks.append(_sketch(spec, scene))
    return "\n".join(chunks), scene


def generate_model(spec_source: str | Path | dict, output_path: str | Path) -> Path:
    spec = ModelSpec.from_json(spec_source)
    output = Path(output_path).expanduser().resolve()
    if output.suffix.lower() != ".mdl":
        output = output.with_suffix(".mdl")
    output.parent.mkdir(parents=True, exist_ok=True)
    text, _ = generate_text(spec)
    output.write_text(text, encoding="utf-8", newline="\n")
    return output
