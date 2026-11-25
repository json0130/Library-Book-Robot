"""The HTTP server over a LinkCore: token, JSON, error bodies, routes. A real server on a free port, a fake Backend."""

import http.client
import json
import threading
import time
import unittest

from support import FakeBackend, Settings, feed_pose, make_core  # noqa: F401  (also puts turtlebot/ on the path)
from robot_link.core import LinkCore
from robot_link.server import ApiServer

TOKEN = "test-token-1234567890"


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = FakeBackend()
        cls.core = LinkCore(cls.backend, Settings())              # the real clock: the server is used in real time
        cls.server = ApiServer(("127.0.0.1", 0), cls.core, TOKEN)
        cls.server.start()
        cls.port = cls.server.server_address[1]
        cls.running = True
        cls.feeder = threading.Thread(target=cls.feed, daemon=True)
        cls.feeder.start()

    @classmethod
    def feed(cls):
        while cls.running:
            cls.core.on_odom(1.0, 2.0, 0.5, 0.0, 0.0)
            cls.core.on_tf(1.0, 2.0, 0.5, time.time())
            cls.core.on_localization_pose()
            cls.core.on_noise(-40.0, -41.0, -39.0)
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.running = False
        cls.server.stop()

    def call(self, method, path, body=None, token=TOKEN, raw=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        h = dict(headers or {})
        if token is not None:
            h["X-Robot-Token"] = token
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        payload = r.read()
        conn.close()
        return r.status, json.loads(payload), r

    def setUp(self):
        self.call("POST", "/api/heartbeat", {"t": time.time()})
        self.call("POST", "/api/cancel", {"goal_id": "*"})

    # ---- reading ----
    def test_health(self):
        status, body, r = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual((body["ok"], body["api_version"], body["ros_ok"], body["localised"]), (True, 1, True, True))
        self.assertEqual(r.getheader("Content-Type"), "application/json")

    def test_state(self):
        status, body, _ = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"pose", "nav", "noise", "time"})
        self.assertEqual(body["pose"]["frame"], "map")
        self.assertEqual(body["noise"]["unit"], "dBFS")

    def test_map_needs_the_token_and_says_when_there_is_none(self):
        self.assertEqual(self.call("GET", "/api/map", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/map", token="wrong")[0], 401)
        status, body, _ = self.call("GET", "/api/map")
        self.assertEqual((status, body), (200, {"ok": True, "available": False}))
        self.assertEqual(self.call("POST", "/api/map", {})[0], 405)

    def test_map_have_parameter(self):
        from robot_link import rules
        data = tuple([0] * 100)
        type(self).backend.map_grid = lambda: (1, rules.GridView(10, 10, 0.05, 0.0, 0.0, data))
        try:
            status, body, _ = self.call("GET", "/api/map")
            self.assertEqual((status, body["available"], body["changed"]), (200, True, True))
            again = self.call("GET", "/api/map?have=%d" % body["version"])[1]
            self.assertEqual((again["changed"], "png_base64" in again), (False, False))
            self.assertTrue(self.call("GET", "/api/map?have=abc")[1]["changed"])
        finally:
            del type(self).backend.map_grid

    # ---- token ----
    def test_missing_wrong_and_empty_tokens_are_401_with_the_error_body(self):
        for token in (None, "nope", "", TOKEN + "x", TOKEN[:-1]):
            status, body, _ = self.call("GET", "/api/health", token=token)
            self.assertEqual((status, body["ok"], body["error"]), (401, False, "unauthorised"), token)
            self.assertIn("detail", body)

    def test_the_token_is_checked_before_anything_else(self):
        self.assertEqual(self.call("GET", "/nothing", token="nope")[0], 401)
        self.assertEqual(self.call("POST", "/api/goal", {"x": 1}, token="nope")[0], 401)

    def test_a_non_ascii_token_header_is_401_not_a_crash(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        conn.request("GET", "/api/health", headers={"X-Robot-Token": "tökén"})
        self.assertEqual(conn.getresponse().status, 401)

    # ---- routes ----
    def test_unknown_paths_404_and_wrong_methods_405(self):
        self.assertEqual(self.call("GET", "/api/nothing")[:2], (404, {"ok": False, "error": "not_found", "detail": "/api/nothing"}))
        self.assertEqual(self.call("POST", "/api/health", {})[0], 405)
        self.assertEqual(self.call("GET", "/api/goal")[0], 405)
        self.assertEqual(self.call("PUT", "/api/goal", {})[0], 405)
        self.assertEqual(self.call("DELETE", "/api/state")[0], 405)

    def test_a_query_string_is_ignored(self):
        self.assertEqual(self.call("GET", "/api/health?x=1")[0], 200)

    # ---- bad requests ----
    def test_malformed_bodies_are_400_with_the_error_body(self):
        cases = [(b"not json", "the body is not JSON"), (b"[1, 2]", "the body must be a JSON object"), (b"", "the body must be a JSON object"),
                 (b"\xff\xfe", "the body is not JSON"), (b'{"goal_id": "a", "x": 1, "y": 2}', None),
                 (b'{"goal_id": "a", "x": NaN, "y": 2, "yaw": 0}', None), (b'{"goal_id": "a", "x": Infinity, "y": 2, "yaw": 0}', None),
                 (b'{"goal_id": "a", "x": true, "y": 2, "yaw": 0}', None), (b'{"goal_id": "a", "x": "1", "y": 2, "yaw": 0}', None),
                 (b'{"goal_id": 7, "x": 1, "y": 2, "yaw": 0}', None), (b'{"goal_id": "", "x": 1, "y": 2, "yaw": 0}', None)]
        before = len(self.backend.sent)
        for raw, detail in cases:
            status, body, _ = self.call("POST", "/api/goal", raw=raw)
            self.assertEqual((status, body["ok"], body["error"]), (400, False, "bad_request"), raw)
            if detail:
                self.assertEqual(body["detail"], detail)
        self.assertEqual(len(self.backend.sent), before)

    def test_other_endpoints_validate_too(self):
        self.assertEqual(self.call("POST", "/api/cancel", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/cancel", {"goal_id": 5})[0], 400)
        self.assertEqual(self.call("POST", "/api/speaking", {"speaking": "yes"})[0], 400)
        self.assertEqual(self.call("POST", "/api/speaking", {"speaking": 1})[0], 400)
        self.assertEqual(self.call("POST", "/api/heartbeat", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/heartbeat", {"t": "now"})[0], 400)
        for m_s in (-1, 0, "fast", None):
            self.assertEqual(self.call("POST", "/api/max_speed", {"m_s": m_s})[0], 400, m_s)

    def test_a_body_over_16_kb_is_413(self):
        status, body, _ = self.call("POST", "/api/goal", raw=b'{"goal_id": "' + b"a" * 20000 + b'"}')
        self.assertEqual((status, body["error"]), (413, "payload_too_large"))
        status, body, _ = self.call("POST", "/api/goal", raw=b'{"goal_id": "' + b"a" * 400000 + b'"}')
        self.assertEqual(status, 413)

    def test_a_bad_content_length_is_400(self):
        status, body, _ = self.call("POST", "/api/heartbeat", raw=b"{}", headers={"Content-Length": "abc"})
        self.assertEqual(status, 400)

    # ---- the verbs ----
    def test_heartbeat_speaking_and_cancel(self):
        status, body, _ = self.call("POST", "/api/heartbeat", {"t": time.time()})
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertAlmostEqual(body["time"], time.time(), delta=2)
        for value in (True, True, False, False):
            self.assertEqual(self.call("POST", "/api/speaking", {"speaking": value})[:2], (200, {"ok": True}))
        self.assertEqual(self.call("POST", "/api/cancel", {"goal_id": "never-sent"})[:2], (200, {"ok": True, "cancelled": False}))

    def test_goal_round_trip(self):
        status, body, _ = self.call("POST", "/api/goal", {"goal_id": "srv-1", "x": 3.0, "y": 2.0, "yaw": 0.0})
        self.assertEqual((status, body["accepted"]), (200, True))
        self.assertEqual(self.call("GET", "/api/state")[1]["nav"]["goal_id"], "srv-1")
        self.assertEqual(self.call("POST", "/api/goal", {"goal_id": "srv-1", "x": 3.0, "y": 2.0, "yaw": 0.0})[1]["duplicate"], True)
        self.assertEqual(self.call("POST", "/api/cancel", {"goal_id": "srv-1"})[1], {"ok": True, "cancelled": True})
        self.assertEqual(self.call("GET", "/api/state")[1]["nav"]["reason"], "cancelled_by_client")

    def test_a_goal_is_refused_with_200_and_a_reason(self):
        status, body, _ = self.call("POST", "/api/goal", {"goal_id": "srv-bad", "x": 3.0, "y": 2.0, "yaw": 9.0})
        self.assertEqual((status, body["accepted"], body["reason"]), (200, False, "invalid_pose"))

    def test_max_speed(self):
        status, body, _ = self.call("POST", "/api/max_speed", {"m_s": 0.6})
        self.assertEqual((status, body), (200, {"ok": True, "applied": 0.25}))

    # ---- speed and robustness ----
    def test_answers_come_quickly_even_in_parallel(self):
        slowest = []

        def hit():
            t = time.time()
            self.call("GET", "/api/state")
            slowest.append(time.time() - t)
        threads = [threading.Thread(target=hit) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLess(max(slowest), 0.5)

    def test_a_client_that_connects_and_says_nothing_does_not_block_others(self):
        import socket
        hung = socket.create_connection(("127.0.0.1", self.port))
        try:
            hung.sendall(b"POST /api/goal HTTP/1.1\r\nContent-Length: 50\r\n")        # never finishes
            t = time.time()
            self.assertEqual(self.call("GET", "/api/health")[0], 200)
            self.assertLess(time.time() - t, 0.5)
        finally:
            hung.close()


if __name__ == "__main__":
    unittest.main()
