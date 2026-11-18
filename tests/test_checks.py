from __future__ import annotations

from pathlib import Path
import unittest

from vensim_ple_mcp.checks import check_equations, format_unit, parse_unit
from vensim_ple_mcp.editor import MdlDocument


ROOT = Path(__file__).resolve().parents[1]
BASS = (ROOT / "examples" / "bass_diffusion.mdl").read_text(encoding="utf-8")
MARKET = (ROOT / "examples" / "market_adoption.mdl").read_text(encoding="utf-8")


def issues(text: str, check: str | None = None) -> list[dict]:
    return [i for i in check_equations(text) if check is None or i["check"] == check]


def replace_equation(text: str, name: str, equation: str, unit: str | None = None) -> str:
    document = MdlDocument(text)
    document.set_equation(name, equation, unit, sync_links=False)
    return document.text()


class UnitParsingTest(unittest.TestCase):
    def test_parse_and_format(self) -> None:
        self.assertEqual(format_unit(parse_unit("Person/Year")), "person/year")
        self.assertEqual(parse_unit("1/Months"), parse_unit("1/Month"))
        self.assertEqual(parse_unit("Dmnl"), {})
        self.assertEqual(parse_unit("Month [0,?]"), parse_unit("Month"))
        self.assertEqual(
            parse_unit("Person/(Person*Year)"), parse_unit("1/Year")
        )
        self.assertEqual(format_unit(parse_unit("$/(Widget*Month)")), "$/(month*widget)")
        self.assertIsNone(parse_unit("???"))
        self.assertIsNone(parse_unit(""))


class EquationChecksTest(unittest.TestCase):
    def test_examples_are_clean(self) -> None:
        self.assertEqual(issues(BASS), [])
        self.assertEqual(issues(MARKET), [])

    def test_undefined_reference_is_an_error(self) -> None:
        text = replace_equation(BASS, "Adoption from Advertising", "Advertising Efectiveness * Potential Adopters")
        found = issues(text, "undefined")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "error")
        self.assertIn("Advertising Efectiveness", found[0]["message"])

    def test_unknown_function_is_an_error_but_vensim_functions_are_not(self) -> None:
        text = replace_equation(BASS, "Adoption from Advertising", "SMOOTHER(Potential Adopters, 2) * Advertising Effectiveness")
        self.assertEqual([i["check"] for i in issues(text) if i["severity"] == "error"], ["function"])
        text = replace_equation(
            BASS,
            "Adoption from Advertising",
            "IF THEN ELSE(Time > 5, Advertising Effectiveness * Potential Adopters, 0) + "
            "MAX(0, STEP(Total Population, 3) * Advertising Effectiveness * 0)",
        )
        self.assertEqual([i for i in issues(text) if i["severity"] == "error"], [])

    def test_unit_mismatch_in_addition(self) -> None:
        text = replace_equation(BASS, "Adoption Rate", "Adoption from Advertising + Total Population")
        found = issues(text, "units")
        self.assertTrue(any("person/year and person" in i["message"] for i in found), found)
        # The message names the variable, since validate_model reports messages alone.
        self.assertTrue(any(i["message"].startswith("Adoption Rate: adds") for i in found), found)

    def test_equation_units_differ_from_declared(self) -> None:
        text = replace_equation(BASS, "Adoption from Advertising", "Advertising Effectiveness * Potential Adopters", unit="Person")
        messages = [i["message"] for i in issues(text, "units")]
        self.assertIn(
            "Adoption from Advertising works out to person/year, but person is expected", messages
        )
        # The wrong declared unit also shows up where the variable is used.
        self.assertTrue(any("adds quantities in different units" in m for m in messages), messages)

    def test_stock_net_flow_units(self) -> None:
        text = replace_equation(BASS, "Adopters", "INTEG(Adoption Rate * 2, 0)", unit="Person/Year")
        found = issues(text, "units")
        self.assertTrue(any("net flow works out to person/year" in i["message"] for i in found), found)

    def test_delay_time_must_be_in_time_units(self) -> None:
        text = replace_equation(BASS, "Adoption from Advertising", "SMOOTH(Advertising Effectiveness * Potential Adopters, Total Population)")
        self.assertTrue(any("SMOOTH delay time" in i["message"] for i in issues(text, "units")))

    def test_numbers_fit_any_units(self) -> None:
        text = replace_equation(BASS, "Adoption Rate", "Adoption from Advertising + Adoption from Word of Mouth + 0")
        self.assertEqual(issues(text, "units"), [])

    def test_arrow_without_use_and_use_without_arrow(self) -> None:
        # Drop Potential Adopters from the equation but keep its arrow, and use
        # Word of Mouth Strength without drawing one.
        text = replace_equation(BASS, "Adoption from Advertising", "Advertising Effectiveness * Word of Mouth Strength * Total Population")
        messages = [i["message"] for i in issues(text, "arrow")]
        self.assertTrue(any("arrow from Potential Adopters to Adoption from Advertising" in m for m in messages), messages)
        self.assertTrue(any("uses Word of Mouth Strength, but no arrow" in m for m in messages), messages)

    def test_stock_flow_mismatches(self) -> None:
        missing = replace_equation(BASS, "Adopters", "INTEG(0, 0)")
        self.assertTrue(any("flows into Adopters on the diagram but is not in its net flow" in i["message"] for i in issues(missing, "stock-flow")))
        flipped = replace_equation(BASS, "Adopters", "INTEG(-Adoption Rate, 0)")
        self.assertTrue(any("is subtracted" in i["message"] for i in issues(flipped, "stock-flow")))
        unpiped = replace_equation(BASS, "Adopters", "INTEG(Adoption Rate + Adoption from Advertising, 0)", unit="Person")
        self.assertTrue(any("has no pipe" in i["message"] for i in issues(unpiped, "stock-flow")))

    def test_quoted_flow_names_match_their_pipes(self) -> None:
        document = MdlDocument(BASS)
        document.rename_variable("Adoption Rate", "Adoption-Rate")
        self.assertEqual(issues(document.text(), "stock-flow"), [])

    def test_unused_constant(self) -> None:
        document = MdlDocument(BASS)
        document.add_variable("Spare Knob", "3", "Dmnl", kind="constant")
        found = issues(document.text(), "unused")
        self.assertEqual([i["variable"] for i in found], ["Spare Knob"])


if __name__ == "__main__":
    unittest.main()
