from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from vensim_ple_mcp.generator import generate_model
from vensim_ple_mcp.layout import build_scene
from vensim_ple_mcp.model import ModelSpec
from vensim_ple_mcp.native import detect_vensim, generate_dss_command
from vensim_ple_mcp.parser import inspect_model, validate_model
from vensim_ple_mcp.renderer import render_svg


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "examples" / "market_adoption.json"


class WorkflowTest(unittest.TestCase):
    def test_generate_validate_and_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            model = generate_model(SPEC, directory / "market.mdl")
            result = validate_model(model)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["metrics"]["physical_pipes"], 4)
            self.assertGreaterEqual(result["metrics"]["information_links"], 10)
            self.assertEqual(result["metrics"]["overlaps"], 0)
            self.assertEqual(result["metrics"]["route_node_collisions"], 0)
            self.assertLessEqual(result["metrics"]["route_crossings"], 11)
            self.assertLessEqual(result["metrics"]["information_route_crossings"], 7)
            preview = render_svg(model, directory / "market.svg")
            self.assertIn("<svg", preview.read_text(encoding="utf-8"))
            inspected = inspect_model(model)
            self.assertIn("Active Users", {item["name"] for item in inspected["equations"]})

    def test_information_routes_mix_straight_and_curved_links(self) -> None:
        scene = build_scene(ModelSpec.from_json(SPEC))
        controls = [route.control for route in scene.routes if route.kind == "information"]
        self.assertIn(None, controls)
        self.assertTrue(any(control is not None for control in controls))

    def test_duplicate_names_are_rejected(self) -> None:
        data = json.loads(SPEC.read_text(encoding="utf-8"))
        data["variables"].append(
            {"name": "Active_Users", "equation": "1", "unit": "User", "kind": "constant"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "duplicate variable"):
                generate_model(data, Path(tmp) / "bad.mdl")

    def test_json_string_is_supported_for_mcp_calls(self) -> None:
        payload = SPEC.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            model = generate_model(payload, Path(tmp) / "from_mcp.mdl")
            self.assertTrue(validate_model(model)["ok"])

    def test_dss_command_is_explicitly_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            model = generate_model(SPEC, directory / "market.mdl")
            command = generate_dss_command(model, directory / "run.cmd", "baseline")
            text = command.read_text(encoding="utf-8")
            self.assertIn("SPECIAL>LOADMODEL", text)
            self.assertIn("SIMULATE>RUNNAME|baseline", text)

    def test_real_mac_installation_is_detectable_when_present(self) -> None:
        app = Path("/Applications/VensimPLE.app")
        if app.exists():
            installation = detect_vensim()
            self.assertIsNotNone(installation)
            self.assertTrue(installation.supports_open)
            self.assertFalse(installation.supports_command_files)


if __name__ == "__main__":
    unittest.main()
