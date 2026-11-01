"""tools/fake_base.py, the stand-in for the Dell: its model driven by hand with a clock, and a few things over HTTP."""

import json
import unittest
import urllib.error
import urllib.request

from robot.services.floorplan import FloorPlan
from robot.config import load_config
from tools.fake_base import FakeBase, FakeBaseServer, read_sections


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(**kw):
    clock = Clock()
    cfg = load_config()
    plan, zones = read_sections(cfg.path("data/floorplan.yaml"), cfg.path("data/shelves.csv"))
    kw.setdefault("start", (2.0, 2.0, 0.0))
    kw.setdefault("level", 3)
    base = FakeBase("tok", plan, zones, clock=clock, wall=lambda: 1.7e9 + clock.t, **kw)
    return base, clock


def beat(base, clock):
    base.heartbeat()


def drive(base, clock, seconds, step=0.1):
    for _ in range(round(seconds / step)):
        clock.t += step
        base.heartbeat()
        base.update()


class ModelTest(unittest.TestCase):
    def test_a_goal_is_driven_at_the_speed_with_distance_left_and_succeeds(self):
        base, clock = make(speed=1.0)
        beat(base, clock)
        status, reply = base.send_goal("g-1", 5.0, 2.0, 1.0)
        self.assertEqual((status, reply["accepted"]), (200, True))
        drive(base, clock, 1.0)
        nav = base.state()["nav"]
        self.assertEqual((nav["status"], nav["goal_id"]), ("active", "g-1"))
        self.assertAlmostEqual(nav["distance_left"], 2.0, delta=0.2)             # 3 m minus about 1 m
        drive(base, clock, 3.0)
        state = base.state()
        self.assertEqual((state["nav"]["status"], state["nav"]["distance_left"]), ("succeeded", None))
        self.assertEqual((state["pose"]["x"], state["pose"]["y"], state["pose"]["yaw"]), (5.0, 2.0, 1.0))
        self.assertEqual(base.state()["nav"]["goal_id"], "g-1")                   # the outcome stays reported

    def test_the_same_goal_id_twice_is_the_same_goal(self):
        base, clock = make(speed=1.0)
        beat(base, clock)
        base.send_goal("g-1", 5.0, 2.0, 0.0)
        drive(base, clock, 1.0)
        here = base.state()["pose"]["x"]
        status, reply = base.send_goal("g-1", 5.0, 2.0, 0.0)
        self.assertEqual((status, reply["accepted"], reply.get("duplicate")), (200, True, True))
        self.assertEqual(base.state()["pose"]["x"], here)                         # nothing restarted
        drive(base, clock, 5.0)
        self.assertEqual(base.send_goal("g-1", 5.0, 2.0, 0.0)[1].get("duplicate"), True)   # even when it is over

    def test_a_new_goal_replaces_the_active_one(self):
        base, clock = make(speed=1.0)
        beat(base, clock)
        base.send_goal("g-1", 8.0, 2.0, 0.0)
        drive(base, clock, 0.5)
        base.send_goal("g-2", 2.0, 4.0, 0.0)
        self.assertEqual(base.known["g-1"]["status"], "cancelled")
        self.assertEqual(base.known["g-1"]["reason"], "superseded")
        self.assertEqual(base.state()["nav"]["goal_id"], "g-2")

    def test_cancel_stops_the_base_at_once(self):
        base, clock = make(speed=1.0)
        beat(base, clock)
        base.send_goal("g-1", 8.0, 2.0, 0.0)
        drive(base, clock, 1.0)
        self.assertEqual(base.cancel("g-1"), {"ok": True, "cancelled": True})
        stopped = base.state()
        drive(base, clock, 2.0)
        self.assertEqual(base.state()["pose"]["x"], stopped["pose"]["x"])
        self.assertEqual((stopped["nav"]["status"], stopped["nav"]["reason"]), ("cancelled", "cancelled_by_client"))
        self.assertEqual(base.cancel("g-1"), {"ok": True, "cancelled": False})    # idempotent
        self.assertEqual(base.cancel("nothing"), {"ok": True, "cancelled": False})

    def test_no_heartbeat_for_three_seconds_stops_the_base(self):
        base, clock = make(speed=0.5)
        beat(base, clock)
        base.send_goal("g-1", 8.0, 2.0, 0.0)
        drive(base, clock, 1.0)
        for _ in range(35):                                                       # silence
            clock.t += 0.1
            base.update()
        nav = base.state()["nav"]
        self.assertEqual((nav["status"], nav["reason"]), ("cancelled", "heartbeat_timeout"))
        stopped = base.state()["pose"]["x"]
        clock.t += 5
        self.assertEqual(base.state()["pose"]["x"], stopped)

    def test_it_does_not_start_driving_again_by_itself_and_refuses_goals_until_a_heartbeat(self):
        base, clock = make()
        beat(base, clock)
        clock.t += 4
        status, reply = base.send_goal("g-1", 5.0, 2.0, 0.0)
        self.assertEqual((reply["accepted"], reply["reason"]), (False, "no_heartbeat"))
        beat(base, clock)
        self.assertTrue(base.send_goal("g-1", 5.0, 2.0, 0.0)[1]["accepted"])

    def test_goals_are_refused_while_not_localised_and_when_outside_the_map(self):
        base, clock = make(unlocalised_for=5.0)
        beat(base, clock)
        self.assertEqual(base.send_goal("g-1", 5.0, 2.0, 0.0)[1], {"ok": True, "accepted": False, "goal_id": "g-1", "reason": "not_localised"})
        self.assertFalse(base.health()["localised"])
        self.assertFalse(base.state()["pose"]["localised"])
        clock.t += 6
        beat(base, clock)
        self.assertEqual(base.send_goal("g-2", 99.0, 2.0, 0.0)[1]["reason"], "outside_map")
        self.assertEqual(base.send_goal("g-3", 5.0, 2.0, 9.0)[1]["reason"], "invalid_pose")
        self.assertTrue(base.send_goal("g-4", 5.0, 2.0, 0.0)[1]["accepted"])

    def test_ros_down_is_a_503(self):
        base, clock = make()
        base.ros_ok = False
        beat(base, clock)
        status, reply = base.send_goal("g-1", 5.0, 2.0, 0.0)
        self.assertEqual((status, reply["error"]), (503, "ros_unavailable"))
        self.assertFalse(base.health()["ros_ok"])

    def test_scripted_failures_happen_on_the_way(self):
        base, clock = make(speed=1.0)
        beat(base, clock)
        base.fail_next = 1
        base.send_goal("g-1", 7.0, 2.0, 0.0)
        drive(base, clock, 6.0)
        nav = base.state()["nav"]
        self.assertEqual(nav["status"], "failed")
        self.assertIn(nav["reason"], ("no_path", "blocked", "aborted"))
        self.assertLess(base.state()["pose"]["x"], 7.0)
        base.send_goal("g-2", 7.0, 2.0, 0.0)                                       # the next one is fine
        drive(base, clock, 8.0)
        self.assertEqual(base.state()["nav"]["status"], "succeeded")

    def test_fail_rate_one_fails_every_goal_and_the_same_seed_fails_the_same_way(self):
        outcomes = []
        for _ in range(2):
            base, clock = make(speed=2.0, fail_rate=1.0, seed=7)
            beat(base, clock)
            base.send_goal("g-1", 7.0, 2.0, 0.0)
            drive(base, clock, 5.0)
            outcomes.append((base.state()["nav"], base.state()["pose"]["x"]))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][0]["status"], "failed")

    def test_max_speed_limits_driving_and_a_base_without_it_says_so(self):
        base, clock = make(speed=2.0)
        beat(base, clock)
        self.assertEqual(base.set_max_speed(0.5), {"ok": True, "applied": 0.5})
        base.send_goal("g-1", 8.0, 2.0, 0.0)
        drive(base, clock, 2.0)
        self.assertAlmostEqual(base.state()["pose"]["x"], 3.0, delta=0.2)          # 0.5 m/s for 2 s
        base.max_speed_offered = False
        self.assertIsNone(base.set_max_speed(0.5))


