"""Native Vensim discovery and carefully bounded process integration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import time


@dataclass(frozen=True)
class VensimInstallation:
    edition: str
    app_path: str
    executable: str
    supports_open: bool
    supports_command_files: bool
    platform: str

    def to_dict(self) -> dict:
        return asdict(self)


def _mac_candidates() -> list[tuple[str, Path]]:
    return [
        ("DSS", Path("/Applications/VensimDSS.app")),
        ("Professional", Path("/Applications/VensimPro.app")),
        ("PLE Plus", Path("/Applications/VensimPLEPlus.app")),
        ("PLE", Path("/Applications/VensimPLE.app")),
    ]


def detect_vensim() -> VensimInstallation | None:
    system = platform.system()
    if system == "Darwin":
        for edition, app in _mac_candidates():
            executable_name = app.stem
            executable = app / "Contents" / "MacOS" / executable_name
            if app.is_dir() and executable.is_file():
                return VensimInstallation(
                    edition=edition,
                    app_path=str(app),
                    executable=str(executable),
                    supports_open=True,
                    supports_command_files=edition == "DSS",
                    platform=system,
                )
    elif system == "Windows":
        roots = [Path(os.environ.get(name, "")) for name in ("ProgramFiles", "ProgramFiles(x86)")]
        candidates = []
        for root in roots:
            if root.is_dir():
                candidates.extend(root.glob("Vensim/**/Vensim*.exe"))
        for executable in candidates:
            name = executable.name.casefold()
            edition = "DSS" if "dss" in name else "PLE" if "ple" in name else "Professional"
            return VensimInstallation(
                edition=edition,
                app_path=str(executable.parent),
                executable=str(executable),
                supports_open=True,
                supports_command_files=edition == "DSS",
                platform=system,
            )
    else:
        executable = shutil.which("vensim")
        if executable:
            return VensimInstallation("Unknown", executable, executable, True, False, system)
    return None


def open_in_vensim(model_path: str | Path) -> dict:
    model = Path(model_path).expanduser().resolve()
    if not model.is_file() or model.suffix.lower() != ".mdl":
        raise FileNotFoundError(f"Vensim .mdl not found: {model}")
    installation = detect_vensim()
    if installation is None:
        raise RuntimeError("Vensim was not detected on this machine")
    if installation.platform == "Darwin":
        command = ["open", "-a", installation.app_path, str(model)]
    elif installation.platform == "Windows":
        command = [installation.executable, str(model)]
    else:
        command = [installation.executable, str(model)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Vensim failed to open the model")
    return {
        "opened": True,
        "model": str(model),
        "installation": installation.to_dict(),
        "next_check": "Run Model > Check Model and Model > Units Check in Vensim.",
    }


def _safe_run_name(run_name: str) -> str:
    safe = "".join(char for char in run_name if char.isalnum() or char in "-_")
    if not safe:
        raise ValueError("run_name must contain letters, numbers, '-' or '_'")
    return safe


def generate_dss_command(
    model_path: str | Path,
    output_path: str | Path,
    run_name: str = "current",
    exit_after: bool = True,
) -> Path:
    model = Path(model_path).expanduser().resolve()
    if not model.is_file():
        raise FileNotFoundError(model)
    output = Path(output_path).expanduser().resolve()
    if output.suffix.lower() != ".cmd":
        output = output.with_suffix(".cmd")
    output.parent.mkdir(parents=True, exist_ok=True)
    safe_run_name = _safe_run_name(run_name)
    lines = [
        "SPECIAL>NOINTERACTION",
        f"SPECIAL>LOADMODEL|{model}",
        f"SIMULATE>RUNNAME|{safe_run_name}",
        "MENU>RUN|O",
    ]
    if exit_after:
        lines.append("MENU>EXIT")
    command = "\n".join(lines + [""])
    output.write_text(command, encoding="utf-8", newline="\n")
    return output


ACCESSIBILITY_HELP = (
    "macOS blocked control of Vensim. Open System Settings > Privacy & Security > "
    "Accessibility, turn on the app that runs this MCP server (Claude Desktop, Terminal, "
    "iTerm, VS Code, ...), then quit and reopen that app. This is needed once per machine."
)

# Drives Vensim PLE's Simulation Control dialog. PLE has no scripting interface and its
# toolbar is custom-drawn, but this dialog exposes real buttons to the accessibility API.
# Returns "ok", "exists", or "alert:<text>" (an alert Vensim showed, which is dismissed).
_MAC_SIMULATE_SCRIPT = r"""
on alertText(p)
    tell application "System Events"
        repeat with w in (windows of p)
            if description of w is "alert" then
                set msg to ""
                repeat with t in (static texts of w)
                    try
                        set msg to msg & (value of t as text) & " "
                    end try
                end repeat
                return msg
            end if
        end repeat
    end tell
    return missing value
