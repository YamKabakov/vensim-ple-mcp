"""Deterministic stock-flow layout with obstacle-aware information routing."""

from __future__ import annotations

from dataclasses import dataclass
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
    vertical: bool = False

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
    head: bool = True


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


LEVEL_GAP = 330  # distance between stocks joined by a flow
SIDE_VALVE = 160  # stock centre to valve for a cloud flow drawn sideways
SIDE_CLOUD = 290  # stock centre to cloud for a cloud flow drawn sideways
DOWN_VALVE = 105  # stock centre to valve for a cloud flow drawn downwards
DOWN_CLOUD = 185  # stock centre to cloud for a cloud flow drawn downwards
LANE_STEP = 65  # vertical spacing of the variable lanes above each row
MARGIN = 70


def _chains(spec: ModelSpec) -> list[list[str]]:
    """Group stocks joined by flows into chains, each ordered along its flows."""
    names = [level.name for level in spec.levels]
    parent = {name: name for name in names}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    internal = [f for f in spec.flows if f.source in parent and f.target in parent]
    for flow in internal:
        parent[find(flow.source)] = find(flow.target)

    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(find(name), []).append(name)

    chains = []
    for members in groups.values():
        incoming = {name: 0 for name in members}
        for flow in internal:
            if flow.target in incoming and flow.source != flow.target:
                incoming[flow.target] += 1
        ordered: list[str] = []
        ready = [name for name in members if incoming[name] == 0]
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for flow in internal:
                if flow.source == current and flow.target in incoming and flow.target not in ordered:
                    incoming[flow.target] -= 1
                    if incoming[flow.target] == 0:
                        ready.append(flow.target)
        ordered += [name for name in members if name not in ordered]  # feedback cycles
        chains.append(ordered)
    return chains


def _pack(items: list[tuple[float, float, object]], gap: float = 40) -> list[tuple[float, float, object]]:
    """Spread boxes along one lane: keep each near its wanted x, never overlapping."""
    items = sorted(items, key=lambda item: item[0])
    xs: list[float] = []
    for index, (wanted, width, _) in enumerate(items):
        if index == 0:
            xs.append(wanted)
            continue
        previous_width = items[index - 1][1]
        xs.append(max(wanted, xs[-1] + (previous_width + width) / 2 + gap))
    # Pushing right only drifts the lane; centre it back on the wanted positions.
    shift = sum(item[0] for item in items) / len(items) - sum(xs) / len(xs)
    return [(x + shift, width, payload) for x, (_, width, payload) in zip(xs, items)]


def _segments_cross(a, b, c, d) -> bool:
    def orientation(p, q, r) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    return (
        orientation(a, b, c) * orientation(a, b, d) < -1e-6
        and orientation(c, d, a) * orientation(c, d, b) < -1e-6
    )


def _straight_cost(shapes: dict[str, Shape], links: list[tuple[str, str]]) -> int:
    """Cheap diagram score: straight links through boxes count far more than crossings."""
    segments = [
        (source, target, (shapes[source].x, shapes[source].y), (shapes[target].x, shapes[target].y))
        for source, target in links
    ]
    cost = 0
    for source, target, start, end in segments:
        sample = _route_points(start, None, end)[2:-2]
        for shape in shapes.values():
            # Arrows into a flow always pass its valve and label; only score movable boxes.
            if shape.kind in ("valve", "cloud", "flow") or shape.key in (source, target):
                continue
            if _hits_box(shape.box, sample):
                cost += 10
    for index, (s1, t1, a, b) in enumerate(segments):
        for s2, t2, c, d in segments[index + 1 :]:
            if not {s1, t1} & {s2, t2} and _segments_cross(a, b, c, d):
                cost += 1
    return cost


def _slide(shapes: dict[str, Shape], members: list[str], name: str, step: float) -> None:
    """Move one lane variable by step and push neighbours on that side out of its way."""
    shapes[name].x += step
    direction = 1 if step > 0 else -1
    ordered = sorted(members, key=lambda other: direction * shapes[other].x)
    start = ordered.index(name)
    for previous, current in zip(ordered[start:], ordered[start + 1 :]):
        a, b = shapes[previous], shapes[current]
        needed = (a.width + b.width) / 2 + 20
        if direction * (b.x - a.x) < needed:
            b.x = a.x + direction * needed


