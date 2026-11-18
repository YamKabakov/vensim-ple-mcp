from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
import unittest

import pandas as pd

from vensim_ple_mcp.generator import generate_model
from vensim_ple_mcp.plot import _nice_ticks, plot_results, render_results_svg


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "examples" / "market_adoption.json"


class TicksTest(unittest.TestCase):
    def test_ticks_cover_the_data(self) -> None:
        for low, high in [(0, 103.3), (12.85, 57.2), (-3, 0.002), (0, 1e-4), (5, 5)]:
            ticks = _nice_ticks(low, high)
            self.assertLessEqual(ticks[0], low)
            self.assertGreaterEqual(ticks[-1], high)
            self.assertLess(len(ticks), 12)


class RenderTest(unittest.TestCase):
    def test_units_get_separate_panels_and_nan_breaks_lines(self) -> None:
        frame = pd.DataFrame(
            {"Stock": [1.0, 2.0, 3.0], "Flow": [0.5, float("nan"), 0.7]},
            index=pd.Index([0.0, 1.0, 2.0], name="time"),
        )
        svg = render_results_svg(frame, {"Stock": "Widget", "Flow": "Widget/Month"}, "T & T")
        self.assertIn("T &amp; T", svg)
        self.assertIn(">Widget<", svg)
        self.assertIn(">Widget/Month<", svg)
        # Stock is one line; Flow has a gap, leaving two single-point segments.
        self.assertEqual(svg.count("<polyline"), 3)


class PlotResultsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        os.environ["VENSIM_PLE_MCP_CACHE"] = str(self.dir / "cache")
        self.model = generate_model(SPEC, self.dir / "market.mdl")

    def tearDown(self) -> None:
        os.environ.pop("VENSIM_PLE_MCP_CACHE", None)
        self.tmp.cleanup()

    def test_defaults_to_stocks_and_writes_svg(self) -> None:
        result = plot_results(self.model, self.dir / "chart")
        self.assertTrue(result["chart"].endswith("chart.svg"))
        self.assertIn("Active Users", result["variables"])
        svg = Path(result["chart"]).read_text()
        self.assertTrue(svg.startswith("<svg"))
        self.assertEqual(len(re.findall("<polyline", svg)), len(result["variables"]))


if __name__ == "__main__":
    unittest.main()
