"""Find feedback loops and label them reinforcing or balancing.

The causal graph comes from the equations: an edge runs from every variable an equation
uses to the variable it defines, and from each flow to the stocks whose INTEG uses it.
Each edge's polarity is the sign of the partial derivative, worked out symbolically with
the usual system dynamics assumption that variables are positive (constants use their
actual sign). Lookups take the direction of their table. A loop is reinforcing when it
has an even number of negative links and balancing when odd; a link whose sign can't be
determined (an IF THEN ELSE condition, a non-monotonic lookup) makes the loop's polarity
unknown rather than a guess.

A loop with no stock and no delay function is an algebraic loop (Vensim's "simultaneous
equations"), which Vensim refuses to simulate.
"""

from __future__ import annotations

import math
import re

from .checks import TOKEN_RE
from .editor import MdlDocument, _free_spot, _line_samples, _split_integ, fold
from .model import CONTROL_NAMES


# Functions with stocks inside: dependence through them is delayed, which breaks
# algebraic loops.
DELAY_FUNCTIONS = {
    "smooth", "smoothi", "smooth3", "smooth3i", "smooth n", "delay1", "delay1i", "delay3",
    "delay3i", "delay n", "delay fixed", "delay information", "delay material",
    "delay conveyor", "trend", "forecast", "npv", "npve", "integ",
}
# Output rises with the first argument.
INCREASING_FIRST = DELAY_FUNCTIONS | {"active initial", "step", "ramp", "quantum"}
# Output rises with every argument.
INCREASING_ALL = {"max", "min", "exp", "ln", "log", "sqrt", "integer", "vmax", "vmin", "sum"}
# Values frozen after the initial time: no causal link.
FROZEN = {"initial"}


# --------------------------------------------------------------------------- parsing


class _Parser:
    def __init__(self, text: str):
        self.tokens = [
            (match.lastgroup, match.group())
            for match in TOKEN_RE.finditer(text)
            if match.lastgroup != "space"
        ]
        self.position = 0

    def peek(self, offset: int = 0):
        index = self.position + offset
        return self.tokens[index][1] if index < len(self.tokens) else None

    def take(self):
        token = self.tokens[self.position]
        self.position += 1
        return token

    def expect(self, value: str) -> None:
        if self.peek() != value:
            raise SyntaxError(f"expected {value!r}")
        self.position += 1

    def parse(self):
        node = self.logical()
        if self.position != len(self.tokens):
            raise SyntaxError("trailing tokens")
        return node

    def logical(self):
        node = self.comparison()
        while self.peek() in (":AND:", ":OR:"):
            self.take()
            node = ("logic", node, self.comparison())
        return node

    def comparison(self):
        node = self.additive()
        while self.peek() in ("=", "<", ">", "<=", ">=", "<>"):
            self.take()
            node = ("cmp", node, self.additive())
        return node

    def additive(self):
        node = self.multiplicative()
        while self.peek() in ("+", "-"):
            operator = self.take()[1]
            node = ("add" if operator == "+" else "sub", node, self.multiplicative())
        return node

    def multiplicative(self):
        node = self.power()
        while self.peek() in ("*", "/"):
            operator = self.take()[1]
            node = ("mul" if operator == "*" else "div", node, self.power())
        return node

    def power(self):
        node = self.unary()
        if self.peek() == "^":
            self.take()
            node = ("pow", node, self.unary())
        return node

    def unary(self):
        if self.peek() in ("-", "+", ":NOT:"):
            operator = self.take()[1]
            inner = self.unary()
            return {"-": ("neg", inner), "+": inner}.get(operator, ("not", inner))
        return self.primary()

    def raw_until_close(self, start: int) -> str:
        """Collect a parenthesised literal (a lookup table) starting at token `start`."""
        self.position = start
        depth = 0
        pieces = []
        while self.position < len(self.tokens):
            value = self.take()[1]
            pieces.append(value)
            depth += {"(": 1, ")": -1}.get(value, 0)
            if depth == 0:
                break
        return "".join(pieces)

    def primary(self):
        if self.peek() is None:
            raise SyntaxError("unexpected end")
        start = self.position
        kind, value = self.take()
        if kind == "number":
            return ("num", float(value))
        if kind == "logical":  # :NA:
            return ("num", None)
        if value == "(":
            if self.peek() in ("[", "("):
                return ("table", table_points(self.raw_until_close(start)))
            node = self.logical()
            if self.peek() == ",":
                return ("table", table_points(self.raw_until_close(start)))
            self.expect(")")
            return node
        if kind in ("name", "quoted"):
            key = fold(value)
            if self.peek() == "[":
                depth = 0
                while self.peek() is not None:
                    token = self.take()[1]
                    depth += {"[": 1, "]": -1}.get(token, 0)
                    if depth == 0:
                        break
            if self.peek() == "(":
                return ("call", key, self.arguments())
            return ("var", key)
        raise SyntaxError(f"unexpected {value!r}")

    def arguments(self) -> list:
        self.expect("(")
        args = []
        if self.peek() == ")":
            self.take()
            return args
        while True:
            args.append(self.logical())
            if self.peek() == ",":
                self.take()
                continue
            self.expect(")")
            return args


