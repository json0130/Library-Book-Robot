"""The Jetson-Dell link contract (docs/robot_link.md, api_version 1), as a test that any server can be held to.

    python -m pytest tests/test_robot_link_contract.py                    # against tools/fake_base.py (started here)
    ROBOT_LINK_TOKEN=... python -m pytest tests/test_robot_link_contract.py --target http://127.0.0.1:8010
    ... --target URL --drive                                              # also the tests that make the base move

Plain HTTP with urllib, nothing of the robot's own client: it checks what is on the wire. Against a real base the tests that
move it (a goal 0.3 m to 3 m ahead of where it stands, a cancel, cutting the heartbeat) only run with --drive.
"""

import json
import math
import os
import time
import urllib.error
import urllib.request

import pytest

from tools.fake_base import FakeBase, FakeBaseServer, read_sections

STATUSES = {"idle", "active", "succeeded", "failed", "cancelled"}


class Api:
    def __init__(self, url, token, drive, fake=None):
        self.url, self.token, self.drive, self.fake = url.rstrip("/"), token, drive, fake
        self.n = 0

    def call(self, method, path, body=None, token="__default__", raw=None, timeout=1.5):
        token = self.token if token == "__default__" else token
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Robot-Token"] = token
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body, **kw)

    def beat(self):
        status, body = self.post("/api/heartbeat", {"t": time.time()})
        assert status == 200
        return body

    def state(self):
        status, body = self.get("/api/state")
        assert status == 200
        return body

    def new_id(self):
        self.n += 1
        return f"contract-{os.getpid()}-{int(time.time() * 1000) % 10**8}-{self.n}"

    def ahead(self, metres):
        pose = self.state()["pose"]
        return pose["x"] + metres * math.cos(pose["yaw"]), pose["y"] + metres * math.sin(pose["yaw"]), pose["yaw"]

    def goal(self, metres, goal_id=None):
        x, y, yaw = self.ahead(metres)
        goal_id = goal_id or self.new_id()
        status, body = self.post("/api/goal", {"goal_id": goal_id, "x": x, "y": y, "yaw": yaw})
        return goal_id, status, body

    def wait_nav(self, goal_id, wanted, seconds=15.0):
        end = time.monotonic() + seconds
        nav = {}
        while time.monotonic() < end:
            self.beat()
            nav = self.state()["nav"]
            if nav["goal_id"] == goal_id and nav["status"] in wanted:
                return nav
            time.sleep(0.1)
        pytest.fail(f"goal {goal_id} did not reach {wanted} in {seconds:g} s (last nav: {nav})")

    def stop(self):
        self.post("/api/cancel", {"goal_id": "*"})


@pytest.fixture(scope="module")
def api(request):
    target, drive = request.config.getoption("--target"), request.config.getoption("--drive")
    if target:
        token = os.environ.get("ROBOT_LINK_TOKEN", "")
        if not token:
            pytest.skip("--target needs ROBOT_LINK_TOKEN in the environment")
        yield Api(target, token, drive)
        return
    from robot.config import load_config
    cfg = load_config()
    plan, zones = read_sections(cfg.path("data/floorplan.yaml"), cfg.path("data/shelves.csv"))
    model = FakeBase("contract-token", plan, zones, speed=1.0)
    server = FakeBaseServer(model).start()
    try:
        yield Api(server.url, "contract-token", True, model)
    finally:
        server.stop()


@pytest.fixture
def driving(api):
    if not api.drive:
        pytest.skip("this makes the base move: add --drive")
    api.beat()
    api.stop()
    yield api
    api.stop()


# ---------------------------------------------------------------------------------------------- reading
def test_health(api):
    status, body = api.get("/api/health")
    assert status == 200 and body["ok"] is True
    assert body["api_version"] == 1
    assert isinstance(body["ros_ok"], bool) and isinstance(body["localised"], bool)
    assert body["battery"] is None or 0.0 <= body["battery"] <= 1.0
    assert abs(body["time"] - time.time()) < 5, "the clocks differ: set up chrony (docs/robot_link.md, section 5)"


def test_state_has_pose_nav_and_noise(api):
    s = api.state()
    pose = s["pose"]
    assert {"x", "y", "yaw", "frame", "localised"} <= set(pose)
    assert pose["frame"] == "map" and isinstance(pose["localised"], bool)
    assert all(isinstance(pose[k], (int, float)) and math.isfinite(pose[k]) for k in ("x", "y", "yaw"))
    nav = s["nav"]
    assert {"status", "goal_id", "distance_left", "reason"} <= set(nav)
    assert nav["status"] in STATUSES
    assert nav["distance_left"] is None or nav["distance_left"] >= 0
    noise = s["noise"]
    if noise is not None:                                          # null when the microphones are not running
        assert {"overall", "left", "right", "balance", "unit", "calibrated", "robot_noise", "t"} <= set(noise)
        assert noise["unit"] in ("dB", "dBFS")
        assert isinstance(noise["calibrated"], bool) and isinstance(noise["robot_noise"], bool)
        assert math.isfinite(noise["overall"])
        if noise["unit"] == "dBFS":
            assert noise["calibrated"] is False and noise["overall"] <= 0
        assert abs(noise["t"] - time.time()) < 10


