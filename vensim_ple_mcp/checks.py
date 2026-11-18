"""Equation-level checks: undefined names, arrows vs equations, stocks vs flows, and units.

These catch the common mistakes before the model is opened in Vensim. They are
conservative: when something cannot be decided statically (an unfamiliar function, a
variable without units) the check stays silent rather than guess. Vensim's Check Model
and Units Check remain the authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
import re

from .editor import Block, MdlDocument, _split_integ, _terms, fold
from .model import CONTROL_NAMES


@dataclass
class Issue:
    severity: str  # "error" or "warning"
    check: str  # undefined, function, arrow, stock-flow, units, unused
    variable: str
    message: str


# Vensim functions, folded. Anything else called like a function must be a lookup.
FUNCTIONS = {
    fold(name)
    for name in """
    ABS ACTIVE_INITIAL ALLOCATE_AVAILABLE ALLOCATE_BY_PRIORITY ARCCOS ARCSIN ARCTAN COS COSH
    DELAY1 DELAY1I DELAY3 DELAY3I DELAY_CONVEYOR DELAY_FIXED DELAY_INFORMATION DELAY_MATERIAL
    DELAY_N ELMCOUNT EXP FIND_ZERO FORECAST GAME GAMMA_LN GET_123_DATA GET_DATA_AT_TIME
    GET_DATA_BETWEEN_TIMES GET_DATA_MAX GET_DATA_MEAN GET_DATA_MIN GET_DIRECT_CONSTANTS
    GET_DIRECT_DATA GET_DIRECT_LOOKUPS GET_DIRECT_SUBSCRIPT GET_TIME_VALUE GET_VDF_DATA
    GET_XLS_CONSTANTS GET_XLS_DATA GET_XLS_LOOKUPS IF_THEN_ELSE INITIAL INTEG INTEGER
    INVERT_MATRIX LN LOG LOOKUP_AREA LOOKUP_BACKWARD LOOKUP_EXTRAPOLATE LOOKUP_FORWARD
    LOOKUP_INVERT LOOKUP_SLOPE MAX MIN MODULO NPV NPVE PI POWER PROD PULSE PULSE_TRAIN QUANTUM
    QUEUE_ATTRIB QUEUE_FIFO RAMP RAMP_FROM_TO RANDOM_BETA RANDOM_BINOMIAL RANDOM_EXPONENTIAL
    RANDOM_GAMMA RANDOM_LOGNORMAL RANDOM_NEGATIVE_BINOMIAL RANDOM_NORMAL RANDOM_POISSON
    RANDOM_TRIANGULAR RANDOM_UNIFORM RANDOM_WEIBULL SAMPLE_IF_TRUE SHIFT_IF_TRUE SIGN SIN SINH
    SMOOTH SMOOTH3 SMOOTH3I SMOOTHI SMOOTH_N SQRT STEP SUM TAN TANH TIME_BASE TREND VECTOR_ELM_MAP
    VECTOR_LOOKUP VECTOR_RANK VECTOR_REORDER VECTOR_SELECT VECTOR_SORT_ORDER VMAX VMIN
    WITH_LOOKUP XIDZ ZIDZ
    """.split()
}
BUILTINS = {"time", "pi"}

TOKEN_RE = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<quoted>"[^"]*")
  | (?P<number>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<logical>:(?:AND|OR|NOT|NA):)
  | (?P<name>[^\W\d][\w$'&]*(?:[ _]+[\w$'&]+)*)
  | (?P<op><=|>=|<>|[-+*/^=<>(),\[\]])
    """,
    re.X,
)


# --------------------------------------------------------------------------- units


Unit = dict  # base unit -> Fraction exponent; {} is dimensionless
WILD = "wild"  # a bare number: fits whatever it is combined with
TIME_WORDS = {"second", "minute", "hour", "day", "week", "month", "quarter", "year"}


def _base(name: str) -> str:
    word = " ".join(name.split()).casefold()
    if word.endswith("s") and word[:-1] in TIME_WORDS:
        word = word[:-1]
    return word