def table_points(text: str) -> list[tuple[float, float]]:
    text = re.sub(r"\[[^\]]*\]", "", text)
    number = r"-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?"
    return [(float(x), float(y)) for x, y in re.findall(rf"\(\s*({number})\s*,\s*({number})\s*\)", text)]


def table_sign(points: list[tuple[float, float]]) -> str:
    ys = [y for _, y in sorted(points)]
    if len(ys) < 2:
        return "?"
    rising = all(b >= a for a, b in zip(ys, ys[1:]))
    falling = all(b <= a for a, b in zip(ys, ys[1:]))
    if rising and falling:
        return "0"
    return "+" if rising else "-" if falling else "?"


# --------------------------------------------------------------------------- signs


def _flip(sign: str) -> str:
    return {"+": "-", "-": "+"}.get(sign, sign)


def _times(a: str, b: str) -> str:
    if "0" in (a, b):
        return "0"
    if "?" in (a, b):
        return "?"
    return "+" if a == b else "-"


def _plus(a: str, b: str) -> str:
    if a == "0":
        return b
    if b == "0":
        return a
    return a if a == b else "?"


INF = math.inf
ANY = (-INF, INF)
NONNEGATIVE = (0.0, INF)


def _interval_sign(interval: tuple[float, float]) -> str:
    low, high = interval
    if low == high == 0:
        return "0"
    if low >= 0:
        return "+"
    if high <= 0:
        return "-"
    return "?"


def _mul(a, b):
    products = []
    for x in a:
        for y in b:
            product = x * y
            products.append(0.0 if math.isnan(product) else product)
    return (min(products), max(products))


def _inverse(interval):
    low, high = interval
    if low < 0 < high or (low < 0 and high == 0) or (low == 0 and high < 0):
        return ANY
    if low >= 0:
        # Variables are taken as positive, so a divisor that may reach zero still
        # gives a non-negative quotient.
        return (1 / high if high not in (0, INF) else 0.0, 1 / low if low > 0 else INF)
    return (1 / low if low != -INF else 0.0, 1 / high)


def _union(*intervals):
    return (min(i[0] for i in intervals), max(i[1] for i in intervals))