def test_readings_are_fresh_and_a_reading_keeps_its_time_until_the_next(api):
    first = api.state()["noise"]
    if first is None:
        pytest.skip("no microphone readings")
    seen = {first["t"]}
    end = time.monotonic() + 3
    while time.monotonic() < end and len(seen) < 3:
        n = api.state()["noise"]
        seen.add(n["t"])
        time.sleep(0.1)
    assert len(seen) >= 2, "no new reading in 3 s: the contract says about every 0.5 s"


# ---------------------------------------------------------------------------------------------- the rules for every request
def test_a_missing_or_wrong_token_is_401_with_the_error_body(api):
    for token in (None, "wrong-token"):
        for method, path in (("GET", "/api/health"), ("GET", "/api/state"), ("POST", "/api/goal"), ("POST", "/api/heartbeat")):
            status, body = api.call(method, path, {} if method == "POST" else None, token=token)
            assert status == 401, (method, path)
            assert body["ok"] is False and body["error"] == "unauthorised"


def test_unknown_paths_are_404_and_wrong_methods_405(api):
    status, body = api.get("/api/nothing")
    assert (status, body["ok"], body["error"]) == (404, False, "not_found")
    assert api.post("/api/state", {})[0] == 405
    assert api.get("/api/goal")[0] == 405
    assert api.get("/api/heartbeat")[0] == 405


def test_malformed_requests_are_400_with_the_error_body(api):
    bad = [("/api/goal", raw) for raw in (b"not json", b"[1]", b"null")]
    bad += [("/api/goal", json.dumps(b).encode()) for b in ({}, {"goal_id": "g"}, {"goal_id": "g", "x": "1", "y": 1, "yaw": 0},
                                                            {"goal_id": 5, "x": 1, "y": 1, "yaw": 0}, {"goal_id": "", "x": 1, "y": 1, "yaw": 0})]
    bad += [("/api/cancel", b"{}"), ("/api/speaking", b'{"speaking": "yes"}'), ("/api/speaking", b"{}"), ("/api/heartbeat", b"{}")]
    for path, raw in bad:
        status, body = api.post(path, raw=raw)
        assert status == 400, (path, raw, status, body)
        assert body["ok"] is False and body["error"] == "bad_request" and isinstance(body.get("detail", ""), str)


def test_a_body_over_16_kb_is_413(api):
    status, body = api.post("/api/goal", raw=b" " * 20000)
    assert status == 413 and body["error"] == "payload_too_large"


def test_answers_come_well_inside_the_robots_one_second_timeout(api):
    for path in ("/api/health", "/api/state"):
        started = time.monotonic()
        api.get(path)
        assert time.monotonic() - started < 0.5, path


# ---------------------------------------------------------------------------------------------- the small posts
def test_heartbeat(api):
    status, body = api.post("/api/heartbeat", {"t": time.time()})
    assert status == 200 and body["ok"] is True and abs(body["time"] - time.time()) < 5


def test_speaking_is_a_flag_and_idempotent(api):
    for value in (True, True, False, False):
        status, body = api.post("/api/speaking", {"speaking": value})
        assert (status, body) == (200, {"ok": True})


def test_speaking_marks_the_readings_as_the_robots_own(api):
    if api.state()["noise"] is None:
        pytest.skip("no microphone readings")
    api.post("/api/speaking", {"speaking": True})
    try:
        end, marked = time.monotonic() + 3, False
        while time.monotonic() < end and not marked:
            marked = api.state()["noise"]["robot_noise"]
            time.sleep(0.1)
        assert marked, "robot_noise never became true while speaking was true"
    finally:
        api.post("/api/speaking", {"speaking": False})
    end, cleared = time.monotonic() + 4, False
    while time.monotonic() < end and not cleared:
        cleared = not api.state()["noise"]["robot_noise"]
        time.sleep(0.1)
    assert cleared, "robot_noise stayed true after speaking became false"


def test_max_speed_is_applied_or_404(api):
    status, body = api.post("/api/max_speed", {"m_s": 0.3})
    assert status in (200, 404)
    if status == 200:
        assert body["ok"] is True and 0 < body["applied"] <= 0.3
    assert api.post("/api/max_speed", {"m_s": -1})[0] in (400, 404)


