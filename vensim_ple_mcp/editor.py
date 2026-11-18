"""Edit an existing .mdl in place while keeping its diagram.

Edits work on the file's text: only the equation blocks and sketch records an operation
touches are rewritten, so everything else (formatting of other equations, views, the
footer, records this module does not understand) stays byte for byte. That matters for
models saved by Vensim itself, which carry settings this package never generates.

Every edit also keeps the diagram consistent with the equations: information arrows are
added for new references and removed for dropped ones, a new flow gets its valve, pipes
and clouds, and adding or deleting a flow updates the INTEG equations of the stocks it
connects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
from datetime import datetime
import math
import os
from pathlib import Path
import re
import shutil

from .layout import _label_width
from .model import CONTROL_NAMES


SKETCH_START = "\\\\\\---///"
SKETCH_END = "///---\\\\\\"
LEVEL_HALF = (46, 27)  # half width/height of a new stock box
AUX_HALF_HEIGHT = 16
SIDE_VALVE, SIDE_CLOUD = 160, 290  # same spacing the generator uses for cloud flows
DOWN_VALVE, DOWN_CLOUD = 105, 185


class EditError(ValueError):
    """An edit that cannot be applied; the message says why and what to do instead."""


def fold(name: str) -> str:
    """Vensim's identity for a name: quotes dropped, underscores as spaces, any case."""
    bare = name.strip()
    if len(bare) >= 2 and bare[0] == bare[-1] == '"':
        bare = bare[1:-1]
    return " ".join(bare.replace("_", " ").split()).casefold()


def quote(name: str) -> str:
    """How a name is written in equations and sketch records."""
    name = name.strip()
    if name.startswith('"'):
        return name
    return f'"{name}"' if re.search(r"[^\w\s]", name) else name


def _check_name(name: str) -> str:
    name = name.strip()
    bare = name[1:-1] if len(name) >= 2 and name[0] == name[-1] == '"' else name
    if not bare.strip():
        raise EditError("variable name is empty")
    if any(char in bare for char in '~|="[]'):
        raise EditError(f"invalid Vensim variable name: {name!r}")
    if fold(bare).upper() in CONTROL_NAMES:
        raise EditError(f"reserved control variable name: {name!r}")
    return bare


# --------------------------------------------------------------------------- equations


@dataclass
class Block:
    """One `... ~ unit ~ note |` block of the equation section."""

    start: int
    end: int  # index just past the closing '|'
    definition_end: int  # index of the first '~'
    name: str  # as written, quotes included, without subscripts
    expression: str | None  # None unless a plain `name = expression`
    unit: str
    note: str

    @property
    def key(self) -> str:
        return fold(self.name)

    @property
    def is_level(self) -> bool:
        return bool(self.expression) and re.match(r"\s*INTEG\s*\(", self.expression, re.I) is not None


DEFINITION_RE = re.compile(r'\s*("[^"]*"|[^=\[\(:~"]+?)\s*(\[[^\]]*\])?\s*(:=|==|=|\(|:|$)', re.S)


def scan_blocks(section: str) -> list[Block]:
    blocks: list[Block] = []
    position = 0
    while True:
        bar = section.find("|", position)
        if bar < 0:
            return blocks
        chunk_start, position = position, bar + 1
        chunk = section[chunk_start:bar]
        offset = len(chunk) - len(chunk.lstrip())
        body = chunk[offset:]
        if body.startswith("{UTF-8}"):
            rest = body[len("{UTF-8}") :]
            offset += len("{UTF-8}") + len(rest) - len(rest.lstrip())
            body = rest.lstrip()
        if not body or body[0] in "*:" or body.count("~") < 2:
            continue
        definition, unit, note = body.split("~", 2)
        match = DEFINITION_RE.match(definition)
        if not match:
            continue
        plain = match.group(3) == "=" and match.group(2) is None
        start = chunk_start + offset
        blocks.append(
            Block(
                start=start,
                end=bar + 1,
                definition_end=start + len(definition),
                name=match.group(1).strip(),
                expression=" ".join(definition[match.end() :].split()) if plain else None,
                unit=unit.strip(),
                note=note.strip(),
            )
        )


def _name_pattern(name: str) -> re.Pattern:
    bare = name.strip().strip('"')
    core = r"[\s_]+".join(re.escape(word) for word in re.split(r"[\s_]+", bare.strip()))
    return re.compile(rf'"{core}"|(?<![\w"]){core}(?![\w"])', re.I)


