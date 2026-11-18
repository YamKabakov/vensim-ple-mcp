# vensim-ple-mcp

An MCP server that lets Claude (or any MCP client) build, inspect, and **simulate** Vensim system dynamics models, built for **Vensim PLE on macOS**.

Vensim PLE has no scripting interface, DLL, or batch mode. This server works on the `.mdl` text file directly and runs simulations through [PySD](https://github.com/SDXorg/pysd), so an assistant can answer questions like "what happens to peak adoption if word of mouth doubles?" without Vensim DSS. PLE stays the place you open the model, run Check Model and Units Check, and present it.

![Bass diffusion example](examples/bass_diffusion.svg)

## Tools

| Tool | What it does |
|---|---|
| `list_model_parameters` | Constants with their current values, other variables, time settings |
| `run_vensim_model` | Simulate with optional constant overrides; returns per-variable summaries (initial, final, min/max and when) and a downsampled series; can save full results to CSV |
| `sweep_model_parameter` | Run once per value of one constant and compare outcomes |
| `generate_vensim_model` | Write a `.mdl` (equations and laid-out diagram) from a JSON spec |
| `set_vensim_equation` | Change a variable's equation, unit or note in an existing model; arrows into it are added or removed to match |
| `add_vensim_variable` | Add an auxiliary, constant, stock or flow and draw it next to the variables it uses. A new flow gets its valve, pipes and clouds, and its stocks' INTEG equations are updated |
| `rename_vensim_variable` | Rename a variable in its equation, every equation that uses it, and the diagram |
| `delete_vensim_variable` | Remove a variable and its diagram objects. Refuses while other equations use it; a deleted flow is also taken out of its stocks' equations |
| `find_feedback_loops` | List feedback loops, each labelled reinforcing (R) or balancing (B), with the polarity of every link and the stocks it passes through. Flags algebraic loops (no stock or delay), which Vensim won't simulate. With `annotate`, writes link polarities onto the diagram's arrows and places R1/B1 labels inside the loops |
| `inspect_vensim_model` | Read equations, diagram objects, arrows, and views |
| `validate_vensim_model` | Static audit. Equations: undefined variables, unknown functions, algebraic loops, circular initialization (a SMOOTH or DELAY whose starting value depends on itself), unit mismatches (including stock and delay-time units), unused constants. Diagram: arrows that don't match equations, flows piped to a stock but missing from its `INTEG` (or with the wrong sign), overlaps, crossing links. Each finding is also returned as a structured `issues` entry |
| `render_vensim_preview` | SVG preview drawn from the model's own diagram records |
| `plot_vensim_results` | Simulate with PySD and save an SVG line chart (stocks by default; one panel per unit). Works without Vensim |
| `simulate_in_vensim` | Run the simulation inside the installed Vensim, so its Graph and Table tools have a loaded run (`<run_name>.vdfx` next to the model). See [Simulating inside Vensim](#simulating-inside-vensim) |
| `open_vensim_model` | Open the model in the installed Vensim app |
| `vensim_doctor` | Detect the local Vensim edition and what it supports |
| `create_vensim_dss_script` | Batch script for Vensim DSS only |

Overrides apply to one run only; the `.mdl` file is never modified by simulation.

The editing tools change only the equation blocks and diagram records they touch, so models saved by Vensim keep their formatting, settings and line endings. Each edit writes the file in place and keeps a copy of the previous version in `~/.cache/vensim-ple-mcp/backups` (or pass `output_path` to write elsewhere). If the model is open in Vensim, close it without saving and reopen it after an edit. PySD translations are cached in `~/.cache/vensim-ple-mcp` (override with `VENSIM_PLE_MCP_CACHE`), so no extra files appear next to your model.

## Setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/YamKabakov/vensim-ple-mcp.git
cd vensim-ple-mcp
uv sync
uv run python -m unittest discover tests
```

Add it to Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "vensim": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/vensim-ple-mcp", "run", "vensim-ple-mcp-server"]
    }
  }
}
```

Or Claude Code: `claude mcp add vensim -- uv --directory /absolute/path/to/vensim-ple-mcp run vensim-ple-mcp-server`

## Simulating inside Vensim

Vensim's Graph tool only shows runs that Vensim simulated itself, so PySD results are not enough there. `simulate_in_vensim` makes Vensim do the run:

| Vensim | How | One-time setup |
|---|---|---|
| PLE, PLE Plus, Professional on macOS | Drives the Simulation Control dialog through the macOS accessibility API | Turn on the app that runs the server (Claude Desktop, Terminal, iTerm, VS Code, ...) in System Settings > Privacy & Security > Accessibility, then restart that app |
| DSS (macOS or Windows) | Vensim command script | None |
| PLE on Windows | Not automated yet: open the model and click Simulate | None |

Without the permission, the tool returns these steps instead of failing silently. It answers Vensim's routine prompts: it switches to an already-open model, opens the file instead of an autosave backup, and overwrites an existing run unless `overwrite` is false. Any other Vensim message, such as "Model has errors and cannot be simulated", is returned as the error. Vensim simulates the model as it is open in Vensim. Vensim stays in the background: it opens the model without coming forward, comes to the front for about half a second (it only builds its Simulation menu while active) to open Simulation Control and set the run name, then hands focus back to the app you were using while the run finishes behind it.

## Command line

```bash
uv run vensim-ple-mcp params examples/bass_diffusion.mdl
uv run vensim-ple-mcp run examples/bass_diffusion.mdl --set "Word of Mouth Strength=0.5" --var Adopters --csv out.csv
uv run vensim-ple-mcp sweep examples/bass_diffusion.mdl "Word of Mouth Strength" 0.2 0.38 0.6 --var "Adoption Rate"
uv run vensim-ple-mcp add model.mdl "Churn" "Adopters * 0.05" --unit Person/Year --kind flow --source Adopters
uv run vensim-ple-mcp set model.mdl "Word of Mouth Strength" 0.5
uv run vensim-ple-mcp rename model.mdl "Adopters" "Customers"
uv run vensim-ple-mcp delete model.mdl "Churn"
uv run vensim-ple-mcp loops examples/bass_diffusion.mdl            # add --annotate to label the diagram
uv run vensim-ple-mcp generate examples/bass_diffusion.json -o examples/bass_diffusion.mdl
uv run vensim-ple-mcp render examples/bass_diffusion.mdl -o examples/bass_diffusion.svg
uv run vensim-ple-mcp plot examples/bass_diffusion.mdl -o adoption.svg --var Adopters --var "Potential Adopters"
uv run vensim-ple-mcp vensim-run examples/bass_diffusion.mdl --run-name baseline
```

The test suite skips the test that drives a real Vensim; run it with `VENSIM_LIVE=1 uv run python -m unittest tests.test_native_simulate`.

## Limits

- The unit check is conservative: it skips anything it cannot infer (unfamiliar functions, variables without units) and only knows that time units like Month and Months are the same. Vensim's Units Check remains the authority.
- Link polarity is worked out from the equations assuming variables are positive (constants use their actual sign), and lookups take their table's direction. When a sign depends on values, such as an IF THEN ELSE condition or a lookup that rises then falls, the loop is reported as unknown (U1, ...) with the undetermined links named, rather than guessed.
- Simulation covers what PySD supports, which is most standard Vensim functions; see the [PySD docs](https://pysd.readthedocs.io/) for exceptions. Vensim remains the authority on Model Check and Units Check.
- The JSON spec supports stocks, flows, auxiliaries, and constants. No lookups, subscripts, or multiple views yet.
- Editing covers plain equations. Subscripted, lookup and data variables are left alone and must be edited in Vensim.
- Automatic placement looks for free space near related variables. In a crowded diagram it can overlap other objects, and says so; pass `x` and `y` or drag the object in Vensim.

## Roadmap

1. Loop dominance over time: which loops drive behaviour in each phase of a run
2. Lookups and multiple views in the spec

## Credits

Built on [Cybing521/vensim-mcp](https://github.com/Cybing521/vensim-mcp) (MIT), which provides the `.mdl` generator, diagram layout, parser, SVG renderer, and native Vensim handoff. This project adds PySD simulation, parameter sweeps, and a PLE-focused workflow.

Vensim is a product of Ventana Systems, Inc. This project is not affiliated with Ventana and does not include Vensim.
