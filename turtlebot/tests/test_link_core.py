"""LinkCore: the behaviour docs/robot_link.md asks of the Dell, tested without ROS or HTTP."""

import math
import unittest

from support import Clock, FakeBackend, Settings, Unavailable, Unsupported, drive_clock, feed_pose, make_core
from robot_link import calibration
from robot_link.core import KNOWN_GOALS


def goal(core, goal_id="g-1", x=3.0, y=2.0, yaw=0.0):
    return core.handle_goal(goal_id, x, y, yaw)


class HealthTest(unittest.TestCase):
    def test_health_has_the_contract_fields(self):
        core, backend, clock = make_core()
        h = core.health()
        self.assertEqual((h["ok"], h["api_version"], h["ros_ok"], h["localised"], h["battery"]), (True, 1, True, True, 0.8))
        self.assertAlmostEqual(h["time"], clock(), places=2)

    def test_health_answers_when_ros_is_down(self):
        core, backend, _ = make_core(localised=False, heartbeat=False)
        backend.ok, backend.battery_value = False, None
        h = core.health()
        self.assertEqual((h["ros_ok"], h["localised"], h["battery"]), (False, False, None))

    def test_state_before_any_pose_is_valid_and_not_localised(self):
        core, _, _ = make_core(localised=False)
        s = core.state()
        self.assertEqual(s["pose"]["localised"], False)
        self.assertEqual((s["pose"]["x"], s["pose"]["y"], s["pose"]["frame"]), (0.0, 0.0, "map"))
        self.assertEqual(s["nav"], {"status": "idle", "goal_id": "", "distance_left": None, "reason": ""})
        self.assertIsNone(s["noise"])


class LocalisedRuleTest(unittest.TestCase):
    def test_fresh_transform_and_recent_localisation(self):
        core, _, clock = make_core()
        self.assertTrue(core.localised())

    def test_never_localised_is_not_localised_even_with_a_transform(self):
        core, _, clock = make_core(localised=False)
        feed_pose(core, clock, localisation=False)
        self.assertFalse(core.localised())

    def test_old_transform_is_not_localised(self):
        core, _, clock = make_core()
        clock.advance(1.5)
        core.on_odom(1.0, 2.0, 0.5, 0.0, 0.0)
        self.assertFalse(core.localised())

    def test_old_localisation_while_moving_is_lost(self):
        core, _, clock = make_core()
        for _ in range(60):                                   # 6 s of driving 0.1 m/s with no localisation
            clock.advance(0.1)
            x = core.odom[0] + 0.01
            core.on_odom(x, 2.0, 0.5, 0.1, 0.0)
            core.on_tf(x, 2.0, 0.5, clock())
        self.assertFalse(core.localised())

    def test_old_localisation_while_standing_still_is_fine(self):
        core, _, clock = make_core()
        drive_clock(core, clock, 120, step=1.0)               # two minutes without a localisation, wheels say nothing moved
        self.assertTrue(core.localised())

    def test_small_creep_still_counts_as_still_but_5_cm_does_not(self):
        core, _, clock = make_core()
        drive_clock(core, clock, 10, step=0.5)
        core.on_odom(1.04, 2.0, 0.5, 0.0, 0.0)
        core.on_tf(1.04, 2.0, 0.5, clock())
        self.assertTrue(core.localised())
        core.on_odom(1.07, 2.0, 0.5, 0.0, 0.0)
        core.on_tf(1.07, 2.0, 0.5, clock())
        self.assertFalse(core.localised())

    def test_turning_more_than_5_degrees_is_moving(self):
        core, _, clock = make_core()
        drive_clock(core, clock, 10, step=0.5)
        core.on_odom(1.0, 2.0, 0.5 + math.radians(4), 0.0, 0.0)
        core.on_tf(1.0, 2.0, 0.5, clock())
        self.assertTrue(core.localised())
        core.on_odom(1.0, 2.0, 0.5 + math.radians(6), 0.0, 0.0)
        self.assertFalse(core.localised())

    def test_a_new_localisation_resets_the_reference(self):
        core, _, clock = make_core()
        drive_clock(core, clock, 10, step=0.5)
        core.on_odom(2.0, 2.0, 0.5, 0.0, 0.0)                 # moved 1 m, old localisation: lost
        core.on_tf(2.0, 2.0, 0.5, clock())
        self.assertFalse(core.localised())
        core.on_localization_pose()                           # RTAB-Map finds us again here
        self.assertTrue(core.localised())
        drive_clock(core, clock, 20, step=1.0)
        self.assertTrue(core.localised())

    def test_a_pose_rtabmap_sent_while_lost_means_not_localised(self):
        core, _, clock = make_core()
        drive_clock(core, clock, 10, step=0.5)
        self.assertTrue(core.localised())
        core.on_localization_pose(trusted=False)              # covariance 9999: RTAB-Map has lost the robot
        self.assertFalse(core.localised(), "standing still does not make a lost robot found")
        drive_clock(core, clock, 20, step=1.0)
        self.assertFalse(core.localised())
        self.assertEqual(core.debug()["loc_lost"], 1)
        core.on_localization_pose(trusted=True)               # it finds the place again
        self.assertTrue(core.localised())

    def test_stale_odometry_never_counts_as_still(self):
        core, _, clock = make_core()
        for _ in range(100):                                  # the transform stays fresh, the odometry stops arriving
            clock.advance(0.1)
            core.on_tf(1.0, 2.0, 0.5, clock())
        self.assertFalse(core.localised())