def _improve_lanes(shapes: dict[str, Shape], links: list[tuple[str, str]], names: set[str]) -> None:
    """Swap and slide same-lane variables while that lowers the diagram cost.

    The cost is _straight_cost plus a small pull back to each variable's packed position
    (1 per 100px, the price of one crossing), so the search doesn't sprawl the diagram.
    """
    lanes: dict[float, list[str]] = {}
    for name in names:
        lanes.setdefault(round(shapes[name].y), []).append(name)
    home = {name: shapes[name].x for name in names}

    def total() -> float:
        drift = sum(abs(shapes[name].x - x) for name, x in home.items())
        return _straight_cost(shapes, links) + drift / 100

    best = total()
    for _ in range(3):
        improved = False
        for members in lanes.values():
            for i, left in enumerate(members):
                for right in members[i + 1 :]:
                    a, b = shapes[left], shapes[right]
                    if abs(a.width - b.width) > 60:
                        continue
                    a.x, b.x = b.x, a.x
                    if not any(
                        other.key not in (left, right)
                        and round(other.y) == round(a.y)
                        and abs(other.x - moved.x) < (other.width + moved.width) / 2 + 10
                        for moved in (a, b)
                        for other in shapes.values()
                    ):
                        cost = total()
                        if cost < best:
                            best, improved = cost, True
                            continue
                    a.x, b.x = b.x, a.x
        # Slide each variable sideways, pushing lane neighbours that would overlap.
        for members in lanes.values():
            for name in members:
                for step in (50, -50, 100, -100, 150, -150, 200, -200, 250, -250):
                    saved = {other: shapes[other].x for other in members}
                    _slide(shapes, members, name, step)
                    # A long slide can jump past a neighbour, which _slide doesn't push.
                    ordered = sorted(members, key=lambda other: shapes[other].x)
                    clear = all(
                        shapes[b].x - shapes[a].x >= (shapes[a].width + shapes[b].width) / 2 + 10
                        for a, b in zip(ordered, ordered[1:])
                    )
                    cost = total() if clear else best
                    if cost < best:
                        best, improved = cost, True
                        break
                    for other, x in saved.items():
                        shapes[other].x = x
        if not improved:
            break


