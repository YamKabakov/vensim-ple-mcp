"""Native Vensim discovery and carefully bounded process integration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import platform
import shutil
import subprocess


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


def generate_dss_command(
    model_path: str | Path, output_path: str | Path, run_name: str = "current"
) -> Path:
    model = Path(model_path).expanduser().resolve()
    if not model.is_file():
        raise FileNotFoundError(model)
    output = Path(output_path).expanduser().resolve()
    if output.suffix.lower() != ".cmd":
        output = output.with_suffix(".cmd")
    output.parent.mkdir(parents=True, exist_ok=True)
    safe_run_name = "".join(char for char in run_name if char.isalnum() or char in "-_")
    if not safe_run_name:
        raise ValueError("run_name must contain letters, numbers, '-' or '_'")
    command = "\n".join(
        [
            "SPECIAL>NOINTERACTION",
            f"SPECIAL>LOADMODEL|{model}",
            f"SIMULATE>RUNNAME|{safe_run_name}",
            "MENU>RUN|O",
            "MENU>EXIT",
            "",
        ]
    )
    output.write_text(command, encoding="utf-8", newline="\n")
    return output


def doctor() -> dict:
    installation = detect_vensim()
    return {
        "vensim": installation.to_dict() if installation else None,
        "graphviz_dot": shutil.which("dot"),
        "native_boundary": (
            "macOS Vensim PLE can open and interactively validate .mdl files, but official "
            "command-file batch processing and the Vensim DLL require DSS/Windows capabilities."
        ),
    }
