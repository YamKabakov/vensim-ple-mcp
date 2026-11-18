"""Read and audit the documented text portions of Vensim .mdl files."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
import re


SKETCH_MARKER = r"\\\---///"
EQUATION_RE = re.compile(r"(?ms)^([^\n=]+?)\s*=\s*(.*?)\n\s*~\s*([^\n]*)\n\s*~\s*(.*?)\s*\|\s*$")


@dataclass
class Equation:
    name: str
    expression: str
    unit: str
    note: str


@dataclass
class SketchObject:
    object_id: int
    record_type: int
    name: str
    x: float
    y: float
    width: float
    height: float
    shape: int
    kind: str
    raw: str

    @property
    def box(self) -> tuple[float, float, float, float]:
        return (
            self.x - self.width,
            self.y - self.height,
            self.x + self.width,
            self.y + self.height,
        )


@dataclass
class SketchArrow:
    object_id: int
    source_id: int
    target_id: int
    kind: str
    control_points: tuple[tuple[float, float], ...]
    raw: str


def _read(path: str | Path) -> tuple[Path, str]:
    model_path = Path(path).expanduser().resolve()
    if model_path.suffix.lower() != ".mdl":
        raise ValueError(f"expected a .mdl file: {model_path}")
    if not model_path.is_file():
        raise FileNotFoundError(model_path)
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return model_path, model_path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"unable to decode model: {model_path}")


def parse_equations(text: str) -> list[Equation]:
    equation_text = text.split(SKETCH_MARKER, 1)[0]
    equations = []
    for match in EQUATION_RE.finditer(equation_text):
        name = match.group(1).strip()
        if name == "{UTF-8}":
            continue
        equations.append(
            Equation(
                name=name,
                expression=" ".join(match.group(2).split()),
                unit=match.group(3).strip(),
                note=match.group(4).strip(),
            )
        )
    return equations


def _points(line: str) -> tuple[tuple[float, float], ...]:
    if "|" not in line:
        return ()
    return tuple(
        (float(x), float(y))
        for x, y in re.findall(r"\((-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)\)", line)
    )


def parse_sketch(text: str) -> tuple[list[SketchObject], list[SketchArrow], list[str]]:
    if SKETCH_MARKER not in text:
        return [], [], []
    objects: list[SketchObject] = []
    arrows: list[SketchArrow] = []
    views: list[str] = []
    lines = text[text.index(SKETCH_MARKER) :].splitlines()
    for line in lines:
        if line.startswith("*"):
            views.append(line[1:].strip())
            continue
        if not re.match(r"^(1|10|11|12),", line):
            continue
        fields = line.split("|", 1)[0].split(",")
        try:
            record_type = int(fields[0])
            object_id = int(fields[1])
        except (ValueError, IndexError):
            continue
        if record_type == 1:
            if len(fields) < 5:
                continue
            arrows.append(
                SketchArrow(
                    object_id=object_id,
                    source_id=int(fields[2]),
                    target_id=int(fields[3]),
                    kind="physical" if int(fields[4]) == 4 else "information",
                    control_points=_points(line),
                    raw=line,
                )
            )
            continue
        if len(fields) < 9:
            continue
        if record_type == 10:
            name = fields[2]
            kind = {3: "level", 40: "flow"}.get(int(fields[7]), "variable")
        elif record_type == 11:
            name = ""
            kind = "valve"
        else:
            name = ""
            kind = "cloud" if fields[2] == "48" else "comment"
        objects.append(
            SketchObject(
                object_id=object_id,
                record_type=record_type,
                name=name,
                x=float(fields[3]),
                y=float(fields[4]),
                width=float(fields[5]),
                height=float(fields[6]),
                shape=int(fields[7]),
                kind=kind,
                raw=line,
            )
        )
    return objects, arrows, views


def _balanced(expression: str) -> bool:
    depth = 0
    for char in expression:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _overlap(left: SketchObject, right: SketchObject) -> bool:
    lx0, ly0, lx1, ly1 = left.box
    rx0, ry0, rx1, ry1 = right.box
    return lx0 < rx1 and lx1 > rx0 and ly0 < ry1 and ly1 > ry0


def _arrow_points(
    arrow: SketchArrow, objects: dict[int, SketchObject]
) -> list[tuple[float, float]]:
    source = objects[arrow.source_id]
    target = objects[arrow.target_id]
    start = (source.x, source.y)
    end = (target.x, target.y)
    control = arrow.control_points[0] if arrow.control_points else None
    if control is None:
        return [
            (start[0] + (end[0] - start[0]) * step / 20, start[1] + (end[1] - start[1]) * step / 20)
            for step in range(21)
        ]
    return [
        (
            (1 - step / 20) ** 2 * start[0]
            + 2 * (1 - step / 20) * (step / 20) * control[0]
            + (step / 20) ** 2 * end[0],
            (1 - step / 20) ** 2 * start[1]
            + 2 * (1 - step / 20) * (step / 20) * control[1]
            + (step / 20) ** 2 * end[1],
        )
        for step in range(21)
    ]


def _paths_cross(left: list[tuple[float, float]], right: list[tuple[float, float]]) -> bool:
    def orientation(a, b, c) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    for a, b in zip(left, left[1:]):
        for c, d in zip(right, right[1:]):
            if (
                orientation(a, b, c) * orientation(a, b, d) < -1e-6
                and orientation(c, d, a) * orientation(c, d, b) < -1e-6
            ):
                return True
    return False


def _path_hits_object(points: list[tuple[float, float]], obj: SketchObject) -> bool:
    x0, y0, x1, y1 = obj.box
    margin = 6
    return any(
        x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin for x, y in points
    )


def inspect_model(path: str | Path) -> dict:
    model_path, text = _read(path)
    equations = parse_equations(text)
    objects, arrows, views = parse_sketch(text)
    return {
        "path": str(model_path),
        "views": views,
        "equations": [asdict(item) for item in equations],
        "objects": [asdict(item) for item in objects],
        "arrows": [asdict(item) for item in arrows],
    }


def validate_model(path: str | Path) -> dict:
    model_path, text = _read(path)
    equations = parse_equations(text)
    objects, arrows, views = parse_sketch(text)
    errors: list[str] = []
    warnings: list[str] = []

    names = [equation.name for equation in equations]
    counts = Counter(name.replace("_", " ").casefold() for name in names)
    duplicates = [name for name, count in counts.items() if count > 1]
    if duplicates:
        errors.append(f"duplicate equations: {', '.join(duplicates)}")
    controls = {name.upper() for name in names}
    missing_controls = sorted({"INITIAL TIME", "FINAL TIME", "TIME STEP", "SAVEPER"} - controls)
    if missing_controls:
        errors.append(f"missing simulation controls: {', '.join(missing_controls)}")
    for equation in equations:
        if not equation.unit or equation.unit == "???":
            errors.append(f"missing unit: {equation.name}")
        if not _balanced(equation.expression):
            errors.append(f"unbalanced parentheses: {equation.name}")
    if not objects:
        errors.append("model has no readable sketch objects")

    ids = [obj.object_id for obj in objects] + [arrow.object_id for arrow in arrows]
    duplicate_ids = [str(object_id) for object_id, count in Counter(ids).items() if count > 1]
    if duplicate_ids:
        errors.append(f"duplicate sketch object IDs: {', '.join(duplicate_ids)}")
    known_ids = {obj.object_id for obj in objects}
    for arrow in arrows:
        missing = [
            str(value) for value in (arrow.source_id, arrow.target_id) if value not in known_ids
        ]
        if missing:
            errors.append(
                f"arrow {arrow.object_id} references missing object(s): {', '.join(missing)}"
            )

    equation_names = {name.replace("_", " ").casefold() for name in names}
    sketch_names = {
        obj.name.replace("_", " ").casefold()
        for obj in objects
        if obj.name and obj.kind != "comment"
    }
    controls_folded = {
        name.casefold() for name in {"INITIAL TIME", "FINAL TIME", "TIME STEP", "SAVEPER"}
    }
    for missing in sorted(sketch_names - equation_names):
        errors.append(f"sketch variable has no equation: {missing}")
    omitted = sorted(equation_names - sketch_names - controls_folded)
    if omitted:
        warnings.append(f"equations omitted from sketch: {', '.join(omitted[:12])}")

    visible = [obj for obj in objects if obj.kind in {"level", "flow", "variable"}]
    overlaps = []
    for index, left in enumerate(visible):
        for right in visible[index + 1 :]:
            if _overlap(left, right):
                overlaps.append(f"{left.name or left.object_id} / {right.name or right.object_id}")
    if overlaps:
        warnings.append(f"overlapping labels or nodes: {', '.join(overlaps[:10])}")
    long_links = 0
    object_map = {obj.object_id: obj for obj in objects}
    for arrow in arrows:
        source = object_map.get(arrow.source_id)
        target = object_map.get(arrow.target_id)
        if (
            source
            and target
            and ((source.x - target.x) ** 2 + (source.y - target.y) ** 2) ** 0.5 > 650
        ):
            long_links += 1
    if long_links:
        warnings.append(f"{long_links} links are longer than 650 sketch pixels")

    routed = [
        arrow for arrow in arrows if arrow.source_id in object_map and arrow.target_id in object_map
    ]
    route_points = {arrow.object_id: _arrow_points(arrow, object_map) for arrow in routed}
    crossings = 0
    information_crossings = 0
    physical_crossings = 0
    for index, left in enumerate(routed):
        for right in routed[index + 1 :]:
            if {left.source_id, left.target_id} & {right.source_id, right.target_id}:
                continue
            if _paths_cross(route_points[left.object_id], route_points[right.object_id]):
                crossings += 1
                if left.kind == right.kind == "information":
                    information_crossings += 1
                else:
                    physical_crossings += 1
    if information_crossings:
        warnings.append(f"{information_crossings} information-link pairs cross")
    if physical_crossings:
        warnings.append(f"{physical_crossings} crossings involve a physical pipe")

    route_node_collisions = 0
    for arrow in routed:
        for obj in visible:
            if obj.object_id in {arrow.source_id, arrow.target_id}:
                continue
            if _path_hits_object(route_points[arrow.object_id][2:-2], obj):
                route_node_collisions += 1
    if route_node_collisions:
        warnings.append(f"{route_node_collisions} routes pass through unrelated labels or nodes")

    return {
        "ok": not errors,
        "path": str(model_path),
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "equations": len(equations),
            "views": len(views),
            "objects": len(objects),
            "information_links": sum(arrow.kind == "information" for arrow in arrows),
            "physical_pipes": sum(arrow.kind == "physical" for arrow in arrows),
            "overlaps": len(overlaps),
            "route_crossings": crossings,
            "information_route_crossings": information_crossings,
            "physical_route_crossings": physical_crossings,
            "route_node_collisions": route_node_collisions,
        },
        "boundary": "Static .mdl audit; Vensim Model Check and Units Check remain authoritative.",
    }