class _Signs:
    """Value ranges and derivative signs over the parsed equations of one model.

    Ranges make the analysis see what a modeller sees: constants have their value,
    stocks and other variables are non-negative, MIN/MAX and lookups bound their result.
    So 1 - MIN(0.9, x) is known to be positive.
    """

    def __init__(
        self,
        constants: dict[str, float],
        lookups: dict[str, str],
        lookup_ranges: dict[str, tuple[float, float]] | None = None,
        trees: dict[str, tuple] | None = None,
    ):
        self.constants = constants
        self.lookups = lookups
        self.lookup_ranges = lookup_ranges or {}
        self.trees = trees or {}
        self._ranges: dict[str, tuple[float, float]] = {}
        self._visiting: set[str] = set()

    def value(self, node) -> str:
        return _interval_sign(self.range(node))

    def variable_range(self, key: str) -> tuple[float, float]:
        if key in self.constants:
            return (self.constants[key], self.constants[key])
        if key in self._ranges:
            return self._ranges[key]
        tree = self.trees.get(key)
        if tree is None or key in self._visiting:
            return NONNEGATIVE  # stocks, data, and anything circular
        self._visiting.add(key)
        result = self.range(tree)
        self._visiting.discard(key)
        # A variable whose equation can't be bounded is still assumed non-negative.
        if result == ANY:
            result = NONNEGATIVE
        self._ranges[key] = result
        return result

    def range(self, node) -> tuple[float, float]:
        kind = node[0]
        if kind == "num":
            return ANY if node[1] is None else (node[1], node[1])
        if kind == "var":
            return self.variable_range(node[1])
        if kind == "neg":
            low, high = self.range(node[1])
            return (-high, -low)
        if kind == "add":
            a, b = self.range(node[1]), self.range(node[2])
            return (a[0] + b[0], a[1] + b[1])
        if kind == "sub":
            a, b = self.range(node[1]), self.range(node[2])
            return (a[0] - b[1], a[1] - b[0])
        if kind == "mul":
            return _mul(self.range(node[1]), self.range(node[2]))
        if kind == "div":
            return _mul(self.range(node[1]), _inverse(self.range(node[2])))
        if kind == "pow":
            base = self.range(node[1])
            return NONNEGATIVE if base[0] >= 0 else ANY
        if kind in ("cmp", "logic", "not"):
            return (0.0, 1.0)
        if kind == "call":
            return self._call_range(node[1], node[2])
        return ANY

    def _call_range(self, name: str, args: list) -> tuple[float, float]:
        if name in self.lookup_ranges:
            return self.lookup_ranges[name]
        ranges = [self.range(arg) for arg in args]
        if not ranges:
            return ANY
        if name == "min":
            return (min(r[0] for r in ranges), min(r[1] for r in ranges))
        if name == "max":
            return (max(r[0] for r in ranges), max(r[1] for r in ranges))
        if name == "with lookup" and len(args) == 2 and args[1][0] == "table":
            ys = [y for _, y in args[1][1]]
            return (min(ys), max(ys)) if ys else ANY
        if name == "if then else" and len(ranges) == 3:
            return _union(ranges[1], ranges[2])
        if name in ("xidz", "zidz") and len(ranges) >= 2:
            quotient = _mul(ranges[0], _inverse(ranges[1]))
            return _union(quotient, ranges[2]) if len(ranges) > 2 else _union(quotient, (0.0, 0.0))
        if name in DELAY_FUNCTIONS | {"initial", "active initial", "integer", "sample if true"}:
            first = ranges[1] if name == "sample if true" and len(ranges) > 1 else ranges[0]
            return first
        if name in ("exp", "sqrt", "abs"):
            return NONNEGATIVE
        if name == "step":
            return _union(ranges[0], (0.0, 0.0))
        return ANY

    def derivative(self, node, key: str) -> str:
        """Sign of d(node)/d(key)."""
        kind = node[0]
        if kind in ("num", "table"):
            return "0"
        if kind == "var":
            return "+" if node[1] == key else "0"
        if kind == "neg":
            return _flip(self.derivative(node[1], key))
        if kind == "not":
            return "?" if self.derivative(node[1], key) != "0" else "0"
        if kind == "add":
            return _plus(self.derivative(node[1], key), self.derivative(node[2], key))
        if kind == "sub":
            return _plus(self.derivative(node[1], key), _flip(self.derivative(node[2], key)))
        if kind == "mul":
            a, b = node[1], node[2]
            return _plus(
                _times(self.derivative(a, key), self.value(b)),
                _times(self.value(a), self.derivative(b, key)),
            )
        if kind == "div":
            a, b = node[1], node[2]
            return _plus(
                _times(self.derivative(a, key), self.value(b)),
                _flip(_times(self.value(a), self.derivative(b, key))),
            )
        if kind == "pow":
            base, exponent = node[1], node[2]
            if self.derivative(exponent, key) != "0":
                return "?"
            return _times(self.derivative(base, key), self.value(exponent))
        if kind in ("cmp", "logic"):
            return "?" if any(self.derivative(n, key) != "0" for n in node[1:]) else "0"
        if kind == "call":
            return self._call(node[1], node[2], key)
        return "?"

    def _call(self, name: str, args: list, key: str) -> str:
        signs = [self.derivative(arg, key) for arg in args]
        if all(sign == "0" for sign in signs):
            return "0"
        if name in self.lookups:
            return _times(signs[0], self.lookups[name]) if len(args) == 1 else "?"
        if name in FROZEN:
            return "0"
        if name == "with lookup" and len(args) == 2 and args[1][0] == "table":
            return _times(signs[0], table_sign(args[1][1]))
        if name == "if then else" and len(args) == 3:
            if signs[0] != "0":
                return "?"
            return _plus(signs[1], signs[2])
        if name in ("xidz", "zidz") and len(args) >= 2:
            division = self.derivative(("div", args[0], args[1]), key)
            return division if len(args) == 2 or signs[2] == "0" else "?"
        if name == "active initial":
            return signs[0]
        if name == "sample if true" and len(args) >= 2:
            return signs[1] if signs[0] == "0" and (len(args) < 3 or signs[2] == "0") else "?"
        if name in INCREASING_ALL:
            result = "0"
            for sign in signs:
                result = _plus(result, sign)
            return result
        if name in INCREASING_FIRST:
            return signs[0] if all(sign == "0" for sign in signs[1:]) else "?"
        return "?"


