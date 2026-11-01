"""Base motion: the style's speed limit and going to a place.

Two backends, chosen by `navigation.backend` in config/robot.yaml:

    sim        the ESP32 `navigate` command, which the fake driver (the simulator) only logs; the dialogue's drive is a timer
               (`returning.sim_drive_s`). Exactly as it always was.
    turtlebot  the Dell on the TurtleBot, over the link (drivers/turtlebot_link.py, docs/robot_link.md). A goal gets a new
               goal_id each time; the dialogue waits for NavArrived instead of the timer.

Both have the same methods, so the rest of the robot does not know which one it has. `async_arrival` says whether the
dialogue should wait for NavArrived / NavFailed (True) or run its timer (False).
"""

from __future__ import annotations

import logging
import math
from typing import Optional

log = logging.getLogger("robot.nav")


class Navigation:
    async_arrival = False                  # the dialogue's drive is a timer
    current_goal: Optional[str] = None

    def __init__(self, esp32):
        self.esp32 = esp32

    def available(self) -> bool:
        return True

    def set_speed_limit(self, mps: float) -> None:
        self.esp32.send("speed_limit", mps=round(max(0.0, mps), 2))

    def go_to_pose(self, x: float, y: float, yaw: float, floor: Optional[int] = None) -> Optional[str]:
        """Drive to a map pose (metres, metres, radians), e.g. a shelf from data/shelves.csv. Returns the goal's id (None here)."""
        self.esp32.send("navigate", x=round(x, 2), y=round(y, 2), yaw=round(yaw, 2))
        return None

    def stop(self) -> None:
        """Stop where it is (someone wants to talk). The drive is started again with go_to_pose."""
        self.esp32.send("stop")

    def go_to(self, area: str) -> None:
        """Drive to a named area from config/zones.yaml. Not implemented: needs localisation."""
        raise NotImplementedError("navigation is not implemented yet")


class GoalRejected(ValueError):
    """The goal fails the safety checks and is never sent."""


class TurtleBotNavigation(Navigation):
    async_arrival = True

    def __init__(self, link, plan=None, max_goal_distance_m: float = 40.0, floor: Optional[int] = None):
        self.link, self.plan = link, plan
        self.max_goal_distance_m = float(max_goal_distance_m)
        self.floor = floor                                  # the floor the base's map covers (None: any floor of the plan)
        self.current_goal: Optional[str] = None
        self.speed_limit: Optional[float] = None

    def available(self) -> bool:
        return self.link.available()

    def set_speed_limit(self, mps: float) -> None:
        mps = round(max(0.0, mps), 2)
        if mps != self.speed_limit:
            self.speed_limit = mps
            self.link.set_max_speed(mps)

    def check_goal(self, x: float, y: float, yaw: float, floor: Optional[int] = None) -> None:
        """Raises GoalRejected unless the goal is a real pose inside the floor plan and not absurdly far away."""
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (x, y, yaw)):
            raise GoalRejected(f"not a pose: ({x}, {y}, {yaw})")
        if self.floor is not None and floor is not None and int(floor) != int(self.floor):
            raise GoalRejected(f"it is on level {floor}, the base drives on level {self.floor}")
        if self.plan is not None:
            level = self.floor if self.floor is not None else floor
            if level is None and self.plan.station is not None:
                level = self.plan.station.level
            plan_floor = self.plan.floor(level) if level is not None else None
            if plan_floor is not None and not (0 <= x <= plan_floor.width and 0 <= y <= plan_floor.height):
                raise GoalRejected(f"({x:.2f}, {y:.2f}) is outside level {plan_floor.level} "
                                   f"({plan_floor.width:g} x {plan_floor.height:g} m)")
        pose = self.link.pose
        if pose and self.link.localised:
            far = math.hypot(x - float(pose.get("x", 0.0)), y - float(pose.get("y", 0.0)))
            if far > self.max_goal_distance_m:
                raise GoalRejected(f"{far:.1f} m from the base, more than the {self.max_goal_distance_m:g} m limit")

    def go_to_pose(self, x: float, y: float, yaw: float, floor: Optional[int] = None) -> Optional[str]:
        goal_id = self.link.new_goal_id()
        self.current_goal = goal_id
        try:
            self.check_goal(x, y, yaw, floor)
        except GoalRejected as exc:
            log.warning("[nav] goal %s rejected, not sent: %s", goal_id, exc)
            self.link.reject(goal_id, f"rejected: {exc}")
            return goal_id
        log.info("[nav] goal %s: (%.2f, %.2f, %.2f)", goal_id, x, y, yaw)
        self.link.send_goal(goal_id, x, y, yaw)
        return goal_id

    def stop(self) -> None:
        """Cancel the goal being driven; the base stops at once."""
        goal, self.current_goal = self.current_goal, None
        if goal is not None:
            log.info("[nav] cancel %s", goal)
            self.link.cancel(goal)