def parse_unit(text: str) -> Unit | None:
    """Parse a Vensim unit string such as 'Person/Year' or '1/(Month*Month)'."""
    text = re.sub(r"\[.*$", "", text or "").strip()
    if not text or "?" in text:
        return None
    tokens = re.findall(r"[*/^()]|-?\d+(?:\.\d+)?|[^*/^()\s][^*/^()]*", text)
    tokens = [token.strip() for token in tokens if token.strip()]
    position = 0

    def factor() -> Unit | None:
        nonlocal position
        if position >= len(tokens):
            return None
        token = tokens[position]
        position += 1
        if token == "(":
            inner = product()
            if position >= len(tokens) or tokens[position] != ")":
                return None
            position += 1
            result = inner
        elif re.fullmatch(r"-?\d+(?:\.\d+)?", token):
            result = {}
        elif token.casefold() in {"dmnl", "dimensionless", "fraction"}:
            result = {}
        else:
            result = {_base(token): Fraction(1)}
        if result is not None and position < len(tokens) and tokens[position] == "^":
            position += 1
            try:
                power = Fraction(tokens[position])
            except (IndexError, ValueError):
                return None
            position += 1
            result = _power(result, power)
        return result

    def product() -> Unit | None:
        nonlocal position
        result = factor()
        while result is not None and position < len(tokens) and tokens[position] in "*/":
            operator = tokens[position]
            position += 1
            right = factor()
            if right is None:
                return None
            result = _multiply(result, right if operator == "*" else _power(right, Fraction(-1)))
        return result

    unit = product()
    return unit if position == len(tokens) else None


def _multiply(left: Unit, right: Unit) -> Unit:
    result = dict(left)
    for base, power in right.items():
        result[base] = result.get(base, Fraction(0)) + power
        if result[base] == 0:
            del result[base]
    return result


def _power(unit: Unit, power: Fraction) -> Unit:
    return {base: exponent * power for base, exponent in unit.items()}


def format_unit(unit: Unit) -> str:
    if not unit:
        return "Dmnl"

    def term(base: str, power: Fraction) -> str:
        return base if power == 1 else f"{base}^{power}"

    top = [term(b, p) for b, p in sorted(unit.items()) if p > 0]
    bottom = [term(b, -p) for b, p in sorted(unit.items()) if p < 0]
    text = "*".join(top) or "1"
    if bottom:
        text += "/" + (bottom[0] if len(bottom) == 1 else f"({'*'.join(bottom)})")
    return text


# --------------------------------------------------------------------------- parsing


