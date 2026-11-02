"""LinkCore: everything the API decides, with no ROS and no HTTP in it.

The ROS node (nodes/robot_link_api.py) feeds it what ROS says (`on_tf`, `on_odom`, `on_localization_pose`, `on_plan`, `on_noise`,
`on_goal_result`) and gives it a Backend to act with (send / cancel a goal, stop the wheels, speed limit, the speaking topic).
The HTTP server (server.py) calls `health`, `state`, `handle_goal`, `handle_cancel`, `handle_speaking`, `handle_heartbeat`,
`handle_max_speed`. Every method answers from what it already holds and never waits for ROS, so no request is slow.
`tick()` runs about ten times a second (and before every request): it is the heartbeat watchdog and the other timers.

Goal states (`nav.status`): idle, active, succeeded, failed, cancelled. A finished goal stays reported until another goal arrives.
"""

import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, fields
from typing import Callable, Dict, List, Optional, Tuple

from robot_link import API_VERSION, calibration, rules
from robot_link.activity import RobotActivity

log = logging.getLogger("robot_link")

KNOWN_GOALS = 32                    # goal ids remembered, for "the same goal_id twice is the same goal"
YAW_TOLERANCE = 1e-3                # a rounding error beyond +-pi is still a valid yaw


class Unsupported(Exception):
    """The optional feature does not exist here: the API answers 404 and the Jetson never asks again."""


class Unavailable(Exception):
    """The feature exists but ROS cannot do it right now: the API answers 503."""


class Backend:
    """What the core needs from ROS. nodes/robot_link_api.py implements it; the tests and dev_server.py have fakes.
    None of these may block for long: they are called while a request is being answered."""

    def ros_ok(self) -> bool: raise NotImplementedError
    def battery(self) -> Optional[float]: return None                       # 0..1, None if unknown
    def outside_map(self, x: float, y: float) -> bool: return False         # outside the known map, or in a permanent obstacle
    def send_goal(self, goal_id: str, x: float, y: float, yaw: float) -> None: raise NotImplementedError
    def cancel_goal(self) -> None: raise NotImplementedError                # our goal only
    def stop_base(self) -> None: raise NotImplementedError                  # zero velocity
    def set_speaking(self, speaking: bool) -> None: pass                    # publish /robot_speaking
    def set_max_speed(self, m_s: float) -> float: raise Unsupported()       # returns what was applied


@dataclass
class Settings:
    heartbeat_timeout_s: float = 3.0            # fixed by the contract
    max_speed_limit: float = 0.25               # m/s: the most /api/max_speed will ever apply (the DWA limit in hokuyo_costmap.yaml)
    min_speed: float = 0.05                     # m/s: the least (below this DWA would not move)
    tf_max_age_s: float = 1.0                   # map -> base_footprint older than this: no pose
    loc_max_age_s: float = 5.0                  # /rtabmap/localization_pose older than this counts only if the robot stood still
    still_dist_m: float = 0.05                  # "stood still" = less than this ...
    still_turn_deg: float = 5.0                 # ... and this much turning in the wheel odometry since that localisation
    odom_max_age_s: float = 1.0
    lost_localisation_after_s: float = 5.0      # a goal fails if the base has been not localised this long while driving
    goal_timeout_s: float = 900.0
    ros_down_after_s: float = 2.0               # ROS gone this long while driving: the goal fails
    noise_stale_s: float = 3.0                  # no reading for this long: noise is null (the microphones are not running)
    plan_max_age_s: float = 3.0
    speaking_stale_s: float = 30.0
    robot_noise_grace_s: float = 0.5

    @classmethod
    def from_mapping(cls, values: Dict[str, object]) -> "Settings":
        known = {f.name: f.type for f in fields(cls)}
        return cls(**{k: float(v) for k, v in values.items() if k in known and v is not None})


def error(status: int, code: str, detail: str = "") -> Tuple[int, dict]:
    return status, {"ok": False, "error": code, "detail": detail}