def _occurrences(node, key: str, delayed: bool = False):
    """Yield, for each use of `key`, whether it sits inside a delay function."""
    kind = node[0]
    if kind == "var":
        if node[1] == key:
            yield delayed
        return
    if kind == "call":
        name, args = node[1], node[2]
        if name in FROZEN:
            return
        for index, arg in enumerate(args):
            if name == "active initial" and index == 1:
                continue
            yield from _occurrences(arg, key, delayed or name in DELAY_FUNCTIONS)
        return
    for child in node[1:]:
        if isinstance(child, tuple):
            yield from _occurrences(child, key, delayed)


# Delay functions with an explicit initial value, and that argument's position. They
# don't need their input at the initial time; the others start equal to their input.
EXPLICIT_INITIAL = {
    "smoothi": 2, "smooth3i": 2, "delay1i": 2, "delay3i": 2, "smooth n": 2, "delay n": 2,
    "delay fixed": 2, "trend": 2,
}


def _initial_keys(node) -> set[str]:
    """Variables needed to compute `node` at the initial time."""
    kind = node[0]
    if kind == "var":
        return {node[1]}
    if kind == "call":
        name, args = node[1], node[2]
        if name in EXPLICIT_INITIAL and len(args) > EXPLICIT_INITIAL[name]:
            # The initial value and the delay time; not the input.
            needed = [args[EXPLICIT_INITIAL[name]], args[1]]
        elif name == "active initial" and len(args) == 2:
            needed = [args[1]]
        else:
            needed = args
        return {name}.union(*(_initial_keys(arg) for arg in needed)) if needed else {name}
    result: set[str] = set()
    for child in node[1:]:
        if isinstance(child, tuple):
            result |= _initial_keys(child)
    return result


def _keys(node) -> set[str]:
    kind = node[0]
    if kind == "var":
        return {node[1]}
    if kind == "call":
        return {node[1]}.union(*(_keys(arg) for arg in node[2])) if node[2] else {node[1]}
    result: set[str] = set()
    for child in node[1:]:
        if isinstance(child, tuple):
            result |= _keys(child)
    return result


# --------------------------------------------------------------------------- graph