class _Expression:
    """Recursive-descent walk over one equation that resolves names and infers units."""

    def __init__(self, text: str, model: "_Model", variable: str, issues: list[Issue]):
        self.tokens = []
        for match in TOKEN_RE.finditer(text):
            kind = match.lastgroup
            if kind != "space":
                self.tokens.append((kind, match.group()))
        self.position = 0
        self.model = model
        self.variable = variable
        self.issues = issues
        self.units_ok = True  # false once any part can't be inferred

    # -- helpers

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        index = self.position + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.tokens[self.position]
        self.position += 1
        return token

    def expect(self, value: str) -> None:
        if self.peek() and self.peek()[1] == value:
            self.position += 1
        else:
            raise SyntaxError(value)

    def unit_issue(self, message: str) -> None:
        self.issues.append(Issue("warning", "units", self.variable, f"{self.variable}: {message}"))

    # -- grammar: comparison < additive < multiplicative < power < unary < primary

    def parse(self):
        result = self.logical()
        if self.position != len(self.tokens):
            raise SyntaxError("trailing tokens")
        return result

    def logical(self):
        left = self.comparison()
        while self.peek() and self.peek()[0] == "logical" and self.peek()[1] != ":NOT:":
            self.take()
            self.comparison()
            left = {}
        return left

    def comparison(self):
        left = self.additive()
        while self.peek() and self.peek()[1] in ("=", "<", ">", "<=", ">=", "<>"):
            operator = self.take()[1]
            right = self.additive()
            self.same(left, right, f"compares quantities in different units with '{operator}'")
            left = {}
        return left

    def additive(self):
        left = self.multiplicative()
        while self.peek() and self.peek()[1] in ("+", "-"):
            operator = self.take()[1]
            right = self.multiplicative()
            left = self.same(left, right, f"{'adds' if operator == '+' else 'subtracts'} quantities in different units")
        return left

    def same(self, left, right, message: str):
        if left is None or right is None:
            return None
        if left == WILD:
            return right
        if right == WILD:
            return left
        if left != right:
            self.unit_issue(f"{message}: {format_unit(left)} and {format_unit(right)}")
            return None
        return left

    def multiplicative(self):
        left = self.power()
        while self.peek() and self.peek()[1] in ("*", "/"):
            operator = self.take()[1]
            right = self.power()
            if left is None or right is None:
                left = None
            elif left == WILD and right == WILD:
                left = WILD
            else:
                l = {} if left == WILD else left
                r = {} if right == WILD else right
                left = _multiply(l, r if operator == "*" else _power(r, Fraction(-1)))
        return left

    def power(self):
        base = self.unary()
        if self.peek() and self.peek()[1] == "^":
            self.take()
            exponent_token = self.peek()
            exponent = self.unary()
            if base in (WILD, None) or base == {}:
                return base if base != {} else {}
            if exponent_token and exponent_token[0] == "number" and exponent == WILD:
                return _power(base, Fraction(exponent_token[1]).limit_denominator(100))
            return None
        return base

    def unary(self):
        if self.peek() and (self.peek()[1] in ("-", "+") or self.peek()[1] == ":NOT:"):
            operator = self.take()[1]
            value = self.unary()
            return {} if operator == ":NOT:" else value
        return self.primary()

    def primary(self):
        token = self.peek()
        if token is None:
            raise SyntaxError("unexpected end")
        kind, value = self.take()
        if kind == "number":
            return WILD
        if kind == "logical":  # :NA:
            return WILD
        if value == "(":
            inner = self.logical()
            if self.peek() and self.peek()[1] == ",":
                # A literal lookup table such as ((0,0),(1,1)) or [(0,0)-(1,1)].
                while self.peek() and self.peek()[1] != ")":
                    self.take()
                inner = None
            self.expect(")")
            return inner
        if value == "[":
            depth = 1
            while depth and self.peek():
                depth += {"[": 1, "]": -1}.get(self.take()[1], 0)
            return None
        if kind in ("name", "quoted"):
            return self.name(value, quoted=kind == "quoted")
        raise SyntaxError(value)

    def arguments(self) -> list:
        self.expect("(")
        args = []
        if self.peek() and self.peek()[1] == ")":
            self.take()
            return args
        while True:
            args.append(self.logical())
            if self.peek() and self.peek()[1] == ",":
                self.take()
                continue
            self.expect(")")
            return args

    def name(self, written: str, quoted: bool):
        key = fold(written)
        # Subscripts: Name[sub] -- skip the subscript list.
        if self.peek() and self.peek()[1] == "[":
            self.primary()
        called = self.peek() is not None and self.peek()[1] == "("
        block = self.model.by_key.get(key)
        if block is not None:
            self.model.used.add(key)
            if called:  # a lookup call
                self.arguments()
            return self.model.unit(block)
        if called and not quoted:
            if key not in FUNCTIONS:
                self.issues.append(
                    Issue("error", "function", self.variable,
                          f"{self.variable} calls {written}, which is neither a Vensim function nor a lookup in this model")
                )
                self.arguments()
                return None
            return self.function(key, self.arguments())
        if key in BUILTINS:
            return self.model.time_unit if key == "time" else {}
        self.issues.append(
            Issue("error", "undefined", self.variable,
                  f"{self.variable} uses {written}, which is not defined in this model")
        )
        return None

    def function(self, key: str, args: list):
        time = self.model.time_unit
        first = args[0] if args else None
        if key in {"abs", "integer", "initial", "quantum", "sign"}:
            return {} if key == "sign" else first
        if key in {"max", "min", "modulo"} and len(args) == 2:
            return self.same(args[0], args[1], f"{key.upper()} of quantities in different units")
        if key == "if then else" and len(args) == 3:
            return self.same(args[1], args[2], "IF THEN ELSE branches have different units")
        if key == "active initial" and len(args) == 2:
            return self.same(args[0], args[1], "ACTIVE INITIAL arguments have different units")
        if key in {"smooth", "smoothi", "smooth3", "smooth3i", "delay1", "delay1i", "delay3",
                   "delay3i", "delay fixed", "delay n", "smooth n", "delay information",
                   "delay material", "trend", "forecast"} and len(args) >= 2:
            delay = args[1]
            if time is not None and delay not in (None, WILD) and delay != time:
                self.unit_issue(
                    f"{key.upper()} delay time is in {format_unit(delay)}, expected {format_unit(time)}"
                )
            if key == "trend":
                return _power(time, Fraction(-1)) if time is not None else None
            return first
        if key in {"xidz", "zidz"} and len(args) >= 2:
            if args[0] is None or args[1] is None:
                return None
            if args[0] == WILD and args[1] == WILD:
                return WILD
            a = {} if args[0] == WILD else args[0]
            b = {} if args[1] == WILD else args[1]
            return _multiply(a, _power(b, Fraction(-1)))
        if key == "sqrt" and first not in (None, WILD):
            return _power(first, Fraction(1, 2))
        if key in {"exp", "ln", "log", "sin", "cos", "tan", "arcsin", "arccos", "arctan",
                   "sinh", "cosh", "tanh", "pulse", "pulse train", "random uniform",
                   "random normal"}:
            return {} if key not in ("random uniform", "random normal") else None
        if key == "step" and args:
            return first
        if key == "ramp" and args and first not in (None, WILD) and time is not None:
            return _multiply(first, time)
        return None