def build_scene(spec: ModelSpec) -> Scene:
    """Lay out one row per chain of connected stocks, with variables in lanes above it.

    Positions are first computed relative to each row (dy), then rows are stacked so
    nothing from one row reaches into the next.
    """
    chains = _chains(spec) or [[]]
    row_of = {name: row for row, chain in enumerate(chains) for name in chain}

    # Which side of each stock is still free for a cloud flow.
    used: dict[str, set[str]] = {level.name: set() for level in spec.levels}
    for chain in chains:
        for left, right in zip(chain, chain[1:]):
            used[left].add("right")
            used[right].add("left")

    # (key, name, kind, x, dy, width, height, row, attached_to, vertical)
    placed: list[tuple] = []
    level_x: dict[str, float] = {}
    for chain in chains:
        needs_left = chain and any(
            f.target == chain[0] and not f.source for f in spec.flows
        ) and "left" not in used[chain[0]]
        x = MARGIN + (SIDE_CLOUD + 20 if needs_left else 80)
        for name in chain:
            level_x[name] = x
            x += LEVEL_GAP

    for level in spec.levels:
        placed.append(
            (level.name, level.name, "level", level_x[level.name], 0.0,
             max(125, _label_width(level.name)), 54, row_of[level.name], None, False)
        )

    for flow in spec.flows:
        valve_key = f"@valve:{flow.name}"
        cloud_key = f"@cloud:{flow.name}"
        label_width = _label_width(flow.name, 70, 145)
        if flow.source in level_x and flow.target in level_x:
            sx, tx = level_x[flow.source], level_x[flow.target]
            row = row_of[flow.source]
            vx, vdy, vertical, cloud = (sx + tx) / 2, 0.0, False, None
        else:
            stock = flow.target or flow.source or ""
            row = row_of[stock]
            sx = level_x[stock]
            preferred = ["left", "right"] if flow.target else ["right", "left"]
            side = next((name for name in preferred if name not in used[stock]), None)
            if side is None:
                side = "down" if "down" not in used[stock] else "down2"
            used[stock].add(side)
            if side in ("left", "right"):
                sign = -1 if side == "left" else 1
                vx, vdy, vertical = sx + sign * SIDE_VALVE, 0.0, False
                cloud = (sx + sign * SIDE_CLOUD, 0.0)
            else:
                shift = 0 if side == "down" else 55
                vx, vdy, vertical = sx + shift, float(DOWN_VALVE), True
                cloud = (sx + shift, float(DOWN_CLOUD))
        placed.append((valve_key, flow.name, "valve", vx, vdy, 14, 18, row, None, vertical))
        if vertical:
            label = (vx + label_width / 2 + 14, vdy)
        else:
            label = (vx, vdy + 30)
        placed.append(
            (flow.name, flow.name, "flow", label[0], label[1], label_width, 24, row, valve_key, False)
        )
        if cloud:
            placed.append((cloud_key, "", "cloud", cloud[0], cloud[1], 20, 16, row, None, False))

    # Variables sit above the row of whatever they feed, near its x position.
    position = {item[0]: (item[3], item[7]) for item in placed}
    targets: dict[str, list[str]] = {variable.name: [] for variable in spec.variables}
    sources: dict[str, list[str]] = {variable.name: [] for variable in spec.variables}
    for link in spec.links:
        if link.source in targets:
            targets[link.source].append(link.target)
        if link.target in sources:
            sources[link.target].append(link.source)

    def anchor(name: str, graph: dict[str, list[str]], trail: tuple[str, ...] = ()):
        points = []
        for other in graph.get(name, []):
            if other in position:
                points.append(position[other])
            elif other in graph and other not in trail:
                found = anchor(other, graph, (*trail, name))
                if found:
                    points.append(found)
        if not points:
            return None
        row = min(row for _, row in points)
        xs = [x for x, r in points if r == row]
        return sum(xs) / len(xs), row

    # A variable's lane is its distance from the stocks and flows it feeds, so links
    # step outward one lane at a time instead of cutting through other variables.
    def depth(name: str, trail: tuple[str, ...] = ()) -> int:
        deeper = [
            depth(other, (*trail, name))
            for other in targets.get(name, [])
            if other in targets and other not in trail
        ]
        return 1 + max(deeper) if deeper else 0

    by_lane: dict[tuple[int, int], list] = {}
    unplaced = sorted(spec.variables, key=lambda v: depth(v.name))
    max_depth = max((depth(v.name) for v in unplaced), default=0)
    for lane in range(max_depth + 1):
        # Anchor each variable in this lane on what it feeds (already placed lanes).
        wanted: dict[int, list[tuple[float, float, object]]] = {}
        for variable in unplaced:
            if depth(variable.name) != lane:
                continue
            found = anchor(variable.name, targets) or anchor(variable.name, sources)
            x, row = found or (MARGIN + 200, 0)
            wanted.setdefault(row, []).append(
                (x, _label_width(variable.name, 85, 155), variable)
            )
        for row, items in wanted.items():
            for x, width_, variable in _pack(items):
                position[variable.name] = (x, row)
                placed.append(
                    (variable.name, variable.name, variable.kind, x,
                     -(115 + lane * LANE_STEP), width_,
                     38 if variable.kind == "auxiliary" else 32, row, None, False)
                )

    # Stack rows: each row's top lane clears the bottom of the row above.
    row_count = len(chains)
    top = [0.0] * row_count
    bottom = [0.0] * row_count
    for item in placed:
        _, _, _, _, dy, _, height, row, _, _ = item
        top[row] = max(top[row], -dy + height / 2)
        bottom[row] = max(bottom[row], dy + height / 2)
    row_y = []
    y = 80.0
    for row in range(row_count):
        y += top[row]
        row_y.append(y)
        y += bottom[row] + 70

    shapes: dict[str, Shape] = {}
    for key, name, kind, x, dy, width_, height_, row, attached, vertical in placed:
        shapes[key] = Shape(
            key, name, kind, x, row_y[row] + dy, width_, height_,
            attached_to=attached, vertical=vertical,
        )
    _improve_lanes(
        shapes,
        [(link.source, link.target) for link in spec.links
         if link.source in shapes and link.target in shapes],
        {variable.name for variable in spec.variables},
    )
    # Packing and the search may use space left of the margin; shift everything back in.
    offset = MARGIN - min(shape.x - shape.width / 2 for shape in shapes.values())
    for shape in shapes.values():
        shape.x += offset
    width = int(max(960, max(s.x + s.width / 2 for s in shapes.values()) + MARGIN))
    height = int(max(630, y + 10))

    routes: list[Route] = []
    for flow in spec.flows:
        valve_key = f"@valve:{flow.name}"
        cloud_key = f"@cloud:{flow.name}"
        # Vensim draws both halves of a pipe from the valve; only the downstream half has a head.
        routes.append(Route(valve_key, flow.source or cloud_key, "physical", head=False))
        routes.append(Route(valve_key, flow.target or cloud_key, "physical", head=True))

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