class CausalGraph:
    def __init__(self, document: MdlDocument):
        self.document = document
        blocks = document.blocks()
        self.names = {block.key: block.name.strip('"') for block in blocks}
        self.stocks = {block.key for block in blocks if block.is_level}
        constants = {}
        for block in blocks:
            if block.expression and re.fullmatch(r"-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?", block.expression):
                constants[block.key] = float(block.expression)
        tables = {
            key: table_points(definition)
            for key, definition, is_lookup in _all_blocks(document)
            if is_lookup
        }
        lookups = {key: table_sign(points) for key, points in tables.items()}
        lookup_ranges = {
            key: (min(y for _, y in points), max(y for _, y in points))
            for key, points in tables.items()
            if points
        }
        trees = {}
        for block in blocks:
            if block.expression and not block.is_level:
                try:
                    trees[block.key] = _Parser(block.expression).parse()
                except (SyntaxError, IndexError, ValueError):
                    pass
        signs = _Signs(constants, lookups, lookup_ranges, trees)
        # edges[source][target] = (sign, delayed)
        self.edges: dict[str, dict[str, tuple[str, bool]]] = {}
        # init_edges[source] = variables that need source's value at the initial time
        self.init_edges: dict[str, set[str]] = {}
        self.unreadable: list[str] = []
        for block in blocks:
            if not block.expression or block.key.upper() in CONTROL_NAMES:
                continue
            integ = _split_integ(block.expression)
            try:
                initial_tree = _Parser(integ[1] if integ else block.expression).parse()
                for key in _initial_keys(initial_tree):
                    if key in self.names and key.upper() not in CONTROL_NAMES:
                        self.init_edges.setdefault(key, set()).add(block.key)
            except (SyntaxError, IndexError, ValueError):
                pass
            text = integ[0] if integ else block.expression
            try:
                tree = _Parser(text).parse()
            except (SyntaxError, IndexError, ValueError):
                self.unreadable.append(block.name)
                continue
            for key in _keys(tree):
                if key not in self.names:
                    continue
                if key.upper() in CONTROL_NAMES or key in lookups:
                    continue
                uses = list(_occurrences(tree, key))
                if not uses:
                    continue
                sign = signs.derivative(tree, key)
                if sign == "0":
                    continue
                delayed = bool(integ) or all(uses)
                self.edges.setdefault(key, {})[block.key] = (sign, delayed)

    def sign(self, source: str, target: str) -> str | None:
        edge = self.edges.get(source, {}).get(target)
        return edge[0] if edge else None

    # ---- cycles

    def cycles(self, limit: int = 200, budget: int = 200_000) -> tuple[list[list[str]], bool]:
        """Elementary cycles, each once, via a DFS from each node over higher-ranked nodes."""
        order = sorted(self.edges.keys() | {t for targets in self.edges.values() for t in targets})
        rank = {node: index for index, node in enumerate(order)}
        found: list[list[str]] = []
        steps = 0
        truncated = False
        for start in order:
            stack = [(start, iter(sorted(self.edges.get(start, {}))))]
            path = [start]
            on_path = {start}
            while stack:
                steps += 1
                if steps > budget or len(found) >= limit:
                    return found, True
                node, children = stack[-1]
                child = next(children, None)
                if child is None:
                    stack.pop()
                    on_path.discard(path.pop())
                    continue
                if child == start:
                    found.append(list(path))
                elif rank[child] > rank[start] and child not in on_path:
                    path.append(child)
                    on_path.add(child)
                    stack.append((child, iter(sorted(self.edges.get(child, {})))))
        return found, truncated

    def algebraic_loops(self) -> list[list[str]]:
        """Groups of variables that depend on each other with no stock or delay between."""
        graph = {
            source: [t for t, (_, delayed) in targets.items() if not delayed]
            for source, targets in self.edges.items()
        }
        return self._cyclic_groups(graph)

    def initialization_loops(self) -> list[list[str]]:
        """Groups of variables whose values at the initial time depend on each other."""
        return self._cyclic_groups(
            {source: sorted(targets) for source, targets in self.init_edges.items()}
        )

    def _cyclic_groups(self, graph: dict[str, list[str]]) -> list[list[str]]:
        """Strongly connected groups (with more than one member, or a self-loop)."""
        index_of: dict[str, int] = {}
        low: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        groups: list[list[str]] = []
        counter = [0]

        def connect(node: str) -> None:
            # Iterative Tarjan, so large models don't hit the recursion limit.
            work = [(node, iter(graph.get(node, [])))]
            index_of[node] = low[node] = counter[0]
            counter[0] += 1
            stack.append(node)
            on_stack.add(node)
            while work:
                current, children = work[-1]
                child = next(children, None)
                if child is not None:
                    if child not in index_of:
                        index_of[child] = low[child] = counter[0]
                        counter[0] += 1
                        stack.append(child)
                        on_stack.add(child)
                        work.append((child, iter(graph.get(child, []))))
                    elif child in on_stack:
                        low[current] = min(low[current], index_of[child])
                    continue
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[current])
                if low[current] == index_of[current]:
                    group = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        group.append(member)
                        if member == current:
                            break
                    if len(group) > 1 or current in graph.get(current, []):
                        groups.append(sorted(group))

        for node in sorted(graph):
            if node not in index_of:
                connect(node)
        return [[self.names.get(key, key) for key in group] for group in groups]