class GoalTest(unittest.TestCase):
    def test_accepted_goal_is_sent_and_active(self):
        core, backend, _ = make_core()
        status, body = goal(core, "g-1", 3.0, 2.0, 1.0)
        self.assertEqual((status, body), (200, {"ok": True, "accepted": True, "goal_id": "g-1", "reason": ""}))
        self.assertEqual(backend.sent, [("g-1", 3.0, 2.0, 1.0)])
        nav = core.state()["nav"]
        self.assertEqual((nav["status"], nav["goal_id"]), ("active", "g-1"))

    def test_the_same_goal_id_is_the_same_goal(self):
        core, backend, _ = make_core()
        goal(core)
        status, body = goal(core)
        self.assertEqual((status, body["accepted"], body["duplicate"]), (200, True, True))
        self.assertEqual(len(backend.sent), 1)

    def test_a_duplicate_is_accepted_even_when_the_heartbeat_is_late(self):
        core, backend, clock = make_core()
        goal(core)
        clock.advance(10)
        status, body = goal(core)
        self.assertEqual((body["accepted"], body["duplicate"]), (True, True))

    def test_only_the_last_32_ids_are_remembered(self):
        core, backend, clock = make_core()
        for i in range(KNOWN_GOALS + 1):
            self.assertTrue(goal(core, "g-%d" % i)[1]["accepted"])
            core.handle_cancel("*")
        self.assertNotIn("duplicate", goal(core, "g-0")[1])        # forgotten: a new goal
        self.assertTrue(goal(core, "g-5")[1]["duplicate"])

    def test_a_different_goal_replaces_the_active_one(self):
        core, backend, _ = make_core()
        goal(core, "g-1")
        self.assertTrue(goal(core, "g-2")[1]["accepted"])
        nav = core.state()["nav"]
        self.assertEqual((nav["goal_id"], nav["status"]), ("g-2", "active"))
        self.assertEqual(backend.stopped, 0)                       # no stop in between: the planner switches over
        self.assertEqual([g[0] for g in backend.sent], ["g-1", "g-2"])
        # the old goal's late result from move_base changes nothing
        core.on_goal_result("g-1", "preempted")
        self.assertEqual(core.state()["nav"]["status"], "active")

    def test_refused_without_a_heartbeat(self):
        core, backend, _ = make_core(heartbeat=False)
        status, body = goal(core)
        self.assertEqual((status, body["accepted"], body["reason"]), (200, False, "no_heartbeat"))
        self.assertEqual(backend.sent, [])

    def test_refused_when_not_localised_and_nothing_moves(self):
        core, backend, _ = make_core(localised=False)
        status, body = goal(core)
        self.assertEqual((body["accepted"], body["reason"]), (False, "not_localised"))
        self.assertEqual(backend.sent, [])
        self.assertEqual(core.state()["nav"]["status"], "idle")

    def test_refused_for_a_bad_yaw_and_nothing_moves(self):
        core, backend, _ = make_core()
        self.assertEqual(goal(core, "a", yaw=4.0)[1]["reason"], "invalid_pose")
        self.assertEqual(goal(core, "b", yaw=-3.2)[1]["reason"], "invalid_pose")
        self.assertTrue(goal(core, "c", yaw=math.pi + 1e-4)[1]["accepted"])      # a rounding error is fine
        self.assertEqual(len(backend.sent), 1)

    def test_a_yaw_just_over_pi_is_wrapped_before_it_is_sent(self):
        core, backend, _ = make_core()
        goal(core, "c", yaw=math.pi + 5e-4)
        self.assertLessEqual(abs(backend.sent[0][3]), math.pi)

    def test_refused_outside_the_map(self):
        core, backend, _ = make_core()
        backend.outside = True
        self.assertEqual(goal(core)[1]["reason"], "outside_map")
        self.assertEqual(backend.sent, [])

    def test_ros_down_is_a_503(self):
        core, backend, _ = make_core()
        backend.ok = False
        status, body = goal(core)
        self.assertEqual((status, body["ok"], body["error"]), (503, False, "ros_unavailable"))

    def test_a_backend_that_cannot_send_is_a_503_and_leaves_no_goal(self):
        core, backend, _ = make_core()
        backend.fail_send = True
        self.assertEqual(goal(core)[0], 503)
        self.assertEqual(core.state()["nav"]["status"], "idle")
        backend.fail_send = False
        self.assertTrue(goal(core)[1]["accepted"])                 # the id was not burnt