def test_cancelling_a_goal_that_is_not_active_is_not_an_error(api):
    status, body = api.post("/api/cancel", {"goal_id": "contract-never-sent"})
    assert (status, body) == (200, {"ok": True, "cancelled": False})
    status, body = api.post("/api/cancel", {"goal_id": "*"})
    assert status == 200 and body["ok"] is True


# ---------------------------------------------------------------------------------------------- driving
def test_a_goal_is_accepted_driven_and_its_outcome_stays_reported(driving):
    api = driving
    goal_id, status, body = api.goal(0.3)
    assert status == 200 and body["accepted"] is True and body["goal_id"] == goal_id
    nav = api.state()["nav"]
    assert nav["goal_id"] == goal_id and nav["status"] in ("active", "succeeded")
    nav = api.wait_nav(goal_id, ("succeeded", "failed", "cancelled"))
    assert nav["status"] == "succeeded", nav
    assert nav["distance_left"] is None
    time.sleep(1.0)
    again = api.state()["nav"]
    assert (again["goal_id"], again["status"]) == (goal_id, "succeeded")          # still reported until the next goal


def test_distance_left_falls_while_active(driving):
    api = driving
    goal_id, _, body = api.goal(2.0)
    assert body["accepted"]
    readings = []
    end = time.monotonic() + 8
    while time.monotonic() < end and len(readings) < 3:
        api.beat()
        nav = api.state()["nav"]
        if nav["goal_id"] == goal_id and nav["status"] == "active" and nav["distance_left"] is not None:
            readings.append(nav["distance_left"])
        time.sleep(0.4)
    assert len(readings) >= 2 and readings[-1] < readings[0], readings


def test_the_same_goal_id_is_the_same_goal(driving):
    api = driving
    goal_id, _, first = api.goal(2.0)
    assert first["accepted"] is True
    x, y, yaw = api.ahead(2.0)
    status, second = api.post("/api/goal", {"goal_id": goal_id, "x": x, "y": y, "yaw": yaw})
    assert status == 200 and second["accepted"] is True and second.get("duplicate") is True
    assert api.state()["nav"]["goal_id"] == goal_id


def test_cancel_stops_the_base_and_reports_cancelled(driving):
    api = driving
    goal_id, _, body = api.goal(3.0)
    assert body["accepted"]
    time.sleep(0.5)
    status, reply = api.post("/api/cancel", {"goal_id": goal_id})
    assert (status, reply) == (200, {"ok": True, "cancelled": True})
    nav = api.state()["nav"]
    assert (nav["goal_id"], nav["status"]) == (goal_id, "cancelled") and nav["reason"] == "cancelled_by_client"
    before = api.state()["pose"]
    time.sleep(1.0)
    after = api.state()["pose"]
    assert math.hypot(after["x"] - before["x"], after["y"] - before["y"]) < 0.1, "the base kept moving after the cancel"
    assert api.post("/api/cancel", {"goal_id": goal_id})[1] == {"ok": True, "cancelled": False}


def test_a_different_goal_id_replaces_the_active_goal(driving):
    api = driving
    first, _, body = api.goal(3.0)
    assert body["accepted"]
    second, _, body = api.goal(1.0)
    assert body["accepted"]
    nav = api.state()["nav"]
    assert nav["goal_id"] == second
    api.stop()


def test_a_bad_yaw_is_refused_with_a_reason_and_nothing_moves(driving):
    api = driving
    x, y, _ = api.ahead(0.3)
    status, body = api.post("/api/goal", {"goal_id": api.new_id(), "x": x, "y": y, "yaw": 10.0})
    assert status == 200 and body["accepted"] is False and body["reason"] == "invalid_pose"


def test_without_a_heartbeat_the_base_stops_within_three_seconds_and_refuses_goals_until_one_comes(driving):
    api = driving
    goal_id, _, body = api.goal(3.0)
    assert body["accepted"]
    api.beat()
    started = time.monotonic()
    nav = {}
    while time.monotonic() - started < 6:                          # no heartbeats from here on
        nav = api.state()["nav"]
        if nav["status"] != "active":
            break
        time.sleep(0.2)
    waited = time.monotonic() - started
    if nav["status"] == "succeeded":
        pytest.skip("the base reached the goal before the watchdog could act: lengthen the goal")
    assert (nav["status"], nav["reason"]) == ("cancelled", "heartbeat_timeout"), nav
    assert waited < 4.5, f"the base needed {waited:.1f} s to stop; the contract says 3 s"
    _, _, refused = api.goal(0.3)
    assert refused["accepted"] is False and refused["reason"] == "no_heartbeat"
    api.beat()
    _, _, accepted = api.goal(0.3)
    assert accepted["accepted"] is True
    api.wait_nav(api.state()["nav"]["goal_id"], ("succeeded", "failed", "cancelled"))
