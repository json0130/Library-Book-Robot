"""The floor plan on the robot's own map: a floor that starts at negative coordinates, the picture of the map under it, and
directions that go round the walls (robot/services/routing.py, tools/map_to_plan.py, data/floorplan.yaml)."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

import numpy as np

from robot.core.screen import directions
from robot.services.floorplan import FloorPlan, FloorPlanError
from robot.services.routing import Walkable, length
from tools import map_to_plan

ROOT = Path(__file__).resolve().parent.parent
MAPS = ROOT / "display" / "ui" / "maps"

# a corridor shaped like an L, walls everywhere else; rows[0] is the lowest y, 0.1 m cells, the grid's corner at (-1, -1)
L_ROWS = ["1" * 30] * 3 + ["0" * 27 + "111"] * 20
FLOOR = {"level": 1, "name": "Ground", "x0": -1.0, "y0": -1.0, "width": 3.0, "height": 2.3}


def plan_with(floor: dict, **extra) -> FloorPlan:
    return FloorPlan.from_dict({"floors": [floor], **extra})


class FloorOriginTest(unittest.TestCase):
    def test_a_floor_can_start_below_zero(self):
        plan = plan_with({**FLOOR, "sections": [{"name": "Fiction", "x": -0.5, "y": -0.5, "w": 1.0, "h": 1.0}],
                          "landmarks": [{"kind": "lift", "name": "Lift", "x": 1.5, "y": 1.0}]},
                         station={"level": 1, "x": -0.8, "y": 0.0})
        floor = plan.floor(1)
        self.assertEqual((floor.x0, floor.y0), (-1.0, -1.0))
        self.assertTrue(floor.contains(-0.8, -0.9))
        self.assertFalse(floor.contains(2.5, 0.0))

    def test_what_sticks_out_of_the_shifted_floor_is_refused(self):
        with self.assertRaises(FloorPlanError) as raised:
            plan_with({**FLOOR, "sections": [{"name": "A", "x": -1.5, "y": 0, "w": 1.0, "h": 1.0}]})
        self.assertIn("outside the floor", str(raised.exception))
        with self.assertRaises(FloorPlanError):
            plan_with({**FLOOR, "landmarks": [{"kind": "lift", "name": "Lift", "x": 2.5, "y": 0}]})
        with self.assertRaises(FloorPlanError):
            plan_with(FLOOR, station={"level": 1, "x": 5.0, "y": 0.0})

    def test_the_origin_defaults_to_zero_so_the_old_plans_still_load(self):
        floor = plan_with({"level": 1, "width": 4, "height": 3}).floor(1)
        self.assertEqual((floor.x0, floor.y0, floor.base_map), (0.0, 0.0, ""))

    def test_the_base_map_is_a_plain_name(self):
        self.assertEqual(plan_with({**FLOOR, "base_map": "lab"}).floor(1).base_map, "lab")
        for bad in ("../secret", "a/b", "lab.json", "x" * 41):
            with self.subTest(bad=bad), self.assertRaises(FloorPlanError):
                plan_with({**FLOOR, "base_map": bad})


class WalkableTest(unittest.TestCase):
    def setUp(self):
        self.walk = Walkable(L_ROWS, 0.1, -1.0, -1.0)

    def test_the_way_goes_round_the_corner_not_across_the_wall(self):
        route = self.walk.route((-0.8, -0.8), (1.8, 1.0))                      # along the bottom, then up the right-hand corridor
        self.assertEqual(route[0], (-0.8, -0.8))
        self.assertEqual(route[-1], (1.8, 1.0))
        self.assertGreater(len(route), 2)
        corner = route[1]
        self.assertGreater(corner[0], 1.4)                                      # it turns where the corridor does
        self.assertLess(corner[1], -0.5)
        straight = length([route[0], route[-1]])
        self.assertGreater(length(route), straight + 0.3)                       # longer than the line through the wall
        for a, b in zip(route, route[1:]):                                      # and every leg stays on walkable ground
            self.assertTrue(self.walk.clear(self.walk.cell(*a), self.walk.cell(*b)), (a, b))

    def test_a_point_in_a_wall_is_joined_to_the_nearest_ground(self):
        route = self.walk.route((-0.8, -0.8), (0.0, 0.5))                       # a shelf standing in the wall
        self.assertEqual(route[-1], (0.0, 0.5))
        self.assertTrue(self.walk.walkable(*self.walk.cell(*route[-2])))

    def test_no_way_is_none(self):
        rows = ["11100000000", "11100000000", "00000000011", "00000000011"]
        self.assertIsNone(Walkable(rows, 0.1, 0.0, 0.0).route((0.1, 0.1), (1.0, 0.3)))

    def test_a_missing_or_broken_file_is_none_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(Walkable.load(Path(tmp) / "nope.json"))
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json")
            self.assertIsNone(Walkable.load(broken))
            broken.write_text(json.dumps({"floor": "M0 0"}))                     # a picture without a walk grid
            self.assertIsNone(Walkable.load(broken))


class FloorPlanRouteTest(unittest.TestCase):
    def test_a_floor_without_a_base_map_has_no_route(self):
        self.assertIsNone(plan_with(FLOOR).route(1, (-0.8, -0.8), (1.8, 1.0)))

    def test_the_plan_routes_over_the_map_it_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "corner.json").write_text(json.dumps({"walk": {"rows": L_ROWS, "res": 0.1, "x0": -1.0, "y0": -1.0}}))
            plan = plan_with({**FLOOR, "base_map": "corner"})
            plan.maps_dir = Path(tmp)
            route = plan.route(1, (-0.8, -0.8), (1.8, 1.0))
            self.assertGreater(len(route), 2)
            payload = directions(plan, "section", "the Sci section", 1, (1, -0.8, -0.8), target=(1.8, 1.0))
            self.assertEqual(payload.path[0], [-0.8, -0.8])
            self.assertEqual(payload.path[-1], [1.8, 1.0])
            self.assertGreater(len(payload.path), 2)
            self.assertEqual((payload.x0, payload.y0, payload.base_map), (-1.0, -1.0, "corner"))
            self.assertGreaterEqual(payload.distance_m, round(length([tuple(q) for q in payload.path])) - 1)

    def test_without_a_map_the_old_l_shaped_line_is_drawn(self):
        payload = directions(plan_with(FLOOR), "section", "x", 1, (1, -0.8, -0.8), target=(1.8, 1.0))
        self.assertEqual(payload.path, [[-0.8, -0.8], [1.8, -0.8], [1.8, 1.0]])
        self.assertEqual(payload.base_map, "")


class TheRealPlanTest(unittest.TestCase):
    """data/floorplan.yaml puts level 1 on the TurtleBot's own map of the lab."""

    @classmethod
    def setUpClass(cls):
        cls.plan = FloorPlan.load(ROOT / "data" / "floorplan.yaml")

    def test_every_base_map_the_plan_names_is_there_and_has_a_walk_grid(self):
        for floor in self.plan.floors.values():
            if floor.base_map:
                with self.subTest(map=floor.base_map):
                    picture = json.loads((MAPS / f"{floor.base_map}.json").read_text())
                    self.assertTrue(picture["floor"].startswith("M") and picture["walls"].startswith("M"))
                    self.assertTrue(picture["walk"]["rows"])

    def test_the_floor_covers_the_whole_picture(self):
        floor = self.plan.floor(1)
        b = json.loads((MAPS / f"{floor.base_map}.json").read_text())["bounds"]
        self.assertLessEqual(floor.x0, b["x0"])
        self.assertLessEqual(floor.y0, b["y0"])
        self.assertGreaterEqual(floor.x0 + floor.width, b["x1"])
        self.assertGreaterEqual(floor.y0 + floor.height, b["y1"])

    def test_the_station_and_the_shelf_are_on_ground_the_robot_can_stand_on(self):
        walk = Walkable.load(MAPS / f"{self.plan.floor(1).base_map}.json")
        station = self.plan.station
        self.assertTrue(walk.walkable(*walk.cell(station.x, station.y)), "the station is not on walkable ground")
        self.assertTrue(walk.walkable(*walk.cell(-1.0, 1.0)), "shelf 1-FIC-A is not on walkable ground")

    def test_directions_to_the_fiction_shelf_follow_the_map(self):
        s = self.plan.station
        payload = directions(self.plan, "book", "Dune", 1, (1, s.x, s.y), "Fiction", "1-FIC-A", (-1.0, 1.0))
        self.assertEqual(payload.base_map, "lab_lidar")
        self.assertEqual(payload.path[0], [s.x, s.y])
        self.assertEqual(payload.path[-1], [-1.0, 1.0])
        walk = Walkable.load(MAPS / "lab_lidar.json")
        for a, b in zip(payload.path, payload.path[1:]):
            self.assertTrue(walk.clear(walk.cell(*a), walk.cell(*b)), f"{a} -> {b} crosses a wall")

    def test_the_other_floors_are_still_drawn_as_plain_plans(self):
        for level in (2, 3):
            floor = self.plan.floor(level)
            self.assertEqual((floor.base_map, floor.x0, floor.y0), ("", 0.0, 0.0))


