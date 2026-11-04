"""RETURNING with the turtlebot backend: the dialogue waits for the base (NavArrived), cancels and resends around a visitor,
retries and skips a shelf that cannot be reached, and waits out a lost link. The link is a stub that records what it is asked."""

import asyncio
import logging
import unittest

from robot.actuation.navigation import TurtleBotNavigation
from robot.app import build_app, fake_drivers
from robot.actuation.face import FaceDisplay
from robot.config import load_config
from robot.events import (ContextChanged, LinkDown, LinkUp, NavArrived, NavCancelled, NavFailed, PersonEngaged,
                          PersonNearbyBusy, PoseUpdated, ReplySentence, ScreenUpdate, UserChoice)
from tests.test_dialogue import advance, run
from tests.test_state_machine import go, sent, watch


class StubLink:
    """What the dialogue needs of the link, recording instead of talking."""

    def __init__(self):
        self.goals, self.cancelled, self.rejected, self.speed, self.speaking = [], [], [], [], []
        self.n = 0
        self.up, self.is_localised = True, True
        self.pose, self.localised = {}, True
        self.settings = type("S", (), {"base_url": "http://stub"})()

    @property
    def status(self):
        return "connected" if self.up else "lost"

    def available(self):
        return self.up and self.is_localised

    def new_goal_id(self):
        self.n += 1
        return f"g-{self.n}"

    def send_goal(self, goal_id, x, y, yaw):
        self.goals.append((goal_id, x, y, yaw))

    def cancel(self, goal_id=None):
        self.cancelled.append(goal_id)

    def reject(self, goal_id, reason):
        self.rejected.append((goal_id, reason))

    def set_max_speed(self, m_s):
        self.speed.append(m_s)

    def set_speaking(self, flag):
        self.speaking.append(flag)

    def info(self):
        return {"backend": "turtlebot", "url": "http://stub", "status": self.status, "pose": {}, "nav": {}, "noise": None}


def link_app(zone="common", books=2, **settings):
    logging.disable(logging.INFO)
    cfg = load_config()
    cfg.robot.setdefault("books", {})["online_fallback"] = False
    cfg.robot.setdefault("screen", {})["greeting_hold_s"] = 0
    link = StubLink()
    app = watch(build_app(cfg, fake_drivers(), face=FaceDisplay(enabled=False), log_sessions=False, persist_storage=False, link=link))
    app.now = [1000.0]
    app.dialogue.clock = lambda: app.now[0]
    app.dialogue.settings.returning["auto_start"] = True
    app.dialogue.settings.returning.update(settings)
    run(app, ContextChanged(zone, 1))
    app.dialogue.fill_storage(books)
    return app, link


def navigate(app):
    go(app, "PLAN")
    assert app.dialogue.state == "RETURNING.NAVIGATING", app.dialogue.state


def publish(app, event):
    run(app, event)


class ArrivalTest(unittest.TestCase):
    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_the_navigation_is_the_base_not_the_simulator(self):
        app, link = link_app()
        self.assertIsInstance(app.actuators.nav, TurtleBotNavigation)
        self.assertEqual(sent(app, "navigate"), [])                              # nothing for the ESP32

    def test_the_drive_waits_for_the_base_and_not_for_the_timer(self):
        app, link = link_app()
        navigate(app)
        self.assertEqual(len(link.goals), 1)
        stop = app.dialogue.stops[0]
        self.assertEqual(link.goals[0][1:], (stop.x, stop.y, stop.yaw))
        advance(app, 30)                                                         # far longer than sim_drive_s
        self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")
        publish(app, NavArrived(link.goals[0][0]))
        self.assertEqual(app.dialogue.state, "RETURNING.AT_SHELF")

    def test_an_arrival_for_another_goal_is_ignored(self):
        app, link = link_app()
        navigate(app)
        publish(app, NavArrived("g-99"))
        self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")

    def test_the_whole_trip_goes_through_the_shelves_and_home_on_arrivals(self):
        app, link = link_app()
        navigate(app)
        shelves = []
        for _ in range(6):
            state = app.dialogue.state
            if state == "IDLE":
                break
            if state in ("RETURNING.NAVIGATING", "RETURNING.HOMING"):
                shelves.append(app.nav[-1].shelf or app.nav[-1].title)
                publish(app, NavArrived(link.goals[-1][0]))
            else:
                advance(app, 4)                                                   # the unload is still a timer
        self.assertEqual(app.dialogue.state, "IDLE")
        self.assertTrue(app.dialogue.storage.empty)
        self.assertEqual(shelves[-1], "home")
        self.assertEqual(len(link.goals), len(shelves))
        self.assertEqual(len({g[0] for g in link.goals}), len(link.goals))        # every drive has its own goal_id

    def test_a_drive_that_never_ends_fails_after_drive_timeout(self):
        app, link = link_app(drive_timeout_s=60, nav_retries=0)
        navigate(app)
        advance(app, 61)
        self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")             # skipped the first shelf, on to the second
        self.assertEqual(len(app.dialogue.skipped), 1)
        self.assertIn("timed out", app.dialogue.skipped_log[0]["reason"])

    def test_the_simulator_still_drives_by_timer(self):
        from tests.test_dialogue import make_app
        app = watch(make_app())
        app.dialogue.settings.returning["auto_start"] = True
        run(app, ContextChanged("common", 1))
        app.dialogue.fill_storage(2)
        navigate(app)
        advance(app, 7)
        self.assertEqual(app.dialogue.state, "RETURNING.AT_SHELF")
        self.assertEqual(len(sent(app, "navigate")), 1)


