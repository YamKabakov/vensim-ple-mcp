from __future__ import annotations

import csv
import os
from pathlib import Path
import tempfile
import unittest

from vensim_ple_mcp.generator import generate_model
from vensim_ple_mcp.simulate import list_parameters, run_model, sweep_parameter


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "examples" / "market_adoption.json"


class SimulateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        os.environ["VENSIM_PLE_MCP_CACHE"] = str(self.dir / "cache")
        self.model = generate_model(SPEC, self.dir / "market.mdl")

    def tearDown(self) -> None:
        os.environ.pop("VENSIM_PLE_MCP_CACHE", None)
        self.tmp.cleanup()

    def test_parameters_lists_constants_with_values(self) -> None:
        info = list_parameters(self.model)
        constants = {item["name"]: item["value"] for item in info["constants"]}
        self.assertEqual(constants["Churn Fraction"], 0.025)
        self.assertEqual(info["time"]["final_time"], 60)
        # Time is PySD's clock, not a model variable, and nothing is reported as NaN.
        self.assertNotIn("Time", [item["name"] for item in info["variables"]])
        for item in info["constants"] + info["variables"]:
            self.assertIsInstance(item["units"], str, item)
            self.assertIsInstance(item["type"], str, item)

    def test_run_returns_summary_and_downsampled_series(self) -> None:
        result = run_model(self.model, max_points=7)
        users = result["summary"]["Active Users"]
        self.assertEqual(users["initial"], 100)
        self.assertGreater(users["final"], users["initial"])
        self.assertEqual(len(result["series"]["time"]), 7)
        self.assertEqual(result["series"]["time"][-1], 60)
        self.assertNotIn("Churn Fraction", result["summary"])

    def test_overrides_change_results_without_editing_the_file(self) -> None:
        before = self.model.read_bytes()
        base = run_model(self.model, variables=["Active Users"])
        faster = run_model(
            self.model, overrides={"Contact Effectiveness": 0.07}, variables=["Active Users"]
        )
        self.assertGreater(
            faster["summary"]["Active Users"]["final"], base["summary"]["Active Users"]["final"]
        )
        self.assertEqual(self.model.read_bytes(), before)
        # Overrides must not leak into the next run.
        again = run_model(self.model, variables=["Active Users"])
        self.assertEqual(again["summary"], base["summary"])

    def test_no_translation_files_written_next_to_model(self) -> None:
        run_model(self.model, variables=["Active Users"])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["cache", "market.mdl"])

    def test_csv_export(self) -> None:
        result = run_model(self.model, variables=["Active Users"], csv_path=self.dir / "out.csv")
        with open(result["csv"], newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["time", "Active Users"])
        self.assertEqual(len(rows) - 1, result["time"]["points"])

    def test_sweep_higher_churn_means_fewer_users(self) -> None:
        sweep = sweep_parameter(self.model, "Churn Fraction", [0.01, 0.025, 0.05], ["Active Users"])
        finals = [run["summary"]["Active Users"]["final"] for run in sweep["runs"]]
        self.assertEqual(finals, sorted(finals, reverse=True))

    def test_missing_model_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            run_model(self.dir / "nope.mdl")


if __name__ == "__main__":
    unittest.main()