class _Model:
    def __init__(self, document: MdlDocument):
        self.document = document
        self.blocks = document.blocks()
        self.by_key = {block.key: block for block in self.blocks}
        self.used: set[str] = set()
        time_block = self.by_key.get("time step") or self.by_key.get("initial time")
        self.time_unit = parse_unit(time_block.unit) if time_block else None

    def unit(self, block: Block):
        return parse_unit(block.unit)


# --------------------------------------------------------------------------- checks


def _check_expressions(model: _Model, issues: list[Issue]) -> None:
    for block in model.blocks:
        if block.expression is None:
            # Lookups and data still count as used names, but have no expression to read.
            continue
        declared = parse_unit(block.unit)
        integ = _split_integ(block.expression)
        parts = [("net flow", integ[0]), ("initial value", integ[1])] if integ else [("", block.expression)]
        for label, text in parts:
            walker = _Expression(text, model, block.name, issues)
            try:
                inferred = walker.parse()
            except SyntaxError:
                issues.append(Issue("warning", "syntax", block.name,
                                    f"could not read the equation of {block.name}; check it in Vensim"))
                continue
            if declared is None or inferred in (None, WILD):
                continue
            if integ and label == "net flow":
                if model.time_unit is None:
                    continue
                expected = _multiply(declared, _power(model.time_unit, Fraction(-1)))
            else:
                expected = declared
            if inferred != expected:
                where = f" {label}" if label else ""
                issues.append(
                    Issue("warning", "units", block.name,
                          f"{block.name}{where} works out to {format_unit(inferred)}, "
                          f"but {format_unit(expected)} is expected")
                )


def _check_unused(model: _Model, issues: list[Issue]) -> None:
    for block in model.blocks:
        if block.key.upper() in CONTROL_NAMES or block.key in model.used:
            continue
        if block.expression and re.fullmatch(r"-?[\d.]+(?:[eE][+-]?\d+)?", block.expression):
            issues.append(Issue("warning", "unused", block.name, f"constant {block.name} is not used by any equation"))


