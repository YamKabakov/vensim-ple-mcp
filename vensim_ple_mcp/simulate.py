"""Simulate Vensim .mdl models with PySD, so results are available without Vensim DSS.

Vensim PLE has no scripting interface, so the model's equations are translated to Python
by PySD and run here. Translations are cached outside the user's folder, keyed by the
model file's contents, so repeated runs are fast and no stray .py files appear next to
the .mdl.
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import shutil
from typing import Any

import pandas as pd


def _cache_root() -> Path:
    base = os.environ.get("VENSIM_PLE_MCP_CACHE") or os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "vensim-ple-mcp"


def _resolve(model_path: str | Path) -> Path:
    model = Path(model_path).expanduser().resolve()
    if not model.is_file() or model.suffix.lower() != ".mdl":
        raise FileNotFoundError(f"Vensim .mdl not found: {model}")
    return model


def load_model(model_path: str | Path):
    """Translate (or reuse a cached translation of) a .mdl and return a PySD model."""
    import pysd

    model = _resolve(model_path)
    digest = hashlib.sha256(model.read_bytes()).hexdigest()[:16]
    workdir = _cache_root() / f"{model.stem}-{digest}"
    translated = workdir / f"{model.stem}.py"
    if translated.is_file():
        return pysd.load(str(translated))
    workdir.mkdir(parents=True, exist_ok=True)
    copy = workdir / model.name
    shutil.copyfile(model, copy)
    return pysd.read_vensim(str(copy))


def _clean(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, 6)


def _summary(series: pd.Series) -> dict:
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.dropna().empty:
        return {"initial": None, "final": None, "min": None, "max": None}
    return {
        "initial": _clean(numeric.iloc[0]),
        "final": _clean(numeric.iloc[-1]),
        "min": _clean(numeric.min()),
        "time_of_min": _clean(numeric.idxmin()),
        "max": _clean(numeric.max()),
        "time_of_max": _clean(numeric.idxmax()),
    }


def _downsample(frame: pd.DataFrame, max_points: int) -> pd.DataFrame:
    if max_points <= 1 or len(frame) <= max_points:
        return frame
    step = math.ceil((len(frame) - 1) / (max_points - 1))
    keep = list(range(0, len(frame), step))
    if keep[-1] != len(frame) - 1:
        keep.append(len(frame) - 1)
    return frame.iloc[keep]


def _default_columns(model) -> list[str]:
    """Stocks and flows/auxiliaries, skipping constants, lookups and time controls."""
    doc = model.doc
    skip_types = {"Constant", "Lookup", "Data"}
    controls = {"INITIAL TIME", "FINAL TIME", "TIME STEP", "SAVEPER", "Time", "TIME"}
    names = []
    for _, row in doc.iterrows():
        if row.get("Type") in skip_types or row["Real Name"] in controls:
            continue
        names.append(row["Real Name"])
    return names


def list_parameters(model_path: str | Path) -> dict:
    """List the model's constants (what can be overridden) and its other variables."""
    model = load_model(model_path)
    doc = model.doc
    controls = {"INITIAL TIME", "FINAL TIME", "TIME STEP", "SAVEPER"}
    constants, others = [], []
    for _, row in doc.iterrows():
        name = row["Real Name"]
        if name in controls:
            continue
        entry = {"name": name, "units": row.get("Units") or "", "type": row.get("Type")}
        if row.get("Type") == "Constant":
            try:
                entry["value"] = _clean(model[name])
            except Exception:  # subscripted or otherwise non-scalar constants
                entry["value"] = None
            constants.append(entry)
        else:
            others.append(entry)
    time = {
        "initial_time": _clean(model["INITIAL TIME"]),
        "final_time": _clean(model["FINAL TIME"]),
        "time_step": _clean(model["TIME STEP"]),
    }
    return {"model": str(_resolve(model_path)), "time": time, "constants": constants, "variables": others}


def run_model(
    model_path: str | Path,
    overrides: dict[str, float] | None = None,
    variables: list[str] | None = None,
    final_time: float | None = None,
    time_step: float | None = None,
    max_points: int = 25,
    csv_path: str | Path | None = None,
) -> dict:
    """Run one simulation and return a summary per variable plus a downsampled time series.

    The full-resolution results can be written to csv_path for plotting or comparison.
    """
    model = load_model(model_path)
    columns = variables or _default_columns(model)
    results = model.run(
        params=dict(overrides or {}),
        return_columns=columns,
        final_time=final_time,
        time_step=time_step,
        reload=True,
    )
    results.index.name = "time"

    output: dict[str, Any] = {
        "model": str(_resolve(model_path)),
        "overrides": dict(overrides or {}),
        "time": {
            "initial": _clean(results.index[0]),
            "final": _clean(results.index[-1]),
            "points": len(results),
        },
        "summary": {name: _summary(results[name]) for name in results.columns},
    }
    sample = _downsample(results, max_points)
    output["series"] = {
        "time": [_clean(t) for t in sample.index],
        **{name: [_clean(v) for v in sample[name]] for name in sample.columns},
    }
    if csv_path:
        destination = Path(csv_path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        results.to_csv(destination)
        output["csv"] = str(destination)
    return output


def sweep_parameter(
    model_path: str | Path,
    parameter: str,
    values: list[float],
    variables: list[str] | None = None,
    base_overrides: dict[str, float] | None = None,
    final_time: float | None = None,
) -> dict:
    """Run the model once per parameter value and compare summaries side by side."""
    if not values:
        raise ValueError("values must contain at least one number")
    model = load_model(model_path)
    columns = variables or _default_columns(model)
    runs = []
    for value in values:
        params = {**(base_overrides or {}), parameter: value}
        results = model.run(params=params, return_columns=columns, final_time=final_time, reload=True)
        runs.append(
            {
                "value": _clean(value),
                "summary": {name: _summary(results[name]) for name in results.columns},
            }
        )
    return {
        "model": str(_resolve(model_path)),
        "parameter": parameter,
        "base_overrides": dict(base_overrides or {}),
        "runs": runs,
    }