class LinkCore:
    def __init__(self, backend: Backend, settings: Optional[Settings] = None, clock: Callable[[], float] = time.time):
        self.b, self.s, self.clock = backend, settings or Settings(), clock
        self.lock = threading.RLock()
        self.activity = RobotActivity(grace_s=self.s.robot_noise_grace_s, speaking_stale_s=self.s.speaking_stale_s, clock=clock,
                                      odom_max_age_s=self.s.odom_max_age_s)
        self.calibration: Optional[calibration.Calibration] = None
        self.last_heartbeat: Optional[float] = None
        self.goal: Optional[dict] = None
        self.known: "OrderedDict[str, bool]" = OrderedDict()
        self.tf: Optional[Tuple[float, float, float, float]] = None            # x, y, yaw, stamp
        self.odom: Optional[Tuple[float, float, float, float]] = None          # x, y, yaw, arrival
        self.loc_at: Optional[float] = None                                    # when /rtabmap/localization_pose last came
        self.loc_odom: Optional[Tuple[float, float, float]] = None             # the wheel odometry pose at that moment
        self.loc_count = 0
        self.plan: List[Tuple[float, float]] = []
        self.plan_at: Optional[float] = None
        self.reading: Optional[dict] = None                                    # raw dBFS reading: overall, left, right, t, robot_noise
        self._speaking_published = False
        self._ros_down_since: Optional[float] = None
        self._unlocalised_since: Optional[float] = None

    def configure(self, settings: Settings) -> None:
        with self.lock:
            self.s = settings
            self.activity.speaking_stale_s = settings.speaking_stale_s
            self.activity.grace_s = settings.robot_noise_grace_s
            self.activity.odom_max_age_s = settings.odom_max_age_s

    def set_calibration(self, cal: Optional[calibration.Calibration]) -> None:
        with self.lock:
            self.calibration = cal

    # ------------------------------------------------------------------------------------------ what ROS says
    def on_tf(self, x: float, y: float, yaw: float, stamp: float) -> None:
        with self.lock:
            self.tf = (x, y, yaw, stamp)

    def on_odom(self, x: float, y: float, yaw: float, vx: float, wz: float) -> None:
        now = self.clock()
        with self.lock:
            self.odom = (x, y, yaw, now)
        self.activity.on_odom(vx, wz, now)

    def on_localization_pose(self) -> None:
        """/rtabmap/localization_pose arrived: RTAB-Map found the robot on the map. Remember where the wheels say we are."""
        now = self.clock()
        with self.lock:
            self.loc_at, self.loc_count = now, self.loc_count + 1
            self.loc_odom = self.odom[:3] if self.odom is not None else None

    def on_plan(self, points: List[Tuple[float, float]]) -> None:
        with self.lock:
            self.plan, self.plan_at = list(points), self.clock()

    def on_noise(self, overall: float, left: float, right: float) -> None:
        """A reading from noise_logger in dBFS. Whether it is the robot's own noise is decided now, as it arrives."""
        now = self.clock()
        reading = {"overall": overall, "left": left, "right": right, "t": round(now, 3),
                   "robot_noise": self.activity.reading_is_robot_noise(now)}
        with self.lock:
            self.reading = reading

    def on_goal_result(self, goal_id: str, outcome: str, text: str = "") -> None:
        """move_base finished the goal we sent: outcome is succeeded, aborted, rejected or preempted."""
        with self.lock:
            g = self.goal
            if g is None or g["goal_id"] != goal_id or g["status"] != "active":
                return                                                # an old goal, or one we ended ourselves
            now = self.clock()
            if outcome == "succeeded":
                self._finish(g, "succeeded", "")
            elif outcome in ("aborted", "rejected"):
                self._finish(g, "failed", rules.failure_reason(text, self._localised(now), self._ros_ok()))
            else:                                                     # preempted / recalled by someone else: the click-to-go page, say
                self._finish(g, "cancelled", "manual_override")
            log.info("goal %s: %s%s (%s)", goal_id, g["status"], " - " + g["reason"] if g["reason"] else "", text or outcome)

    # ------------------------------------------------------------------------------------------ derived
    def _ros_ok(self) -> bool:
        try:
            return bool(self.b.ros_ok())
        except Exception:
            log.exception("ros_ok failed")
            return False

    def _tf_age(self, now: float) -> Optional[float]:
        return None if self.tf is None else max(0.0, now - self.tf[3])

    def _still(self, now: float) -> bool:
        if self.loc_odom is None or self.odom is None or now - self.odom[3] > self.s.odom_max_age_s:
            return False
        return not rules.has_moved(self.loc_odom, self.odom[:3], self.s.still_dist_m, math.radians(self.s.still_turn_deg))

    def _localised(self, now: float) -> bool:
        loc_age = None if self.loc_at is None else now - self.loc_at
        return rules.is_localised(self._tf_age(now), loc_age, self._still(now), self.s.tf_max_age_s, self.s.loc_max_age_s)

    def localised(self) -> bool:
        with self.lock:
            return self._localised(self.clock())

    def _heartbeat_ok(self, now: float) -> bool:
        return self.last_heartbeat is not None and now - self.last_heartbeat <= self.s.heartbeat_timeout_s

    # ------------------------------------------------------------------------------------------ goals
    def _finish(self, goal: dict, status: str, reason: str, stop: bool = False) -> None:
        goal["status"], goal["reason"] = status, reason
        self._unlocalised_since = self._ros_down_since = None
        if stop:
            try:
                self.b.cancel_goal()
            except Exception:
                log.exception("cancel_goal failed")
            try:
                self.b.stop_base()
            except Exception:
                log.exception("stop_base failed")

    def tick(self) -> None:
        with self.lock:
            self._tick(self.clock())

    def _tick(self, now: float) -> None:
        g = self.goal
        if g is not None and g["status"] == "active":
            ros_ok, localised = self._ros_ok(), self._localised(now)
            self._ros_down_since = None if ros_ok else (self._ros_down_since or now)
            self._unlocalised_since = None if localised else (self._unlocalised_since or now)
            if not self._heartbeat_ok(now):
                log.warning("goal %s: no heartbeat for %.1f s: the base stops", g["goal_id"],
                            now - (self.last_heartbeat if self.last_heartbeat is not None else g["accepted_at"]))
                self._finish(g, "cancelled", "heartbeat_timeout", stop=True)
            elif now - g["accepted_at"] > self.s.goal_timeout_s:
                self._finish(g, "failed", "timeout", stop=True)
            elif self._ros_down_since is not None and now - self._ros_down_since >= self.s.ros_down_after_s:
                self._finish(g, "failed", "ros_unavailable", stop=True)
            elif self._unlocalised_since is not None and now - self._unlocalised_since >= self.s.lost_localisation_after_s:
                log.warning("goal %s: the base is not localised any more: stopping", g["goal_id"])
                self._finish(g, "failed", "lost_localisation", stop=True)
        speaking = self.activity.speaking(now)               # a flag nobody cleared lapses by itself
        if speaking != self._speaking_published:
            self._publish_speaking(speaking)

    def handle_goal(self, goal_id: str, x: float, y: float, yaw: float) -> Tuple[int, dict]:
        with self.lock:
            now = self.clock()
            self._tick(now)
            if goal_id in self.known:
                return 200, {"ok": True, "accepted": True, "duplicate": True, "goal_id": goal_id, "reason": ""}
            if not self._ros_ok():
                return error(503, "ros_unavailable", "move_base is not running")
            refuse = ""
            if not self._heartbeat_ok(now):
                refuse = "no_heartbeat"
            elif not self._localised(now):
                refuse = "not_localised"
            elif not all(rules.finite(v) for v in (x, y, yaw)) or abs(yaw) > math.pi + YAW_TOLERANCE:
                refuse = "invalid_pose"
            elif self.b.outside_map(x, y):
                refuse = "outside_map"
            if refuse:
                log.info("goal %s refused: %s", goal_id, refuse)
                return 200, {"ok": True, "accepted": False, "goal_id": goal_id, "reason": refuse}
            try:
                self.b.send_goal(goal_id, x, y, rules.wrap_pi(yaw))
            except Exception as exc:
                log.exception("send_goal failed")
                return error(503, "ros_unavailable", str(exc))
            old = self.goal
            if old is not None and old["status"] == "active":
                self._finish(old, "cancelled", "superseded")
            self.goal = {"goal_id": goal_id, "x": x, "y": y, "yaw": yaw, "status": "active", "reason": "", "accepted_at": now}
            self.known[goal_id] = True
            while len(self.known) > KNOWN_GOALS:
                self.known.popitem(last=False)
            self._unlocalised_since = self._ros_down_since = None
            log.info("goal %s accepted: (%.2f, %.2f, %.2f)", goal_id, x, y, yaw)
            return 200, {"ok": True, "accepted": True, "goal_id": goal_id, "reason": ""}

    def handle_cancel(self, goal_id: str) -> Tuple[int, dict]:
        with self.lock:
            g = self.goal
            hit = g is not None and g["status"] == "active" and goal_id in ("*", g["goal_id"])
            if hit:
                log.info("goal %s cancelled by the robot", g["goal_id"])
                self._finish(g, "cancelled", "cancelled_by_client", stop=True)
            return 200, {"ok": True, "cancelled": bool(hit)}

    def handle_heartbeat(self) -> Tuple[int, dict]:
        now = self.clock()
        with self.lock:
            self.last_heartbeat = now
        return 200, {"ok": True, "time": round(now, 3)}

    def _publish_speaking(self, speaking: bool) -> None:
        self._speaking_published = speaking
        try:
            self.b.set_speaking(speaking)
        except Exception:
            log.exception("publishing /robot_speaking failed")

    def handle_speaking(self, speaking: bool) -> Tuple[int, dict]:
        with self.lock:
            self.activity.set_speaking(speaking)
            self._publish_speaking(speaking)
        return 200, {"ok": True}

    def handle_max_speed(self, m_s: float) -> Tuple[int, dict]:
        wanted = min(max(m_s, self.s.min_speed), self.s.max_speed_limit)
        try:
            applied = self.b.set_max_speed(wanted)
        except Unsupported:
            return error(404, "not_found", "/api/max_speed")
        except Unavailable as exc:
            return error(503, "ros_unavailable", str(exc) or "move_base is not running")
        except Exception as exc:
            log.exception("set_max_speed failed")
            return error(500, "internal", str(exc))
        return 200, {"ok": True, "applied": round(float(applied), 3)}

    # ------------------------------------------------------------------------------------------ answers
    def health(self) -> dict:
        ros_ok = self._ros_ok()
        try:
            battery = self.b.battery()
        except Exception:
            battery = None
        with self.lock:
            self._tick(self.clock())
            return {"ok": True, "api_version": API_VERSION, "ros_ok": ros_ok, "localised": self._localised(self.clock()),
                    "battery": battery, "time": round(self.clock(), 3)}

    def _pose_block(self, now: float) -> dict:
        x, y, yaw, stamp = self.tf if self.tf is not None else (0.0, 0.0, 0.0, 0.0)
        return {"x": round(x, 3), "y": round(y, 3), "yaw": round(yaw, 3), "frame": "map", "localised": self._localised(now),
                "t": round(stamp, 3)}

    def _nav_block(self, now: float) -> dict:
        g = self.goal
        if g is None:
            return {"status": "idle", "goal_id": "", "distance_left": None, "reason": ""}
        left = None
        if g["status"] == "active" and self.tf is not None:
            plan = self.plan if self.plan_at is not None and now - self.plan_at <= self.s.plan_max_age_s else None
            left = rules.distance_left(plan, self.tf[0], self.tf[1], g["x"], g["y"])
        return {"status": g["status"], "goal_id": g["goal_id"], "distance_left": left, "reason": g["reason"]}

    def _noise_block(self, now: float) -> Optional[dict]:
        r = self.reading
        if r is None or now - r["t"] > self.s.noise_stale_s:
            return None
        if self.calibration is not None:
            body = self.calibration.apply(r["left"], r["right"])
            body.update(unit="dB", calibrated=True)
        else:
            body = {"overall": round(r["overall"], 1), "left": round(r["left"], 1), "right": round(r["right"], 1),
                    "balance": round(r["left"] - r["right"], 1), "unit": "dBFS", "calibrated": False}
        body.update(robot_noise=r["robot_noise"], t=r["t"])
        return body

    def state(self) -> dict:
        with self.lock:
            now = self.clock()
            self._tick(now)
            return {"pose": self._pose_block(now), "nav": self._nav_block(now), "noise": self._noise_block(now), "time": round(now, 3)}

    def debug(self) -> dict:
        """For the node's log line: what the localisation rule sees."""
        with self.lock:
            now = self.clock()
            return {"tf_age": self._tf_age(now), "loc_age": None if self.loc_at is None else round(now - self.loc_at, 2),
                    "still": self._still(now), "localised": self._localised(now), "loc_count": self.loc_count,
                    "heartbeat_age": None if self.last_heartbeat is None else round(now - self.last_heartbeat, 2),
                    "speaking": self.activity.speaking(now), "moving": self.activity.moving(now),
                    "goal": None if self.goal is None else dict(self.goal)}
