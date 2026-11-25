import math
import unittest

from support import Clock  # noqa: F401  (puts turtlebot/ on the path)
from robot_link import rules
from robot_link.activity import RobotActivity


class RulesTest(unittest.TestCase):
    def test_wrap_and_yaw(self):
        self.assertAlmostEqual(rules.wrap_pi(3 * math.pi / 2), -math.pi / 2)
        for yaw in (-3.0, -1.0, 0.0, 0.7, 3.1):
            q = rules.quaternion_from_yaw(yaw)
            self.assertAlmostEqual(rules.yaw_from_quaternion(*q), yaw)

    def test_has_moved(self):
        a = (0.0, 0.0, 0.0)
        self.assertFalse(rules.has_moved(a, (0.03, 0.03, 0.05), 0.05, math.radians(5)))
        self.assertTrue(rules.has_moved(a, (0.06, 0.0, 0.0), 0.05, math.radians(5)))
        self.assertTrue(rules.has_moved(a, (0.0, 0.0, math.radians(6)), 0.05, math.radians(5)))
        self.assertTrue(rules.has_moved(a, (0.0, 0.0, math.radians(-6)), 0.05, math.radians(5)))
        self.assertTrue(rules.has_moved(None, a, 0.05, 0.1))
        self.assertFalse(rules.has_moved((0, 0, math.pi - 0.01), (0, 0, -math.pi + 0.01), 0.05, 0.1))      # across the +-pi seam

    def test_is_localised_table(self):
        L = rules.is_localised
        self.assertTrue(L(0.1, 2.0, False, 1.0, 5.0))
        self.assertFalse(L(None, 2.0, True, 1.0, 5.0))          # no transform
        self.assertFalse(L(1.5, 2.0, True, 1.0, 5.0))           # stale transform
        self.assertFalse(L(0.1, None, True, 1.0, 5.0))          # never localised
        self.assertFalse(L(0.1, 9.0, False, 1.0, 5.0))          # old localisation, moved
        self.assertTrue(L(0.1, 9.0, True, 1.0, 5.0))            # old localisation, stood still
        self.assertTrue(L(1.0, 5.0, False, 1.0, 5.0))           # exactly at the limits

    def test_a_pose_with_covariance_9999_is_not_a_fix(self):
        def cov(x, y, yaw):
            c = [0.0] * 36
            c[0], c[7], c[35] = x, y, yaw
            return c
        T = rules.pose_is_trusted
        self.assertTrue(T(cov(0.001, 0.002, 0.01), 1.0))
        self.assertTrue(T(cov(1.0, 1.0, 1.0), 1.0))              # at the limit
        self.assertFalse(T(cov(9999.0, 9999.0, 9999.0), 1.0))    # RTAB-Map: lost
        self.assertFalse(T(cov(0.001, 0.001, 9999.0), 1.0))      # any one of them
        self.assertFalse(T(cov(float("nan"), 0.0, 0.0), 1.0))
        self.assertFalse(T([], 1.0))
        self.assertFalse(T(None, 1.0))

    def test_battery(self):
        self.assertEqual(rules.battery_fraction(16.5), 1.0)
        self.assertEqual(rules.battery_fraction(13.2), 0.0)
        self.assertEqual(rules.battery_fraction(20), 1.0)
        self.assertEqual(rules.battery_fraction(10), 0.0)
        self.assertAlmostEqual(rules.battery_fraction(14.85), 0.5)
        self.assertIsNone(rules.battery_fraction(None))
        self.assertIsNone(rules.battery_fraction(float("nan")))
        self.assertIsNone(rules.battery_fraction(0))

    def test_remaining_along(self):
        plan = [(0, 0), (0, 1), (0, 2), (1, 2)]
        self.assertEqual(rules.remaining_along(plan, 0, 0), 3.0)
        self.assertAlmostEqual(rules.remaining_along(plan, 0.2, 1.0), 0.2 + 2.0)
        self.assertIsNone(rules.remaining_along([], 0, 0))
        self.assertEqual(rules.distance_left(plan, 0, 0, 1, 2), 3.0)
        self.assertEqual(rules.distance_left(plan, 0, 0, 5, 5), round(math.hypot(5, 5), 2))     # the plan does not lead to this goal

    def test_grid_view(self):
        data = [0, 100, -1, 0, 0, 0]                              # 3 x 2 cells of 1 m, origin (10, 20)
        g = rules.GridView(3, 2, 1.0, 10.0, 20.0, data)
        self.assertEqual(g.cell(10.5, 20.5), 0)
        self.assertEqual(g.cell(11.5, 20.5), 100)
        self.assertIsNone(g.cell(9.9, 20.5))
        self.assertIsNone(g.cell(10.5, 22.1))
        self.assertTrue(g.refuses(11.5, 20.5))                    # a known obstacle
        self.assertTrue(g.refuses(0, 0))                          # outside the map
        self.assertFalse(g.refuses(12.5, 20.5))                   # unknown space is allowed
        self.assertFalse(g.refuses(10.5, 21.5))
        self.assertFalse(g.refuses(12.99, 21.99))

    def test_failure_reason_priorities(self):
        self.assertEqual(rules.failure_reason("Failed to find a valid plan", True, False), "ros_unavailable")
        self.assertEqual(rules.failure_reason("Failed to find a valid plan", False, True), "lost_localisation")
        self.assertEqual(rules.failure_reason("Failed to find a valid plan", True, True), "no_path")


class ActivityTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(100.0)
        self.act = RobotActivity(clock=self.clock)

    def test_idle(self):
        self.assertFalse(self.act.busy())
        self.assertFalse(self.act.reading_is_robot_noise())

    def test_speaking_then_one_second_of_grace(self):
        self.act.set_speaking(True)
        self.clock.advance(10)
        self.assertTrue(self.act.speaking() and self.act.reading_is_robot_noise())
        self.act.set_speaking(False)
        self.clock.advance(0.9)
        self.assertTrue(self.act.reading_is_robot_noise())
        self.clock.advance(0.2)
        self.assertFalse(self.act.reading_is_robot_noise())

    def test_the_flag_lapses_after_30_s(self):
        self.act.set_speaking(True)
        self.clock.advance(31)
        self.assertFalse(self.act.speaking())

    def test_moving_needs_fresh_odometry(self):
        self.act.on_odom(0.2, 0.0)
        self.assertTrue(self.act.moving())
        self.clock.advance(1.5)
        self.assertFalse(self.act.moving())               # nothing heard for a while: not "moving"
        self.assertTrue(self.act.busy(lookback_s=2.0))

    def test_small_drift_is_not_moving(self):
        self.act.on_odom(0.005, 0.01)
        self.assertFalse(self.act.moving())
        self.act.on_odom(float("nan"), 0.0)
        self.assertFalse(self.act.moving())

    def test_turning_counts(self):
        self.act.on_odom(0.0, 0.3)
        self.assertTrue(self.act.moving())


if __name__ == "__main__":
    unittest.main()