end alertText

on clickAlert(p, labels)
    tell application "System Events"
        repeat with w in (windows of p)
            if description of w is "alert" then
                repeat with b in labels
                    if exists button (b as text) of w then
                        click button (b as text) of w
                        return
                    end if
                end repeat
                click button 1 of w
                return
            end if
        end repeat
    end tell
end clickAlert

on run argv
    set processName to item 1 of argv
    set modelName to item 2 of argv
    set runName to item 3 of argv
    set overwrite to (item 4 of argv is "1")
    set timeoutSeconds to (item 5 of argv) as integer

    tell application "System Events"
        set p to process processName
        set frontmost of p to true

        -- Wait for the model's window; stop on any alert Vensim raises while loading.
        set found to false
        repeat (timeoutSeconds * 4) times
            set msg to my alertText(p)
            if msg is not missing value then
                if msg contains "already open" then
                    -- Informational: Vensim switches to the open copy of the model.
                    my clickAlert(p, {"OK", "Yes"})
                else if msg contains "autosave" then
                    -- Simulate the file that was asked for, not Vensim's autosave copy.
                    my clickAlert(p, {"No"})
                else
                    my clickAlert(p, {"OK", "Cancel", "No"})
                    return "alert:" & msg
                end if
            end if
            repeat with w in (windows of p)
                if (name of w) contains (": " & modelName) then set found to true
            end repeat
            if found then exit repeat
            delay 0.25
        end repeat
        if not found then return "alert:Vensim did not show " & modelName & " in time."

        click menu item "Simulation Control" of menu 1 of menu bar item "Simulation" of menu bar 1 of p
        -- A model with errors raises "cannot be simulated" instead of opening the dialog.
        repeat 40 times
            if exists window "Simulation Control" of p then exit repeat
            set msg to my alertText(p)
            if msg is not missing value then
                my clickAlert(p, {"No", "OK", "Cancel"})
                return "alert:" & msg
            end if
            delay 0.25
        end repeat
        if not (exists window "Simulation Control" of p) then
            return "alert:Vensim did not open Simulation Control."
        end if
        set dialog to window "Simulation Control" of p
        set field to text field 1 of group 1 of dialog
        set focused of field to true
        keystroke "a" using command down
        keystroke runName
        delay 0.2
        click (first button of dialog whose help is "Simulate")

        -- Simulating closes the dialog; an existing run name raises an overwrite alert.
        repeat (timeoutSeconds * 4) times
            delay 0.25
            set msg to my alertText(p)
            if msg is not missing value then
                if msg contains "already exists" then
                    if overwrite then
                        my clickAlert(p, {"Yes"})
                    else
                        my clickAlert(p, {"Cancel"})
                        if exists window "Simulation Control" of p then
                            click button "Cancel" of window "Simulation Control" of p
                        end if
                        return "exists"
                    end if
                else
                    my clickAlert(p, {"OK", "Cancel", "No"})
                    if exists window "Simulation Control" of p then
                        click button "Cancel" of window "Simulation Control" of p
                    end if
                    return "alert:" & msg
                end if
            else if not (exists window "Simulation Control" of p) then
                return "ok"
            end if
        end repeat
        return "alert:Vensim did not finish the simulation in time."
    end tell