def _all_blocks(document: MdlDocument):
    """(key, definition text, is_lookup) for every block, lookups included."""
    for block in document.blocks():
        definition = document.equations[block.start : block.definition_end]
        is_lookup = block.expression is None and re.match(
            r'\s*("[^"]*"|[^=\[\(:~"]+?)\s*\(', definition
        ) is not None
        yield block.key, definition, is_lookup


# --------------------------------------------------------------------------- analysis


def analyse(document: MdlDocument, max_loops: int = 200) -> dict:
    graph = CausalGraph(document)
    cycles, truncated = graph.cycles(limit=max_loops)
    loops = []
    for cycle in cycles:
        # Start at a stock when there is one; it reads more naturally.
        first = next((i for i, key in enumerate(cycle) if key in graph.stocks), 0)
        cycle = cycle[first:] + cycle[:first]
        links = []
        for source, target in zip(cycle, cycle[1:] + cycle[:1]):
            links.append({
                "from": graph.names[source],
                "to": graph.names[target],
                "sign": graph.sign(source, target),
            })
        signs = [link["sign"] for link in links]
        if "?" in signs:
            polarity = "unknown"
        else:
            polarity = "balancing" if signs.count("-") % 2 else "reinforcing"
        delayed = any(graph.edges[s][t][1] for s, t in zip(cycle, cycle[1:] + cycle[:1]))
        loops.append({
            "polarity": polarity,
            "length": len(cycle),
            "variables": [graph.names[key] for key in cycle],
            "stocks": [graph.names[key] for key in cycle if key in graph.stocks],
            "links": links,
            "algebraic": not delayed,
            "_keys": cycle,
        })
    prefix = {"reinforcing": "R", "balancing": "B", "unknown": "U"}
    loops.sort(key=lambda loop: ("RBU".index(prefix[loop["polarity"]]), loop["length"], loop["variables"]))
    counters = {"R": 0, "B": 0, "U": 0}
    for loop in loops:
        letter = prefix[loop["polarity"]]
        counters[letter] += 1
        loop["id"] = f"{letter}{counters[letter]}"
        if loop["polarity"] == "unknown":
            loop["undetermined_links"] = [
                f"{link['from']} -> {link['to']}" for link in loop["links"] if link["sign"] == "?"
            ]
    membership: dict[str, int] = {}
    for loop in loops:
        for name in loop["variables"]:
            membership[name] = membership.get(name, 0) + 1
    return {
        "summary": {
            "loops": len(loops),
            "reinforcing": counters["R"],
            "balancing": counters["B"],
            "unknown": counters["U"],
            "truncated": truncated,
        },
        "loops": [{key: value for key, value in loop.items() if key != "_keys"} for loop in loops],
        "most_connected": sorted(membership.items(), key=lambda item: (-item[1], item[0]))[:8],
        "algebraic_loops": graph.algebraic_loops(),
        "unreadable_equations": graph.unreadable,
        "_graph": graph,
        "_cycles": {loop["id"]: loop["_keys"] for loop in loops},
        "assumptions": (
            "Link polarity assumes variables are positive (constants use their actual sign) "
            "and that lookups follow their table's direction. 'unknown' means at least one "
            "link's sign depends on values, such as an IF THEN ELSE condition."
        ),
    }