class InterruptionTest(unittest.TestCase):
    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_a_person_engaging_cancels_the_goal_and_resuming_sends_the_same_target_with_a_new_id(self):
        for zone in ("common", "quiet"):
            with self.subTest(zone=zone):
                app, link = link_app(zone)
                navigate(app)
                first = link.goals[0]
                advance(app, 2)
                publish(app, PersonEngaged())
                self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")
                self.assertEqual(link.cancelled, [first[0]])
                publish(app, NavArrived(first[0]))                                # it got there just before the cancel: too late
                self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")
                run(app, UserChoice("done"))
                advance(app, 5)
                self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")
                self.assertEqual(len(link.goals), 2)
                self.assertEqual(link.goals[1][1:], first[1:])                    # the same pose
                self.assertNotEqual(link.goals[1][0], first[0])                   # a new goal_id
                publish(app, NavArrived(link.goals[1][0]))
                self.assertEqual(app.dialogue.state, "RETURNING.AT_SHELF")

    def test_someone_nearby_and_busy_does_not_stop_the_base_and_its_arrival_is_not_lost(self):
        app, link = link_app()
        navigate(app)
        publish(app, PersonNearbyBusy())
        self.assertEqual(app.dialogue.state, "RETURNING.ANNOUNCE")
        publish(app, NavArrived(link.goals[0][0]))                                # the base got there during "excuse me"
        advance(app, 4)
        self.assertEqual(app.dialogue.state, "RETURNING.AT_SHELF")
        self.assertEqual(link.cancelled, [])
        self.assertEqual(len(link.goals), 1)