def _check_diagram(model: _Model, issues: list[Issue]) -> None:
    document = model.document
    references = {block.key: {fold(r) for r in document.references(block)} for block in model.blocks if block.expression}
    for view in document.views():
        objects = view.by_id()
        valves = view.valve_of()
        flow_of_valve = {valve: label for label, valve in valves.items()}
        named = {record.id: fold(record.name) for record in view.records if record.type == 10 and record.name}
        on_view = set(named.values())

        # Pipes: which flows fill and drain each stock.
        fills: dict[int, set[str]] = {}
        drains: dict[int, set[str]] = {}
        for arrow in view.arrows():
            if not arrow.is_pipe or arrow.source not in flow_of_valve:
                continue
            flow = named.get(flow_of_valve[arrow.source])
            stock = objects.get(arrow.target)
            if flow is None or stock is None or stock.type != 10:
                continue
            (fills if arrow.fields[4] == "4" else drains).setdefault(stock.id, set()).add(flow)

        linked: set[tuple[str, str]] = set()
        for arrow in view.arrows():
            if arrow.is_pipe:
                continue
            source, target = named.get(arrow.source), named.get(arrow.target)
            if source is None or target is None:
                continue
            linked.add((source, target))
            if target in references and source not in references[target] and target != source:
                issues.append(
                    Issue("warning", "arrow", model.by_key[target].name,
                          f"arrow from {objects[arrow.source].name} to {objects[arrow.target].name}, "
                          f"but {objects[arrow.target].name}'s equation does not use it")
                )

        for record in view.records:
            if record.type != 10 or not record.name:
                continue
            key = fold(record.name)
            block = model.by_key.get(key)
            if block is None or block.expression is None:
                continue
            piped = fills.get(record.id, set()) | drains.get(record.id, set())
            for ref in sorted(references.get(key, ())):
                if ref in piped or ref.upper() in CONTROL_NAMES or ref not in on_view:
                    continue
                if (ref, key) not in linked:
                    issues.append(
                        Issue("warning", "arrow", block.name,
                              f"{block.name} uses {model.by_key[ref].name}, but no arrow connects them on view {view.name!r}")
                    )
            if not block.is_level:
                continue
            integ = _split_integ(block.expression)
            if integ is None:
                continue
            signs: dict[str, str] = {}
            for sign, term in _terms(integ[0]):
                signs[fold(term)] = sign
            for flow in sorted(fills.get(record.id, set())):
                name = model.by_key[flow].name if flow in model.by_key else flow
                if flow not in signs:
                    issues.append(Issue("warning", "stock-flow", block.name,
                                        f"{name} flows into {block.name} on the diagram but is not in its net flow"))
                elif signs[flow] != "+":
                    issues.append(Issue("warning", "stock-flow", block.name,
                                        f"{name} flows into {block.name} but is subtracted in its net flow"))
            for flow in sorted(drains.get(record.id, set())):
                name = model.by_key[flow].name if flow in model.by_key else flow
                if flow not in signs:
                    issues.append(Issue("warning", "stock-flow", block.name,
                                        f"{name} drains {block.name} on the diagram but is not in its net flow"))
                elif signs[flow] != "-":
                    issues.append(Issue("warning", "stock-flow", block.name,
                                        f"{name} drains {block.name} but is added in its net flow"))
            for term in signs:
                if term in model.by_key and term in on_view and term not in piped:
                    issues.append(Issue("warning", "stock-flow", block.name,
                                        f"{block.name}'s net flow uses {model.by_key[term].name}, which has no pipe to it"))


def _check_algebraic_loops(model: _Model, issues: list[Issue]) -> None:
    from .loops import CausalGraph

    graph = CausalGraph(model.document)
    for group in graph.initialization_loops():
        issues.append(
            Issue("error", "initialization-loop", group[0],
                  f"{', '.join(group)} need each other's values at the initial time "
                  "(circular initialization). A SMOOTH or DELAY starts equal to its input; "
                  "use SMOOTHI or DELAY1I with an explicit initial value to break the cycle")
        )
    for group in graph.algebraic_loops():
        issues.append(
            Issue("error", "algebraic-loop", group[0],
                  f"{', '.join(group)} depend on each other with no stock or delay between "
                  "them (simultaneous equations); Vensim will not simulate this")
        )


def check_equations(text: str) -> list[dict]:
    """All equation-level issues in a model's text, errors first."""
    model = _Model(MdlDocument(text))
    issues: list[Issue] = []
    _check_expressions(model, issues)
    _check_unused(model, issues)
    _check_diagram(model, issues)
    _check_algebraic_loops(model, issues)
    unique = list({(i.severity, i.check, i.variable, i.message): i for i in issues}.values())
    unique.sort(key=lambda issue: issue.severity != "error")
    return [asdict(issue) for issue in unique]