# --------------------------------------------------------------------------- annotation


LABEL_RE = re.compile(r"^[RBU]\d+$")
LABEL_HALF = (16, 12)


def annotate(document: MdlDocument, max_labels: int = 12) -> None:
    """Write link polarities onto information arrows and place loop labels (R1, B2, ...)."""
    result = analyse(document)
    graph: CausalGraph = result["_graph"]
    cycles: dict[str, list[str]] = result["_cycles"]
    signed = 0
    labelled = 0
    for view in document.views():
        objects = view.by_id()
        edits: dict[int, str | None] = {}
        # Remove labels from an earlier run: comment records whose text is a loop id.
        for record in view.records:
            if record.type == 12 and record.fields[2] == "0":
                following = document.lines[record.line + 1] if record.line + 1 < len(document.lines) else ""
                if LABEL_RE.match(following.strip()):
                    edits[record.line] = None
                    edits[record.line + 1] = None
        for arrow in view.arrows():
            if arrow.is_pipe:
                continue
            source, target = objects.get(arrow.source), objects.get(arrow.target)
            if not source or not target or source.type != 10 or target.type != 10:
                continue
            sign = graph.sign(fold(source.name), fold(target.name))
            code = {"+": "43", "-": "45"}.get(sign or "")
            if code and arrow.fields[6] != code:
                arrow.fields[6] = code
                edits[arrow.line] = arrow.text()
                signed += 1

        occupied = [
            (r.x - r.half[0], r.y - r.half[1], r.x + r.half[0], r.y + r.half[1])
            for r in view.records
            if r.type in (10, 11) or (r.type == 12 and r.line not in edits)
        ]
        # Keep labels off the links too, or a two-variable loop's label sits on its arrow.
        occupied += _line_samples(view, physical=True) + _line_samples(view, physical=False)
        positions = {fold(r.name): (r.x, r.y) for r in view.records if r.type == 10 and r.name}
        new_lines: list[str] = []
        next_id = view.next_id()
        for loop in result["loops"][:max_labels]:
            keys = cycles[loop["id"]]
            points = [positions[key] for key in keys if key in positions]
            if len(points) < max(2, len(keys) - 1):
                continue
            centre = (sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points))
            x, y = _free_spot(centre, [(0, 0, *LABEL_HALF)], occupied, step=10, radius=200)
            occupied.append((x - LABEL_HALF[0], y - LABEL_HALF[1], x + LABEL_HALF[0], y + LABEL_HALF[1]))
            new_lines.append(
                f"12,{next_id},0,{x},{y},{LABEL_HALF[0]},{LABEL_HALF[1]},8,135,0,18,-1,0,0,0,"
                "-1--1--1,0-0-0,||B|0-0-0,0,0,0,0,0,0"
            )
            new_lines.append(loop["id"])
            next_id += 1
            labelled += 1
        document._set_lines(edits, {view.end: new_lines} if new_lines else {})
    document.changes.append(f"set polarity on {signed} arrow(s)")
    document.changes.append(f"placed {labelled} loop label(s)")
    if len(result["loops"]) > max_labels:
        document.warnings.append(
            f"labelled the first {max_labels} of {len(result['loops'])} loops; raise max_labels for more"
        )
    unknown = [loop["id"] for loop in result["loops"] if loop["polarity"] == "unknown"]
    if unknown:
        document.warnings.append(f"polarity could not be determined for {', '.join(unknown)}")


def find_loops(
    model_path, max_loops: int = 200, annotate_diagram: bool = False,
    max_labels: int = 12, output_path=None,
) -> dict:
    """Analyse a model file; optionally write polarities and loop labels onto its diagram."""
    from .editor import _load, edit_model

    model, text, _, _ = _load(model_path)
    result = analyse(MdlDocument(text), max_loops)
    result = {key: value for key, value in result.items() if not key.startswith("_")}
    result["model"] = str(model)
    if annotate_diagram:
        result["annotation"] = edit_model(
            model, annotate, output_path, max_labels=max_labels
        )
    return result
