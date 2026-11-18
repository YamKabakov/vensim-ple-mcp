"""End-to-end tests that talk to the MCP server over stdio, the way Claude does."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
BASS = ROOT / "examples" / "bass_diffusion.mdl"
SPEC = ROOT / "examples" / "market_adoption.json"

EXPECTED_TOOLS = {
    "vensim_doctor",
    "generate_vensim_model",
    "inspect_vensim_model",
    "validate_vensim_model",
    "render_vensim_preview",
    "open_vensim_model",
    "create_vensim_dss_script",
    "list_model_parameters",
    "run_vensim_model",
    "sweep_model_parameter",
}


class ServerStdioTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        env = dict(os.environ)
        env["VENSIM_PLE_MCP_CACHE"] = str(self.dir / "cache")
        env["PYTHONWARNINGS"] = "ignore"
        self.params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "vensim_ple_mcp.server"],
            env=env,
            cwd=str(ROOT),
        )
        # The server logs every request to stderr; keep test output readable.
        self.errlog = open(os.devnull, "w")

    def tearDown(self) -> None:
        self.errlog.close()
        self.tmp.cleanup()

    async def _call(self, session: ClientSession, tool: str, args: dict) -> dict:
        result = await session.call_tool(tool, args)
        self.assertFalse(result.isError, result.content)
        return json.loads(result.content[0].text)

    async def test_server_end_to_end(self) -> None:
        # One server process for all checks: startup and the PySD import dominate the cost.
        async with stdio_client(self.params, errlog=self.errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                with self.subTest("lists every tool"):
                    tools = await session.list_tools()
                    self.assertEqual({t.name for t in tools.tools}, EXPECTED_TOOLS)

                with self.subTest("list_model_parameters"):
                    info = await self._call(
                        session, "list_model_parameters", {"model_path": str(BASS)}
                    )
                    names = {c["name"] for c in info["constants"]}
                    self.assertIn("Word of Mouth Strength", names)

                with self.subTest("run_vensim_model with override"):
                    base = await self._call(
                        session, "run_vensim_model", {"model_path": str(BASS)}
                    )
                    strong = await self._call(
                        session,
                        "run_vensim_model",
                        {
                            "model_path": str(BASS),
                            "overrides": {"Word of Mouth Strength": 0.6},
                            "variables": ["Adoption Rate"],
                        },
                    )
                    self.assertEqual(set(strong["summary"]), {"Adoption Rate"})
                    # Stronger word of mouth peaks higher and earlier.
                    b, s = base["summary"]["Adoption Rate"], strong["summary"]["Adoption Rate"]
                    self.assertGreater(s["max"], b["max"])
                    self.assertLess(s["time_of_max"], b["time_of_max"])

                with self.subTest("sweep_model_parameter"):
                    sweep = await self._call(
                        session,
                        "sweep_model_parameter",
                        {
                            "model_path": str(BASS),
                            "parameter": "Word of Mouth Strength",
                            "values": [0.2, 0.6],
                            "variables": ["Adoption Rate"],
                        },
                    )
                    self.assertEqual([r["value"] for r in sweep["runs"]], [0.2, 0.6])

                with self.subTest("generate, validate and render"):
                    mdl = self.dir / "market.mdl"
                    svg = self.dir / "market.svg"
                    await self._call(
                        session,
                        "generate_vensim_model",
                        {"spec_json": SPEC.read_text(), "output_path": str(mdl)},
                    )
                    self.assertTrue(mdl.exists())
                    await self._call(session, "validate_vensim_model", {"model_path": str(mdl)})
                    await self._call(
                        session,
                        "render_vensim_preview",
                        {"model_path": str(mdl), "output_path": str(svg)},
                    )
                    self.assertIn("<svg", svg.read_text())

                with self.subTest("missing model is reported as a tool error"):
                    result = await session.call_tool(
                        "run_vensim_model", {"model_path": str(self.dir / "nope.mdl")}
                    )
                    self.assertTrue(result.isError)


if __name__ == "__main__":
    unittest.main()