class FailureTest(unittest.TestCase):
    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_a_failed_drive_is_tried_once_more_then_the_shelf_is_skipped_and_its_books_stay(self):
        app, link = link_app()
        navigate(app)
        first_shelf = app.dialogue.stops[0].shelf
        in_storage = len(app.dialogue.storage)
        publish(app, NavFailed(link.goals[0][0], "no_path"))
        self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")
        self.assertEqual(len(link.goals), 2)                                       # tried again, a new id, the same place
        self.assertEqual(link.goals[1][1:], link.goals[0][1:])
        self.assertNotEqual(link.goals[1][0], link.goals[0][0])
        publish(app, NavFailed(link.goals[1][0], "blocked"))
        self.assertEqual(len(link.goals), 3)                                       # skipped: on to the next shelf
        self.assertNotEqual(link.goals[2][1:], link.goals[0][1:])
        self.assertEqual(app.dialogue.current_shelf, app.dialogue.stops[0].shelf)
        self.assertNotEqual(app.dialogue.current_shelf, first_shelf)
        self.assertEqual([k["shelf"] for k in app.dialogue.skipped_log], [first_shelf])
        self.assertEqual(app.dialogue.skipped_log[0]["reason"], "blocked")
        self.assertEqual(len(app.dialogue.storage), in_storage)                    # nothing was put back
        publish(app, NavArrived(link.goals[2][0]))
        advance(app, 4)
        self.assertEqual(app.dialogue.state, "RETURNING.HOMING")
        publish(app, NavArrived(link.goals[3][0]))
        self.assertEqual(app.dialogue.state, "IDLE")
        self.assertEqual(len(app.dialogue.storage), in_storage - 1)                 # the skipped shelf's book is still on board
        self.assertEqual([b.shelf for b in app.dialogue.storage.stored()], [first_shelf])

    def test_it_is_flagged_on_the_operator_page_data(self):
        app, link = link_app()
        navigate(app)
        publish(app, NavFailed(link.goals[0][0], "no_path"))
        publish(app, NavFailed(link.goals[1][0], "no_path"))
        info = app.state_info()["link"]["returning"]
        self.assertEqual(len(info["skipped"]), 1)
        self.assertEqual(info["skipped"][0]["reason"], "no_path")
        self.assertEqual(info["skipped"][0]["books"], 1)

    def test_a_skipped_shelf_is_not_planned_again_in_the_same_trip(self):
        app, link = link_app(books=1)
        navigate(app)
        publish(app, NavFailed(link.goals[0][0], "no_path"))
        publish(app, NavFailed(link.goals[1][0], "no_path"))
        self.assertEqual(app.dialogue.state, "RETURNING.HOMING")                    # nothing else to visit
        self.assertEqual(app.nav[-1].title, "home")
        publish(app, NavArrived(link.goals[-1][0]))
        self.assertEqual(app.dialogue.state, "IDLE")
        self.assertEqual(len(app.dialogue.storage), 1)

    def test_a_failure_on_the_way_home_is_tried_once_more_then_it_stays_where_it_is(self):
        app, link = link_app(books=1)
        navigate(app)
        publish(app, NavArrived(link.goals[0][0]))
        advance(app, 4)
        self.assertEqual(app.dialogue.state, "RETURNING.HOMING")
        publish(app, NavFailed(link.goals[-1][0], "no_path"))
        self.assertEqual(app.dialogue.state, "RETURNING.HOMING")
        publish(app, NavFailed(link.goals[-1][0], "no_path"))
        self.assertEqual(app.dialogue.state, "IDLE")

    def test_a_base_that_is_not_localised_is_waited_for_not_skipped(self):
        app, link = link_app()
        navigate(app)
        link.is_localised = False
        publish(app, NavFailed(link.goals[0][0], "not_localised"))
        self.assertEqual(app.dialogue.skipped, set())
        self.assertEqual(app.dialogue.nav_blocked, "not_localised")
        advance(app, 5)
        self.assertEqual(len(link.goals), 1)                                       # still waiting
        link.is_localised = True
        advance(app, 3)
        self.assertEqual(len(link.goals), 2)                                       # sent again once it knows where it is
        self.assertIsNone(app.dialogue.nav_blocked)

    def test_a_goal_the_base_cancelled_by_itself_counts_as_a_failure_but_its_watchdog_does_not(self):
        app, link = link_app()
        navigate(app)
        publish(app, NavCancelled(link.goals[0][0], "cancelled_by_client"))        # someone cancelled it on the Dell
        self.assertEqual(len(link.goals), 2)                                       # retried
        publish(app, NavCancelled(link.goals[1][0], "heartbeat_timeout"))
        self.assertEqual(app.dialogue.nav_blocked, "link_down")                    # a link matter, no shelf is skipped
        self.assertEqual(app.dialogue.skipped, set())


class LinkLostTest(unittest.TestCase):
    def tearDown(self):
        logging.disable(logging.NOTSET)

    def status_view(self, app):
        updates = [u for u in app.screens if u.view == "status"]
        return updates[-1].payload if updates else None

    def test_the_trip_pauses_with_a_status_and_resumes_by_resending_when_the_link_is_back(self):
        app, link = link_app("quiet")
        navigate(app)
        first = link.goals[0]
        link.up = False
        publish(app, LinkDown("no reply"))
        self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")               # it waits where it is
        status = self.status_view(app)
        self.assertEqual(status["heading"], "Navigation unavailable")
        self.assertEqual(app.dialogue.returning_info()["blocked"], "link_down")
        advance(app, 10)
        self.assertEqual(len(link.goals), 1)                                       # no goal while the link is down
        link.up = True
        publish(app, LinkUp("http://stub"))
        self.assertEqual(len(link.goals), 2)
        self.assertEqual(link.goals[1][1:], first[1:])                             # the same target, a new goal_id
        self.assertNotEqual(link.goals[1][0], first[0])
        self.assertEqual(self.status_view(app)["heading"], "Returning books")
        publish(app, NavArrived(link.goals[1][0]))
        self.assertEqual(app.dialogue.state, "RETURNING.AT_SHELF")

    def test_nothing_is_spoken_in_a_quiet_zone_but_a_common_one_may_say_it(self):
        for zone, spoken in (("quiet", False), ("common", True)):
            with self.subTest(zone=zone):
                app, link = link_app(zone)
                navigate(app)
                app.said.clear()
                link.up = False
                publish(app, LinkDown("no reply"))
                self.assertEqual(any("can't drive" in s for s in app.said), spoken)
                self.assertEqual(self.status_view(app)["heading"], "Navigation unavailable")

    def test_a_link_that_is_down_when_the_trip_starts_waits_before_the_first_goal(self):
        app, link = link_app()
        link.up = False
        navigate(app)
        self.assertEqual(link.goals, [])
        self.assertEqual(self.status_view(app)["heading"], "Navigation unavailable")
        link.up = True
        advance(app, 3)
        self.assertEqual(len(link.goals), 1)

    def test_the_link_returning_while_a_visitor_is_being_helped_does_nothing(self):
        app, link = link_app()
        navigate(app)
        publish(app, PersonEngaged())
        link.up = False
        publish(app, LinkDown("x"))
        link.up = True
        publish(app, LinkUp("x"))
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")
        self.assertEqual(len(link.goals), 1)

    def test_link_down_while_the_robot_is_idle_changes_nothing(self):
        app, link = link_app()
        link.up = False
        publish(app, LinkDown("x"))
        self.assertEqual(app.dialogue.state, "IDLE")
        self.assertIsNone(app.dialogue.nav_blocked)