class CancelTest(unittest.TestCase):
    def test_cancel_stops_the_base_and_reports_cancelled(self):
        core, backend, _ = make_core()
        goal(core, "g-1")
        self.assertEqual(core.handle_cancel("g-1"), (200, {"ok": True, "cancelled": True}))
        self.assertEqual((backend.cancelled, backend.stopped), (1, 1))
        nav = core.state()["nav"]
        self.assertEqual((nav["status"], nav["reason"], nav["goal_id"]), ("cancelled", "cancelled_by_client", "g-1"))

    def test_cancel_with_another_goal_id_does_nothing(self):
        core, backend, _ = make_core()
        goal(core, "g-1")
        self.assertEqual(core.handle_cancel("g-9"), (200, {"ok": True, "cancelled": False}))
        self.assertEqual(core.state()["nav"]["status"], "active")
        self.assertEqual((backend.cancelled, backend.stopped), (0, 0))

    def test_star_cancels_whatever_is_active(self):
        core, backend, _ = make_core()
        goal(core, "g-1")
        self.assertTrue(core.handle_cancel("*")[1]["cancelled"])

    def test_cancel_is_idempotent_and_ok_with_nothing_active(self):
        core, backend, _ = make_core()
        self.assertEqual(core.handle_cancel("*"), (200, {"ok": True, "cancelled": False}))
        goal(core, "g-1")
        core.handle_cancel("g-1")
        self.assertEqual(core.handle_cancel("g-1"), (200, {"ok": True, "cancelled": False}))
        self.assertEqual(backend.cancelled, 1)


class WatchdogTest(unittest.TestCase):
    def test_no_heartbeat_for_3_s_stops_the_base(self):
        core, backend, clock = make_core()
        goal(core)
        drive_clock(core, clock, 2.9, beat=False)
        self.assertEqual(core.state()["nav"]["status"], "active")
        self.assertEqual(backend.stopped, 0)
        drive_clock(core, clock, 0.3, beat=False)
        nav = core.state()["nav"]
        self.assertEqual((nav["status"], nav["reason"]), ("cancelled", "heartbeat_timeout"))
        self.assertEqual((backend.cancelled, backend.stopped), (1, 1))

    def test_heartbeats_keep_the_goal_going(self):
        core, backend, clock = make_core()
        goal(core)
        drive_clock(core, clock, 30, beat=True)
        self.assertEqual(core.state()["nav"]["status"], "active")
        self.assertEqual(backend.stopped, 0)

    def test_after_the_watchdog_goals_are_refused_until_a_heartbeat_and_nothing_restarts(self):
        core, backend, clock = make_core()
        goal(core, "g-1")
        drive_clock(core, clock, 3.5, beat=False)
        self.assertEqual(goal(core, "g-2")[1]["reason"], "no_heartbeat")
        core.handle_heartbeat()
        self.assertEqual(core.state()["nav"]["status"], "cancelled")      # the heartbeat does not revive the goal
        self.assertTrue(goal(core, "g-3")[1]["accepted"])

    def test_the_watchdog_does_nothing_without_a_goal(self):
        core, backend, clock = make_core()
        drive_clock(core, clock, 10, beat=False)
        self.assertEqual((backend.cancelled, backend.stopped), (0, 0))

    def test_the_watchdog_also_fires_from_a_request_without_the_ticker(self):
        core, backend, clock = make_core()
        goal(core)
        clock.advance(3.2)
        core.on_tf(1.0, 2.0, 0.5, clock())
        self.assertEqual(core.state()["nav"]["reason"], "heartbeat_timeout")


