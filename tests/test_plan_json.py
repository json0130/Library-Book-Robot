"""display/server.py plan_json(): the floor, sections, shelves, station and keep-out zones the operator page draws."""

import tempfile
import unittest
from pathlib import Path

from display.server import allowed, plan_json

FLOORPLAN = """floors:
  - level: 2
    name: Upstairs
    x0: 0
    y0: 0
    width: 5
    height: 5
  - level: 1
    name: Ground floor
    x0: -3.9
    y0: -4.0
    width: 13.4
    height: 11.5
    base_map: lab_lidar
    sections:
      - {name: Fiction, x: 0.0, y: 3.5, w: 2.0, h: 1.5}
    landmarks:
      - {kind: lift, name: Lift, x: -3.0, y: 1.5}
station:
  level: 1
  x: 0.0
  y: 0.0
  yaw: 0.672
"""
SHELVES = """# comment line
shelf,section,floor,zone,map_x,map_y,map_yaw,description
1-FIC-A,Fiction,1,common,0.52,4.42,-0.04,"Fiction"
2-SF-A,Science Fiction,2,common,1.0,2.0,1.57,"other floor"
1-BAD,Fiction,1,common,,,,"no pose"
"""
KEEPOUT = """clearance_m: 0.25
zones:
  - {name: desk1, rect: [1.0, 1.0, 2.0, 3.0]}
  - {name: corner, polygon: [[4.0, 2.0], [5.0, 2.0], [5.0, 3.0]]}
"""


def make_root(keepout=KEEPOUT):
    root = Path(tempfile.mkdtemp())
    (root / "data").mkdir()
    (root / "turtlebot" / "config").mkdir(parents=True)
    (root / "data" / "floorplan.yaml").write_text(FLOORPLAN)
    (root / "data" / "shelves.csv").write_text(SHELVES)
    if keepout is not None:
        (root / "turtlebot" / "config" / "keepout.yaml").write_text(keepout)
    return root


class PlanJsonTest(unittest.TestCase):
    def test_the_floor_with_the_robots_map_is_chosen_and_everything_on_it_is_listed(self):
        p = plan_json(make_root())
        self.assertTrue(p["available"])
        self.assertEqual((p["level"], p["base_map"], p["x0"], p["height"]), (1, "lab_lidar", -3.9, 11.5))
        self.assertEqual([s["name"] for s in p["sections"]], ["Fiction"])
        self.assertEqual(p["station"], {"level": 1, "x": 0.0, "y": 0.0, "yaw": 0.672})
        self.assertEqual([(s["code"], s["x"], s["y"]) for s in p["shelves"]], [("1-FIC-A", 0.52, 4.42)])   # other floor, no pose: left out
        self.assertEqual([z["name"] for z in p["keepout"]], ["desk1", "corner"])
        self.assertEqual(p["keepout"][0]["points"], [[1.0, 1.0], [2.0, 1.0], [2.0, 3.0], [1.0, 3.0]])
        self.assertEqual(len(p["keepout"][1]["points"]), 3)
        self.assertEqual(p["clearance_m"], 0.25)

    def test_a_missing_or_empty_keepout_file_is_no_zones(self):
        self.assertEqual(plan_json(make_root(keepout=None))["keepout"], [])
        self.assertEqual(plan_json(make_root(keepout="zones: []\n"))["keepout"], [])

    def test_a_missing_floor_plan_is_reported_not_raised(self):
        p = plan_json(Path(tempfile.mkdtemp()))
        self.assertFalse(p["available"])
        self.assertIn("floorplan.yaml", p["error"])

    def test_another_computer_may_read_the_plan_and_the_map_pictures_but_not_write(self):
        self.assertTrue(allowed("GET", "/plan.json", local=False))
        self.assertTrue(allowed("GET", "/maps/lab_lidar.json", local=False))
        self.assertFalse(allowed("GET", "/maps/../data/x.yaml", local=False))
        self.assertFalse(allowed("POST", "/plan.json", local=False))


if __name__ == "__main__":
    unittest.main()