class NoiseTest(unittest.TestCase):
    def reading(self, base, clock):
        clock.t += 1.0
        return base.state()["noise"]

    def test_quiet_sections_are_quiet_and_everywhere_else_is_not(self):
        quiet, clock = make(start=(4.0, 1.5, 0.0))                                 # inside Science on level 3
        common, clock2 = make(start=(10.0, 7.0, 0.0))
        levels = lambda b, c: [self.reading(b, c)["overall"] for _ in range(8)]
        self.assertLess(max(levels(quiet, clock)), 45)
        self.assertGreater(min(levels(common, clock2)), 55)

    def test_readings_are_new_every_half_second_and_the_same_between(self):
        base, clock = make()
        a = base.state()["noise"]
        b = base.state()["noise"]
        self.assertEqual(a, b)
        clock.t += 0.6
        self.assertNotEqual(base.state()["noise"]["t"], a["t"])
        self.assertEqual({"overall", "left", "right", "balance", "unit", "calibrated", "robot_noise", "t"}, set(a))

    def test_speaking_makes_the_readings_the_robots_own_and_louder_until_a_moment_after(self):
        base, clock = make(start=(4.0, 1.5, 0.0))
        calm = self.reading(base, clock)
        self.assertFalse(calm["robot_noise"])
        base.set_speaking(True)
        loud = self.reading(base, clock)
        self.assertTrue(loud["robot_noise"])
        self.assertGreater(loud["overall"], calm["overall"] + 8)
        base.set_speaking(False)
        clock.t += 2
        self.assertFalse(base.state()["noise"]["robot_noise"])

    def test_the_flag_clears_itself_if_the_robot_goes_quiet_without_saying_so(self):
        base, clock = make()
        base.set_speaking(True)
        clock.t += 31
        base.update()
        self.assertFalse(base.speaking)

    def test_uncalibrated_reports_dBFS(self):
        base, clock = make(uncalibrated=True)
        n = self.reading(base, clock)
        self.assertEqual((n["unit"], n["calibrated"]), ("dBFS", False))
        self.assertLess(n["overall"], 0)

    def test_the_keyboard_offset_makes_it_louder(self):
        base, clock = make(start=(4.0, 1.5, 0.0))
        before = self.reading(base, clock)["overall"]
        base.noise_offset_db += 20
        self.assertGreater(self.reading(base, clock)["overall"], before + 15)


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeBaseServer(FakeBase("tok", start=(1, 1, 0))).start()
        self.addCleanup(self.server.stop)

    def call(self, method, path, body=None, token="tok", raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/json", **({"X-Robot-Token": token} if token else {})}
        req = urllib.request.Request(self.server.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_authentication(self):
        self.assertEqual(self.call("GET", "/api/health", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/health", token="nope")[1], {"ok": False, "error": "unauthorised", "detail": "missing or wrong X-Robot-Token"})
        self.assertEqual(self.call("GET", "/api/health")[0], 200)

    def test_errors_use_the_contracts_statuses(self):
        self.assertEqual(self.call("GET", "/api/nothing")[0], 404)
        self.assertEqual(self.call("POST", "/api/state", {})[0], 405)
        self.assertEqual(self.call("GET", "/api/goal")[0], 405)
        self.assertEqual(self.call("POST", "/api/goal", raw=b"not json")[0], 400)
        self.assertEqual(self.call("POST", "/api/goal", raw=b"[1, 2]")[0], 400)
        self.assertEqual(self.call("POST", "/api/goal", {"goal_id": "g", "x": "1", "y": 1, "yaw": 0})[0], 400)
        self.assertEqual(self.call("POST", "/api/goal", {"goal_id": "g", "x": float("nan"), "y": 1, "yaw": 0})[0], 400)
        self.assertEqual(self.call("POST", "/api/goal", {"x": 1, "y": 1, "yaw": 0})[0], 400)
        self.assertEqual(self.call("POST", "/api/cancel", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/speaking", {"speaking": "yes"})[0], 400)
        self.assertEqual(self.call("POST", "/api/heartbeat", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/max_speed", {"m_s": -1})[0], 400)
        status, body = self.call("POST", "/api/goal", raw=b"x" * 20000)
        self.assertEqual((status, body["error"]), (413, "payload_too_large"))
        for status, body in (self.call("GET", "/api/nothing"), self.call("POST", "/api/cancel", {})):
            self.assertEqual(set(body), {"ok", "error", "detail"})

    def test_a_dropped_link_gets_no_answer(self):
        self.server.model.link_dropped = True
        with self.assertRaises(Exception):
            self.call("GET", "/api/health")
        self.server.model.link_dropped = False
        self.assertEqual(self.call("GET", "/api/health")[0], 200)

    def test_the_link_can_be_dropped_after_a_time_and_for_a_while(self):
        server = FakeBaseServer(FakeBase("tok"), drop_after_s=0.0, drop_for_s=0.4).start()
        self.addCleanup(server.stop)
        with self.assertRaises(Exception):
            urllib.request.urlopen(urllib.request.Request(server.url + "/api/health", headers={"X-Robot-Token": "tok"}), timeout=1)
        import time
        time.sleep(0.5)
        self.assertEqual(urllib.request.urlopen(urllib.request.Request(server.url + "/api/health", headers={"X-Robot-Token": "tok"}), timeout=1).status, 200)


if __name__ == "__main__":
    unittest.main()
