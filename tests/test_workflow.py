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

    def test_layout_is_the_same_in_every_process(self) -> None:
        # Python randomises string hashing per process; iterating a set of names once
        # made the diagram (and this file's collision count) change between runs.
        import os
        import subprocess
        import sys

        outputs = set()
        with tempfile.TemporaryDirectory() as tmp:
            for seed in ("1", "2", "3", "4"):
                target = Path(tmp) / f"m{seed}.mdl"
                subprocess.run(
                    [sys.executable, "-m", "vensim_ple_mcp.cli", "generate", str(SPEC), "-o", str(target)],
                    check=True, capture_output=True, env={**os.environ, "PYTHONHASHSEED": seed},
                )
                outputs.add(target.read_text(encoding="utf-8"))
        self.assertEqual(len(outputs), 1)

    def test_information_routes_mix_straight_and_curved_links(self) -> None:
        scene = build_scene(ModelSpec.from_json(SPEC))
        controls = [route.control for route in scene.routes if route.kind == "information"]
        self.assertIn(None, controls)
        self.assertTrue(any(control is not None for control in controls))

    def test_sketch_records_follow_vensim_conventions(self) -> None:
        # Formats taken from models saved by Vensim PLE 10.5 itself.
        with tempfile.TemporaryDirectory() as tmp:
            model = generate_model(SPEC, Path(tmp) / "market.mdl")
            sketch = model.read_text(encoding="utf-8").split("Sketch information", 1)[1]
        records = [
            line.split("|", 1)[0].split(",")
            for line in sketch.splitlines()
            if line.split(",", 1)[0] in ("1", "10", "11", "12")
        ]
        by_id = {fields[1]: fields for fields in records if fields[0] != "1"}
        order = [fields[1] for fields in records if fields[0] != "1"]
        # A flow's name record must directly follow its valve, or Vensim detaches it.
        for index, object_id in enumerate(order):
            if by_id[object_id][0] == "11":
                follower = by_id[order[index + 1]]
                self.assertEqual((follower[0], follower[7]), ("10", "40"))
        pipes = [fields for fields in records if fields[0] == "1" and fields[4] in ("4", "100")]
        self.assertEqual(len(pipes), 4)
        for fields in pipes:
            # Both halves of a pipe start at the valve; only the downstream half has a head.
            self.assertEqual(by_id[fields[2]][0], "11")
        self.assertEqual(sorted(fields[4] for fields in pipes), ["100", "100", "4", "4"])
        links = [fields for fields in records if fields[0] == "1" and fields[4] in ("0", "1")]
        # Polarity is stored as a character code in field 6, not as text elsewhere.
        self.assertTrue(all(fields[6] in ("0", "43", "45") for fields in links))
        self.assertIn("45", {fields[6] for fields in links})

    def test_separate_stock_chains_get_separate_rows(self) -> None:
        spec = {
            "name": "two", "title": "Two",
            "simulation": {"initial_time": 0, "final_time": 10, "time_step": 1, "time_unit": "Year"},
            "levels": [
                {"name": "A", "equation": "INTEG ( In A - Out A, 1 )", "unit": "x"},
                {"name": "B", "equation": "INTEG ( In B - Out B, 1 )", "unit": "x"},
            ],
            "flows": [
                {"name": "In A", "equation": "1", "unit": "x", "source": None, "target": "A"},
                {"name": "Out A", "equation": "1", "unit": "x", "source": "A", "target": None},
                {"name": "In B", "equation": "1", "unit": "x", "source": None, "target": "B"},
                {"name": "Out B", "equation": "1", "unit": "x", "source": "B", "target": None},
            ],
            "variables": [],
            "links": [],
        }
        scene = build_scene(ModelSpec.from_json(spec))
        self.assertNotEqual(scene.shapes["A"].y, scene.shapes["B"].y)
        self.assertTrue(all(shape.x > 0 and shape.y > 0 for shape in scene.shapes.values()))

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