end run
"""


def _wait_for_dataset(dataset: Path, started: float, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dataset.is_file() and dataset.stat().st_mtime >= started - 1:
            return True
        time.sleep(0.25)
    return False


def _simulate_mac_gui(
    installation: VensimInstallation, model: Path, run_name: str, overwrite: bool, timeout: int
) -> None:
    opened = subprocess.run(
        ["open", "-a", installation.app_path, str(model)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if opened.returncode != 0:
        raise RuntimeError(opened.stderr.strip() or "Vensim failed to open the model")
    process_name = Path(installation.executable).name
    completed = subprocess.run(
        ["osascript", "-", process_name, model.name, run_name, "1" if overwrite else "0",
         str(timeout)],
        input=_MAC_SIMULATE_SCRIPT, capture_output=True, text=True, timeout=timeout * 2 + 30,
        check=False,
    )
    error = completed.stderr.strip()
    if "-1719" in error or "-25211" in error or "assistive access" in error:
        raise PermissionError(ACCESSIBILITY_HELP)
    if completed.returncode != 0:
        raise RuntimeError(f"Could not drive Vensim: {error or 'unknown AppleScript error'}")
    status = completed.stdout.strip()
    if status == "exists":
        raise FileExistsError(
            f"Run '{run_name}' already exists next to the model; pass overwrite=True or "
            "choose another run_name."
        )
    if status.startswith("alert:"):
        raise RuntimeError(f"Vensim reported: {status[len('alert:'):].strip()}")


def _simulate_dss(
    installation: VensimInstallation, model: Path, run_name: str, timeout: int
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        script = generate_dss_command(model, Path(tmp) / "run.cmd", run_name, exit_after=False)
        if installation.platform == "Darwin":
            command = ["open", "-W", "-a", installation.app_path, str(script)]
        else:
            command = [installation.executable, str(script)]
        subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def simulate_in_vensim(
    model_path: str | Path,
    run_name: str = "current",
    overwrite: bool = True,
    timeout: int = 120,
) -> dict:
    """Run one simulation inside the installed Vensim so its Graph tools have a loaded run.

    Vensim saves the run as <run_name>.vdfx next to the model. On macOS PLE, PLE Plus and
    Professional this drives the Simulation Control dialog through the accessibility API,
    which needs a one-time Accessibility permission. Vensim DSS uses a command script.
    """
    model = Path(model_path).expanduser().resolve()
    if not model.is_file() or model.suffix.lower() != ".mdl":
        raise FileNotFoundError(f"Vensim .mdl not found: {model}")
    safe_run_name = _safe_run_name(run_name)
    installation = detect_vensim()
    if installation is None:
        raise RuntimeError(
            "Vensim was not detected on this machine. Use run_vensim_model (PySD) instead."
        )
    dataset = model.parent / f"{safe_run_name}.vdfx"
    if dataset.exists() and not overwrite:
        raise FileExistsError(
            f"Run '{safe_run_name}' already exists at {dataset}; pass overwrite=True or "
            "choose another run_name."
        )
    started = time.time()

    if installation.edition == "DSS":
        method = "dss_command_script"
        _simulate_dss(installation, model, safe_run_name, timeout)
    elif installation.platform == "Darwin":
        method = "macos_accessibility"
        _simulate_mac_gui(installation, model, safe_run_name, overwrite, timeout)
    else:
        raise NotImplementedError(
            f"Driving Vensim {installation.edition} on {installation.platform} is not supported "
            "yet. Open the model with open_vensim_model and click Simulate, or use "
            "run_vensim_model (PySD)."
        )

    if not _wait_for_dataset(dataset, started, 15):
        raise RuntimeError(f"Vensim finished but no dataset appeared at {dataset}")
    return {
        "simulated": True,
        "model": str(model),
        "run_name": safe_run_name,
        "dataset": str(dataset),
        "method": method,
        "edition": installation.edition,
        "note": (
            "The run is loaded in Vensim, so its Graph and Table tools work now. Vensim "
            "simulates the model as it is open in Vensim: unsaved edits there are included, "
            "and a file changed on disk while open may need File > Open to reload."
        ),
    }


def doctor() -> dict:
    installation = detect_vensim()
    return {
        "vensim": installation.to_dict() if installation else None,
        "graphviz_dot": shutil.which("dot"),
        "native_boundary": (
            "simulate_in_vensim drives Vensim PLE, PLE Plus and Professional on macOS through "
            "the accessibility API (one-time Accessibility permission) and uses command "
            "scripts with Vensim DSS. Windows PLE is not automated yet; run_vensim_model "
            "simulates with PySD anywhere."
        ),
    }
