"""Model Context Protocol server exposing Vensim generation and native handoff tools."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .generator import generate_model
from .native import doctor, generate_dss_command, open_in_vensim
from .parser import inspect_model, validate_model
from .renderer import render_svg
from .simulate import list_parameters, run_model, sweep_parameter


mcp = FastMCP(
    "Vensim PLE MCP",
    instructions=(
        "Generate, inspect, and simulate Vensim .mdl models. Simulation runs through PySD, so it "
        "works with Vensim PLE, which has no scripting interface. Call list_model_parameters "
        "before run_model or sweep_parameter to get exact variable names. Static validation is "
        "not a replacement for Vensim's Model Check or Units Check: open the model in PLE for "
        "the authoritative check."
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
    """Run a conservative static audit before opening the model in Vensim."""
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


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
