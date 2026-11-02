"""The Jetson's link client (robot/drivers/turtlebot_link.py): requests, retries, timeouts, status, events, goals.

Most tests drive the client by hand with a scripted HTTP object and a clock they move, so nothing waits; the last ones run the
real threads against tools/fake_base.py."""

import socket
import threading
import time
import unittest

from robot.config import load_config
from robot.drivers.turtlebot_link import (ApiError, Command, Http, LinkConfigError, LinkError, LinkSettings, TurtleBotLink,
                                          Unreachable)
from robot.events import (LinkDown, LinkUp, NavArrived, NavCancelled, NavFailed, NavProgress, NoiseReading, PoseUpdated)
from tools.fake_base import FakeBase, FakeBaseServer

GOOD_STATE = {"pose": {"x": 1.0, "y": 2.0, "yaw": 0.5, "frame": "map", "localised": True},
              "nav": {"status": "idle", "goal_id": "", "distance_left": None, "reason": ""},
              "noise": {"overall": 41.0, "left": 41.0, "right": 41.0, "balance": 0.0, "unit": "dB", "calibrated": True,
                        "robot_noise": False, "t": 100.0}}


class ScriptedHttp:
    """Answers requests from a script: a dict (the reply), an exception (raised) or a callable (given the path and body)."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def push(self, *items):
        """From now on answer with these (the last one repeats)."""
        self.script = list(items)

    def _next(self, method, path, body):
        self.calls.append((method, path, body))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if callable(item):
            item = item(path, body)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, path):
        return self._next("GET", path, None)

    def post(self, path, body):
        return self._next("POST", path, body)

    def paths(self):
        return [p for _, p, _ in self.calls]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make_link(*script, retries=2, lost_after_s=3.0):
    http = ScriptedHttp(*(script or ({"ok": True},)))
    clock = Clock()
    link = TurtleBotLink(LinkSettings(base_url="http://base", token="t", retries=retries, lost_after_s=lost_after_s), http=http,
                         clock=clock, wall=lambda: 1.7e9 + clock.t, sleep=lambda s: clock.advance(s))
    link.events = []
    link.sink = link.events.append
    return link, http, clock


def kinds(link):
    return [type(e).__name__ for e in link.events]


class SettingsTest(unittest.TestCase):
    def test_the_token_comes_only_from_the_environment(self):
        cfg = load_config()
        with self.assertRaises(LinkConfigError) as caught:
            LinkSettings.from_config(cfg, env={})
        self.assertIn("ROBOT_LINK_TOKEN", str(caught.exception))
        settings = LinkSettings.from_config(cfg, env={"ROBOT_LINK_TOKEN": " s3cret "})
        self.assertEqual(settings.token, "s3cret")
        self.assertNotIn("token", cfg.turtlebot)
        self.assertNotIn("token:", cfg.path("config/robot.yaml").read_text())               # no key to put a secret in

    def test_the_settings_come_from_the_config_and_a_url_can_override_them(self):
        cfg = load_config()
        s = LinkSettings.from_config(cfg, env={"ROBOT_LINK_TOKEN": "x"}, base_url="http://10.0.0.5:9/")
        self.assertEqual((s.base_url, s.timeout_s, s.poll_hz, s.heartbeat_hz, s.lost_after_s, s.retries),
                         ("http://10.0.0.5:9", 1.0, 2.0, 1.0, 3.0, 2))
        self.assertRegex(cfg.turtlebot["base_url"], r"^http://[\w.\-]+:8010$")             # an address in the config, not in the code
        self.assertEqual(cfg.navigation["backend"], "sim")                    # the default stays the simulator


class HeartbeatAndStatusTest(unittest.TestCase):
    def test_a_heartbeat_posts_the_time_and_is_not_retried(self):
        link, http, _ = make_link(LinkError("refused"))
        self.assertFalse(link.heartbeat_once())
        self.assertEqual(http.calls, [("POST", "/api/heartbeat", {"t": 1.7e9 + 1000.0})])      # once: the next beat is the retry

    def test_the_status_goes_connected_degraded_lost_and_back(self):
        link, http, clock = make_link({"ok": True})
        self.assertEqual(link.status, "lost")                                   # nothing has answered yet
        self.assertEqual(kinds(link), [])                                       # ...and that is not a LinkDown
        link.heartbeat_once()
        self.assertEqual((link.status, kinds(link)), ("connected", ["LinkUp"]))
        http.push(LinkError("timed out"))
        clock.advance(1)
        link.heartbeat_once()
        self.assertEqual(link.status, "degraded")
        clock.advance(2.5)
        link.heartbeat_once()                                                   # failing for 3.5 s since the last good reply
        self.assertEqual(link.status, "lost")
        self.assertEqual(kinds(link), ["LinkUp", "LinkDown"])
        link.heartbeat_once()
        link.heartbeat_once()
        self.assertEqual(kinds(link), ["LinkUp", "LinkDown"])                   # said once, not on every failed beat
        http.push({"ok": True})
        clock.advance(0.5)
        link.heartbeat_once()
        self.assertEqual((link.status, kinds(link)), ("connected", ["LinkUp", "LinkDown", "LinkUp"]))

    def test_link_down_says_to_check_the_dells_ip_when_nothing_answers_at_all(self):
        link, http, clock = make_link({"ok": True})
        link.heartbeat_once()
        http.push(Unreachable("base unreachable at http://base: [Errno 111] Connection refused"))
        clock.advance(3.5)
        link.heartbeat_once()
        down = [e for e in link.events if isinstance(e, LinkDown)]
        self.assertEqual(len(down), 1)
        self.assertIn("base unreachable at http://base", down[0].reason)
        self.assertIn("check the Dell's IP", down[0].reason)
        self.assertIn("robot_link_api", down[0].reason)

    def test_link_down_for_another_reason_has_no_ip_advice(self):
        link, http, clock = make_link({"ok": True})
        link.heartbeat_once()
        http.push(LinkError("the reply is not JSON"))
        clock.advance(3.5)
        link.heartbeat_once()
        self.assertNotIn("Dell's IP", [e for e in link.events if isinstance(e, LinkDown)][0].reason)

    def test_a_closed_port_raises_unreachable_with_the_address_in_the_message(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        from robot.drivers.turtlebot_link import Http
        with self.assertRaises(Unreachable) as e:
            Http(f"http://127.0.0.1:{port}", "t", 0.5).get("/api/health")
        self.assertIn(f"http://127.0.0.1:{port}", str(e.exception))

    def test_a_link_that_goes_quiet_is_lost_without_another_reply(self):
        link, _, clock = make_link({"ok": True})
        link.heartbeat_once()
        clock.advance(3.1)
        self.assertEqual(link.evaluate(), "lost")                               # the poller checks on a timer
        self.assertEqual(kinds(link), ["LinkUp", "LinkDown"])

    def test_a_link_that_comes_back_clears_what_the_base_may_still_be_doing(self):
        link, http, clock = make_link({"ok": True})
        link.heartbeat_once()
        while not link._commands.empty():
            link._commands.get_nowait()
        clock.advance(4)
        link.evaluate()
        link.heartbeat_once()
        queued = []
        while not link._commands.empty():
            queued.append(link._commands.get_nowait())
        self.assertEqual([(c.kind, c.body) for c in queued], [("cancel", {"goal_id": "*"})])

    def test_a_goal_sent_since_the_link_came_back_is_not_cleared_by_that(self):
        link, http, _ = make_link({"ok": True, "accepted": True})
        link.heartbeat_once()
        link.send_goal("g-1", 1, 1, 0)
        http.calls.clear()
        self.assertTrue(link.run_command(Command("cancel", {"goal_id": "*"})))
        self.assertEqual(http.calls, [])

    def test_the_health_check_reads_the_base_and_refuses_another_api_version(self):
        link, _, clock = make_link({"ok": True, "api_version": 1, "ros_ok": True, "localised": True, "battery": 0.8,
                                    "time": 1.7e9 + 1000.2})
        link.health_once()
        self.assertEqual((link.ros_ok, link.localised, link.battery, link.status, link.available()),
                         (True, True, 0.8, "connected", True))
        self.assertAlmostEqual(link.clock_skew_s, 0.2, places=2)
        link, _, _ = make_link({"ok": True, "api_version": 2, "ros_ok": True, "localised": True})
        link.health_once()
        self.assertEqual((link.status, link.available()), ("lost", False))
        self.assertIn("api_version 2", link.incompatible)

    def test_not_localised_or_ros_down_means_not_available(self):
        link, _, _ = make_link({"ok": True, "api_version": 1, "ros_ok": True, "localised": False, "time": 1.7e9 + 1000})
        link.health_once()
        self.assertFalse(link.available())
        link, _, _ = make_link({"ok": True, "api_version": 1, "ros_ok": False, "localised": True, "time": 1.7e9 + 1000})
        link.health_once()
        self.assertFalse(link.available())


class OperatorInfoTest(unittest.TestCase):
    def test_what_a_lost_base_last_said_is_not_shown_as_current(self):
        link, http, clock = make_link(GOOD_STATE)
        link.poll_once()
        self.assertEqual(link.info()["pose"]["x"], 1.0)
        self.assertEqual(link.info()["nav"]["status"], "idle")
        clock.advance(4)
        info = link.info()
        self.assertEqual((info["status"], info["nav"]["status"], info["noise"]), ("lost", "unknown (link lost)", None))


class StateTest(unittest.TestCase):
    def test_a_poll_publishes_the_pose_and_each_new_noise_reading_once(self):
        link, http, clock = make_link(GOOD_STATE)
        link.poll_once()
        link.poll_once()                                                        # the same reading again
        self.assertEqual(kinds(link), ["LinkUp", "PoseUpdated", "NoiseReading"])
        pose = [e for e in link.events if isinstance(e, PoseUpdated)][0]
        self.assertEqual((pose.x, pose.y, pose.yaw, pose.localised), (1.0, 2.0, 0.5, True))
        reading = [e for e in link.events if isinstance(e, NoiseReading)][0]
        self.assertEqual((reading.overall, reading.unit, reading.calibrated, reading.robot_noise), (41.0, "dB", True, False))
        http.script[0] = {**GOOD_STATE, "noise": {**GOOD_STATE["noise"], "t": 100.5, "robot_noise": True}}
        link.poll_once()
        self.assertEqual(kinds(link)[-1], "NoiseReading")
        self.assertTrue(link.events[-1].robot_noise)

    def test_the_pose_is_published_when_it_moves_or_localisation_changes_or_now_and_then(self):
        link, http, clock = make_link(GOOD_STATE)
        link.poll_once()
        link.poll_once()
        self.assertEqual(kinds(link).count("PoseUpdated"), 1)
        http.script[0] = {**GOOD_STATE, "pose": {**GOOD_STATE["pose"], "x": 1.2}}
        link.poll_once()
        self.assertEqual(kinds(link).count("PoseUpdated"), 2)
        http.script[0] = {**GOOD_STATE, "pose": {**GOOD_STATE["pose"], "x": 1.2, "localised": False}}
        link.poll_once()
        self.assertEqual(kinds(link).count("PoseUpdated"), 3)
        clock.advance(6)
        link.poll_once()
        self.assertEqual(kinds(link).count("PoseUpdated"), 4)

    def test_a_state_without_a_pose_or_nav_counts_as_a_failure(self):
        link, _, _ = make_link({"nav": {}})
        self.assertIsNone(link.poll_once())
        self.assertEqual(link.failures, 1)


class GoalTest(unittest.TestCase):
    def goal(self, link, gid="g-1"):
        link.send_goal(gid, 3.0, 2.0, 1.0)
        return link._commands.get_nowait()

    def test_a_goal_is_sent_with_its_id_and_the_base_confirms_it(self):
        link, http, _ = make_link({"ok": True, "accepted": True})
        command = self.goal(link)
        self.assertEqual(command.body, {"goal_id": "g-1", "x": 3.0, "y": 2.0, "yaw": 1.0})
        self.assertTrue(link.run_command(command))
        self.assertEqual(http.calls, [("POST", "/api/goal", command.body)])
        self.assertIsNotNone(link._goal.accepted_at)

    def test_ids_are_new_each_time_and_do_not_repeat_across_runs(self):
        a, _, _ = make_link()
        ids = {a.new_goal_id() for _ in range(5)}
        self.assertEqual(len(ids), 5)
        self.assertTrue(all(i.startswith("g-") for i in ids))

    def test_a_lost_reply_is_retried_with_the_same_goal_id(self):
        link, http, _ = make_link(LinkError("timed out"), LinkError("timed out"), {"ok": True, "accepted": True})
        command = self.goal(link)
        self.assertTrue(link.run_command(command))
        self.assertEqual(len(http.calls), 3)
        self.assertEqual({c[2]["goal_id"] for c in http.calls}, {"g-1"})        # the same goal each time: the base deduplicates

    def test_after_the_retries_the_goal_stays_unconfirmed_and_the_poller_sends_it_again(self):
        link, http, clock = make_link(LinkError("timed out"), retries=1)
        self.assertFalse(link.run_command(self.goal(link)))
        self.assertEqual(len(http.calls), 2)                                    # one try and one retry
        self.assertIsNone(link._goal.accepted_at)
        self.assertFalse(link.resend_unconfirmed())                             # not at once...
        clock.advance(1.1)
        link.failures = 0
        link.last_ok = clock()                                                  # ...and only while the link lives
        self.assertTrue(link.resend_unconfirmed())
        again = link._commands.get_nowait()
        self.assertEqual(again.body["goal_id"], "g-1")
        self.assertFalse(link.resend_unconfirmed())                             # one is on its way

    def test_a_goal_replaced_while_it_waited_is_not_sent(self):
        link, http, _ = make_link({"ok": True, "accepted": True})
        old = self.goal(link, "g-1")
        link.send_goal("g-2", 1, 1, 0)
        self.assertTrue(link.run_command(old))
        self.assertEqual(http.calls, [])

    def test_a_refusal_is_reported_as_a_failed_goal_with_its_reason(self):
        for reason in ("not_localised", "outside_map", "invalid_pose"):
            link, _, _ = make_link({"ok": True, "accepted": False, "reason": reason})
            link.run_command(self.goal(link))
            failed = [e for e in link.events if isinstance(e, NavFailed)]
            self.assertEqual([(f.goal_id, f.reason) for f in failed], [("g-1", reason)])
            self.assertIsNone(link._goal)

    def test_no_heartbeat_is_tried_again_after_a_moment(self):
        link, http, _ = make_link({"ok": True, "accepted": False, "reason": "no_heartbeat"}, {"ok": True, "accepted": True})
        self.assertTrue(link.run_command(self.goal(link)))
        self.assertEqual(len(http.calls), 2)
        self.assertEqual([e for e in link.events if isinstance(e, NavFailed)], [])

    def test_error_statuses_are_failures_of_the_goal_not_of_the_link(self):
        link, _, _ = make_link(ApiError(503, "ros_unavailable"))
        link.run_command(self.goal(link))
        self.assertEqual([(e.goal_id, e.reason) for e in link.events if isinstance(e, NavFailed)], [("g-1", "ros_unavailable")])
        self.assertEqual(link.status, "connected")                              # it answered
        link, _, _ = make_link(ApiError(400, "bad_request"))
        link.run_command(self.goal(link))
        self.assertEqual([e.reason for e in link.events if isinstance(e, NavFailed)], ["rejected: bad_request"])

    def test_a_goal_the_robot_refused_to_send_is_reported_as_failed(self):
        link, http, _ = make_link()
        link.reject("g-9", "rejected: outside level 1")
        self.assertEqual([(e.goal_id, e.reason) for e in link.events], [("g-9", "rejected: outside level 1")])
        self.assertEqual(http.calls, [])


class OutcomeTest(unittest.TestCase):
    """The outcome of a goal is read from nav in the state, for the goal that was sent, and published once."""

    def accepted_goal(self, link, clock):
        link.send_goal("g-1", 3.0, 2.0, 1.0)
        link._commands.get_nowait()
        link._accepted("g-1")
        clock.advance(0.1)

    def state(self, status, goal_id="g-1", distance=None, reason=""):
        return {**GOOD_STATE, "nav": {"status": status, "goal_id": goal_id, "distance_left": distance, "reason": reason}}

    def test_progress_then_arrival_each_published_once(self):
        link, http, clock = make_link(self.state("active", distance=2.0))
        self.accepted_goal(link, clock)
        link.poll_once()
        link.poll_once()                                                        # unchanged distance: nothing new
        http.script[0] = self.state("active", distance=1.2)
        link.poll_once()
        http.script[0] = self.state("succeeded")
        link.poll_once()
        link.poll_once()
        events = [e for e in link.events if type(e).__name__.startswith("Nav")]
        self.assertEqual([(type(e).__name__, getattr(e, "distance_left", None)) for e in events],
                         [("NavProgress", 2.0), ("NavProgress", 1.2), ("NavArrived", None)])
        self.assertIsNone(link._goal)

    def test_a_failure_carries_the_reason(self):
        link, http, clock = make_link(self.state("failed", reason="no_path"))
        self.accepted_goal(link, clock)
        link.poll_once()
        failed = [e for e in link.events if isinstance(e, NavFailed)]
        self.assertEqual([(f.goal_id, f.reason) for f in failed], [("g-1", "no_path")])

    def test_a_cancellation_is_published(self):
        link, http, clock = make_link(self.state("cancelled", reason="cancelled_by_client"))
        self.accepted_goal(link, clock)
        link.cancel("g-1")
        link.poll_once()
        cancelled = [e for e in link.events if isinstance(e, NavCancelled)]
        self.assertEqual([(c.goal_id, c.reason) for c in cancelled], [("g-1", "cancelled_by_client")])
        self.assertEqual(link._commands.get_nowait().body, {"goal_id": "g-1"})

    def test_another_goals_outcome_is_never_taken_for_ours(self):
        link, http, clock = make_link(self.state("succeeded", goal_id="g-0"))
        self.accepted_goal(link, clock)
        link.poll_once()
        self.assertEqual([e for e in link.events if isinstance(e, NavArrived)], [])      # g-0 arrived, ours did not
        self.assertEqual([(e.goal_id, e.reason) for e in link.events if isinstance(e, NavFailed)], [("g-1", "goal_lost")])

    def test_a_goal_the_base_forgot_is_reported_failed(self):
        link, http, clock = make_link({**GOOD_STATE})
        self.accepted_goal(link, clock)
        link.poll_once()                                                        # idle, no goal: it lost ours
        self.assertEqual([(e.goal_id, e.reason) for e in link.events if isinstance(e, NavFailed)], [("g-1", "goal_lost")])

    def test_a_poll_asked_for_before_the_base_said_yes_is_not_believed(self):
        link, http, clock = make_link({**GOOD_STATE})
        link.send_goal("g-1", 3, 2, 1)
        link._commands.get_nowait()
        requested = clock()
        link._accepted("g-1")                                                   # the reply to the goal came after the poll was sent
        link._read_nav(GOOD_STATE["nav"], requested)
        self.assertEqual([e for e in link.events if isinstance(e, NavFailed)], [])

    def test_losing_the_link_drops_the_goal_quietly(self):
        link, http, clock = make_link({"ok": True})
        link.heartbeat_once()
        self.accepted_goal(link, clock)
        clock.advance(4)
        link.evaluate()
        self.assertIsNone(link._goal)
        self.assertEqual([e for e in link.events if isinstance(e, (NavFailed, NavCancelled))], [])
        self.assertIn("LinkDown", kinds(link))


class CommandsTest(unittest.TestCase):
    def test_speaking_and_max_speed_are_sent_and_resent_when_the_link_returns(self):
        link, http, clock = make_link({"ok": True})
        link.set_speaking(True)
        link.set_max_speed(0.5)
        sent = [link._commands.get_nowait() for _ in range(2)]
        self.assertEqual([(c.kind, c.body) for c in sent], [("speaking", {"speaking": True}), ("max_speed", {"m_s": 0.5})])
        link.heartbeat_once()                                                   # the link comes up: what the base may have forgotten
        again = []
        while not link._commands.empty():
            again.append(link._commands.get_nowait().kind)
        self.assertEqual(again, ["cancel", "speaking", "max_speed"])

    def test_a_base_without_max_speed_is_not_asked_again(self):
        link, http, _ = make_link(ApiError(404, "not_found"))
        link.set_max_speed(0.3)
        link.run_command(link._commands.get_nowait())
        link.set_max_speed(0.6)
        while not link._commands.empty():
            link.run_command(link._commands.get_nowait())
        self.assertEqual([c for c in http.calls if c[1] == "/api/max_speed"], [("POST", "/api/max_speed", {"m_s": 0.3})])

    def test_idempotent_commands_are_retried_up_to_the_limit(self):
        link, http, _ = make_link(LinkError("x"), retries=2)
        self.assertFalse(link.run_command(Command("speaking", {"speaking": False})))
        self.assertEqual(len(http.calls), 3)


class RealHttpTest(unittest.TestCase):
    def test_a_silent_server_times_out_in_about_the_timeout(self):
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        self.addCleanup(silent.close)
        http = Http(f"http://127.0.0.1:{silent.getsockname()[1]}", "t", timeout_s=0.3)
        started = time.monotonic()
        with self.assertRaises(LinkError):
            http.get("/api/state")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_refused_connection_is_a_link_error(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(LinkError):
            Http(f"http://127.0.0.1:{port}", "t", 0.3).get("/api/health")

    def test_error_statuses_carry_the_codes_of_the_contract(self):
        server = FakeBaseServer(FakeBase("tok")).start()
        self.addCleanup(server.stop)
        with self.assertRaises(ApiError) as caught:
            Http(server.url, "wrong", 1).get("/api/health")
        self.assertEqual((caught.exception.status, caught.exception.error), (401, "unauthorised"))
        with self.assertRaises(ApiError) as caught:
            Http(server.url, "tok", 1).get("/api/nothing")
        self.assertEqual(caught.exception.status, 404)


class WithTheFakeBaseTest(unittest.TestCase):
    """The real threads against tools/fake_base.py."""

    def start(self, **options):
        model = FakeBase("tok", speed=options.pop("speed", 5.0), **options)
        server = FakeBaseServer(model).start()
        self.addCleanup(server.stop)
        link = TurtleBotLink(LinkSettings(base_url=server.url, token="tok", timeout_s=0.5, poll_hz=20, lost_after_s=1.0))
        link.events = []
        link.sink = link.events.append
        link.start_threads()
        self.addCleanup(link.stop_threads)
        return link, model, server

    def wait_for(self, condition, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_a_goal_is_driven_and_arrives_and_the_heartbeat_keeps_the_base_going(self):
        link, model, _ = self.start()
        self.assertTrue(self.wait_for(lambda: link.status == "connected"))
        gid = link.new_goal_id()
        link.send_goal(gid, model.x + 1.5, model.y, 0.0)
        self.assertTrue(self.wait_for(lambda: any(isinstance(e, NavArrived) for e in link.events)), kinds(link))
        self.assertEqual([e.goal_id for e in link.events if isinstance(e, NavArrived)], [gid])
        self.assertTrue(any(isinstance(e, NavProgress) for e in link.events))

    def test_cancelling_stops_the_base(self):
        link, model, _ = self.start(speed=0.5)
        self.assertTrue(self.wait_for(lambda: link.status == "connected"))
        gid = link.new_goal_id()
        link.send_goal(gid, model.x + 5.0, model.y, 0.0)
        self.assertTrue(self.wait_for(lambda: any(isinstance(e, NavProgress) for e in link.events)))
        link.cancel(gid)
        self.assertTrue(self.wait_for(lambda: any(isinstance(e, NavCancelled) for e in link.events)))
        self.assertEqual(model.goal["reason"], "cancelled_by_client")

    def test_pulling_the_cable_is_a_link_down_and_the_base_stops_by_itself_then_it_comes_back(self):
        link, model, server = self.start(speed=0.2)
        self.assertTrue(self.wait_for(lambda: link.status == "connected"))
        link.send_goal(link.new_goal_id(), model.x + 5.0, model.y, 0.0)
        self.assertTrue(self.wait_for(lambda: any(isinstance(e, NavProgress) for e in link.events)))
        model.link_dropped = True
        self.assertTrue(self.wait_for(lambda: any(isinstance(e, LinkDown) for e in link.events), 4), kinds(link))
        self.assertTrue(self.wait_for(lambda: model.goal["status"] == "cancelled", 6))
        self.assertEqual(model.goal["reason"], "heartbeat_timeout")
        model.link_dropped = False
        self.assertTrue(self.wait_for(lambda: kinds(link).count("LinkUp") == 2, 5), kinds(link))
        self.assertEqual(link.status, "connected")


if __name__ == "__main__":
    unittest.main()