class MapToPlanTest(unittest.TestCase):
    def grid(self):
        """40 x 30 cells: a 20 x 14 room of free floor with a wall round it, unknown outside, a speck of noise elsewhere."""
        g = np.full((30, 40), 205, dtype=np.uint8)
        g[5:25, 8:32] = 0                           # the wall (occupied)
        g[7:23, 10:30] = 254                        # the floor
        g[2, 2] = 254                               # one stray free cell
        return g

    def test_the_floor_and_the_walls_come_out_in_metres_with_y_up(self):
        picture = map_to_plan.build(self.grid(), 0.05, [-1.0, -0.5])
        b = picture["bounds"]                       # the walls are drawn a cell thicker than the map's, hence the 0.06
        self.assertAlmostEqual(b["x0"], -1.0 + 8 * 0.05, delta=0.06)
        self.assertAlmostEqual(b["x1"], -1.0 + 32 * 0.05, delta=0.06)
        self.assertAlmostEqual(b["y0"], -0.5 + (30 - 25) * 0.05, delta=0.06)         # picture row 25 is the lowest wall: y grows upward
        self.assertAlmostEqual(b["y1"], -0.5 + (30 - 5) * 0.05, delta=0.06)
        self.assertTrue(picture["floor"].startswith("M") and picture["walls"].startswith("M"))
        nums = [float(v) for v in re.findall(r"-?\d+\.\d+", picture["floor"])]
        self.assertTrue(nums)

    def test_noise_is_dropped_and_the_walk_grid_keeps_off_the_walls(self):
        picture = map_to_plan.build(self.grid(), 0.05, [0.0, 0.0])
        self.assertLess(picture["bounds"]["x0"], 1.0)                                  # the stray cell at column 2 is not part of the map
        self.assertGreater(picture["bounds"]["x0"], 0.3)
        rows = picture["walk"]["rows"]
        self.assertTrue(any("1" in r for r in rows))
        self.assertEqual(len({len(r) for r in rows}), 1)
        walk = picture["walk"]                                                          # the floor starts at x = 0.5 m: a margin is kept
        self.assertGreater(walk["x0"], 0.5 + 0.15)
        self.assertLess(walk["x0"] + walk["w"] * walk["res"], 1.5 - 0.15)

    def test_an_empty_map_is_refused(self):
        with self.assertRaises(SystemExit):
            map_to_plan.build(np.full((10, 10), 205, dtype=np.uint8), 0.05, [0.0, 0.0])

    def test_a_pgm_is_read_with_its_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            pgm = Path(tmp) / "m.pgm"
            pixels = bytes(range(12))
            pgm.write_bytes(b"P5\n# CREATOR: map_saver.cpp 0.050 m/pix\n4 3\n255\n" + pixels)
            grid = map_to_plan.read_pgm(pgm)
            self.assertEqual(grid.shape, (3, 4))
            self.assertEqual(list(grid.flatten()), list(pixels))

    def lidar_map(self, folder: Path, image: str) -> Path:
        """What turtlebot/run_lidar_mapping.sh leaves: map_saver's files for a gmapping map. The walls are LiDAR lines one cell
        thick, the map is 30 x 30 m round the start at (0, 0), and the room (4 x 3 m) is the only part that is not unknown."""
        g = np.full((600, 600), 205, dtype=np.uint8)
        top, left = 600 - (300 + 30), 300 - 20                     # picture rows count down from y = +15 m
        g[top:top + 60, left:left + 80] = 254
        g[top, left:left + 80] = g[top + 59, left:left + 80] = 0
        g[top:top + 60, left] = g[top:top + 60, left + 79] = 0
        (folder / "lidar_map.pgm").write_bytes(b"P5\n# CREATOR: map_saver.cpp 0.050 m/pix\n600 600\n255\n" + g.tobytes())
        yaml_file = folder / "lidar_map.yaml"
        yaml_file.write_text(f"image: {image}\nresolution: 0.050000\norigin: [-15.000000, -15.000000, 0.000000]\n"
                             "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n\n", encoding="utf-8")
        return yaml_file

    def test_a_lidar_map_from_map_saver_converts_with_the_image_named_either_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for image in ("lidar_map.pgm", str(folder / "lidar_map.pgm")):        # the script writes it relative; map_saver -f /abs absolute
                yaml_file = self.lidar_map(folder, image)
                self.assertEqual(map_to_plan.main([str(yaml_file), "lab_lidar", "--out", str(folder / "out")]), 0)
                picture = json.loads((folder / "out" / "lab_lidar.json").read_text(encoding="utf-8"))
                b = picture["bounds"]
                self.assertAlmostEqual(b["x0"], -1.0, delta=0.06)                 # the room: x -1 .. 3, y -1.5 .. 1.5
                self.assertAlmostEqual(b["x1"], 3.0, delta=0.06)
                self.assertAlmostEqual(b["y0"], -1.5, delta=0.06)
                self.assertAlmostEqual(b["y1"], 1.5, delta=0.06)
                walk = Walkable(picture["walk"]["rows"], picture["walk"]["res"], picture["walk"]["x0"], picture["walk"]["y0"])
                self.assertIsNotNone(walk.route((0.0, 0.0), (2.0, 1.0)))


if __name__ == "__main__":
    unittest.main()
