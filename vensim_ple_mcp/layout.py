"""Deterministic stock-flow layout with obstacle-aware information routing."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Iterable

from .model import ModelSpec


@dataclass
class Shape:
    key: str
    name: str
    kind: str
    x: float
    y: float
    width: float
    height: float
    object_id: int = 0
    attached_to: str | None = None

    @property
    def box(self) -> tuple[float, float, float, float]:
        return (
            self.x - self.width / 2,
            self.y - self.height / 2,
            self.x + self.width / 2,
            self.y + self.height / 2,
        )


@dataclass
class Route:
    source: str
    target: str
    kind: str
    control: tuple[float, float] | None = None
    polarity: str = ""


@dataclass
class Scene:
    width: int
    height: int
    title: str
    shapes: dict[str, Shape]
    routes: list[Route]


def _label_width(name: str, minimum: int = 60, maximum: int = 150) -> int:
    ascii_count = sum(ord(char) < 128 for char in name)
    wide_count = len(name) - ascii_count
    return max(minimum, min(maximum, 18 + ascii_count * 7 + wide_count * 13))


def _hits_box(
    box: tuple[float, float, float, float], points: Iterable[tuple[float, float]]
) -> bool:
    x0, y0, x1, y1 = box
    margin = 12
    for x, y in points:
        if x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin:
            return True
    return False


def _route_points(
    start: tuple[float, float], control: tuple[float, float] | None, end: tuple[float, float]
) -> list[tuple[float, float]]:
    if control is None:
        return [
            (start[0] + (end[0] - start[0]) * step / 20, start[1] + (end[1] - start[1]) * step / 20)
            for step in range(21)
        ]
    points = []
    for step in range(21):
        t = step / 20
        x = (1 - t) ** 2 * start[0] + 2 * (1 - t) * t * control[0] + t**2 * end[0]
        y = (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * control[1] + t**2 * end[1]
        points.append((x, y))
    return points


def _crosses(left: list[tuple[float, float]], right: list[tuple[float, float]]) -> bool:
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


def _choose_control(
    source: Shape,
    target: Shape,
    obstacles: Iterable[Shape],
    prior_paths: list[tuple[str, str, list[tuple[float, float]]]],
    slot: int,
    width: int,
    height: int,
) -> tuple[tuple[float, float] | None, list[tuple[float, float]]]:
    start = (source.x, source.y)
    end = (target.x, target.y)
    dx, dy = end[0] - start[0], end[1] - start[1]
    distance = max(math.hypot(dx, dy), 1)
    normal = (-dy / distance, dx / distance)
    base = max(38, min(115, distance * 0.18))
    amplitudes = [0.0]
    for multiplier in (1.0, 1.5, 2.0, 2.5, 3.5, 5.0):
        bend = (base + slot * 18) * multiplier
        amplitudes.extend((bend, -bend))

    candidates = []
    for amplitude in amplitudes:
        control = None
        if amplitude:
            control = (
                (start[0] + end[0]) / 2 + normal[0] * amplitude,
                (start[1] + end[1]) / 2 + normal[1] * amplitude,
            )
        sample = _route_points(start, control, end)
        collisions = sum(
            _hits_box(shape.box, sample[2:-2])
            for shape in obstacles
            if shape.key not in {source.key, target.key}
        )
        crossings = sum(
            _crosses(sample, prior)
            for prior_source, prior_target, prior in prior_paths
            if not {source.key, target.key} & {prior_source, prior_target}
        )
        outside = sum(x < 35 or x > width - 35 or y < 72 or y > height - 35 for x, y in sample)
        score = collisions * 10_000 + crossings * 600 + outside * 1_000 + abs(amplitude) * 0.05
        candidates.append((score, abs(amplitude), control, sample))
    _, _, control, sample = min(candidates, key=lambda item: (item[0], item[1]))
    return control, sample


def build_scene(spec: ModelSpec) -> Scene:
    """Lay out a readable stock-flow spine with auxiliaries in two support lanes."""
    level_count = max(len(spec.levels), 1)
    width = max(960, 360 + level_count * 300)
    height = max(630, 530 + ((len(spec.variables) + 3) // 4) * 50)
    center_y = 315
    if len(spec.levels) == 1:
        level_x = {spec.levels[0].name: width / 2}
    else:
        left = 220
        usable = width - 440
        level_x = {
            level.name: left + (index * usable / max(level_count - 1, 1))
            for index, level in enumerate(spec.levels)
        }

    shapes: dict[str, Shape] = {}
    for level in spec.levels:
        shapes[level.name] = Shape(
            key=level.name,
            name=level.name,
            kind="level",
            x=level_x[level.name],
            y=center_y,
            width=max(125, _label_width(level.name)),
            height=54,
        )

    flow_gap = 165
    for flow in spec.flows:
        if flow.source and flow.target:
            source_x = level_x[flow.source]
            target_x = level_x[flow.target]
            x = (source_x + target_x) / 2
        elif flow.target:
            x = level_x[flow.target] - flow_gap
        else:
            x = level_x[flow.source or ""] + flow_gap
        valve_key = f"@valve:{flow.name}"
        cloud_key = f"@cloud:{flow.name}"
        label_key = flow.name
        shapes[valve_key] = Shape(valve_key, flow.name, "valve", x, center_y, 14, 18)
        shapes[label_key] = Shape(
            label_key,
            flow.name,
            "flow",
            x,
            center_y + 42,
            _label_width(flow.name, 70, 145),
            24,
            attached_to=valve_key,
        )
        if not flow.source:
            shapes[cloud_key] = Shape(cloud_key, "", "cloud", x - 130, center_y, 20, 16)
        elif not flow.target:
            shapes[cloud_key] = Shape(cloud_key, "", "cloud", x + 130, center_y, 20, 16)

    targets: dict[str, list[str]] = {variable.name: [] for variable in spec.variables}
    for link in spec.links:
        if link.source in targets:
            targets[link.source].append(link.target)

    variable_names = set(targets)

    @lru_cache(maxsize=None)
    def downstream_anchor(name: str, trail: tuple[str, ...] = ()) -> float:
        if name in trail:
            return width / 2
        positions = []
        for target in targets.get(name, []):
            if target in shapes:
                positions.append(shapes[target].x)
            elif target in variable_names:
                positions.append(downstream_anchor(target, (*trail, name)))
        return sum(positions) / len(positions) if positions else width / 2

    upper_constants: list[tuple[float, str, str]] = []
    upper_auxiliaries: list[tuple[float, str, str]] = []
    lower: list[tuple[float, str, str]] = []
    for index, variable in enumerate(spec.variables):
        anchor = downstream_anchor(variable.name)
        if variable.kind == "constant":
            lane = upper_constants
        else:
            lane = upper_auxiliaries if index % 2 == 0 else lower
        lane.append((anchor, variable.name, variable.kind))

    def place_lane(items: list[tuple[float, str, str]], base_y: int, direction: int) -> None:
        ordered = sorted(items)
        occupied: dict[int, list[float]] = {}
        for index, (anchor, name, kind) in enumerate(ordered):
            chosen: tuple[int, float] | None = None
            for row in range(4):
                for offset in (0, 175, -175):
                    candidate = min(max(anchor + offset, 105), width - 105)
                    if all(abs(candidate - prior) >= 165 for prior in occupied.get(row, [])):
                        chosen = (row, candidate)
                        break
                if chosen:
                    break
            if chosen is None:
                row = index // 5
                column = index % 5
                chosen = (row, 105 + column * (width - 210) / 4)
            row, x = chosen
            occupied.setdefault(row, []).append(x)
            y = base_y + direction * row * 72
            shapes[name] = Shape(
                key=name,
                name=name,
                kind=kind,
                x=x,
                y=y,
                width=_label_width(name, 85, 155),
                height=38 if kind == "auxiliary" else 32,
            )

    def place_constants(items: list[tuple[float, str, str]]) -> None:
        ordered = sorted(items)
        for index, (_, name, kind) in enumerate(ordered):
            row, column = divmod(index, 5)
            row_count = min(5, len(ordered) - row * 5)
            if row_count == 1:
                x = width / 2
            else:
                x = 105 + column * (width - 210) / (row_count - 1)
            shapes[name] = Shape(
                key=name,
                name=name,
                kind=kind,
                x=x,
                y=125 + row * 58,
                width=_label_width(name, 85, 155),
                height=32,
            )

    place_constants(upper_constants)
    place_lane(upper_auxiliaries, 245, -1)
    place_lane(lower, 515, 1)

    routes: list[Route] = []
    for flow in spec.flows:
        valve_key = f"@valve:{flow.name}"
        cloud_key = f"@cloud:{flow.name}"
        if flow.source:
            routes.append(Route(flow.source, valve_key, "physical"))
        else:
            routes.append(Route(cloud_key, valve_key, "physical"))
        if flow.target:
            routes.append(Route(valve_key, flow.target, "physical"))
        else:
            routes.append(Route(valve_key, cloud_key, "physical"))

    pair_slots: dict[tuple[str, str], int] = {}
    prior_paths = [
        (
            route.source,
            route.target,
            _route_points(
                (shapes[route.source].x, shapes[route.source].y),
                None,
                (shapes[route.target].x, shapes[route.target].y),
            ),
        )
        for route in routes
    ]
    for link in spec.links:
        if link.source not in shapes or link.target not in shapes:
            continue
        pair = tuple(sorted((link.source, link.target)))
        slot = pair_slots.get(pair, 0)
        pair_slots[pair] = slot + 1
        control, sample = _choose_control(
            shapes[link.source],
            shapes[link.target],
            shapes.values(),
            prior_paths,
            slot,
            width,
            height,
        )
        routes.append(Route(link.source, link.target, "information", control, link.polarity))
        prior_paths.append((link.source, link.target, sample))

    return Scene(width=width, height=height, title=spec.title, shapes=shapes, routes=routes)
