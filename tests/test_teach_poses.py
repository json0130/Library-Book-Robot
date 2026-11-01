"""tools/teach_poses.py: recording poses into shelves.csv and floorplan.yaml with a backup, keeping comments and layout."""

import shutil
import tempfile
import unittest
from pathlib import Path

from robot.config import ROOT
from robot.services.floorplan import FloorPlan
from tools.teach_poses import TeachError, Teacher, update_floorplan, update_shelves


class TeachTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.shelves, self.plan = self.dir / "shelves.csv", self.dir / "floorplan.yaml"
        shutil.copy(ROOT / "data" / "shelves.csv", self.shelves)
        shutil.copy(ROOT / "data" / "floorplan.yaml", self.plan)
        self.pose = [(3.1, 2.2, 0.5, True)]
        self.checked = []
        self.t = Teacher(lambda: self.pose[0], self.shelves, self.plan, 3, check=lambda: self.checked.append(1) or 0,
                         clock=lambda: 1.7e9)

    def at(self, x, y, yaw=0.0, localised=True):
        self.pose[0] = (x, y, yaw, localised)


class ShelvesTest(TeachTest):
    def test_a_shelf_gets_the_live_pose_and_loses_its_placeholder_note(self):
        self.t.command("shelf 3-SCI-A")
        self.t.command("save")
        rows = {l.split(",")[0]: l for l in self.shelves.read_text().splitlines() if l and not l.startswith("#")}
        self.assertIn("3-SCI-A,Science,3,quiet,3.10,2.20,0.50,Astronomy and physics.", rows["3-SCI-A"])
        self.assertNotIn("TODO", rows["3-SCI-A"])
        self.assertIn("TODO", rows["3-HIS-A"])                                      # the others are left alone
        self.assertIn("4.0,1.0,3.14", (ROOT / "data" / "shelves.csv").read_text())    # (and the repo's file was not touched)

    def test_comments_the_order_and_the_other_rows_stay_exactly_as_they_were(self):
        before = self.shelves.read_text().splitlines()
        self.t.command("shelf 3-HIS-A")
        self.t.command("save")
        after = self.shelves.read_text().splitlines()
        self.assertEqual(len(before), len(after))
        changed = [(a, b) for a, b in zip(before, after) if a != b]
        self.assertEqual(len(changed), 1)
        self.assertTrue(changed[0][0].startswith("3-HIS-A,"))
        self.assertEqual(before[:4], after[:4])                                      # the comment block

    def test_a_new_shelf_needs_its_section_and_takes_the_floor_and_zone_of_its_neighbours(self):
        with self.assertRaises(TeachError) as caught:
            self.t.command("shelf 3-SCI-B")
        self.assertIn("section", str(caught.exception))
        self.t.command("shelf 3-SCI-B Science")
        self.t.command("save")
        row = [l for l in self.shelves.read_text().splitlines() if l.startswith("3-SCI-B")][0]
        self.assertEqual(row.split(",")[:7], ["3-SCI-B", "Science", "3", "quiet", "3.10", "2.20", "0.50"])

    def test_the_result_still_loads_as_a_shelves_file(self):
        from robot.services.book_graph import BookGraph
        self.t.command("shelf 3-SCI-A")
        self.t.command("shelf 3-SCI-B Science")
        self.t.command("save")
        graph = BookGraph.from_files(ROOT / "data" / "catalog.csv", ROOT / "data" / "topics.yaml", self.shelves, ROOT / "data" / "curated_links.csv")
        self.assertEqual(graph.shelves["3-SCI-A"].map_x, 3.1)
        self.assertIn("3-SCI-B", graph.shelves)

    def test_updating_an_unknown_shelf_is_an_error(self):
        with self.assertRaises(TeachError):
            update_shelves(self.shelves.read_text(), {"9-NOPE": (1, 2, 3)}, {})