class OutcomeTest(unittest.TestCase):
    def active(self):
        core, backend, clock = make_core()
        goal(core, "g-1")
        return core, backend, clock

    def test_succeeded_stays_reported_until_another_goal(self):
        core, backend, clock = self.active()
        core.on_goal_result("g-1", "succeeded")
        drive_clock(core, clock, 30)
        nav = core.state()["nav"]
        self.assertEqual((nav["status"], nav["goal_id"], nav["distance_left"], nav["reason"]), ("succeeded", "g-1", None, ""))
        goal(core, "g-2")
        self.assertEqual(core.state()["nav"]["goal_id"], "g-2")

    def test_aborted_reasons(self):
        for text, reason in (("Failed to find a valid plan. Even after executing recovery behaviors.", "no_path"),
                             ("Robot is oscillating. Even after executing recovery behaviors.", "blocked"),
                             ("Failed to find a valid control. Even after executing recovery behaviors.", "blocked"),
                             ("something else", "aborted"), ("", "aborted")):
            core, backend, clock = self.active()
            core.on_goal_result("g-1", "aborted", text)
            nav = core.state()["nav"]
            self.assertEqual((nav["status"], nav["reason"]), ("failed", reason), text)

    def test_aborted_while_not_localised_is_lost_localisation(self):
        core, backend, clock = self.active()
        clock.advance(2.0)                                     # the transform goes stale: not localised
        core.on_goal_result("g-1", "aborted", "Failed to find a valid plan")
        self.assertEqual(core.state()["nav"]["reason"], "lost_localisation")

    def test_rejected_is_failed(self):
        core, backend, clock = self.active()
        core.on_goal_result("g-1", "rejected", "bad quaternion")
        self.assertEqual(core.state()["nav"]["status"], "failed")

    def test_a_goal_taken_over_by_the_web_page_is_cancelled_manual_override(self):
        core, backend, clock = self.active()
        core.on_goal_result("g-1", "preempted", "")
        nav = core.state()["nav"]
        self.assertEqual((nav["status"], nav["reason"]), ("cancelled", "manual_override"))
        self.assertEqual((backend.cancelled, backend.stopped), (0, 0))   # we do not fight the person with the web page

    def test_a_late_result_for_a_goal_we_cancelled_changes_nothing(self):
        core, backend, clock = self.active()
        core.handle_cancel("g-1")
        core.on_goal_result("g-1", "preempted")
        core.on_goal_result("g-1", "succeeded")
        nav = core.state()["nav"]
        self.assertEqual((nav["status"], nav["reason"]), ("cancelled", "cancelled_by_client"))

    def test_a_result_for_another_goal_is_ignored(self):
        core, backend, clock = self.active()
        core.on_goal_result("g-other", "succeeded")
        self.assertEqual(core.state()["nav"]["status"], "active")

    def test_losing_localisation_while_driving_stops_the_goal(self):
        core, backend, clock = self.active()
        for _ in range(125):                                   # 12.5 s: localisation 5 s old counts as lost, and 5 s later the goal fails
            clock.advance(0.1)
            x = core.odom[0] + 0.02
            core.on_odom(x, 2.0, 0.5, 0.2, 0.0)
            core.on_tf(x, 2.0, 0.5, clock())
            core.handle_heartbeat()
            core.tick()
        nav = core.state()["nav"]
        self.assertEqual((nav["status"], nav["reason"]), ("failed", "lost_localisation"))
        self.assertEqual(backend.stopped, 1)

    def test_ros_going_away_fails_the_goal(self):
        core, backend, clock = self.active()
        backend.ok = False
        drive_clock(core, clock, 3)
        nav = core.state()["nav"]
        self.assertEqual((nav["status"], nav["reason"]), ("failed", "ros_unavailable"))
        self.assertEqual(backend.stopped, 1)

    def test_a_goal_that_takes_too_long_times_out(self):
        core, backend, clock = make_core(Settings(goal_timeout_s=20))
        goal(core)
        drive_clock(core, clock, 25)
        self.assertEqual(core.state()["nav"]["reason"], "timeout")


