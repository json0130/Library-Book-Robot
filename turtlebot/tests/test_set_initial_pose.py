"""nodes/set_initial_pose.py: the station pose AMCL starts from. Only the plain helpers: nothing here needs ROS or moves the robot."""

import importlib.util
import math
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("set_initial_pose", os.path.join(os.path.dirname(HERE), "nodes", "set_initial_pose.py"))
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)


def floorplan(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


class StationTest(unittest.TestCase):
    def test_the_repos_floor_plan_has_a_station_to_start_from(self):
        x, y, yaw = node.station_pose(node.DEFAULT_FLOORPLAN)
        self.assertTrue(all(math.isfinite(v) for v in (x, y, yaw)))

    def test_the_station_is_read_with_its_yaw(self):
        path = floorplan("floors: []\nstation:\n  level: 1\n  x: -4.0\n  y: 2.5\n  yaw: 1.57\n")
        try:
            self.assertEqual(node.station_pose(path), (-4.0, 2.5, 1.57))
        finally:
            os.unlink(path)

    def test_a_missing_yaw_faces_along_x(self):
        path = floorplan("station: {level: 1, x: 0, y: 0}\n")
        try:
            self.assertEqual(node.station_pose(path), (0.0, 0.0, 0.0))
        finally:
            os.unlink(path)

    def test_no_station_or_a_broken_one_or_no_file_says_why(self):
        for text, words in (("floors: []\n", "no station"), ("station: {x: here, y: 1}\n", "needs x and y"),
                            ("station: [1, 2]\n", "no station"), ("station: {x: 1, y: .nan}\n", "finite")):
            path = floorplan(text)
            try:
                with self.assertRaises(ValueError) as e:
                    node.station_pose(path)
                self.assertIn(words, str(e.exception))
            finally:
                os.unlink(path)
        with self.assertRaises(ValueError) as e:
            node.station_pose("/nonexistent/floorplan.yaml")
        self.assertIn("cannot read", str(e.exception))

    def test_a_pose_on_the_command_line(self):
        self.assertEqual(node.parse_pose("1,-2.5,0.3"), (1.0, -2.5, 0.3))
        for bad in ("1,2", "a,b,c", "1,2,3,4", "1,inf,0"):
            with self.assertRaises(ValueError):
                node.parse_pose(bad)

    def test_the_covariance_has_only_x_y_and_yaw(self):
        c = node.covariance(0.15, math.radians(8.0))
        self.assertEqual(len(c), 36)
        self.assertAlmostEqual(c[0], 0.0225)
        self.assertAlmostEqual(c[7], 0.0225)
        self.assertAlmostEqual(c[35], math.radians(8.0) ** 2)
        self.assertEqual(sum(1 for v in c if v), 3)

    def test_no_usable_pose_exits_2_before_touching_ros(self):
        path = floorplan("floors: []\n")
        try:
            self.assertEqual(node.main(["--floorplan", path]), 2)
            self.assertEqual(node.main(["--pose", "1,2"]), 2)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
