import os
import subprocess
import sys
import unittest

from support import Clock  # noqa: F401  (puts turtlebot/ on the path)
import measure_localisation as m


class MeasureTest(unittest.TestCase):
    def test_gaps_are_split_by_whether_the_robot_moved(self):
        events = [(0.0, "odom", 0.0, 0.0), (0.0, "loc", 0, 0), (1.0, "loc", 0, 0), (2.0, "loc", 0, 0),      # still: gaps 1, 1
                  (2.5, "odom", 0.2, 0.0), (3.0, "loc", 0, 0), (3.5, "loc", 0, 0), (5.5, "loc", 0, 0)]      # moving: gaps 1, 0.5, 2
        s = m.summarise(events)
        self.assertEqual((s["still"]["n"], s["still"]["median"], s["still"]["max"]), (2, 1.0, 1.0))
        self.assertEqual((s["moving"]["n"], s["moving"]["median"], s["moving"]["max"]), (3, 1.0, 2.0))

    def test_suggestion_is_twice_the_slow_gaps_but_never_under_the_floor(self):
        self.assertEqual(m.suggest_loc_max_age({"moving": {"p95": 2.0}}), 4.0)
        self.assertEqual(m.suggest_loc_max_age({"moving": {"p95": 0.5}}), 3.0)
        self.assertIsNone(m.suggest_loc_max_age({"moving": {"p95": None}}))

    def test_no_events_is_fine(self):
        s = m.summarise([])
        self.assertEqual((s["still"]["n"], s["moving"]["n"]), (0, 0))
        self.assertIsNone(s["moving"]["median"])


HERE = os.path.dirname(os.path.abspath(__file__))


class CommandLineTest(unittest.TestCase):
    def test_command_line_parses(self):
        """--help used to crash (the description came from a module docstring that was not there)."""
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "measure_localisation.py"), "--help"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--seconds", r.stdout)


if __name__ == "__main__":
    unittest.main()
