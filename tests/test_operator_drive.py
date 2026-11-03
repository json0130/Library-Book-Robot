"""The operator's `drive <x> <y> [yaw]` and `stop`: what is sent to the base, and every reason it is refused."""

import asyncio
import logging
import math
import unittest

from robot.operator import Operator
from tests.test_returning_link import link_app, navigate


def operator(app, said):
    return Operator(app, said.append)


def run(coro):
    return asyncio.run(coro)


class DriveTest(unittest.TestCase):
    def setUp(self):
        self.app, self.link = link_app()
        self.link.pose = {"x": 1.0, "y": 1.0, "yaw": 0.5, "localised": True}
        self.said = []
        self.op = operator(self.app, self.said)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_a_point_is_sent_facing_the_way_it_travels(self):
        self.assertTrue(run(self.op.run("drive", "3.0 1.0")))
        goal = self.link.goals[-1]
        self.assertEqual(goal[1:3], (3.0, 1.0))
        self.assertAlmostEqual(goal[3], 0.0)
        self.assertTrue(run(self.op.run("drive", "1.0 3.0")))
        self.assertAlmostEqual(self.link.goals[-1][3], math.pi / 2)

    def test_an_explicit_yaw_wins_and_commas_are_fine(self):
        self.assertTrue(run(self.op.run("drive", "2.5, 1.5, -1.2")))
        self.assertEqual(self.link.goals[-1][1:], (2.5, 1.5, -1.2))

    def test_a_target_on_the_spot_keeps_the_heading(self):
        self.assertTrue(run(self.op.run("drive", "1.01 1.0")))
        self.assertAlmostEqual(self.link.goals[-1][3], 0.5)

    def test_bad_arguments_send_nothing(self):
        for arg in ("", "1", "a b", "1 2 3 4", "nan 1", "inf 1"):
            self.assertFalse(run(self.op.run("drive", arg)), arg)
        self.assertEqual(self.link.goals, [])
        self.assertIn("usage: drive", self.said[-1])

    def test_not_while_the_dialogue_is_on_a_trip(self):
        navigate(self.app)
        before = len(self.link.goals)
        self.assertFalse(run(self.op.run("drive", "2 2")))
        self.assertEqual(len(self.link.goals), before)
        self.assertIn("trip of its own", self.said[-1])

    def test_not_when_the_link_is_down_or_the_base_is_lost(self):
        self.link.up = False
        self.assertFalse(run(self.op.run("drive", "2 2")))
        self.assertIn("not reachable", self.said[-1])
        self.link.up, self.link.localised, self.link.is_localised = True, False, False
        self.assertFalse(run(self.op.run("drive", "2 2")))
        self.assertEqual(self.link.goals, [])

    def test_a_point_of_the_bases_map_outside_the_floor_plan_is_sent(self):
        self.app.actuators.nav.plan = type("P", (), {"station": None, "floor": lambda self, level: type("F", (), {"width": 12, "height": 8, "level": 1})()})()
        self.app.actuators.nav.floor = 1
        self.link.pose = {"x": -0.3, "y": 1.0, "yaw": 0.5, "localised": True}
        self.assertTrue(run(self.op.run("drive", "-0.90 0.94")), self.said)
        self.assertEqual(self.link.goals[-1][1:3], (-0.9, 0.94))

    def test_not_a_point_far_outside(self):
        self.assertFalse(run(self.op.run("drive", "5000 5000")))
        self.assertIn("not sent", self.said[-1])
        self.assertEqual(self.link.goals, [])

    def test_can_be_switched_off(self):
        self.app.cfg.robot.setdefault("turtlebot", {})["operator_drive"] = False
        self.assertFalse(run(self.op.run("drive", "2 2")))
        self.assertIn("switched off", self.said[-1])
        self.assertEqual(self.link.goals, [])
        self.assertTrue(run(self.op.run("stop")), "stop is always allowed")

    def test_stop_cancels_the_goal_and_works_with_no_goal(self):
        run(self.op.run("drive", "2 2"))
        self.assertTrue(run(self.op.run("stop")))
        self.assertTrue(self.link.cancelled)
        self.assertTrue(run(self.op.run("stop")))

    def test_the_page_is_told_what_happened(self):
        run(self.op.run("drive", "1"))
        first = self.app.state_info()["operator_note"]
        run(self.op.run("stop"))
        second = self.app.state_info()["operator_note"]
        self.assertGreater(second["id"], first["id"])
        self.assertIn("stop", second["text"])


class NoBaseTest(unittest.TestCase):
    def test_without_a_base_drive_and_stop_say_so(self):
        from robot.app import build_app, fake_drivers
        from robot.actuation.face import FaceDisplay
        from robot.config import load_config
        logging.disable(logging.INFO)
        try:
            app = build_app(load_config(), fake_drivers(), face=FaceDisplay(enabled=False), log_sessions=False, persist_storage=False)
            said = []
            op = operator(app, said)
            self.assertFalse(run(op.run("drive", "1 1")))
            self.assertFalse(run(op.run("stop")))
            self.assertIn("no base", said[0])
        finally:
            logging.disable(logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
