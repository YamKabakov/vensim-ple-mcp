"""Tests for simulating inside Vensim.

The unit tests replace Vensim and osascript with fakes, so they run anywhere. The live test
drives a real Vensim on macOS and only runs with VENSIM_LIVE=1, because it takes over the
Vensim window and needs Accessibility permission.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from vensim_ple_mcp import native
from vensim_ple_mcp.native import VensimInstallation, generate_dss_command, simulate_in_vensim


ROOT = Path(__file__).resolve().parents[1]
BASS = ROOT / "examples" / "bass_diffusion.mdl"

MAC_PLE = VensimInstallation(
    edition="PLE",
    app_path="/Applications/VensimPLE.app",
    executable="/Applications/VensimPLE.app/Contents/MacOS/VensimPLE",
    supports_open=True,
    supports_command_files=False,
    platform="Darwin",
)


def _done(stdout: str = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


class SimulateInVensimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.model = self.dir / "bass.mdl"
        shutil.copyfile(BASS, self.model)
        self.dataset = self.dir / "baseline.vdfx"
        patcher = mock.patch.object(native, "detect_vensim", return_value=MAC_PLE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fake_vensim(self, osascript_result: subprocess.CompletedProcess, writes: bool = True):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if command[0] == "osascript":
                if writes:
                    self.dataset.write_bytes(b"run")
                return osascript_result
            return _done()

        return mock.patch.object(native.subprocess, "run", side_effect=run), calls

    def test_success_returns_dataset_next_to_model(self) -> None:
        patch, calls = self._fake_vensim(_done("ok"))
        with patch:
            result = simulate_in_vensim(self.model, "baseline")
        self.assertEqual(result["dataset"], str(self.dataset.resolve()))
        self.assertEqual(result["method"], "macos_accessibility")
        opened, script = calls
        self.assertEqual(opened[0][:4], ["open", "-g", "-a", MAC_PLE.app_path])  # -g: stay in the background
        # Model and run names go in as arguments, never spliced into the script text.
        self.assertEqual(script[0][1:5], ["-", "VensimPLE", "bass.mdl", "baseline"])
        self.assertNotIn("baseline", script[1]["input"])

    def test_missing_accessibility_permission_explains_the_fix(self) -> None:
        denied = _done(stderr="execution error: osascript is not allowed assistive access. (-1719)",
                       code=1)
        patch, _ = self._fake_vensim(denied, writes=False)
        with patch, self.assertRaises(PermissionError) as caught:
            simulate_in_vensim(self.model, "baseline")
        self.assertIn("Accessibility", str(caught.exception))

    def test_vensim_alert_is_reported(self) -> None:
        alert = _done("alert:Model has errors and cannot be simulated.")
        patch, _ = self._fake_vensim(alert, writes=False)
        with patch, self.assertRaises(RuntimeError) as caught:
            simulate_in_vensim(self.model, "baseline")
        self.assertIn("cannot be simulated", str(caught.exception))

    def test_existing_run_without_overwrite_never_touches_vensim(self) -> None:
        self.dataset.write_bytes(b"old")
        patch, calls = self._fake_vensim(_done("ok"))
        with patch, self.assertRaises(FileExistsError):
            simulate_in_vensim(self.model, "baseline", overwrite=False)
        self.assertEqual(calls, [])

    def test_run_name_is_sanitized_and_validated(self) -> None:
        patch, calls = self._fake_vensim(_done("ok"))
        with patch:
            result = simulate_in_vensim(self.model, "base line!")
        self.assertEqual(result["run_name"], "baseline")
        with self.assertRaises(ValueError):
            simulate_in_vensim(self.model, "!!!")

    def test_windows_ple_is_reported_as_unsupported(self) -> None:
        windows = VensimInstallation("PLE", "C:/Vensim", "C:/Vensim/vensimple.exe", True, False,
                                     "Windows")
        with mock.patch.object(native, "detect_vensim", return_value=windows):
            with self.assertRaises(NotImplementedError):
                simulate_in_vensim(self.model, "baseline")

    def test_no_vensim_points_to_pysd(self) -> None:
        with mock.patch.object(native, "detect_vensim", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                simulate_in_vensim(self.model, "baseline")
        self.assertIn("run_vensim_model", str(caught.exception))

    def test_dss_script_can_stay_open(self) -> None:
        script = generate_dss_command(self.model, self.dir / "run.cmd", "base", exit_after=False)
        self.assertNotIn("MENU>EXIT", script.read_text())
        script = generate_dss_command(self.model, self.dir / "run.cmd", "base")
        self.assertIn("MENU>EXIT", script.read_text())


@unittest.skipUnless(os.environ.get("VENSIM_LIVE") == "1", "set VENSIM_LIVE=1 to drive real Vensim")
class LiveVensimTest(unittest.TestCase):
    def test_simulates_in_real_vensim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "bass.mdl"
            shutil.copyfile(BASS, model)
            result = simulate_in_vensim(model, "live_check")
            self.assertTrue(Path(result["dataset"]).stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
