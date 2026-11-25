"""keepout.py and the map cleaner's zone painting: keep-out zones are walls in the planner's map only, checked against the map,
the station and the shelf poses."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np

from support import Clock  # noqa: F401  (puts turtlebot/ on the path)
import keepout
from keepout import KeepoutError, Zone

HERE = os.path.dirname(os.path.abspath(__file__))
TURTLEBOT = os.path.dirname(HERE)


def rect(name, x1, y1, x2, y2):
    return keepout.parse({"zones": [{"name": name, "rect": [x1, y1, x2, y2]}]})[0][0]


def write_map(folder, width=100, height=80, resolution=0.05, origin=(-2.0, -1.0), name="lab", yaw=0.0):
    """A map_saver map: an empty room, origin bottom-left."""
    with open(os.path.join(folder, name + ".pgm"), "wb") as f:
        f.write(b"P5\n# CREATOR: map_saver.cpp 0.050 m/pix\n%d %d\n255\n" % (width, height) + b"\xfe" * (width * height))
    path = os.path.join(folder, name + ".yaml")
    with open(path, "w") as f:
        f.write("image: %s.pgm\nresolution: %f\norigin: [%f, %f, %f]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n"
                % (name, resolution, origin[0], origin[1], yaw))
    return path


class ParseTest(unittest.TestCase):
    def test_rectangles_and_polygons(self):
        zones, clearance = keepout.parse({"clearance_m": 0.3, "zones": [
            {"name": "desk", "rect": [2, 1, 0, 0]},                                  # corners in any order
            {"name": "corner", "polygon": [[0, 0], [1, 0], [0, 1]]}]})
        self.assertEqual(clearance, 0.3)
        self.assertEqual([(z.name, z.kind) for z in zones], [("desk", "rect"), ("corner", "polygon")])
        self.assertEqual(zones[0].points, [(0, 0), (2, 0), (2, 1), (0, 1)])

    def test_no_zones_is_fine(self):
        self.assertEqual(keepout.parse({"zones": []}), ([], keepout.DEFAULT_CLEARANCE))
        self.assertEqual(keepout.parse(None), ([], keepout.DEFAULT_CLEARANCE))

    def problems(self, data):
        with self.assertRaises(KeepoutError) as caught:
            keepout.parse(data)
        return " | ".join(caught.exception.problems)

    def test_every_mistake_is_reported_at_once(self):
        text = self.problems({"zones": [
            {"name": "a", "rect": [0, 0, 1]},                                        # three numbers
            {"name": "a", "rect": [0, 0, 1, 1]},                                     # the name again
            {"rect": [0, 0, 1, 1]},                                                  # no name
            {"name": "b", "polygon": [[0, 0], [1, 1]]},                              # two corners
            {"name": "c", "rect": [0, 0, 1, 1], "polygon": [[0, 0], [1, 0], [0, 1]]},
            {"name": "d", "rect": [0, 0, 0, 1]},                                     # no area
            {"name": "e", "rect": [0, 0, "x", 1]},
            {"name": "f", "polygon": [[0, 0], [1, 1], [2, 2]]},                      # a line
            {"name": "g", "rect": [0, 0, float("nan"), 1]},
            {"name": "h", "rect": [0, 0, 1, 1], "colour": "red"}]})
        for expected in ("a: rect must hold numbers", "a: the name is used twice", "zone 3 has no name",
                         "b: a polygon needs three or more corners", "c: needs exactly one of", "d: the rectangle has no area",
                         "e: rect must hold numbers", "f: the polygon has no area", "g: rect must hold numbers",
                         "h: unknown key(s) colour"):
            self.assertIn(expected, text)

    def test_a_bare_off_or_yes_as_a_name_is_caught(self):
        self.assertIn("put it in quotes", self.problems({"zones": [{"name": False, "rect": [0, 0, 1, 1]}]}))

    def test_bad_top_level(self):
        self.assertIn("unknown key(s) zone", self.problems({"zone": []}))
        self.assertIn("clearance_m must be", self.problems({"clearance_m": -1, "zones": []}))
        self.assertIn("zones: must be a list", self.problems({"zones": {"a": 1}}))

    def test_the_repos_file_loads(self):
        zones, clearance = keepout.load(keepout.DEFAULT_FILE)                        # whatever zones the real map has now: the file must load
        self.assertTrue(all(z.kind in ("rect", "polygon") and len(z.points) >= 3 for z in zones))
        self.assertGreaterEqual(clearance, 0.2)

    def test_a_missing_or_broken_file_is_an_error(self):
        with self.assertRaises(KeepoutError):
            keepout.load("/nonexistent/keepout.yaml")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with open(os.path.join(tmp, "k.yaml"), "w") as f:
            f.write("zones: [\n")
        with self.assertRaises(KeepoutError) as caught:
            keepout.load(os.path.join(tmp, "k.yaml"))
        self.assertIn("not valid YAML", str(caught.exception))


class PaintTest(unittest.TestCase):
    """A 20 x 10 grid of 0.1 m cells from (0, 0): cell (row r, col c) covers x c/10..(c+1)/10, y r/10..(r+1)/10."""

    def grid(self):
        return np.zeros((10, 20), dtype=np.int16)

    def test_a_rectangle_paints_exactly_its_cells_and_nothing_else(self):
        out, counts = keepout.paint(self.grid(), 0.1, 0.0, 0.0, [rect("desk", 0.5, 0.2, 1.0, 0.6)])
        self.assertEqual(set(np.unique(out)), {0, 100})
        rows, cols = np.nonzero(out)
        self.assertEqual((rows.min(), rows.max(), cols.min(), cols.max()), (2, 6, 5, 10))    # the edge cells are painted too
        self.assertEqual(counts["desk"], len(rows))
        self.assertTrue(out[2:6, 5:10].all())

    def test_rows_are_y_upwards_like_an_occupancy_grid(self):
        out, _ = keepout.paint(self.grid(), 0.1, 0.0, 0.0, [rect("low", 0.0, 0.0, 0.3, 0.2)])
        self.assertTrue(out[0, 0] and out[1, 2])                                     # row 0 is the lowest y
        self.assertFalse(out[9].any())

    def test_the_origin_is_taken_into_account(self):
        out, _ = keepout.paint(self.grid(), 0.1, -1.0, -0.5, [rect("z", -1.0, -0.5, -0.75, -0.25)])
        rows, cols = np.nonzero(out)
        self.assertEqual((rows.min(), cols.min()), (0, 0))
        self.assertLessEqual(cols.max(), 3)

    def test_a_triangle_paints_its_inside_not_its_bounding_box(self):
        tri = keepout.parse({"zones": [{"name": "t", "polygon": [[0.0, 0.0], [1.0, 0.0], [0.0, 0.9]]}]})[0][0]
        out, _ = keepout.paint(self.grid(), 0.1, 0.0, 0.0, [tri])
        self.assertTrue(out[0, 0] and out[1, 1])
        self.assertFalse(out[8, 8])                                                  # the far corner of the box is outside
        self.assertLess(np.count_nonzero(out), 0.8 * 10 * 9)

    def test_a_concave_polygon_leaves_its_notch_free(self):
        u = keepout.parse({"zones": [{"name": "u", "polygon": [[0, 0], [1.5, 0], [1.5, 0.9], [1.0, 0.9], [1.0, 0.3],
                                                              [0.5, 0.3], [0.5, 0.9], [0, 0.9]]}]})[0][0]
        out, _ = keepout.paint(self.grid(), 0.1, 0.0, 0.0, [u])
        self.assertFalse(out[6, 7])                                                  # (0.75, 0.65): in the notch
        self.assertTrue(out[6, 2] and out[6, 12] and out[1, 7])

    def test_a_zone_thinner_than_a_cell_still_paints_a_line(self):
        out, _ = keepout.paint(self.grid(), 0.1, 0.0, 0.0, [rect("rail", 0.21, 0.42, 1.79, 0.44)])
        self.assertTrue(out[4, 2:18].all())

    def test_a_zone_partly_off_the_grid_is_clipped_and_one_wholly_off_paints_nothing(self):
        out, counts = keepout.paint(self.grid(), 0.1, 0.0, 0.0, [rect("edge", 1.8, 0.8, 2.5, 1.5), rect("away", 5, 5, 6, 6)])
        self.assertTrue(out[8:, 18:].all())
        self.assertEqual(counts["away"], 0)

    def test_the_grid_given_is_not_changed_and_other_values_survive(self):
        g = self.grid()
        g[0, 19] = -1                                                                # unknown
        out, _ = keepout.paint(g, 0.1, 0.0, 0.0, [rect("z", 0, 0, 0.3, 0.3)])
        self.assertEqual(np.count_nonzero(g), 1)
        self.assertEqual(out[0, 19], -1)


class CheckTest(unittest.TestCase):
    AREA = (-2.0, -1.0, 3.0, 3.0)

    def test_fine(self):
        self.assertEqual(keepout.check([rect("desk", 1, 1, 2, 2)], self.AREA, {"station": (0, 0), "shelf 1-A": (2.5, 2.5)}), [])

    def test_a_corner_off_the_map(self):
        problems = keepout.check([rect("desk", 2, 2, 3.5, 2.5)], self.AREA, {})
        self.assertEqual(len(problems), 1)
        self.assertIn("desk: corner (3.50, 2.00) is outside the map", problems[0])

    def test_covering_the_station_or_a_shelf(self):
        problems = keepout.check([rect("desk", -1, -0.5, 1, 0.5)], self.AREA, {"station": (0, 0), "shelf 1-A": (0.9, 0.4)})
        self.assertEqual(sorted(problems), ["desk covers the shelf 1-A at (0.90, 0.40)", "desk covers the station at (0.00, 0.00)"])

    def test_too_close_to_a_pose(self):
        problems = keepout.check([rect("desk", 1, 1, 2, 2)], self.AREA, {"station": (0.9, 1.5)}, clearance=0.25)
        self.assertEqual(len(problems), 1)
        self.assertIn("0.10 m from the station", problems[0])
        self.assertEqual(keepout.check([rect("desk", 1, 1, 2, 2)], self.AREA, {"station": (0.7, 1.5)}, clearance=0.25), [])

    def test_a_pose_in_the_notch_of_a_polygon_is_not_covered(self):
        u = keepout.parse({"zones": [{"name": "u", "polygon": [[0, 0], [3, 0], [3, 2], [2, 2], [2, 1], [1, 1], [1, 2], [0, 2]]}]})[0][0]
        self.assertEqual(keepout.check([u], (-1, -1, 4, 4), {"station": (1.5, 1.8)}, clearance=0.25), [])
        self.assertTrue(keepout.check([u], (-1, -1, 4, 4), {"station": (1.5, 1.1)}, clearance=0.25))


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_map_info_reads_the_yaml_and_the_picture_size(self):
        info = keepout.map_info(write_map(self.dir))
        self.assertEqual((info.width, info.height, info.resolution, info.origin_x, info.origin_y), (100, 80, 0.05, -2.0, -1.0))
        self.assertEqual(keepout.bounds(info), (-2.0, -1.0, 3.0, 3.0))

    def test_a_rotated_or_missing_map_is_an_error(self):
        with self.assertRaises(KeepoutError) as caught:
            keepout.map_info(write_map(self.dir, yaw=0.5))
        self.assertIn("rotated", str(caught.exception))
        with self.assertRaises(KeepoutError):
            keepout.map_info(os.path.join(self.dir, "none.yaml"))

    def test_the_station_and_the_shelves_of_its_floor(self):
        plan = self.write("floorplan.yaml", "floors: []\nstation:\n  level: 1\n  x: 0.5\n  y: -0.5\n  yaw: 0.0\n")
        shelves = self.write("shelves.csv", "# a comment\nshelf,section,floor,zone,map_x,map_y,map_yaw,description\n"
                                            "1-A,Fiction,1,quiet,1.0,2.0,0.0,x\n2-B,Science,2,quiet,9.0,9.0,0.0,other floor\n"
                                            "1-C,Fiction,1,quiet,,,,no pose yet\n")
        self.assertEqual(keepout.poses(plan, shelves), {"station": (0.5, -0.5), "shelf 1-A": (1.0, 2.0)})

    def test_the_repos_data_files_read(self):
        places = keepout.poses(keepout.DEFAULT_FLOORPLAN, keepout.DEFAULT_SHELVES)
        self.assertIn("station", places)

    def test_the_command_line_check(self):
        yaml_map = write_map(self.dir)
        plan = self.write("floorplan.yaml", "station:\n  level: 1\n  x: 0.0\n  y: 0.0\n")
        shelves = self.write("shelves.csv", "shelf,section,floor,zone,map_x,map_y,map_yaw,description\n")
        good = self.write("good.yaml", "zones:\n  - {name: desk, rect: [1, 1, 2, 2]}\n")
        bad = self.write("bad.yaml", "zones:\n  - {name: desk, rect: [-0.5, -0.5, 0.5, 0.5]}\n")
        script = os.path.join(TURTLEBOT, "keepout.py")

        def run(zones):
            return subprocess.run([sys.executable, script, "--map", yaml_map, "--keepout", zones, "--floorplan", plan,
                                   "--shelves", shelves], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        r = run(good)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("1 zone(s) OK: desk", r.stdout)
        r = run(bad)
        self.assertEqual(r.returncode, 1)
        self.assertIn("desk covers the station", r.stdout)


class EditTest(unittest.TestCase):
    def test_an_empty_list_gets_its_first_zone_and_keeps_its_comments(self):
        text = ("# Keep-out zones: a comment that must stay.\n#   rect: two opposite corners [x1, y1, x2, y2]\n\nclearance_m: 0.25\n\n"
                "zones: []   # none yet\n#  - {name: example, rect: [1.20, -0.40, 3.60, 0.40]}\n")      # (not the repo's file: that holds the real zones)
        new = keepout.set_rect_text(text, "desk_1", (1.0, 2.0, 3.5, 4.25))
        self.assertEqual(keepout.parse(__import__("yaml").safe_load(new))[0],
                         [Zone("desk_1", "rect", [(1.0, 2.0), (3.5, 2.0), (3.5, 4.25), (1.0, 4.25)])])
        for line in text.splitlines():
            if line.startswith("#"):
                self.assertIn(line, new)                                             # every comment is kept

    def test_a_zone_is_replaced_by_name_and_a_new_one_goes_after_the_last(self):
        text = "clearance_m: 0.25\nzones:\n  - {name: a, rect: [0, 0, 1, 1]}   # by the door\n  - {name: b, polygon: [[0, 0], [1, 0], [0, 1]]}\n# end\n"
        new = keepout.set_rect_text(text, "a", (5, 5, 6, 6))
        self.assertIn("  - {name: a, rect: [5.00, 5.00, 6.00, 6.00]}", new)
        self.assertEqual(new.count("name: a"), 1)
        new = keepout.set_rect_text(new, "c", (2, 2, 3, 3))
        lines = new.splitlines()
        self.assertEqual(lines.index("  - {name: c, rect: [2.00, 2.00, 3.00, 3.00]}"), 4)    # after b, before the comment
        names = [z.name for z in keepout.parse(__import__("yaml").safe_load(new))[0]]
        self.assertEqual(names, ["a", "b", "c"])

    def test_a_file_without_zones_gets_the_key(self):
        new = keepout.set_rect_text("clearance_m: 0.3\n", "a", (0, 0, 1, 1))
        self.assertEqual([z.name for z in keepout.parse(__import__("yaml").safe_load(new))[0]], ["a"])

    def test_a_name_must_be_plain(self):
        for name in ("two words", "", "off: x", "a" * 41):
            with self.assertRaises(KeepoutError):
                keepout.set_rect_text("zones: []\n", name, (0, 0, 1, 1))


class MapCleanerTest(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("map_cleaner", os.path.join(TURTLEBOT, "nodes", "map_cleaner.py"))
        self.mc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mc)

    def test_a_small_zone_is_painted_after_the_specks_are_removed_so_it_is_not_removed_as_one(self):
        g = np.zeros((40, 40), dtype=np.int16)
        g[5, 5] = 100                                                                # a speck: removed
        g[20:40, 0] = 100                                                            # a wall: kept
        out, removed, painted = self.mc.planner_grid(g, 0.05, 0.0, 0.0, 0.05, [rect("post", 1.0, 1.0, 1.04, 1.04)])
        self.assertEqual(removed, 1)
        self.assertEqual(out[5, 5], 0)
        self.assertTrue(out[20:40, 0].all())
        self.assertTrue(out[20, 20])                                                 # the 4 cm zone is there
        self.assertEqual(painted, {"post": 1})

    def test_without_zones_it_is_the_cleaner_it_was(self):
        g = np.zeros((10, 10), dtype=np.int16)
        g[2, 2] = 100
        out, removed, painted = self.mc.planner_grid(g, 0.05, 0.0, 0.0)
        self.assertEqual((removed, painted, int(out.sum())), (1, {}, 0))
        np.testing.assert_array_equal(out, self.mc.clean_grid(g, 0.05)[0])


if __name__ == "__main__":
    unittest.main()