class FloorPlanTest(TeachTest):
    def test_a_section_needs_two_corners_and_keeps_the_rest_of_its_line(self):
        self.t.command("section Science corner1")
        self.assertIn("only one corner", self.t.command("list"))
        self.at(5.0, 3.0)
        self.t.command("section Science corner2")
        self.t.command("save")
        text = self.plan.read_text()
        self.assertIn("- {name: Science, x: 3.10, y: 2.20, w: 1.90, h: 0.80}", text)
        self.assertEqual(FloorPlan.load(self.plan).floor(3).section("Science").w, 1.9)
        self.assertEqual(text.count("name: History"), 1)

    def test_a_section_with_one_corner_is_not_saved(self):
        self.t.command("section Science corner1")
        self.assertIn("nothing to save", self.t.command("save"))
        self.assertEqual(self.plan.read_text(), (ROOT / "data" / "floorplan.yaml").read_text())

    def test_a_new_section_is_added_to_the_end_of_the_floors_list(self):
        self.t.command("section Maths corner1")
        self.at(9.0, 3.0)
        self.t.command("section Maths corner2")
        self.t.command("save")
        plan = FloorPlan.load(self.plan)
        self.assertEqual([s.name for s in plan.floor(3).sections], ["Science", "History", "Computing", "Maths"])
        self.assertIsNone(plan.floor(2).section("Maths"))

    def test_a_section_keeps_its_zone(self):
        text = update_floorplan(self.plan.read_text().replace("name: Science,", "name: Science, zone: quiet,").replace(
            "{name: Science, zone: quiet, x: 3.1, y: 0.3, w: 1.9, h: 2.7}", "{name: Science, x: 3.1, y: 0.3, w: 1.9, h: 2.7, zone: quiet}"), 3, {"Science": (3, 1, 2, 2)})
        self.assertIn("{name: Science, x: 3.00, y: 1.00, w: 2.00, h: 2.00, zone: quiet}", text)

    def test_landmarks_are_added_or_moved_by_kind_and_name(self):
        self.at(9.1, 3.1)
        self.t.command("landmark toilets Toilets")
        self.t.command("landmark other Cafe")
        self.t.command("save")
        plan = FloorPlan.load(self.plan)
        toilets = [m for m in plan.floor(3).landmarks if m.kind == "toilets"]
        self.assertEqual([(m.name, m.x, m.y) for m in toilets], [("Toilets", 9.1, 3.1)])      # moved, not added twice
        self.assertEqual([m.name for m in plan.floor(3).landmarks if m.kind == "other"], ["Cafe"])
        self.assertEqual(self.plan.read_text().count("kind: lift"), 3)

    def test_the_station_is_written_with_the_floor(self):
        self.at(8.0, 4.0, 1.5)
        self.t.command("station")
        self.t.command("save")
        station = FloorPlan.load(self.plan).station
        self.assertEqual((station.level, station.x, station.y, station.yaw), (3, 8.0, 4.0, 1.5))

    def test_the_comments_and_layout_of_the_file_stay(self):
        before = self.plan.read_text()
        self.at(9.1, 3.1)
        self.t.command("landmark other Cafe")
        self.t.command("save")
        after = self.plan.read_text()
        self.assertEqual([l for l in before.splitlines() if l.startswith("#")], [l for l in after.splitlines() if l.startswith("#")])
        self.assertEqual(len(after.splitlines()), len(before.splitlines()) + 1)

    def test_a_floor_that_is_not_in_the_plan_is_an_error(self):
        with self.assertRaises(TeachError) as caught:
            update_floorplan(self.plan.read_text(), 9, landmarks=[("other", "Cafe", 1, 1)])
        self.assertIn("level 9", str(caught.exception))

    def test_a_point_that_makes_the_plan_invalid_puts_the_old_file_back(self):
        self.at(50.0, 3.0)                                                          # outside the 12 x 8 m floor
        self.t.command("landmark other Cafe")
        before = self.plan.read_text()
        with self.assertRaises(TeachError) as caught:
            self.t.command("save")
        self.assertIn("put back", str(caught.exception))
        self.assertEqual(self.plan.read_text(), before)


class SafetyTest(TeachTest):
    def test_nothing_is_recorded_while_the_base_is_not_localised(self):
        self.at(3.0, 3.0, localised=False)
        for command in ("shelf 3-SCI-A", "section Science corner1", "landmark other Cafe", "station"):
            with self.assertRaises(TeachError) as caught:
                self.t.command(command)
            self.assertIn("not localised", str(caught.exception))
        self.assertEqual(self.t.pending(), "(nothing waiting)")

    def test_an_unreachable_base_is_an_error_not_a_crash(self):
        def broken():
            raise RuntimeError("timed out")
        t = Teacher(broken, self.shelves, self.plan, 3)
        with self.assertRaises(TeachError):
            t.command("shelf 3-SCI-A")
        self.assertIn("no pose", t.describe())

    def test_nothing_is_written_until_save_and_discard_forgets(self):
        before = self.shelves.read_text()
        self.t.command("shelf 3-SCI-A")
        self.assertEqual(self.shelves.read_text(), before)
        self.assertIn("3-SCI-A", self.t.command("list"))
        self.t.command("discard")
        self.assertEqual(self.t.command("list"), "(nothing waiting)")

    def test_each_save_makes_a_timestamped_backup_of_the_old_file_and_runs_the_check(self):
        old = self.shelves.read_text()
        self.t.command("shelf 3-SCI-A")
        report = self.t.command("save")
        backups = sorted((self.dir / "backups").glob("shelves.csv.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0].name, r"^shelves\.csv\.\d{8}-\d{6}\.bak$")
        self.assertEqual(backups[0].read_text(), old)
        self.assertEqual(self.checked, [1])
        self.assertIn("catalog_check exit code 0", report)
        self.t.command("shelf 3-HIS-A")
        self.t.command("save")
        self.assertEqual(len(list((self.dir / "backups").glob("shelves.csv.*.bak"))), 2)     # never overwritten

    def test_unknown_commands_say_what_there_is(self):
        with self.assertRaises(TeachError) as caught:
            self.t.command("teleport")
        self.assertIn("shelf section landmark station", str(caught.exception))
        with self.assertRaises(TeachError):
            self.t.command("landmark castle Keep")
        with self.assertRaises(TeachError):
            self.t.command("section Science corner3")


class WithTheRealCheckTest(TeachTest):
    def test_the_catalogue_check_passes_after_teaching_a_shelf_inside_its_section(self):
        from tools.catalog_check import main as check
        self.at(4.0, 1.5, 3.14)
        t = Teacher(lambda: self.pose[0], self.shelves, self.plan, 3,
                    check=lambda: check(["--shelves", str(self.shelves), "--floorplan", str(self.plan)]))
        t.command("shelf 3-SCI-A")
        report = t.command("save")
        self.assertIn("exit code 0", report)


if __name__ == "__main__":
    unittest.main()
