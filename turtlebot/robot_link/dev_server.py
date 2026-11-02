"""The real API server over a simulated base, to test the Dell's side without ROS (and without the robot):

    ROBOT_LINK_TOKEN=secret python3 -m robot_link.dev_server --port 8010          # from the turtlebot/ folder
    python -m pytest tests/test_robot_link_contract.py --target http://127.0.0.1:8010 --drive    # from the repository root

It uses the same LinkCore and ApiServer as nodes/robot_link_api.py; only the Backend is a simulation (a robot that drives in a
straight line at 0.2 m/s and a microphone that hears 40 dBFS-ish noise), so it shows that the HTTP and the logic keep the contract.
"""

import argparse
import logging
import math
import os
import signal
import sys
import threading
import time

from robot_link.core import Backend, LinkCore, Settings
from robot_link.server import ApiServer


class SimBackend(Backend):
    SPEED = 0.2

    def __init__(self):
        self.core = None
        self.x, self.y, self.yaw = 1.0, 1.0, 0.0
        self.goal = None
        self.max_speed = 0.25
        self.speaking = False
        self.lock = threading.Lock()
        self.next_reading = 0.0

    def ros_ok(self): return True
    def battery(self): return 0.8
    def send_goal(self, goal_id, x, y, yaw):
        with self.lock:
            self.goal = (goal_id, x, y, yaw)
    def cancel_goal(self):
        with self.lock:
            self.goal = None
    def stop_base(self): pass
    def set_speaking(self, speaking): self.speaking = speaking
    def set_max_speed(self, m_s):
        self.max_speed = m_s
        return m_s

    def step(self, dt: float) -> None:
        with self.lock:
            goal = self.goal
            moving = False
            if goal is not None:
                gid, gx, gy, gyaw = goal
                dist = math.hypot(gx - self.x, gy - self.y)
                step = min(self.SPEED, self.max_speed) * dt
                if dist <= step + 0.05:
                    self.x, self.y, self.yaw, self.goal = gx, gy, gyaw, None
                    self.core.on_goal_result(gid, "succeeded")
                else:
                    self.yaw = math.atan2(gy - self.y, gx - self.x)
                    self.x += step * math.cos(self.yaw)
                    self.y += step * math.sin(self.yaw)
                    moving = True
        c = self.core
        c.on_tf(self.x, self.y, self.yaw, time.time())
        c.on_odom(self.x, self.y, self.yaw, self.SPEED if moving else 0.0, 0.0)
        c.on_localization_pose()
        if time.time() >= self.next_reading:                  # a microphone: a reading every half second
            self.next_reading = time.time() + 0.5
            c.on_noise(-40.0, -40.5, -39.5)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8010)
    args = p.parse_args(argv)
    token = os.environ.get("ROBOT_LINK_TOKEN", "")
    if not token:
        print("ROBOT_LINK_TOKEN is not set", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    backend = SimBackend()
    core = LinkCore(backend, Settings())
    backend.core = core
    server = ApiServer((args.host, args.port), core, token)
    server.start()
    print("dev server on http://%s:%d (simulated base)" % server.server_address, flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    last = time.time()
    while not stop.wait(0.05):
        now = time.time()
        backend.step(now - last)
        last = now
        core.tick()
    server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
