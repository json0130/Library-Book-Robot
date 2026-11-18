"""tools/keepout_preview.py: the LiDAR map with the keep-out zones drawn on it, and the same checks as the robot's start."""

import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tools.keepout_preview import main


class PreviewTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "lab.pgm").write_bytes(b"P5\n200 100\n255\n" + b"\xfe" * 20000)    # 10 x 5 m of floor at 5 cm
        (self.dir / "lab.yaml").write_text("image: lab.pgm\nresolution: 0.05\norigin: [-5.0, -1.0, 0.0]\nnegate: 0\n"
                                           "occupied_thresh: 0.65\nfree_thresh: 0.196\n")
        (self.dir / "floorplan.yaml").write_text("station:\n  level: 1\n  x: -4.0\n  y: 0.0\n  yaw: 0.0\n")
        (self.dir / "shelves.csv").write_text("shelf,section,floor,zone,map_x,map_y,map_yaw,description\n"
                                              "1-A,Fiction,1,quiet,3.0,3.0,0.0,x\n")

    def preview(self, zones):
        (self.dir / "keepout.yaml").write_text(zones)
        return main(["--map", str(self.dir / "lab.yaml"), "--keepout", str(self.dir / "keepout.yaml"),
                     "--floorplan", str(self.dir / "floorplan.yaml"), "--shelves", str(self.dir / "shelves.csv"), "--scale", "2"])

    def pixel(self, x, y):
        with Image.open(self.dir / "lab_keepout.png") as picture:
            return picture.convert("RGB").getpixel((int((x + 5.0) / 0.05 * 2), int((100 - (y + 1.0) / 0.05) * 2)))

    def test_the_zones_are_drawn_where_they_are_on_the_map(self):
        self.assertEqual(self.preview("zones:\n  - {name: desk, rect: [0.0, 0.0, 2.0, 1.0]}\n"), 0)
        with Image.open(self.dir / "lab_keepout.png") as picture:
            self.assertEqual(picture.size, (400, 200))
        r, g, b = self.pixel(1.0, 0.5)                                              # inside the desk: reddish
        self.assertGreater(r, g + 40)
        self.assertEqual(self.pixel(-2.0, 2.0), (254, 254, 254))                    # elsewhere: the map as it was

    def test_a_zone_with_a_problem_is_still_drawn_and_the_exit_code_says_so(self):
        self.assertEqual(self.preview("zones:\n  - {name: over_station, rect: [-4.5, -0.5, -3.5, 0.5]}\n"), 1)
        self.assertTrue((self.dir / "lab_keepout.png").is_file())
        r, g, b = self.pixel(-4.4, 0.4)                                             # a corner clear of the station mark
        self.assertGreater(r, b + 40)
        self.assertGreater(g, b + 20)                                               # orange, not red

    def test_a_broken_file_is_an_error(self):
        self.assertEqual(self.preview("zones: [\n"), 1)


if __name__ == "__main__":
    unittest.main()
