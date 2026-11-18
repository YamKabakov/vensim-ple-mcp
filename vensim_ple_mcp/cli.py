"""Command-line entry point for generation, QA, preview, and native handoff."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__
from .editor import edit_model
from .generator import generate_model
from .native import doctor, generate_dss_command, open_in_vensim, simulate_in_vensim
from .parser import inspect_model, validate_model
from .plot import plot_results
from .renderer import render_svg
from .simulate import list_parameters, run_model, sweep_parameter


def _dump(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _overrides(items: list[str]) -> dict[str, float]:
    overrides = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"--set expects NAME=VALUE, got {item!r}")
        overrides[name.strip()] = float(value)
    return overrides


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vensim-ple-mcp",
        description="Generate, validate, and simulate Vensim .mdl models.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="detect local Vensim and automation capabilities")

    generate = commands.add_parser("generate", help="generate .mdl from a JSON specification")
    generate.add_argument("spec", type=Path)
    generate.add_argument("--output", "-o", type=Path, required=True)

    validate = commands.add_parser("validate", help="audit a text .mdl model")
    validate.add_argument("model", type=Path)

    inspect = commands.add_parser("inspect", help="dump equations and sketch records")
    inspect.add_argument("model", type=Path)

    render = commands.add_parser("render", help="render an SVG preview")
    render.add_argument("model", type=Path)
    render.add_argument("--output", "-o", type=Path, required=True)

    open_command = commands.add_parser("open", help="open a model in local Vensim")
    open_command.add_argument("model", type=Path)

    dss = commands.add_parser("dss-script", help="create a DSS-only .cmd batch script")
    dss.add_argument("model", type=Path)
    dss.add_argument("--output", "-o", type=Path, required=True)
    dss.add_argument("--run-name", default="current")

    params = commands.add_parser("params", help="list constants and variables in a model")
    params.add_argument("model", type=Path)

    run = commands.add_parser("run", help="simulate a model with PySD")
    run.add_argument("model", type=Path)
    run.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                     help="override a constant for this run (repeatable)")
    run.add_argument("--var", action="append", default=None, help="variable to report (repeatable)")
    run.add_argument("--final-time", type=float)
    run.add_argument("--csv", type=Path, help="write full-resolution results to CSV")

    sweep = commands.add_parser("sweep", help="run once per value of one constant")
    sweep.add_argument("model", type=Path)
    sweep.add_argument("parameter")
    sweep.add_argument("values", type=float, nargs="+")
    sweep.add_argument("--var", action="append", default=None)

    vensim_run = commands.add_parser(
        "vensim-run", help="simulate inside the installed Vensim (loads a run for its graphs)"
    )
    vensim_run.add_argument("model", type=Path)
    vensim_run.add_argument("--run-name", default="current")
    vensim_run.add_argument("--no-overwrite", action="store_true")

    plot = commands.add_parser("plot", help="simulate with PySD and save an SVG chart")
    plot.add_argument("model", type=Path)
    plot.add_argument("--output", "-o", type=Path, required=True)
    plot.add_argument("--var", action="append", default=None)
    plot.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    plot.add_argument("--final-time", type=float)
    plot.add_argument("--title")

    set_eq = commands.add_parser("set", help="change a variable's equation, unit or note")
    set_eq.add_argument("model", type=Path)
    set_eq.add_argument("variable")
    set_eq.add_argument("equation", nargs="?")
    set_eq.add_argument("--unit")
    set_eq.add_argument("--note")
    set_eq.add_argument("--no-sync-links", action="store_true")

    add = commands.add_parser("add", help="add a variable and draw it on the diagram")
    add.add_argument("model", type=Path)
    add.add_argument("name")
    add.add_argument("equation")
    add.add_argument("--unit", required=True)
    add.add_argument("--kind", default="auxiliary",
                     choices=["auxiliary", "constant", "level", "stock", "flow"])
    add.add_argument("--note", default="")
    add.add_argument("--source", help="stock a flow drains")
    add.add_argument("--target", help="stock a flow fills")
    add.add_argument("--near", help="place next to this variable")
    add.add_argument("--at", nargs=2, type=float, metavar=("X", "Y"))
    add.add_argument("--view")

    rename = commands.add_parser("rename", help="rename a variable everywhere")
    rename.add_argument("model", type=Path)
    rename.add_argument("old")
    rename.add_argument("new")

    delete = commands.add_parser("delete", help="delete a variable and its diagram objects")
    delete.add_argument("model", type=Path)
    delete.add_argument("name")
    delete.add_argument("--force", action="store_true")

    for edit in (set_eq, add, rename, delete):
        edit.add_argument("--output", "-o", type=Path, help="write here instead of in place")

    commands.add_parser("serve", help="run the MCP server over stdio")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        _dump(doctor())
        return 0
    if args.command == "generate":
        output = generate_model(args.spec, args.output)
        _dump({"model": str(output), "validation": validate_model(output)})
        return 0
    if args.command == "validate":
        result = validate_model(args.model)
        _dump(result)
        return 0 if result["ok"] else 1
    if args.command == "inspect":
        _dump(inspect_model(args.model))
        return 0
    if args.command == "render":
        output = render_svg(args.model, args.output)
        _dump({"preview": str(output)})
        return 0
    if args.command == "open":
        _dump(open_in_vensim(args.model))
        return 0
    if args.command == "dss-script":
        output = generate_dss_command(args.model, args.output, args.run_name)
        _dump({"command_file": str(output), "requires": "Vensim DSS"})
        return 0
    if args.command == "params":
        _dump(list_parameters(args.model))
        return 0
    if args.command == "run":
        overrides = _overrides(args.set)
        _dump(run_model(args.model, overrides, args.var, args.final_time, csv_path=args.csv))
        return 0
    if args.command == "vensim-run":
        _dump(simulate_in_vensim(args.model, args.run_name, overwrite=not args.no_overwrite))
        return 0
    if args.command == "plot":
        overrides = _overrides(args.set)
        _dump(plot_results(args.model, args.output, args.var, overrides, args.final_time,
                           title=args.title))
        return 0
    if args.command == "sweep":
        _dump(sweep_parameter(args.model, args.parameter, args.values, args.var))
        return 0
    if args.command in ("set", "add", "rename", "delete"):
        if args.command == "set":
            result = edit_model(args.model, "set_equation", args.output, name=args.variable,
                                equation=args.equation, unit=args.unit, note=args.note,
                                sync_links=not args.no_sync_links)
        elif args.command == "add":
            x, y = args.at if args.at else (None, None)
            result = edit_model(args.model, "add_variable", args.output, name=args.name,
                                equation=args.equation, unit=args.unit, kind=args.kind,
                                note=args.note, source=args.source, target=args.target,
                                near=args.near, x=x, y=y, view_name=args.view)
        elif args.command == "rename":
            result = edit_model(args.model, "rename_variable", args.output, old=args.old,
                                new=args.new)
        else:
            result = edit_model(args.model, "delete_variable", args.output, name=args.name,
                                force=args.force)
        _dump(result)
        return 0 if result["ok"] else 1
    if args.command == "serve":
        from .server import main as serve

        serve()
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
