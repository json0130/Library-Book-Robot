"""Shared helpers for the Dell-side tests: a fake clock and a fake Backend. Python 3.8, no ROS."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from robot_link.core import Backend, LinkCore, Settings, Unavailable, Unsupported       # noqa: E402,F401


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeBackend(Backend):
    def __init__(self):
        self.ok = True
        self.battery_value = 0.8
        self.outside = False
        self.sent, self.cancelled, self.stopped, self.speaking_calls = [], 0, 0, []
        self.speed_result = None            # an Exception to raise, or a number to answer
        self.speeds = []
        self.fail_send = False

    def ros_ok(self): return self.ok
    def battery(self): return self.battery_value
    def outside_map(self, x, y): return self.outside
    def send_goal(self, goal_id, x, y, yaw):
        if self.fail_send:
            raise RuntimeError("no move_base")
        self.sent.append((goal_id, x, y, yaw))
    def cancel_goal(self): self.cancelled += 1
    def stop_base(self): self.stopped += 1
    def set_speaking(self, speaking): self.speaking_calls.append(speaking)
    def set_max_speed(self, m_s):
        self.speeds.append(m_s)
        if isinstance(self.speed_result, Exception):
            raise self.speed_result
        return m_s if self.speed_result is None else self.speed_result


def make_core(settings=None, localised=True, heartbeat=True):
    """A core on a fake clock, with a fresh pose, and (by default) localised and with a heartbeat."""
    clock, backend = Clock(), FakeBackend()
    core = LinkCore(backend, settings or Settings(), clock)
    if localised:
        feed_pose(core, clock)
    if heartbeat:
        core.handle_heartbeat()
    return core, backend, clock


def feed_pose(core, clock, x=1.0, y=2.0, yaw=0.5, localisation=True):
    """What ROS would have said by now: the odometry, the map->base transform and a RTAB-Map localisation."""
    core.on_odom(x, y, yaw, 0.0, 0.0)
    core.on_tf(x, y, yaw, clock())
    if localisation:
        core.on_localization_pose()


def drive_clock(core, clock, seconds, step=0.1, beat=True, pose=True):
    """Advance time in small steps, keeping the transform fresh (and optionally the heartbeat), calling tick like the node does."""
    end = clock() + seconds
    while clock() < end - 1e-9:
        clock.advance(step)
        if pose and core.tf is not None:
            core.on_tf(core.tf[0], core.tf[1], core.tf[2], clock())
            core.on_odom(core.tf[0], core.tf[1], core.tf[2], 0.0, 0.0)
        if beat:
            core.handle_heartbeat()
        core.tick()