class GoalSafetyTest(unittest.TestCase):
    def tearDown(self):
        logging.disable(logging.NOTSET)

    def nav(self, **kw):
        link = StubLink()
        from robot.app import build_plan
        return TurtleBotNavigation(link, build_plan(load_config()), **kw), link

    def test_a_goal_outside_the_floor_plan_is_rejected_and_never_sent(self):
        nav, link = self.nav()
        logging.disable(logging.NOTSET)
        with self.assertLogs("robot.nav", "WARNING"):
            goal = nav.go_to_pose(50.0, 1.0, 0.0, 2)
        self.assertEqual(link.goals, [])
        self.assertEqual([(g, r.split(":")[0]) for g, r in link.rejected], [(goal, "rejected")])
        self.assertIn("outside level 2", link.rejected[0][1])

    def test_nan_and_infinite_poses_are_rejected(self):
        nav, link = self.nav()
        for bad in (float("nan"), float("inf")):
            nav.go_to_pose(bad, 1.0, 0.0, 1)
            nav.go_to_pose(1.0, 1.0, bad, 1)
        self.assertEqual((link.goals, len(link.rejected)), ([], 4))

    def test_a_goal_too_far_from_the_base_is_rejected(self):
        nav, link = self.nav(max_goal_distance_m=5.0)
        link.pose, link.localised = {"x": -6.0, "y": 1.0}, True
        nav.go_to_pose(2.0, 1.0, 0.0, 1)
        self.assertEqual(len(link.rejected), 1)
        self.assertIn("5 m limit", link.rejected[0][1])
        nav.go_to_pose(-3.0, 1.0, 0.0, 1)
        self.assertEqual(len(link.goals), 1)

    def test_a_goal_on_another_floor_than_the_base_is_rejected(self):
        nav, link = self.nav(floor=1)
        nav.go_to_pose(2.0, 1.5, 0.0, 2)
        self.assertEqual((link.goals, len(link.rejected)), ([], 1))
        nav.go_to_pose(2.0, 1.5, 0.0, 1)
        self.assertEqual(len(link.goals), 1)

    def test_a_good_goal_is_sent_with_a_new_id_each_time_and_a_stop_cancels_it(self):
        nav, link = self.nav()
        a = nav.go_to_pose(2.0, 1.5, 0.0, 1)
        b = nav.go_to_pose(2.0, 1.5, 0.0, 1)
        self.assertNotEqual(a, b)
        self.assertEqual(nav.current_goal, b)
        nav.stop()
        self.assertEqual((link.cancelled, nav.current_goal), ([b], None))
        nav.stop()
        self.assertEqual(link.cancelled, [b])                                       # nothing to cancel the second time

    def test_a_rejected_goal_makes_the_trip_retry_then_skip_it(self):
        app, link = link_app()
        app.actuators.nav.max_goal_distance_m = 0.0001
        link.pose, link.localised = {"x": 0.0, "y": 0.0}, True
        navigate(app)
        self.assertEqual(len(link.rejected), 1)                                    # the stub does not publish: the real link does
        publish(app, NavFailed(link.rejected[0][0], link.rejected[0][1]))
        self.assertEqual(len(link.rejected), 2)                                    # retried (and rejected again)
        publish(app, NavFailed(link.rejected[1][0], link.rejected[1][1]))
        self.assertEqual(len(app.dialogue.skipped_log), 1)

    def test_the_style_speed_limit_is_sent_to_the_base_when_it_changes(self):
        app, link = link_app("common")
        run(app, ContextChanged("quiet", 1))
        self.assertEqual(link.speed, [0.3])
        run(app, ContextChanged("quiet", 1))
        self.assertEqual(link.speed, [0.3])                                          # not again for the same limit
        run(app, ContextChanged("common", 1))
        self.assertEqual(link.speed, [0.3, 0.6])


if __name__ == "__main__":
    unittest.main()
