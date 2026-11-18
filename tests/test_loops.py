from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest

from vensim_ple_mcp.checks import check_equations
from vensim_ple_mcp.editor import MdlDocument
from vensim_ple_mcp.loops import _Parser, _Signs, analyse, annotate, find_loops, table_sign
from vensim_ple_mcp.parser import parse_sketch
from vensim_ple_mcp.simulate import run_model


ROOT = Path(__file__).resolve().parents[1]
BASS = ROOT / "examples" / "bass_diffusion.mdl"
MARKET = ROOT / "examples" / "market_adoption.mdl"


def sign(expression: str, key: str, constants: dict | None = None, lookups: dict | None = None) -> str:
    return _Signs(constants or {}, lookups or {}).derivative(_Parser(expression).parse(), key)


def loop_set(result: dict) -> set[tuple[str, tuple[str, ...]]]:
    return {(loop["id"][0], tuple(loop["variables"])) for loop in result["loops"]}


class LinkPolarityTest(unittest.TestCase):
    def test_arithmetic(self) -> None:
        self.assertEqual(sign("a * b", "a"), "+")
        self.assertEqual(sign("a / b", "b"), "-")
        self.assertEqual(sign("c - a", "a"), "-")
        self.assertEqual(sign("-(a - c)", "a"), "-")
        self.assertEqual(sign("a * (1 - b)", "b"), "-")
        self.assertEqual(sign("b ^ 2", "b"), "+")
        self.assertEqual(sign("a * k", "a", constants={"k": -0.5}), "-")
        self.assertEqual(sign("a - a * 2", "a"), "?")  # opposing terms can't be decided
        self.assertEqual(sign("b * 3", "a"), "0")

    def test_functions(self) -> None:
        self.assertEqual(sign("MAX(0, c - a)", "a"), "-")
        self.assertEqual(sign("SMOOTH(a, t)", "a"), "+")
        self.assertEqual(sign("SMOOTH(a, t)", "t"), "?")
        self.assertEqual(sign("IF THEN ELSE(Time > 5, a, 0)", "a"), "+")
        self.assertEqual(sign("IF THEN ELSE(a > 5, 1, 0)", "a"), "?")
        self.assertEqual(sign("ZIDZ(c, a)", "a"), "-")
        self.assertEqual(sign("INITIAL(a)", "a"), "0")
        self.assertEqual(sign("EXP(-a)", "a"), "-")

    def test_lookups(self) -> None:
        self.assertEqual(table_sign([(0, 1), (1, 0.6), (2, 0.2)]), "-")
        self.assertEqual(table_sign([(0, 0), (1, 1), (2, 0.5)]), "?")
        self.assertEqual(sign("Effect(a)", "a", lookups={"effect": "-"}), "-")
        self.assertEqual(sign("WITH LOOKUP(a, ([(0,0)-(2,2)],(0,0),(1,1),(2,2)))", "a"), "+")


class LoopAnalysisTest(unittest.TestCase):
    def test_bass_loops(self) -> None:
        result = analyse(MdlDocument(BASS.read_text(encoding="utf-8")))
        self.assertEqual(result["summary"]["reinforcing"], 1)
        self.assertEqual(result["summary"]["balancing"], 2)
        self.assertEqual(
            loop_set(result),
            {
                ("R", ("Adopters", "Adoption from Word of Mouth", "Adoption Rate")),
                ("B", ("Potential Adopters", "Adoption from Advertising", "Adoption Rate")),
                ("B", ("Potential Adopters", "Adoption from Word of Mouth", "Adoption Rate")),
            },
        )
        self.assertEqual(result["algebraic_loops"], [])

    def test_market_loops_include_a_two_link_outflow_loop(self) -> None:
        result = analyse(MdlDocument(MARKET.read_text(encoding="utf-8")))
        self.assertIn(("B", ("Active Users", "Attrition")), loop_set(result))
        self.assertEqual(result["summary"]["unknown"], 0)

    def test_self_loop_and_lookup_loop(self) -> None:
        document = MdlDocument(BASS.read_text(encoding="utf-8"))
        document.set_equation("Adopters", "INTEG(Adoption Rate - Adopters * 0.1, 0)")
        result = analyse(document)
        self.assertIn(("B", ("Adopters",)), loop_set(result))

    def test_bounded_factors_keep_polarity_known(self) -> None:
        # 1 - Distancing is positive because Distancing = MIN(0.9, ...); without range
        # analysis the Adopters -> Word of Mouth link (and its loop) would be unknown.
        document = MdlDocument(BASS.read_text(encoding="utf-8"))
        document.add_variable("Distancing", "MIN(0.9, Adopters / Total Population)", "Dmnl")
        document.set_equation(
            "Adoption from Word of Mouth",
            "Word of Mouth Strength * (1 - Distancing) * Adopters * Potential Adopters / Total Population",
        )
        result = analyse(document)
        self.assertEqual(result["summary"]["unknown"], 0, result["loops"])
        self.assertIn(("B", ("Adopters", "Distancing", "Adoption from Word of Mouth", "Adoption Rate")), loop_set(result))

    def test_if_then_else_condition_makes_polarity_unknown(self) -> None:
        document = MdlDocument(BASS.read_text(encoding="utf-8"))
        document.set_equation(
            "Adoption from Advertising",
            "IF THEN ELSE(Adopters > 500, 0, Advertising Effectiveness * Potential Adopters)",
        )
        result = analyse(document)
        unknown = [loop for loop in result["loops"] if loop["polarity"] == "unknown"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["id"], "U1")
        self.assertEqual(unknown[0]["undetermined_links"], ["Adopters -> Adoption from Advertising"])


class AlgebraicLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = MdlDocument(BASS.read_text(encoding="utf-8"))
        self.document.add_variable("Pressure", "Adoption Rate * 0.01", "Dmnl")
        self.document.set_equation(
            "Adoption from Advertising",
            "Advertising Effectiveness * Potential Adopters * (1 + Pressure)",
        )

    def test_loop_without_stock_is_an_error(self) -> None:
        errors = [i for i in check_equations(self.document.text()) if i["check"] == "algebraic-loop"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Adoption from Advertising, Adoption Rate, Pressure", errors[0]["message"])

    def test_smooth_still_needs_its_input_at_the_start(self) -> None:
        # SMOOTH starts equal to its input, so it breaks the loop while running but not
        # at the initial time; SMOOTHI with an explicit initial value breaks both.
        self.document.set_equation("Pressure", "SMOOTH(Adoption Rate, 2) * 0.01")
        checks = [i["check"] for i in check_equations(self.document.text())]
        self.assertIn("initialization-loop", checks)
        self.document.set_equation("Pressure", "SMOOTHI(Adoption Rate, 2, 0) * 0.01")
        checks = [i["check"] for i in check_equations(self.document.text())]
        self.assertNotIn("initialization-loop", checks)
        self.assertNotIn("algebraic-loop", checks)

    def test_a_delay_breaks_the_loop(self) -> None:
        self.document.set_equation("Pressure", "SMOOTHI(Adoption Rate, 2, 0) * 0.01")
        self.assertEqual(
            [i for i in check_equations(self.document.text()) if i["check"] == "algebraic-loop"], []
        )


class AnnotateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.environ.get("VENSIM_PLE_MCP_CACHE")
        os.environ["VENSIM_PLE_MCP_CACHE"] = str(Path(self.tmp.name) / "cache")
        self.model = Path(self.tmp.name) / "bass.mdl"
        shutil.copyfile(BASS, self.model)

    def tearDown(self) -> None:
        if self.env is None:
            os.environ.pop("VENSIM_PLE_MCP_CACHE", None)
        else:
            os.environ["VENSIM_PLE_MCP_CACHE"] = self.env
        self.tmp.cleanup()

    def test_annotate_labels_loops_and_is_repeatable(self) -> None:
        # Clear the polarities the generator wrote, so annotation has to restore them.
        text = (
            self.model.read_text(encoding="utf-8")
            .replace(",0,0,43,0,0,192,", ",0,0,0,0,0,192,")
            .replace(",0,0,45,0,0,192,", ",0,0,0,0,0,192,")
        )
        self.model.write_text(text, encoding="utf-8")
        result = find_loops(self.model, annotate_diagram=True)
        annotation = result["annotation"]
        self.assertTrue(annotation["ok"], annotation)
        self.assertEqual(annotation["validation"]["errors"], [])
        self.assertIn("placed 3 loop label(s)", annotation["changes"])
        objects, arrows, _ = parse_sketch(self.model.read_text(encoding="utf-8"))
        labels = sorted(obj.name for obj in objects if obj.kind == "comment" and obj.name != "Bass Diffusion")
        self.assertEqual(labels, ["B1", "B2", "R1"])
        names = {obj.object_id: obj.name for obj in objects}
        signs = {
            (names[a.source_id], names[a.target_id]): a.polarity
            for a in arrows
            if a.kind == "information"
        }
        self.assertEqual(signs[("Potential Adopters", "Adoption from Advertising")], "+")
        self.assertEqual(signs[("Total Population", "Adoption from Word of Mouth")], "-")
        # An initial value is not a causal link, so it gets no sign.
        self.assertEqual(signs[("Total Population", "Potential Adopters")], "")

        find_loops(self.model, annotate_diagram=True)
        objects, _, _ = parse_sketch(self.model.read_text(encoding="utf-8"))
        self.assertEqual(sum(obj.kind == "comment" for obj in objects), 4)  # title + 3 labels
        self.assertIn("Adopters", run_model(self.model, variables=["Adopters"])["summary"])

    def test_annotate_on_document(self) -> None:
        document = MdlDocument(self.model.read_text(encoding="utf-8"))
        annotate(document, max_labels=1)
        self.assertIn("placed 1 loop label(s)", document.changes)
        self.assertTrue(any("first 1 of 3 loops" in w for w in document.warnings))


if __name__ == "__main__":
    unittest.main()