class DistanceTest(unittest.TestCase):
    def test_straight_line_without_a_plan(self):
        core, _, _ = make_core()
        goal(core, "g-1", 4.0, 2.0)
        self.assertEqual(core.state()["nav"]["distance_left"], 3.0)

    def test_along_the_plan_when_it_leads_to_the_goal(self):
        core, _, clock = make_core()
        goal(core, "g-1", 4.0, 2.0)
        core.on_plan([(1.0, 2.0), (1.0, 4.0), (4.0, 4.0), (4.0, 2.0)])      # a detour around something: 2 + 3 + 2 m
        self.assertEqual(core.state()["nav"]["distance_left"], 7.0)

    def test_a_plan_to_another_goal_or_an_old_plan_is_not_used(self):
        core, _, clock = make_core()
        goal(core, "g-1", 4.0, 2.0)
        core.on_plan([(1.0, 2.0), (1.0, 9.0)])
        self.assertEqual(core.state()["nav"]["distance_left"], 3.0)
        core.on_plan([(1.0, 2.0), (1.0, 4.0), (4.0, 4.0), (4.0, 2.0)])
        drive_clock(core, clock, 4.0)
        self.assertEqual(core.state()["nav"]["distance_left"], 3.0)

    def test_distance_is_null_when_not_active(self):
        core, _, _ = make_core()
        self.assertIsNone(core.state()["nav"]["distance_left"])