def find_references(text: str, names: list[str]) -> list[tuple[int, int, str]]:
    """Spans of `names` in `text`; longer names win, so "Adopters" never matches inside
    "Potential Adopters"."""
    taken = [False] * len(text)
    spans = []
    for name in sorted(set(names), key=lambda item: len(fold(item)), reverse=True):
        for match in _name_pattern(name).finditer(text):
            if any(taken[match.start() : match.end()]):
                continue
            taken[match.start() : match.end()] = [True] * (match.end() - match.start())
            spans.append((match.start(), match.end(), name))
    return sorted(spans)


def _split_integ(expression: str) -> tuple[str, str] | None:
    """(net flow, initial value) of an INTEG equation."""
    match = re.match(r"\s*INTEG\s*\(", expression, re.I)
    if not match:
        return None
    depth, comma = 1, None
    for index in range(match.end(), len(expression)):
        char = expression[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                if comma is None:
                    return None
                return expression[match.end() : comma].strip(), expression[comma + 1 : index].strip()
        elif char == "," and depth == 1:
            comma = index
    return None


def _terms(expression: str) -> list[tuple[str, str]]:
    """Split at top-level + and - into (sign, term) pairs."""
    terms: list[tuple[str, str]] = []
    depth, sign, current = 0, "+", ""
    for char in expression:
        if char in "+-" and depth == 0 and current.strip():
            # A sign after e or E inside a number (1e-3) is part of the number.
            if re.search(r"\d[eE]$", current.strip()):
                current += char
                continue
            terms.append((sign, current.strip()))
            sign, current = char, ""
            continue
        if char in "+-" and depth == 0 and not current.strip():
            sign = "-" if (sign == "-") != (char == "-") else "+"
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        current += char
    if current.strip():
        terms.append((sign, current.strip()))
    return terms


def _join_terms(terms: list[tuple[str, str]]) -> str:
    if not terms:
        return "0"
    text = ("-" if terms[0][0] == "-" else "") + terms[0][1]
    for sign, term in terms[1:]:
        text += f" {sign} {term}"
    return text


def _format_block(name: str, expression: str, unit: str, note: str) -> str:
    clean_note = note.replace("~", "-").replace("|", "/")
    # Vensim's own layout: an empty note closes on the same line, a note on the next.
    tail = f"{clean_note}\n\t|" if clean_note else "\t|"
    return f"{name} = {expression}\n\t~\t{unit}\n\t~\t{tail}"


def _balanced(expression: str) -> bool:
    depth = 0
    for char in expression:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth < 0:
            return False
    return depth == 0


# --------------------------------------------------------------------------- sketch


def _split_fields(text: str) -> tuple[list[str], str]:
    """Comma fields before the first '|' outside quotes, and the rest of the line."""
    fields, current, quoted = [], "", False
    for index, char in enumerate(text):
        if char == '"':
            quoted = not quoted
        if char == "|" and not quoted:
            fields.append(current)
            return fields, text[index:]
        if char == "," and not quoted:
            fields.append(current)
            current = ""
            continue
        current += char
    fields.append(current)
    return fields, ""


@dataclass
class Record:
    line: int
    fields: list[str]
    tail: str

    @property
    def type(self) -> int:
        return int(self.fields[0])

    @property
    def id(self) -> int:
        return int(self.fields[1])

    @property
    def name(self) -> str:
        return self.fields[2] if self.type == 10 else ""

    @property
    def x(self) -> float:
        return float(self.fields[3])

    @property
    def y(self) -> float:
        return float(self.fields[4])

    @property
    def half(self) -> tuple[float, float]:
        return float(self.fields[5]), float(self.fields[6])

    @property
    def shape(self) -> int:
        return int(self.fields[7])

    @property
    def is_pipe(self) -> bool:
        return self.type == 1 and self.fields[4] in ("4", "100")

    @property
    def source(self) -> int:
        return int(self.fields[2])

    @property
    def target(self) -> int:
        return int(self.fields[3])

    def text(self) -> str:
        return ",".join(self.fields) + self.tail


@dataclass
class View:
    name: str
    header: int  # line index of the "*View name" line
    end: int  # line index where the next view or the footer starts
    records: list[Record] = field(default_factory=list)

    def by_id(self) -> dict[int, Record]:
        return {record.id: record for record in self.records if record.type != 1}

    def arrows(self) -> list[Record]:
        return [record for record in self.records if record.type == 1]

    def instances(self, name: str) -> list[Record]:
        key = fold(name)
        return [record for record in self.records if record.type == 10 and fold(record.name) == key]

    def next_id(self) -> int:
        return max((record.id for record in self.records), default=0) + 1

    def valve_of(self) -> dict[int, int]:
        """Flow label id -> valve id. Vensim writes a flow's name record right after its valve."""
        mapping = {}
        ordered = [record for record in self.records if record.type != 1]
        for valve, label in zip(ordered, ordered[1:]):
            if valve.type == 11 and label.type == 10 and label.shape == 40:
                mapping[label.id] = valve.id
        return mapping


def _parse_views(lines: list[str]) -> list[View]:
    views: list[View] = []
    for index, line in enumerate(lines):
        if line.startswith("*") and index > 0 and lines[index - 1].startswith("V300"):
            if views:
                views[-1].end = index - 2
            views.append(View(name=line[1:].strip(), header=index, end=len(lines)))
            continue
        if line.startswith(SKETCH_END) and views:
            views[-1].end = min(views[-1].end, index)
            break
        if not views or not re.match(r"^(1|10|11|12),\d+,", line):
            continue
        fields, tail = _split_fields(line)
        if len(fields) < (5 if line.startswith("1,") else 8):
            continue
        views[-1].records.append(Record(line=index, fields=fields, tail=tail))
    return views


def _overlaps(box: tuple[float, float, float, float], others, margin: float = 12) -> bool:
    x0, y0, x1, y1 = box
    return any(
        x0 < ox1 + margin and x1 > ox0 - margin and y0 < oy1 + margin and y1 > oy0 - margin
        for ox0, oy0, ox1, oy1 in others
    )


def _box(x: float, y: float, half_w: float, half_h: float) -> tuple[float, float, float, float]:
    return (x - half_w, y - half_h, x + half_w, y + half_h)


def _record_box(record: Record) -> tuple[float, float, float, float]:
    half_w, half_h = record.half
    return _box(record.x, record.y, half_w, half_h)


def _free_spot(anchor, shapes, occupied, step: float = 20, radius: float = 900):
    """Nearest position to `anchor` where every (dx, dy, half_w, half_h) shape fits."""
    candidates = [(0.0, 0.0)]
    for ring in range(1, int(radius / step) + 1):
        for i in range(-ring, ring + 1):
            for j in (-ring, ring):
                candidates.extend([(i * step, j * step), (j * step, i * step)])
    candidates.sort(key=lambda point: (math.hypot(*point), point[1], point[0]))
    for dx, dy in candidates:
        x, y = anchor[0] + dx, anchor[1] + dy
        boxes = [_box(x + sx, y + sy, w, h) for sx, sy, w, h in shapes]
        if all(b[0] >= 10 and b[1] >= 60 for b in boxes) and not any(
            _overlaps(b, occupied) for b in boxes
        ):
            return round(x), round(y)
    return round(anchor[0]), round(anchor[1])


def _line_samples(view: "View", physical: bool) -> list[tuple[float, float, float, float]]:
    """Small boxes along the view's pipes (or straight information arrows), for collision tests."""
    objects = view.by_id()
    samples = []
    for arrow in view.arrows():
        if arrow.is_pipe != physical:
            continue
        start, end = objects.get(arrow.source), objects.get(arrow.target)
        if not start or not end:
            continue
        length = math.hypot(end.x - start.x, end.y - start.y)
        steps = max(1, int(length / 10))
        samples += [
            _box(start.x + (end.x - start.x) * i / steps, start.y + (end.y - start.y) * i / steps, 2, 2)
            for i in range(1, steps)
        ]
    return samples


def _arrow_line(arrow_id: int, source: int, target: int) -> str:
    return f"1,{arrow_id},{source},{target},0,0,0,0,0,192,0,-1--1--1,,1|(0,0)|"


# --------------------------------------------------------------------------- document


class MdlDocument:
    def __init__(self, text: str):
        if SKETCH_START in text:
            index = text.index(SKETCH_START)
            self.equations, sketch = text[:index], text[index:]
        else:
            self.equations, sketch = text, ""
        self.lines = sketch.split("\n")
        self.changes: list[str] = []
        self.warnings: list[str] = []

    def text(self) -> str:
        return self.equations + "\n".join(self.lines)

    # ---- lookups

    def blocks(self) -> list[Block]:
        return scan_blocks(self.equations)

    def names(self) -> list[str]:
        return [block.name for block in self.blocks()]

    def block(self, name: str) -> Block:
        key = fold(name)
        matches = [block for block in self.blocks() if block.key == key]
        if not matches:
            close = difflib.get_close_matches(
                name, [b.name.strip('"') for b in self.blocks()], n=3, cutoff=0.5
            )
            hint = f"; did you mean {', '.join(repr(c) for c in close)}?" if close else ""
            raise EditError(f"no variable named {name!r}{hint}")
        if len(matches) > 1 or matches[0].expression is None:
            raise EditError(
                f"{name!r} is a subscripted, lookup or data variable; edit it in Vensim for now"
            )
        return matches[0]

    def views(self) -> list[View]:
        return _parse_views(self.lines)

    def references(self, block: Block) -> list[str]:
        """Model variables the block's equation uses, each once, in order."""
        others = [name for name in self.names() if fold(name) != block.key]
        seen, ordered = set(), []
        definition = self.equations[block.start : block.definition_end]
        lhs_end = definition.index("=") + 1
        for _, _, name in find_references(definition[lhs_end:], others):
            if fold(name) not in seen:
                seen.add(fold(name))
                ordered.append(name)
        return ordered

    def dependents(self, name: str) -> list[Block]:
        key = fold(name)
        return [
            block
            for block in self.blocks()
            if block.key != key and key in {fold(n) for n in self.references(block)}
        ]

    # ---- low-level edits

    def _replace_block(self, block: Block, text: str) -> None:
        self.equations = self.equations[: block.start] + text + self.equations[block.end :]

    def _write_equation(self, block: Block, expression: str) -> None:
        if not _balanced(expression):
            raise EditError(f"unbalanced parentheses in equation for {block.name}: {expression}")
        self._replace_block(block, _format_block(block.name, expression, block.unit, block.note))

    def _insert_block(self, text: str) -> None:
        blocks = self.blocks()
        group = re.search(r"(?m)^\*{10,}\s*\n\s*\.Control", self.equations)
        if group:
            at = group.start()
        else:
            controls = [b for b in blocks if b.key.upper() in CONTROL_NAMES]
            at = min((b.start for b in controls), default=None)
            if at is None:
                at = len(self.equations.rstrip()) + 1
        self.equations = self.equations[:at] + text + "\n\n" + self.equations[at:]

    def _set_lines(self, edits: dict[int, str | None], inserts: dict[int, list[str]]) -> None:
        """Replace (str) or delete (None) lines by index and insert lines before indices."""
        result = []
        for index, line in enumerate(self.lines):
            result.extend(inserts.get(index, []))
            if index in edits:
                if edits[index] is not None:
                    result.append(edits[index])
            else:
                result.append(line)
        result.extend(inserts.get(len(self.lines), []))
        self.lines = result

    # ---- diagram sync

    def sync_links(self, name: str, remove: bool = True) -> None:
        """Make the information arrows into `name` match the variables its equation uses."""
        block = self.block(name)
        wanted = {fold(ref): ref for ref in self.references(block)}
        for key in list(wanted):
            if key.upper() in CONTROL_NAMES:
                wanted.pop(key)
        missing_everywhere = set(wanted)
        for view in self.views():
            targets = view.instances(block.name)
            if not targets:
                continue
            objects = view.by_id()
            valves = view.valve_of()
            piped: set[str] = set()
            if block.is_level:
                stock_ids = {t.id for t in targets}
                piped_valves = {
                    a.source for a in view.arrows() if a.is_pipe and a.target in stock_ids
                }
                piped = {
                    fold(objects[label].name)
                    for label, valve in valves.items()
                    if valve in piped_valves and label in objects
                }
            edits: dict[int, str | None] = {}
            new_lines: list[str] = []
            next_id = view.next_id()
            for target in targets:
                linked = set()
                for arrow in view.arrows():
                    if arrow.is_pipe or arrow.target != target.id:
                        continue
                    source = objects.get(arrow.source)
                    if source is None or source.type != 10:
                        continue
                    key = fold(source.name)
                    if key in wanted and key not in piped:
                        linked.add(key)
                    elif remove and key not in wanted:
                        edits[arrow.line] = None
                        self.changes.append(f"removed arrow {source.name} -> {target.name}")
                for key, ref in wanted.items():
                    if key in piped:
                        missing_everywhere.discard(key)
                        continue
                    sources = view.instances(ref)
                    if not sources:
                        continue
                    missing_everywhere.discard(key)
                    if key in linked:
                        continue
                    source = min(sources, key=lambda s: math.hypot(s.x - target.x, s.y - target.y))
                    new_lines.append(_arrow_line(next_id, source.id, target.id))
                    next_id += 1
                    self.changes.append(f"added arrow {source.name} -> {target.name}")
            self._set_lines(edits, {view.end: new_lines} if new_lines else {})
        if missing_everywhere and any(view.instances(block.name) for view in self.views()):
            names = ", ".join(sorted(wanted[key] for key in missing_everywhere))
            self.warnings.append(
                f"{block.name} uses {names}, which is not on the diagram, so no arrow was drawn"
            )

    # ---- operations

    def set_equation(
        self,
        name: str,
        equation: str | None = None,
        unit: str | None = None,
        note: str | None = None,
        sync_links: bool = True,
    ) -> None:
        block = self.block(name)
        expression = " ".join(equation.split()) if equation is not None else block.expression
        if not expression:
            raise EditError("equation is empty")
        was_level = block.is_level
        new = Block(
            block.start, block.end, block.definition_end, block.name, expression,
            block.unit if unit is None else unit.strip(),
            block.note if note is None else note.strip(),
        )
        if not _balanced(expression):
            raise EditError(f"unbalanced parentheses: {expression}")
        self._replace_block(block, _format_block(new.name, expression, new.unit, new.note))
        if expression != block.expression:
            self.changes.append(f"{block.name}: {block.expression}  ->  {expression}")
        if new.unit != block.unit:
            self.changes.append(f"{block.name} unit: {block.unit or '(none)'} -> {new.unit}")
        if new.note != block.note:
            self.changes.append(f"{block.name} note updated")
        if was_level != new.is_level:
            self.warnings.append(
                f"{block.name} is drawn as {'a stock' if was_level else 'an auxiliary'} but its "
                "equation no longer matches; change its type in Vensim"
            )
        if sync_links:
            self.sync_links(block.name)

    def add_variable(
        self,
        name: str,
        equation: str,
        unit: str,
        kind: str = "auxiliary",
        note: str = "",
        source: str | None = None,
        target: str | None = None,
        x: float | None = None,
        y: float | None = None,
        near: str | None = None,
        view_name: str | None = None,
    ) -> None:
        kind = {"stock": "level", "constant": "auxiliary", "rate": "flow"}.get(kind, kind)
        if kind not in {"auxiliary", "level", "flow"}:
            raise EditError(f"unsupported kind {kind!r}; use auxiliary, constant, level or flow")
        bare = _check_name(name)
        written = quote(bare)
        if fold(bare) in {fold(n) for n in self.names()}:
            raise EditError(f"a variable named {bare!r} already exists")
        expression = " ".join(equation.split())
        if not expression or not _balanced(expression):
            raise EditError(f"invalid equation: {equation!r}")
        if kind == "level" and _split_integ(expression) is None:
            raise EditError(
                "a stock's equation must be INTEG(net flow, initial value), for example "
                "INTEG(0, 100); flows added later are appended to the net flow"
            )
        if kind != "level" and re.match(r"\s*INTEG\s*\(", expression, re.I):
            raise EditError("an INTEG equation makes this a stock; use kind 'level'")
        stocks = [s for s in (source, target) if s]
        if kind == "flow":
            if not stocks:
                raise EditError("a flow needs a source stock, a target stock, or both")
            for stock in stocks:
                if not self.block(stock).is_level:
                    raise EditError(f"{stock!r} is not a stock")
        elif stocks:
            raise EditError("source and target apply to flows only")

        self._insert_block(_format_block(written, expression, unit.strip(), note))
        self.changes.append(f"added {kind} {written} = {expression}")
        if kind == "flow":
            for stock, sign in ((source, "-"), (target, "+")):
                if stock:
                    self._add_flow_term(stock, written, sign)

        views = self.views()
        if not views:
            self.warnings.append("model has no diagram; equation added without a sketch object")
            return
        if view_name:
            matching = [v for v in views if v.name.casefold() == view_name.casefold()]
            if not matching:
                raise EditError(f"no view named {view_name!r}; views: {[v.name for v in views]}")
            view = matching[0]
        else:
            anchors = [near, *stocks] if near or stocks else self.references(self.block(written))
            view = max(views, key=lambda v: sum(bool(v.instances(a)) for a in anchors if a))
        if kind == "flow":
            self._draw_flow(view, written, source, target, x, y)
        else:
            self._draw_variable(view, written, kind, x, y, near)
        self.sync_links(written)
        for dependent in self.dependents(written):
            self.sync_links(dependent.name, remove=False)

    def _occupied(self, view: View) -> list[tuple[float, float, float, float]]:
        return [_record_box(r) for r in view.records if r.type != 1]

    def _anchor(self, view: View, name: str, near: str | None) -> tuple[float, float]:
        objects = [r for r in view.records if r.type != 1]
        if near:
            instances = view.instances(near)
            if not instances:
                raise EditError(f"{near!r} is not on view {view.name!r}")
            return instances[0].x, instances[0].y - 70
        refs = [r for ref in self.references(self.block(name)) for r in view.instances(ref)[:1]]
        if refs:
            return sum(r.x for r in refs) / len(refs), sum(r.y for r in refs) / len(refs) - 40
        if not objects:
            return 200, 150
        return max(r.x + r.half[0] for r in objects) + 120, min(r.y for r in objects)

    def _draw_variable(self, view, name, kind, x, y, near) -> None:
        if kind == "level":
            half = (max(LEVEL_HALF[0], _label_width(name.strip('"')) // 2), LEVEL_HALF[1])
            shape = 3
        else:
            half = (_label_width(name.strip('"')) // 2, AUX_HALF_HEIGHT)
            shape = 8
        if x is None or y is None:
            x, y = _free_spot(self._anchor(view, name, near), [(0, 0, *half)], self._occupied(view))
        object_id = view.next_id()
        line = f"10,{object_id},{name},{int(x)},{int(y)},{half[0]},{half[1]},{shape},3,0,0,{0 if kind == 'level' else -1},0,0,0,0,0,0,0,0,0"
        self._set_lines({}, {view.end: [line]})
        self.changes.append(f"drew {name} at ({int(x)}, {int(y)}) on view {view.name!r}")

    def _draw_flow(self, view, name, source, target, x, y) -> None:
        source_obj = view.instances(source)[0] if source and view.instances(source) else None
        target_obj = view.instances(target)[0] if target and view.instances(target) else None
        if (source and not source_obj) or (target and not target_obj):
            missing = source if source and not source_obj else target
            raise EditError(f"stock {missing!r} is not on view {view.name!r}")
        label_half = (_label_width(name.strip('"')) // 2, AUX_HALF_HEIGHT)
        occupied = self._occupied(view)
        cloud: tuple[float, float] | None = None
        if source_obj and target_obj:
            vertical = abs(target_obj.x - source_obj.x) < abs(target_obj.y - source_obj.y)
            vx = (source_obj.x + target_obj.x) / 2 if x is None else x
            vy = (source_obj.y + target_obj.y) / 2 if y is None else y
            if abs(source_obj.x - target_obj.x) > 20 and abs(source_obj.y - target_obj.y) > 20:
                self.warnings.append(
                    f"{source} and {target} are not aligned, so the pipe is drawn at an angle; "
                    "drag the stocks in Vensim to straighten it"
                )
        else:
            stock = source_obj or target_obj
            outflow = source_obj is not None
            # Sides to try, in order: (direction x, direction y).
            sides = [(1, 0), (0, 1), (-1, 0), (0, -1)] if outflow else [(-1, 0), (0, -1), (1, 0), (0, 1)]
            pipes, links = _line_samples(view, physical=True), _line_samples(view, physical=False)
            # Try each side at growing distances; take the first clear spot, else the one
            # that collides with the fewest objects.
            best, best_hits = None, None
            for scale in (1.0, 1.4, 1.8, 2.4, 3.0):
                for dx, dy in sides:
                    vertical = dy != 0
                    v_off, c_off = (DOWN_VALVE, DOWN_CLOUD) if vertical else (SIDE_VALVE, SIDE_CLOUD)
                    v_off, c_off = v_off * scale, c_off * scale
                    v = (stock.x + dx * v_off, stock.y + dy * v_off)
                    c = (stock.x + dx * c_off, stock.y + dy * c_off)
                    label = (v[0] + label_half[0] + 14, v[1]) if vertical else (v[0], v[1] + 30)
                    boxes = [_box(*v, 6, 8), _box(*c, 10, 8), _box(*label, *label_half)]
                    # The pipe itself, from the stock's edge to the cloud.
                    edge = stock.half[0] if dx else stock.half[1]
                    boxes += [
                        _box(stock.x + dx * d, stock.y + dy * d, 2, 2)
                        for d in range(int(edge) + 8, int(c_off), 10)
                    ]
                    if not all(b[0] >= 10 and b[1] >= 60 for b in boxes):
                        continue
                    hits = (
                        3 * sum(_overlaps(b, [o], 6) for b in boxes[:3] for o in occupied)
                        + 3 * sum(_overlaps(b, occupied, 0) for b in boxes[3:])
                        + 2 * sum(_overlaps(b, pipes, 2) for b in boxes)
                        + sum(_overlaps(b, links, 0) for b in boxes)
                    )
                    if best_hits is None or hits < best_hits:
                        best, best_hits = (v, c, vertical), hits
                    if hits == 0:
                        break
                if best_hits == 0:
                    break
            if best is None:
                best = ((stock.x + SIDE_VALVE, stock.y), (stock.x + SIDE_CLOUD, stock.y), False)
            if best_hits:
                self.warnings.append(
                    f"no clear space around {stock.name} for {name}; it overlaps nearby objects, "
                    "so drag it in Vensim or pass x and y"
                )
            chosen = best
            (vx, vy), cloud, vertical = chosen
            if x is not None and y is not None:
                cloud = (cloud[0] + x - vx, cloud[1] + y - vy)
                vx, vy = x, y
        vx, vy = int(vx), int(vy)
        label = (vx + label_half[0] + 14, vy) if vertical else (vx, vy + 30)
        next_id = view.next_id()
        valve_id, label_id = next_id, next_id + 1
        next_id += 2
        lines = [
            f"11,{valve_id},0,{vx},{vy},6,8,34,3,0,0,{4 if vertical else 1},0,0,0,0,0,0,0,0,0",
            f"10,{label_id},{name},{int(label[0])},{int(label[1])},{label_half[0]},{label_half[1]},40,3,0,0,-1,0,0,0,0,0,0,0,0,0",
        ]
        ends: list[tuple[int, float, float, bool]] = []
        if cloud is not None:
            cloud_id = next_id
            next_id += 1
            cx, cy = int(cloud[0]), int(cloud[1])
            lines.append(f"12,{cloud_id},48,{cx},{cy},10,8,0,3,0,0,-1,0,0,0,0,0,0,0,0,0")
            ends.append((cloud_id, cx, cy, target_obj is None))
        if source_obj:
            ends.append((source_obj.id, source_obj.x, source_obj.y, False))
        if target_obj:
            ends.append((target_obj.id, target_obj.x, target_obj.y, True))
        for end_id, ex, ey, head in ends:
            mid = (int((vx + ex) / 2), int((vy + ey) / 2))
            lines.append(
                f"1,{next_id},{valve_id},{end_id},{4 if head else 100},0,0,22,0,192,0,"
                f"-1--1--1,,1|({mid[0]},{mid[1]})|"
            )
            next_id += 1
        self._set_lines({}, {view.end: lines})
        ends_text = f"{source or 'cloud'} -> {target or 'cloud'}"
        self.changes.append(f"drew flow {name} ({ends_text}) at ({vx}, {vy}) on view {view.name!r}")

    def _add_flow_term(self, stock: str, flow: str, sign: str) -> None:
        block = self.block(stock)
        net, initial = _split_integ(block.expression or "") or (None, None)
        if net is None:
            raise EditError(f"cannot read the INTEG equation of {stock!r}")
        terms = [] if net.strip() in ("", "0") else _terms(net)
        terms.append((sign, flow))
        self._write_equation(block, f"INTEG ( {_join_terms(terms)}, {initial} )")
        self.changes.append(f"{block.name}: net flow is now {_join_terms(terms)}")

    def _remove_flow_term(self, stock_block: Block, flow: str) -> bool:
        net, initial = _split_integ(stock_block.expression or "") or (None, None)
        if net is None:
            return False
        terms = _terms(net)
        kept = [(sign, term) for sign, term in terms if fold(term) != fold(flow)]
        if len(kept) == len(terms):
            return False
        self._write_equation(stock_block, f"INTEG ( {_join_terms(kept)}, {initial} )")
        self.changes.append(f"{stock_block.name}: net flow is now {_join_terms(kept)}")
        return True

    def delete_variable(self, name: str, force: bool = False) -> None:
        block = self.block(name)
        # A flow's own stocks lose it from their net flow; any other use blocks the delete.
        for stock in [b for b in self.blocks() if b.is_level and b.key != block.key]:
            self._remove_flow_term(stock, block.name)
        dependents = [b.name for b in self.dependents(block.name)]
        if dependents and not force:
            raise EditError(
                f"{block.name} is used by {', '.join(dependents)}; change those equations first "
                "or pass force=true to leave them referring to a missing variable"
            )
        if dependents:
            self.warnings.append(f"these equations now refer to a missing variable: {', '.join(dependents)}")
        block = self.block(name)
        end = block.end
        while end < len(self.equations) and self.equations[end] in " \t\r\n":
            end += 1
        self.equations = self.equations[: block.start] + self.equations[end:]
        self.changes.append(f"deleted equation {block.name}")
        for view in self.views():
            instances = view.instances(block.name)
            if not instances:
                continue
            objects = view.by_id()
            removed = {record.id for record in instances}
            valves = view.valve_of()
            removed |= {valves[r.id] for r in instances if r.id in valves}
            pipes = [a for a in view.arrows() if a.is_pipe and a.source in removed]
            cloud_ids = {
                end_id
                for a in pipes
                for end_id in (a.target,)
                if objects.get(end_id) and objects[end_id].type == 12 and objects[end_id].fields[2] == "48"
            }
            still_used = {
                a.target for a in view.arrows() if a.is_pipe and a.source not in removed
            } | {a.source for a in view.arrows() if a.is_pipe and a.source not in removed}
            removed |= cloud_ids - still_used
            edits: dict[int, str | None] = {}
            inserts: list[str] = []
            next_id = view.next_id()
            for record in view.records:
                if record.type != 1 and record.id in removed:
                    edits[record.line] = None
                elif record.type == 1 and (record.source in removed or record.target in removed):
                    if record.is_pipe and record.target in removed and record.source not in removed:
                        # A pipe into a deleted stock now ends in a cloud where the stock was.
                        stock = objects[record.target]
                        inserts.append(
                            f"12,{next_id},48,{int(stock.x)},{int(stock.y)},10,8,0,3,0,0,-1,0,0,0,0,0,0,0,0,0"
                        )
                        record.fields[3] = str(next_id)
                        edits[record.line] = record.text()
                        next_id += 1
                    else:
                        edits[record.line] = None
            self._set_lines(edits, {view.end: inserts} if inserts else {})
            self.changes.append(f"removed {block.name} from view {view.name!r}")

    def rename_variable(self, old: str, new: str) -> None:
        block = self.block(old)
        bare = _check_name(new)
        written = quote(bare)
        if fold(bare) != block.key and fold(bare) in {fold(n) for n in self.names()}:
            raise EditError(f"a variable named {bare!r} already exists")
        names = self.names()
        pieces = []
        last = 0
        count = 0
        for b in self.blocks():
            definition = self.equations[b.start : b.definition_end]
            for start, end, ref in find_references(definition, names):
                if fold(ref) == block.key:
                    pieces.append(self.equations[last : b.start + start])
                    pieces.append(written)
                    last = b.start + end
                    count += 1
        pieces.append(self.equations[last:])
        self.equations = "".join(pieces)
        self.changes.append(f"renamed {block.name} to {written} in {count} place(s) in equations")
        for view in self.views():
            edits = {}
            for record in view.instances(block.name):
                record.fields[2] = written
                if record.shape != 3:
                    width = _label_width(bare) // 2
                    if width > float(record.fields[5]):
                        record.fields[5] = str(width)
                edits[record.line] = record.text()
            if edits:
                self._set_lines(edits, {})
                self.changes.append(f"renamed {len(edits)} diagram object(s) on view {view.name!r}")


# --------------------------------------------------------------------------- file I/O


def _backup_dir() -> Path:
    base = os.environ.get("VENSIM_PLE_MCP_CACHE") or os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "vensim-ple-mcp" / "backups"


def _load(path: str | Path) -> tuple[Path, str, str, str]:
    model = Path(path).expanduser().resolve()
    if model.suffix.lower() != ".mdl" or not model.is_file():
        raise FileNotFoundError(f"Vensim .mdl not found: {model}")
    raw = model.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    # utf-8-sig keeps a byte-order mark only when the original file had one.
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = "latin-1"
        text = raw.decode(encoding)
    return model, text.replace("\r\n", "\n"), encoding, newline


def edit_model(path: str | Path, operation: str, output_path: str | Path | None = None, **kwargs) -> dict:
    """Apply one editing operation and write the model, keeping a backup of the original."""
    from .parser import validate_model

    model, text, encoding, newline = _load(path)
    document = MdlDocument(text)
    try:
        getattr(document, operation)(**kwargs)
    except EditError as error:
        return {"ok": False, "error": str(error), "model": str(model), "written": False}
    output = Path(output_path).expanduser().resolve() if output_path else model
    backup = None
    if output == model:
        backup_dir = _backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{model.stem}-{datetime.now():%Y%m%d-%H%M%S-%f}.mdl"
        shutil.copyfile(model, backup)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding=encoding, newline=newline) as handle:
        handle.write(document.text())
    validation = validate_model(output)
    return {
        "ok": True,
        "model": str(output),
        "backup": str(backup) if backup else None,
        "changes": document.changes,
        "warnings": document.warnings,
        "validation": {key: validation[key] for key in ("ok", "errors", "warnings")},
        "note": (
            "If this model is open in Vensim, close it without saving and reopen it; saving "
            "from Vensim would overwrite these edits."
        ),
    }
