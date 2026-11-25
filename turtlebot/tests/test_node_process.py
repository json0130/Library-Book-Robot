"""robot_link_api.py started as a real process WITHOUT ROS (no master): it must serve /api/health with ros_ok false.

Not part of the default run on the Dell: it starts the node. Run it with  ROBOT_LINK_RUN_NODE=1 python3 -m unittest tests/test_node_process.py
(it binds 127.0.0.1 only, and nothing it does can move the robot).
"""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NODE = os.path.join(os.path.dirname(HERE), "nodes", "robot_link_api.py")
TOKEN = "node-test-token-123456"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(os.environ.get("ROBOT_LINK_RUN_NODE") == "1", "set ROBOT_LINK_RUN_NODE=1 to start the node")
class NodeProcessTest(unittest.TestCase):
    def start(self, *args, token=TOKEN):
        env = {k: v for k, v in os.environ.items() if k != "ROBOT_LINK_TOKEN"}
        env["ROS_MASTER_URI"] = "http://127.0.0.1:%d" % free_port()          # nothing is listening there
        if token is not None:
            env["ROBOT_LINK_TOKEN"] = token
        return subprocess.Popen([sys.executable, NODE, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def get(self, port, path, token=TOKEN, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers={"X-Robot-Token": token})
        r = conn.getresponse()
        return r.status, json.loads(r.read())

    def wait_up(self, port, proc):
        end = time.time() + 10
        while time.time() < end:
            if proc.poll() is not None:
                self.fail("the node exited: " + proc.stdout.read())
            try:
                return self.get(port, "/api/health")
            except OSError:
                time.sleep(0.1)
        self.fail("the node did not start")

    def test_refuses_to_start_without_a_token(self):
        p = self.start("_host:=127.0.0.1", token=None)
        out, _ = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 2)
        self.assertIn("ROBOT_LINK_TOKEN is not set", out)
        p = self.start("_host:=127.0.0.1", token="   ")
        p.communicate(timeout=10)
        self.assertEqual(p.returncode, 2)

    def test_refuses_to_start_without_an_address(self):
        p = self.start("_iface:=nope0", "_wait_for_iface_s:=0")
        out, _ = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 3)
        self.assertIn("nope0", out)

    def test_serves_without_ros_and_stops_cleanly(self):
        port = free_port()
        p = self.start("_host:=127.0.0.1", "_port:=%d" % port)
        try:
            status, health = self.wait_up(port, p)
            self.assertEqual((status, health["ros_ok"], health["localised"], health["api_version"]), (200, False, False, 1))
            status, state = self.get(port, "/api/state")
            self.assertEqual((status, state["nav"]["status"], state["pose"]["localised"], state["noise"]), (200, "idle", False, None))
            self.assertEqual(self.get(port, "/api/health", token="wrong")[0], 401)
            self.assertEqual(self.get(port, "/api/heartbeat", method="POST", body={"t": 1.0})[0], 200)
            status, body = self.get(port, "/api/goal", method="POST", body={"goal_id": "x", "x": 1, "y": 1, "yaw": 0})
            self.assertEqual((status, body["error"]), (503, "ros_unavailable"))        # ROS is not there: no goal
            status, body = self.get(port, "/api/max_speed", method="POST", body={"m_s": 0.2})
            self.assertEqual(status, 503)
            self.assertEqual(self.get(port, "/api/cancel", method="POST", body={"goal_id": "*"})[1], {"ok": True, "cancelled": False})
        finally:
            p.send_signal(signal.SIGTERM)
            out, _ = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("listening on http://127.0.0.1:%d" % port, out)

    def test_the_token_never_appears_in_the_log(self):
        port = free_port()
        p = self.start("_host:=127.0.0.1", "_port:=%d" % port)
        try:
            self.wait_up(port, p)
            self.get(port, "/api/health", token="wrong-" + TOKEN)
        finally:
            p.send_signal(signal.SIGTERM)
            out, _ = p.communicate(timeout=10)
        self.assertNotIn(TOKEN, out)


if __name__ == "__main__":
    unittest.main()
