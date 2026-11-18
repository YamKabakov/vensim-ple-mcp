from __future__ import annotations

import os
import re
from pathlib import Path
import shutil
import tempfile
import unittest

from vensim_ple_mcp.editor import MdlDocument, edit_model, find_references, scan_blocks
from vensim_ple_mcp.parser import parse_sketch
from vensim_ple_mcp.simulate import run_model


ROOT = Path(__file__).resolve().parents[1]
BASS = ROOT / "examples" / "bass_diffusion.mdl"

# Written the way Vensim PLE saves a model: no spaces around '=', INTEG over two lines,
# a quoted name, a .Control group, CRLF line endings and a pipe listed before its valve.
VENSIM_SAVED = """{UTF-8}
Stock A= INTEG (-"Out-flow",
\t\t100)
\t~\tWidget
\t~\t\t|

"Out-flow"=Stock A * Drain Rate
\t~\tWidget/Month
\t~\t\t|

Drain Rate=0.1
\t~\t1/Month
\t~\tFraction drained each month.
\t|

********************************************************
\t.Control
********************************************************~
\t\tSimulation Control Parameters
\t|

FINAL TIME  = 20
\t~\tMonth
\t~\tThe final time for the simulation.
\t|

INITIAL TIME  = 0
\t~\tMonth
\t~\tThe initial time for the simulation.
\t|

SAVEPER  =
        TIME STEP
\t~\tMonth [0,?]
\t~\tThe frequency with which output is stored.
\t|

TIME STEP  = 0.25
\t~\tMonth [0,?]
\t~\tThe time step for the simulation.
\t|

\\\\\\---/// Sketch information - do not modify anything except names
V300  Do not put anything below this section - it will be ignored
*View 1
$-1--1--1,0,|12||-1--1--1|-1--1--1|-1--1--1|-1--1--1|-1--1--1|96,96,90,2
10,1,Stock A,300,200,40,20,3,3,0,0,-1,0,0,0,0,0,0,0,0,0
1,2,4,1,100,0,0,22,0,192,0,-1--1--1,,1|(380,200)|
1,3,4,5,4,0,0,22,0,192,0,-1--1--1,,1|(500,200)|
11,4,0,450,200,6,8,34,3,0,0,1,0,0,0,0,0,0,0,0,0
10,6,"Out-flow",450,230,46,26,40,3,0,0,-1,0,0,0,0,0,0,0,0,0
12,5,48,560,200,10,8,0,3,0,0,-1,0,0,0,0,0,0,0,0,0
10,7,Drain Rate,450,320,46,26,8,3,0,0,-1,0,0,0,0,0,0,0,0,0
1,8,7,6,0,0,0,0,0,192,0,-1--1--1,,1|(0,0)|
1,9,1,6,0,0,0,0,0,192,0,-1--1--1,,1|(0,0)|
///---\\\\\\
:L<%^E!@
5:Time
"""


def arrows_between(path: Path) -> set[tuple[str, str]]:
    objects, arrows, _ = parse_sketch(path.read_text(encoding="utf-8"))
    names = {obj.object_id: obj.name for obj in objects}
    return {
        (names.get(a.source_id, ""), names.get(a.target_id, ""))
        for a in arrows
        if a.kind == "information"
    }


class EditorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = os.environ.get("VENSIM_PLE_MCP_CACHE")
        os.environ["VENSIM_PLE_MCP_CACHE"] = str(self.dir / "cache")
        self.model = self.dir / "bass.mdl"
        shutil.copyfile(BASS, self.model)

    def tearDown(self) -> None:
        if self.env is None:
            os.environ.pop("VENSIM_PLE_MCP_CACHE", None)
        else:
            os.environ["VENSIM_PLE_MCP_CACHE"] = self.env
        self.tmp.cleanup()

    def edit(self, operation: str, **kwargs) -> dict:
        result = edit_model(self.model, operation, **kwargs)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["validation"]["errors"], [], result)
        return result

    def test_references_prefer_the_longest_name(self) -> None:
        spans = find_references(
            "Adopters * Potential Adopters / Total_Population",
            ["Adopters", "Potential Adopters", "Total Population"],
        )
        self.assertEqual([name for *_, name in spans], ["Adopters", "Potential Adopters", "Total Population"])

    def test_set_equation_syncs_arrows_and_keeps_other_text(self) -> None:
        before = self.model.read_text(encoding="utf-8")
        result = self.edit(
            "set_equation",
            name="Adoption from Advertising",
            equation="Advertising Effectiveness * Total Population",
        )
        self.assertIn("removed arrow Potential Adopters -> Adoption from Advertising", result["changes"])
        self.assertIn("added arrow Total Population -> Adoption from Advertising", result["changes"])
        links = arrows_between(self.model)
        self.assertIn(("Total Population", "Adoption from Advertising"), links)
        self.assertNotIn(("Potential Adopters", "Adoption from Advertising"), links)
        after = self.model.read_text(encoding="utf-8")
        # Everything outside the edited block and the arrow records is unchanged.
        self.assertEqual(before.split("Adoption from Advertising =")[0], after.split("Adoption from Advertising =")[0])
        self.assertTrue(Path(result["backup"]).read_text(encoding="utf-8") == before)

    def test_add_constant_then_flow_updates_stock_and_simulates(self) -> None:
        baseline = run_model(self.model, variables=["Adopters"])["summary"]["Adopters"]["final"]
        self.edit("add_variable", name="Churn Rate", equation="0.1", unit="1/Year", kind="constant")
        result = self.edit(
            "add_variable",
            name="Churn",
            equation="Adopters * Churn Rate",
            unit="Person/Year",
            kind="flow",
            source="Adopters",
        )
        self.assertIn("Adopters: net flow is now Adoption Rate - Churn", result["changes"])
        objects, arrows, _ = parse_sketch(self.model.read_text(encoding="utf-8"))
        kinds = [obj.kind for obj in objects]
        self.assertEqual(kinds.count("valve"), 2)
        self.assertEqual(kinds.count("cloud"), 1)
        self.assertIn(("Churn Rate", "Churn"), arrows_between(self.model))
        self.assertIn(("Adopters", "Churn"), arrows_between(self.model))
        churned = run_model(self.model, variables=["Adopters"])["summary"]["Adopters"]["final"]
        self.assertLess(churned, baseline - 50)

    def test_new_variable_is_placed_without_overlap(self) -> None:
        self.edit("add_variable", name="Market Share", equation="Adopters / Total Population", unit="Dmnl")
        result = edit_model(self.model, "set_equation", name="Market Share")  # no-op
        self.assertTrue(result["ok"])
        from vensim_ple_mcp.parser import validate_model

        self.assertEqual(validate_model(self.model)["metrics"]["overlaps"], 0)

    def test_rename_leaves_longer_names_alone(self) -> None:
        result = self.edit("rename_variable", old="Adopters", new="Customers")
        text = self.model.read_text(encoding="utf-8")
        self.assertIn("Potential Adopters", text)
        self.assertNotRegex(text, r"(?<!Potential )Adopters\b")
        self.assertIn("renamed 1 diagram object(s) on view 'Main Model'", result["changes"])
        self.assertIn("Customers", run_model(self.model, variables=["Customers"])["summary"])

    def test_delete_refuses_used_variable_and_removes_flow(self) -> None:
        refused = edit_model(self.model, "delete_variable", name="Total Population")
        self.assertFalse(refused["ok"])
        self.assertIn("Adoption from Word of Mouth", refused["error"])
        self.assertEqual(self.model.read_bytes(), BASS.read_bytes())

        self.edit("add_variable", name="Churn", equation="Adopters * 0.1", unit="Person/Year", kind="flow", source="Adopters")
        self.edit("delete_variable", name="Churn")
        self.assertEqual(
            scan_blocks(self.model.read_text(encoding="utf-8"))[1].expression,
            "INTEG ( Adoption Rate, 0 )",
        )
        kinds = [obj.kind for obj in parse_sketch(self.model.read_text(encoding="utf-8"))[0]]
        self.assertEqual((kinds.count("valve"), kinds.count("cloud")), (1, 0))

    def test_flow_between_two_stocks_skips_the_stock_between_them(self) -> None:
        # Three stocks in a row: a flow from the last back to the first must not sit on
        # the middle one, and deleting it must clean both stocks' equations.
        document = MdlDocument(self.model.read_text(encoding="utf-8"))
        document.add_variable("Former Adopters", "INTEG(0, 0)", "Person", kind="level", x=836, y=276)
        document.add_variable(
            "Return", "Former Adopters * 0.1", "Person/Year", kind="flow",
            source="Former Adopters", target="Potential Adopters",
        )
        self.model.write_text(document.text(), encoding="utf-8")
        from vensim_ple_mcp.parser import validate_model

        self.assertEqual(validate_model(self.model)["metrics"]["overlaps"], 0)
        valve_y = [o.y for o in parse_sketch(document.text())[0] if o.kind == "valve"]
        self.assertNotEqual(valve_y[-1], 276)

        document.delete_variable("Return")
        expressions = {b.name: b.expression for b in scan_blocks(document.text())}
        self.assertEqual(expressions["Former Adopters"], "INTEG ( 0, 0 )")
        self.assertEqual(expressions["Potential Adopters"], "INTEG ( -Adoption Rate, Total Population )")

    def test_new_stock_is_moved_into_line_with_its_first_flow(self) -> None:
        # A stock added on its own lands wherever there is room; its first flow should
        # pull it straight below the connected stock instead of crossing the diagram.
        result = self.edit("add_variable", name="Former Adopters", equation="INTEG(0, 0)", unit="Person", kind="level")
        self.assertIn("added level Former Adopters = INTEG(0, 0)", result["changes"])
        result = self.edit(
            "add_variable", name="Churn", equation="Adopters * 0.1", unit="Person/Year",
            kind="flow", source="Adopters", target="Former Adopters",
        )
        self.assertTrue(any(c.startswith("moved Former Adopters") for c in result["changes"]), result)
        self.assertFalse(any("at an angle" in w for w in result["warnings"]), result)
        self.assertFalse(any("no clear path" in w for w in result["warnings"]), result)
        objects = parse_sketch(self.model.read_text(encoding="utf-8"))[0]
        stocks = {o.name: o for o in objects if o.kind == "level"}
        valve = [o for o in objects if o.kind == "valve"][-1]
        source, target = stocks["Adopters"], stocks["Former Adopters"]
        self.assertTrue(
            (source.x == valve.x == target.x) or (source.y == valve.y == target.y),
            (source, valve, target),
        )
        from vensim_ple_mcp.parser import validate_model

        self.assertEqual(validate_model(self.model)["metrics"]["overlaps"], 0)

    def test_constant_added_first_moves_next_to_its_first_user(self) -> None:
        self.edit("add_variable", name="Churn Rate", equation="0.1", unit="1/Year", kind="constant")
        result = self.edit(
            "add_variable", name="Churn", equation="Adopters * Churn Rate", unit="Person/Year",
            kind="flow", source="Adopters",
        )
        self.assertTrue(any(c.startswith("moved Churn Rate") for c in result["changes"]), result)
        objects = {o.name: o for o in parse_sketch(self.model.read_text(encoding="utf-8"))[0] if o.name}
        constant, flow = objects["Churn Rate"], objects["Churn"]
        self.assertLess(abs(constant.x - flow.x) + abs(constant.y - flow.y), 250)
        # A constant already wired to something stays where the modeller put it.
        result = self.edit(
            "add_variable", name="Churn Check", equation="Churn Rate * 2", unit="1/Year",
        )
        self.assertFalse(any(c.startswith("moved") for c in result["changes"]), result)

    def test_new_arrow_bends_around_objects_in_its_way(self) -> None:
        # Placed left of Potential Adopters, so a straight arrow from Adopters would run
        # through the valve, the flow label and the Potential Adopters stock.
        document = MdlDocument(self.model.read_text(encoding="utf-8"))
        document.add_variable("Adopter Share", "Adopters / Total Population", "Dmnl", x=60, y=276)
        view = document.views()[0]
        objects = view.by_id()
        share = view.instances("Adopter Share")[0]
        adopters = view.instances("Adopters")[0]
        arrow = next(a for a in view.arrows() if a.source == adopters.id and a.target == share.id)
        self.assertEqual(arrow.fields[4], "1", arrow.text())  # curved
        handle = tuple(int(v) for v in re.search(r"\((-?\d+),(-?\d+)\)", arrow.tail).groups())
        control = (2 * handle[0] - (adopters.x + share.x) / 2, 2 * handle[1] - (adopters.y + share.y) / 2)
        from vensim_ple_mcp.layout import _route_points

        path = _route_points((adopters.x, adopters.y), control, (share.x, share.y))[2:-2]
        blocker = view.instances("Potential Adopters")[0]
        bx, by = blocker.x, blocker.y
        hw, hh = blocker.half
        self.assertFalse(
            any(bx - hw <= x <= bx + hw and by - hh <= y <= by + hh for x, y in path), path
        )
        self.assertEqual(len(objects), len(view.by_id()))

        # An arrow with nothing in its way stays straight.
        document.set_equation("Adoption from Advertising", "Advertising Effectiveness * Total Population")
        view = document.views()[0]
        source = view.instances("Total Population")[0]
        target = view.instances("Adoption from Advertising")[0]
        arrow = next(a for a in view.arrows() if a.source == source.id and a.target == target.id)
        self.assertEqual(arrow.fields[4], "0", arrow.text())

    def test_connected_stocks_are_never_moved(self) -> None:
        before = {o.name: (o.x, o.y) for o in parse_sketch(self.model.read_text(encoding="utf-8"))[0] if o.kind == "level"}
        result = self.edit(
            "add_variable", name="Lapse", equation="Adopters * 0.1", unit="Person/Year",
            kind="flow", source="Adopters", target="Potential Adopters",
        )
        self.assertFalse(any(c.startswith("moved") for c in result["changes"]), result)
        after = {o.name: (o.x, o.y) for o in parse_sketch(self.model.read_text(encoding="utf-8"))[0] if o.kind == "level"}
        self.assertEqual(before, after)

    def test_added_constant_is_reported_as_constant(self) -> None:
        result = self.edit("add_variable", name="Churn Rate", equation="0.1", unit="1/Year", kind="constant")
        self.assertIn("added constant Churn Rate = 0.1", result["changes"])

    def test_unknown_name_suggests_close_matches(self) -> None:
        result = edit_model(self.model, "set_equation", name="Adoption Rat", equation="0")
        self.assertFalse(result["ok"])
        self.assertIn("Adoption Rate", result["error"])

    def test_vensim_saved_model_keeps_format(self) -> None:
        model = self.dir / "saved.mdl"
        model.write_bytes(VENSIM_SAVED.replace("\n", "\r\n").encode("utf-8"))
        result = edit_model(model, "rename_variable", old="Out-flow", new="Drain")
        self.assertTrue(result["ok"], result)
        result = edit_model(model, "set_equation", name="Drain Rate", equation="0.2")
        self.assertTrue(result["ok"], result)
        raw = model.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))  # still CRLF throughout
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8")
        self.assertIn('Stock A= INTEG (-Drain,\r\n\t\t100)', text)  # untouched formatting
        self.assertIn("Drain Rate = 0.2\r\n\t~\t1/Month\r\n\t~\tFraction drained each month.", text)
        self.assertIn("********************************************************~", text)
        self.assertIn("10,6,Drain,450,230", text)
        summary = run_model(model, variables=["Stock A"])["summary"]["Stock A"]
        self.assertLess(summary["final"], 5)

    def test_add_to_vensim_saved_model_goes_before_control_group(self) -> None:
        document = MdlDocument(VENSIM_SAVED)
        document.add_variable("Half Life", "LN(2) / Drain Rate", "Month")
        text = document.text()
        self.assertLess(text.index("Half Life ="), text.index(".Control"))
        self.assertIn("added arrow Drain Rate -> Half Life", document.changes)


if __name__ == "__main__":
    unittest.main()
