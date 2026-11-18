"""Model Context Protocol server exposing Vensim generation and native handoff tools."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .editor import edit_model
from .generator import generate_model
from .native import doctor, generate_dss_command, open_in_vensim, simulate_in_vensim
from .parser import inspect_model, validate_model
from .plot import plot_results
from .renderer import render_svg
from .simulate import list_parameters, run_model, sweep_parameter


mcp = FastMCP(
    "Vensim PLE MCP",
    instructions=(
        "Generate, inspect, and simulate Vensim .mdl models. Simulation runs through PySD, so it "
        "works with Vensim PLE, which has no scripting interface. Call list_model_parameters "
        "before run_model or sweep_parameter to get exact variable names. Static validation is "
        "not a replacement for Vensim's Model Check or Units Check: open the model in PLE for "
        "the authoritative check. When the user wants a run inside Vensim itself (for example "
        "so Vensim's Graph tool has a loaded run), call simulate_in_vensim. For a chart that "
        "works without Vensim, call plot_vensim_results. To change an existing model, use "
        "set_vensim_equation, add_vensim_variable, rename_vensim_variable and "
        "delete_vensim_variable: they keep the diagram and update arrows, pipes and stock "
        "equations to match. Each keeps a backup and reports what changed."
    ),
    json_response=True,
)


@mcp.tool()
def vensim_doctor() -> dict:
    """Detect local Vensim and report the exact native automation boundary."""
    return doctor()


@mcp.tool()
def generate_vensim_model(spec_json: str, output_path: str) -> dict:
    """Generate a styled, runnable .mdl file from a JSON model specification."""
    output = generate_model(spec_json, output_path)
    return {"model": str(output), "validation": validate_model(output)}


@mcp.tool()
def inspect_vensim_model(model_path: str) -> dict:
    """Read equations, sketch objects, arrows, and views from a text .mdl model."""
    return inspect_model(model_path)


@mcp.tool()
def validate_vensim_model(model_path: str) -> dict:
    """Audit a model before opening it in Vensim.

    Errors (the model won't simulate): undefined variables, unknown functions, duplicate or
    missing equations. Warnings: unit mismatches, arrows that don't match equations, flows
    missing from or wrongly signed in a stock's INTEG, unused constants, and diagram
    overlaps or crossings. `issues` lists the equation findings with the variable each is
    about, so they can be fixed with set_vensim_equation.
    """
    return validate_model(model_path)


@mcp.tool()
def render_vensim_preview(model_path: str, output_path: str) -> dict:
    """Render an SVG preview from the model's actual Vensim sketch records."""
    output = render_svg(model_path, output_path)
    return {"preview": str(output), "source_model": model_path}


@mcp.tool()
def open_vensim_model(model_path: str) -> dict:
    """Open a .mdl in the locally installed Vensim application."""
    return open_in_vensim(model_path)


@mcp.tool(name="simulate_in_vensim")
def simulate_in_vensim_tool(
    model_path: str, run_name: str = "current", overwrite: bool = True, timeout: int = 120
) -> dict:
    """Simulate inside the installed Vensim so its Graph and Table tools have a loaded run.

    Saves <run_name>.vdfx next to the model. On macOS (PLE, PLE Plus, Professional) this
    drives Vensim's Simulation Control dialog and needs a one-time Accessibility permission
    for the app running this server; the error explains how to grant it. Vensim DSS uses a
    command script. Vensim must not be in use by the person while the run starts.
    """
    return simulate_in_vensim(model_path, run_name, overwrite, timeout)


@mcp.tool()
def plot_vensim_results(
    model_path: str,
    output_path: str,
    variables: list[str] | None = None,
    overrides: dict[str, float] | None = None,
    final_time: float | None = None,
    time_step: float | None = None,
    title: str | None = None,
) -> dict:
    """Simulate with PySD and save an SVG line chart; works without Vensim installed.

    By default the model's stocks are drawn. Variables with different units get separate
    stacked panels. overrides apply to this run only.
    """
    return plot_results(
        model_path, output_path, variables, overrides, final_time, time_step, title
    )


@mcp.tool()
def create_vensim_dss_script(
    model_path: str, output_path: str, run_name: str = "current"
) -> dict:
    """Create a Vensim DSS .cmd batch script; PLE does not support this official path."""
    output = generate_dss_command(model_path, output_path, run_name)
    return {
        "command_file": str(output),
        "license_boundary": "Run this only with Vensim DSS; PLE command-file batch mode is unsupported.",
    }


@mcp.tool()
def list_model_parameters(model_path: str) -> dict:
    """List a model's constants (with current values) and other variables, plus time settings.

    Use the exact names returned here for run_model overrides and variables.
    """
    return list_parameters(model_path)


@mcp.tool()
def run_vensim_model(
    model_path: str,
    overrides: dict[str, float] | None = None,
    variables: list[str] | None = None,
    final_time: float | None = None,
    time_step: float | None = None,
    max_points: int = 25,
    csv_path: str | None = None,
) -> dict:
    """Simulate a .mdl with PySD and return per-variable summaries and a downsampled series.

    overrides sets constants for this run only (the .mdl file is not changed). variables
    limits the output; by default stocks, flows and auxiliaries are returned. csv_path saves
    full-resolution results.
    """
    return run_model(model_path, overrides, variables, final_time, time_step, max_points, csv_path)


@mcp.tool()
def sweep_model_parameter(
    model_path: str,
    parameter: str,
    values: list[float],
    variables: list[str] | None = None,
    base_overrides: dict[str, float] | None = None,
    final_time: float | None = None,
) -> dict:
    """Run the model once per value of one constant and compare outcome summaries."""
    return sweep_parameter(model_path, parameter, values, variables, base_overrides, final_time)


@mcp.tool()
def set_vensim_equation(
    model_path: str,
    variable: str,
    equation: str | None = None,
    unit: str | None = None,
    note: str | None = None,
    sync_links: bool = True,
    output_path: str | None = None,
) -> dict:
    """Change a variable's equation, unit or note in an existing .mdl, keeping the diagram.

    Leave a field as null to keep it. With sync_links, information arrows into the
    variable are added for newly used variables and removed for ones no longer used.
    Writes the model in place (a backup is kept) unless output_path is given. If the model
    is open in Vensim, reopen it afterwards without saving.
    """
    return edit_model(
        model_path, "set_equation", output_path,
        name=variable, equation=equation, unit=unit, note=note, sync_links=sync_links,
    )


@mcp.tool()
def add_vensim_variable(
    model_path: str,
    name: str,
    equation: str,
    unit: str,
    kind: str = "auxiliary",
    note: str = "",
    source: str | None = None,
    target: str | None = None,
    near: str | None = None,
    x: float | None = None,
    y: float | None = None,
    view: str | None = None,
    output_path: str | None = None,
) -> dict:
    """Add a variable to an existing .mdl and draw it on the diagram with its arrows.

    kind is auxiliary, constant, level (stock) or flow. A stock's equation must be
    INTEG(net flow, initial value). A flow needs a source and/or target stock (a missing
    end becomes a cloud); the stocks' INTEG equations are updated to include it, and its
    valve, pipes and clouds are drawn. Placement is automatic, next to the variables it
    uses or `near` another variable; pass x and y to place it exactly. Writes in place (a
    backup is kept) unless output_path is given.
    """
    return edit_model(
        model_path, "add_variable", output_path,
        name=name, equation=equation, unit=unit, kind=kind, note=note, source=source,
        target=target, x=x, y=y, near=near, view_name=view,
    )


@mcp.tool()
def rename_vensim_variable(
    model_path: str, old_name: str, new_name: str, output_path: str | None = None
) -> dict:
    """Rename a variable everywhere: its equation, every equation that uses it, and the diagram.

    Writes in place (a backup is kept) unless output_path is given.
    """
    return edit_model(model_path, "rename_variable", output_path, old=old_name, new=new_name)


@mcp.tool()
def delete_vensim_variable(
    model_path: str, name: str, force: bool = False, output_path: str | None = None
) -> dict:
    """Delete a variable's equation and its diagram objects and arrows.

    Deleting a flow also removes it from its stocks' INTEG equations and removes its valve,
    pipes and clouds. If other equations still use the variable the delete is refused and
    they are listed; force=true deletes anyway, leaving those equations broken. Writes in
    place (a backup is kept) unless output_path is given.
    """
    return edit_model(model_path, "delete_variable", output_path, name=name, force=force)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
