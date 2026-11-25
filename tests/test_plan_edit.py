"""display/plan_edit.py: what the floor-plan editor saves (keep-out zones and sections), and what it refuses."""

import shutil
import tempfile
import unittest
from pathlib import Path

from display.plan_edit import EditError, clean_rects, clean_sections, save_edits
from display.server import allowed

FLOORPLAN = """# floor plan comment
floors:
  - level: 1
    name: Ground floor
    x0: -3.9
    y0: -4.0
    width: 13.4
    height: 11.5
    base_map: lab_lidar
    sections:
      - {name: Fiction, x: 0.0, y: 3.5, w: 2.0, h: 1.5, zone: quiet}   # keeps its zone
      - {name: Reading Room, x: 4.0, y: 0.0, w: 2.0, h: 2.0}
    landmarks:
      - {kind: lift, name: Lift, x: -3.0, y: 1.5}
station:
  level: 1
  x: 0.0
  y: 0.0
  yaw: 0.0
"""
SHELVES = """# comment line
shelf,section,floor,zone,map_x,map_y,map_yaw,description
1-FIC-A,Fiction,1,common,0.52,4.42,-0.04,"Fiction"
"""
KEEPOUT = """# keep-out comment
clearance_m: 0.25

zones:   # note
  - {name: desk1, rect: [1.0, 1.0, 2.0, 3.0]}
  - {name: corner, polygon: [[4.0, 2.0], [5.0, 2.0], [5.0, 3.0]]}
#  - {name: example, rect: [1, 1, 2, 2]}
"""
SECTIONS = [{"name": "Fiction", "x": 0.0, "y": 3.5, "w": 2.0, "h": 1.5}, {"name": "Reading Room", "x": 4.0, "y": 0.0, "w": 2.0, "h": 2.0}]
DESK1 = {"name": "desk1", "rect": [1.0, 1.0, 2.0, 3.0]}


def make_root():
    root = Path(tempfile.mkdtemp())
    (root / "data").mkdir()
    (root / "turtlebot" / "config").mkdir(parents=True)
    (root / "data" / "floorplan.yaml").write_text(FLOORPLAN)
    (root / "data" / "shelves.csv").write_text(SHELVES)
    (root / "turtlebot" / "config" / "keepout.yaml").write_text(KEEPOUT)
    return root


class SaveEditsTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.plan = self.root / "data" / "floorplan.yaml"
        self.keep = self.root / "turtlebot" / "config" / "keepout.yaml"

    def test_saving_the_same_again_writes_nothing(self):
        save_edits(self.root, [DESK1], SECTIONS)                     # the first save may tidy the numbers' layout
        before = (self.keep.read_text(), self.plan.read_text(), len(list((self.root / "data" / "backups").glob("*.bak"))))
        r = save_edits(self.root, [DESK1], SECTIONS)
        self.assertEqual(r["changed"], [])
        self.assertEqual((self.keep.read_text(), self.plan.read_text(), len(list((self.root / "data" / "backups").glob("*.bak")))), before)

    def test_a_new_zone_a_moved_zone_and_a_new_section_are_written_with_backups_and_the_comments_stay(self):
        r = save_edits(self.root, [{"name": "desk1", "rect": [1.0, 1.0, 2.5, 3.0]}, {"name": "chair1", "rect": [3.0, -2.0, 3.6, -1.2]}],
                       SECTIONS + [{"name": "Study 2", "x": -3.0, "y": -3.0, "w": 1.5, "h": 1.0}])
        self.assertEqual(sorted(r["changed"]), ["floorplan.yaml", "keepout.yaml"])
        keep = self.keep.read_text()
        self.assertIn("{name: desk1, rect: [1.00, 1.00, 2.50, 3.00]}", keep)
        self.assertIn("{name: chair1, rect: [3.00, -2.00, 3.60, -1.20]}", keep)
        self.assertIn("polygon: [[4.0, 2.0], [5.0, 2.0], [5.0, 3.0]]", keep)       # the polygon is left alone
        self.assertIn("# keep-out comment", keep)
        self.assertIn("#  - {name: example", keep)
        plan = self.plan.read_text()
        self.assertIn("Study 2", plan)
        self.assertIn("zone: quiet", plan)                                          # a section's own zone is kept
        self.assertIn("# floor plan comment", plan)
        self.assertEqual(len(list((self.root / "data" / "backups").glob("*.bak"))), 2)

    def test_a_list_left_out_leaves_that_file_alone(self):
        r = save_edits(self.root, None, [{"name": "Fiction", "x": -1.0, "y": 3.0, "w": 3.0, "h": 3.0}, SECTIONS[1]])
        self.assertEqual(r["changed"], ["floorplan.yaml"])
        self.assertEqual((r["zones"], r["sections"]), (None, 2))
        self.assertEqual(self.keep.read_text(), KEEPOUT)
        r = save_edits(self.root, [{"name": "desk1", "rect": [1.0, 1.0, 2.0, 3.5]}], None)
        self.assertEqual(r["changed"], ["keepout.yaml"])
        self.assertIn("{name: Fiction, x: -1.00, y: 3.00, w: 3.00, h: 3.00, zone: quiet}", self.plan.read_text())

    def test_what_is_not_in_the_list_is_removed(self):
        save_edits(self.root, [], SECTIONS[:1])
        self.assertNotIn("desk1", self.keep.read_text())
        self.assertIn("polygon", self.keep.read_text())
        self.assertNotIn("Reading Room", self.plan.read_text())
        self.assertIn("Fiction", self.plan.read_text())

    def test_the_last_zone_removed_leaves_an_empty_list_that_can_be_added_to_again(self):
        save_edits(self.root, [], SECTIONS)
        # the polygon is still there, so the list is not empty: take it out by hand, as a person would
        self.keep.write_text("clearance_m: 0.25\nzones:\n  - {name: desk1, rect: [1.0, 1.0, 2.0, 3.0]}\n")
        save_edits(self.root, [], SECTIONS)
        self.assertIn("zones: []", self.keep.read_text())
        save_edits(self.root, [DESK1], SECTIONS)
        self.assertIn("{name: desk1, rect: [1.00, 1.00, 2.00, 3.00]}", self.keep.read_text())
        self.assertEqual(self.keep.read_text().count("zones:"), 1)

    def test_all_sections_removed_and_a_section_added_again_does_not_double_the_key(self):
        with self.assertRaises(EditError):                          # Fiction's shelf needs its section
            save_edits(self.root, [DESK1], [])
        (self.root / "data" / "shelves.csv").write_text("shelf,section,floor,zone,map_x,map_y,map_yaw,description\n")
        save_edits(self.root, [DESK1], [])
        self.assertIn("sections: []", self.plan.read_text())
        save_edits(self.root, [DESK1], SECTIONS[:1])
        self.assertEqual(self.plan.read_text().count("sections:"), 1)

    def test_a_zone_over_the_station_or_nearer_than_clearance_is_refused_and_nothing_is_written(self):
        for rect in ([-0.5, -0.5, 0.5, 0.5], [0.1, -1.0, 1.0, -0.1]):             # over the station; 0.1 m from it
            with self.assertRaises(EditError) as e:
                save_edits(self.root, [DESK1, {"name": "bad", "rect": rect}], SECTIONS)
            self.assertIn("station", str(e.exception))
        self.assertEqual(self.keep.read_text(), KEEPOUT)
        self.assertFalse((self.root / "data" / "backups").exists())

    def test_a_section_that_does_not_hold_its_shelf_is_refused(self):
        with self.assertRaises(EditError) as e:
            save_edits(self.root, [DESK1], [{"name": "Fiction", "x": 1.0, "y": 3.5, "w": 2.0, "h": 1.5}, SECTIONS[1]])
        self.assertIn("1-FIC-A", str(e.exception))
        self.assertEqual(self.plan.read_text(), FLOORPLAN)

    def test_a_section_off_the_floor_is_refused(self):
        with self.assertRaises(EditError):
            save_edits(self.root, [DESK1], SECTIONS + [{"name": "Far", "x": 50.0, "y": 50.0, "w": 2.0, "h": 2.0}])

    def test_a_polygon_zones_name_cannot_be_taken_by_a_rectangle(self):
        with self.assertRaises(EditError):
            save_edits(self.root, [DESK1, {"name": "corner", "rect": [6.0, 3.0, 7.0, 4.0]}], SECTIONS)


class CleanTest(unittest.TestCase):
    def test_thin_duplicate_and_badly_named_items_are_refused_with_every_problem_listed(self):
        with self.assertRaises(EditError) as e:
            clean_rects([{"name": "a", "rect": [0, 0, 0.05, 1]}, {"name": "b c", "rect": [0, 0, 1, 1]}, {"name": "d", "rect": [0, 0, 1, 1]},
                         {"name": "d", "rect": [0, 0, 1, 1]}, {"name": "e", "rect": [0, "x", 1, 1]}])
        self.assertEqual(len(e.exception.problems), 4)
        with self.assertRaises(EditError):
            clean_sections([{"name": "ok", "x": 0, "y": 0, "w": 0.05, "h": 1}])
        with self.assertRaises(EditError):
            clean_sections([{"name": "a,b", "x": 0, "y": 0, "w": 1, "h": 1}])
        with self.assertRaises(EditError):
            clean_sections([{"name": "Fiction", "x": 0, "y": 0, "w": 1, "h": 1}, {"name": "fiction", "x": 2, "y": 0, "w": 1, "h": 1}])

    def test_numbers_are_rounded_to_centimetres(self):
        self.assertEqual(clean_rects([{"name": "a", "rect": [0.123, 0, 1.004, 1]}]), {"a": (0.12, 0.0, 1.0, 1.0)})


class AccessTest(unittest.TestCase):
    def test_only_the_robots_own_computer_may_save(self):
        self.assertTrue(allowed("POST", "/plan-edit", local=True))
        self.assertFalse(allowed("POST", "/plan-edit", local=False))


if __name__ == "__main__":
    unittest.main()
