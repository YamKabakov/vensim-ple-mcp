"""Typed model specification used by the generator and MCP tools."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


CONTROL_NAMES = {"INITIAL TIME", "FINAL TIME", "TIME STEP", "SAVEPER"}


def _required(data: dict[str, Any], key: str, context: str) -> Any:
    value = data.get(key)
    if value is None or value == "":
        raise ValueError(f"{context} requires '{key}'")
    return value


@dataclass(frozen=True)
class Level:
    name: str
    equation: str
    unit: str
    note: str = ""


@dataclass(frozen=True)
class Flow:
    name: str
    equation: str
    unit: str
    source: str | None = None
    target: str | None = None
    note: str = ""


@dataclass(frozen=True)
class Variable:
    name: str
    equation: str
    unit: str
    kind: str = "auxiliary"
    note: str = ""


@dataclass(frozen=True)
class Link:
    source: str
    target: str
    polarity: str = ""


@dataclass(frozen=True)
class Simulation:
    initial_time: float = 0
    final_time: float = 60
    time_step: float = 0.25
    time_unit: str = "Month"


@dataclass(frozen=True)
class Theme:
    font: str = "Arial"
    font_size: int = 12
    background: str = "248-250-252"
    text: str = "30-41-59"
    shape: str = "51-65-85"
    arrow: str = "71-85-105"
    fill: str = "219-234-254"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    title: str
    levels: tuple[Level, ...]
    flows: tuple[Flow, ...]
    variables: tuple[Variable, ...] = field(default_factory=tuple)
    links: tuple[Link, ...] = field(default_factory=tuple)
    simulation: Simulation = field(default_factory=Simulation)
    theme: Theme = field(default_factory=Theme)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelSpec":
        if not isinstance(data, dict):
            raise ValueError("model specification must be a JSON object")

        def parse_level(item: dict[str, Any]) -> Level:
            return Level(
                name=str(_required(item, "name", "level")),
                equation=str(_required(item, "equation", "level")),
                unit=str(_required(item, "unit", "level")),
                note=str(item.get("note", "")),
            )

        def parse_flow(item: dict[str, Any]) -> Flow:
            source = item.get("source")
            target = item.get("target")
            if not source and not target:
                raise ValueError(f"flow '{item.get('name', '?')}' needs a source or target")
            return Flow(
                name=str(_required(item, "name", "flow")),
                equation=str(_required(item, "equation", "flow")),
                unit=str(_required(item, "unit", "flow")),
                source=str(source) if source else None,
                target=str(target) if target else None,
                note=str(item.get("note", "")),
            )

        def parse_variable(item: dict[str, Any]) -> Variable:
            kind = str(item.get("kind", "auxiliary"))
            if kind not in {"auxiliary", "constant"}:
                raise ValueError(f"unsupported variable kind: {kind}")
            return Variable(
                name=str(_required(item, "name", kind)),
                equation=str(_required(item, "equation", kind)),
                unit=str(_required(item, "unit", kind)),
                kind=kind,
                note=str(item.get("note", "")),
            )

        def parse_link(item: dict[str, Any]) -> Link:
            polarity = str(item.get("polarity", ""))
            if polarity not in {"", "+", "-", "s", "o"}:
                raise ValueError(f"unsupported link polarity: {polarity}")
            return Link(
                source=str(_required(item, "source", "link")),
                target=str(_required(item, "target", "link")),
                polarity=polarity,
            )

        sim_data = data.get("simulation", {})
        simulation = Simulation(
            initial_time=float(sim_data.get("initial_time", 0)),
            final_time=float(sim_data.get("final_time", 60)),
            time_step=float(sim_data.get("time_step", 0.25)),
            time_unit=str(sim_data.get("time_unit", "Month")),
        )
        if simulation.final_time <= simulation.initial_time:
            raise ValueError("final_time must be greater than initial_time")
        if simulation.time_step <= 0:
            raise ValueError("time_step must be positive")

        theme_data = data.get("theme", {})
        theme = Theme(**{key: value for key, value in theme_data.items() if key in Theme.__annotations__})

        spec = cls(
            name=str(_required(data, "name", "model")),
            title=str(data.get("title") or data["name"]),
            levels=tuple(parse_level(item) for item in data.get("levels", [])),
            flows=tuple(parse_flow(item) for item in data.get("flows", [])),
            variables=tuple(parse_variable(item) for item in data.get("variables", [])),
            links=tuple(parse_link(item) for item in data.get("links", [])),
            simulation=simulation,
            theme=theme,
        )
        spec.validate_names()
        return spec

    @classmethod
    def from_json(cls, source: str | Path | dict[str, Any]) -> "ModelSpec":
        if isinstance(source, dict):
            return cls.from_dict(source)
        if isinstance(source, Path):
            return cls.from_dict(json.loads(source.read_text(encoding="utf-8")))
        stripped = source.lstrip()
        if stripped.startswith("{"):
            return cls.from_dict(json.loads(source))
        candidate = Path(source).expanduser()
        if candidate.exists():
            return cls.from_json(candidate)
        raise FileNotFoundError(candidate)

    def validate_names(self) -> None:
        names = [item.name for item in (*self.levels, *self.flows, *self.variables)]
        folded: dict[str, str] = {}
        for name in names:
            if any(char in name for char in "~,|="):
                raise ValueError(f"invalid Vensim variable name: {name!r}")
            key = name.replace("_", " ").strip().casefold()
            if key in folded:
                raise ValueError(f"duplicate variable name: {folded[key]!r} and {name!r}")
            if name.upper() in CONTROL_NAMES:
                raise ValueError(f"reserved control variable name: {name!r}")
            folded[key] = name

        known = set(names)
        for flow in self.flows:
            for endpoint in (flow.source, flow.target):
                if endpoint and endpoint not in known:
                    raise ValueError(f"flow '{flow.name}' references unknown level '{endpoint}'")
                if endpoint and endpoint not in {level.name for level in self.levels}:
                    raise ValueError(f"flow '{flow.name}' endpoint '{endpoint}' is not a level")
        for link in self.links:
            if link.source not in known or link.target not in known:
                raise ValueError(f"link references unknown variable: {link.source} -> {link.target}")