class SpeakingAndNoiseTest(unittest.TestCase):
    def test_speaking_is_a_flag_and_is_published(self):
        core, backend, _ = make_core()
        for value in (True, True, False, False):
            self.assertEqual(core.handle_speaking(value), (200, {"ok": True}))
        self.assertEqual(backend.speaking_calls[0], True)
        self.assertEqual(backend.speaking_calls[-1], False)

    def test_no_reading_is_null_and_a_stale_one_too(self):
        core, _, clock = make_core()
        self.assertIsNone(core.state()["noise"])
        core.on_noise(-40.0, -41.0, -39.0)
        self.assertIsNotNone(core.state()["noise"])
        drive_clock(core, clock, 3.5)
        self.assertIsNone(core.state()["noise"])

    def test_uncalibrated_readings_are_dbfs(self):
        core, _, _ = make_core()
        core.on_noise(-40.0, -41.0, -39.0)
        n = core.state()["noise"]
        self.assertEqual((n["unit"], n["calibrated"], n["overall"], n["left"], n["right"], n["balance"]), ("dBFS", False, -40.0, -41.0, -39.0, -2.0))
        self.assertEqual(n["robot_noise"], False)

    def test_calibrated_readings_are_db(self):
        core, _, _ = make_core()
        core.set_calibration(calibration.Calibration(80.0, 82.0))
        core.on_noise(-40.0, -41.0, -39.0)
        n = core.state()["noise"]
        self.assertEqual((n["unit"], n["calibrated"], n["left"], n["right"], n["balance"]), ("dB", True, 39.0, 43.0, -4.0))
        self.assertAlmostEqual(n["overall"], 41.4, delta=0.1)           # the average power of 39 and 43 dB
        core.set_calibration(None)
        self.assertEqual(core.state()["noise"]["unit"], "dBFS")

    def test_a_reading_keeps_its_time(self):
        core, _, clock = make_core()
        core.on_noise(-40.0, -41.0, -39.0)
        t = core.state()["noise"]["t"]
        clock.advance(0.2)
        self.assertEqual(core.state()["noise"]["t"], t)

    def test_robot_noise_while_speaking_and_one_second_after(self):
        core, _, clock = make_core()
        core.handle_speaking(True)
        core.on_noise(-30.0, -30.0, -30.0)
        self.assertTrue(core.state()["noise"]["robot_noise"])
        clock.advance(5)
        core.handle_speaking(False)
        core.on_noise(-30.0, -30.0, -30.0)
        self.assertTrue(core.state()["noise"]["robot_noise"])
        clock.advance(0.9)
        core.on_noise(-30.0, -30.0, -30.0)
        self.assertTrue(core.state()["noise"]["robot_noise"])
        clock.advance(0.2)
        core.on_noise(-45.0, -45.0, -45.0)
        self.assertFalse(core.state()["noise"]["robot_noise"])

    def test_robot_noise_while_moving_and_after(self):
        core, _, clock = make_core()
        core.on_odom(1.0, 2.0, 0.5, 0.2, 0.0)
        core.on_noise(-35.0, -35.0, -35.0)
        self.assertTrue(core.state()["noise"]["robot_noise"])
        clock.advance(0.5)
        core.on_odom(1.0, 2.0, 0.5, 0.0, 0.0)
        core.on_noise(-45.0, -45.0, -45.0)
        self.assertTrue(core.state()["noise"]["robot_noise"])
        clock.advance(1.0)
        core.on_odom(1.0, 2.0, 0.5, 0.0, 0.0)
        core.on_noise(-45.0, -45.0, -45.0)
        self.assertFalse(core.state()["noise"]["robot_noise"])

    def test_turning_on_the_spot_is_moving(self):
        core, _, clock = make_core()
        core.on_odom(1.0, 2.0, 0.5, 0.0, 0.4)
        core.on_noise(-35.0, -35.0, -35.0)
        self.assertTrue(core.state()["noise"]["robot_noise"])

    def test_a_speaking_flag_nobody_clears_lapses_after_30_s(self):
        core, backend, clock = make_core()
        core.handle_speaking(True)
        drive_clock(core, clock, 29, step=1.0)
        core.on_noise(-40.0, -40.0, -40.0)
        self.assertTrue(core.state()["noise"]["robot_noise"])
        drive_clock(core, clock, 2, step=1.0)
        self.assertEqual(backend.speaking_calls[-1], False)            # /robot_speaking is cleared too
        drive_clock(core, clock, 2, step=1.0)
        core.on_noise(-40.0, -40.0, -40.0)
        self.assertFalse(core.state()["noise"]["robot_noise"])


class MaxSpeedTest(unittest.TestCase):
    def test_applies_the_request(self):
        core, backend, _ = make_core()
        self.assertEqual(core.handle_max_speed(0.2), (200, {"ok": True, "applied": 0.2}))

    def test_is_clamped_to_the_configured_maximum(self):
        core, backend, _ = make_core()
        self.assertEqual(core.handle_max_speed(0.6)[1]["applied"], 0.25)
        self.assertEqual(backend.speeds, [0.25])
        core, backend, _ = make_core(Settings(max_speed_limit=0.4))
        self.assertEqual(core.handle_max_speed(0.6)[1]["applied"], 0.4)

    def test_is_not_pushed_below_a_speed_that_moves(self):
        core, backend, _ = make_core()
        self.assertEqual(core.handle_max_speed(0.01)[1]["applied"], 0.05)

    def test_unsupported_is_404_and_unavailable_is_503(self):
        core, backend, _ = make_core()
        backend.speed_result = Unsupported()
        status, body = core.handle_max_speed(0.3)
        self.assertEqual((status, body["error"]), (404, "not_found"))
        backend.speed_result = Unavailable("move_base is not running")
        status, body = core.handle_max_speed(0.3)
        self.assertEqual((status, body["error"]), (503, "ros_unavailable"))

    def test_the_reported_value_is_what_ros_applied(self):
        core, backend, _ = make_core()
        backend.speed_result = 0.22
        self.assertEqual(core.handle_max_speed(0.3)[1]["applied"], 0.22)


if __name__ == "__main__":
    unittest.main()
