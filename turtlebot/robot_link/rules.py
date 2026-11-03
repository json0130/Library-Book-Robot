"""Small pure functions behind the API's answers. No ROS, no clocks, no state: easy to test."""

import math
from typing import List, Optional, Sequence, Tuple


def wrap_pi(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


def yaw_from_quaternion(x: float, y: float, z: float, w: float) -> float:
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def quaternion_from_yaw(yaw: float) -> Tuple[float, float, float, float]:
    """(x, y, z, w) of a rotation about z."""
    return 0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0)


def has_moved(a: Optional[Tuple[float, float, float]], b: Optional[Tuple[float, float, float]],
              max_dist_m: float, max_turn_rad: float) -> bool:
    """True if pose b is further than max_dist_m or turned more than max_turn_rad from pose a. An unknown pose counts as moved."""
    if a is None or b is None:
        return True
    return math.hypot(b[0] - a[0], b[1] - a[1]) > max_dist_m or abs(wrap_pi(b[2] - a[2])) > max_turn_rad


def pose_is_trusted(covariance, limit: float) -> bool:
    """Is a /rtabmap/localization_pose a real fix? RTAB-Map republishes the last pose with covariance 9999 when it has lost
    track, so a pose counts only when its x, y and yaw variances (covariance[0], [7], [35]) are all finite and at most `limit`."""
    try:
        values = [float(covariance[i]) for i in (0, 7, 35)]
    except (TypeError, ValueError, IndexError):
        return False
    return all(v == v and 0.0 <= v <= limit for v in values)


def is_localised(tf_age_s: Optional[float], loc_age_s: Optional[float], still: bool,
                 tf_max_age_s: float, loc_max_age_s: float) -> bool:
    """Does the base know where it is on the map?

    All of these must hold:
      * the map -> base_footprint transform is fresh (tf_age_s <= tf_max_age_s); without it there is no pose at all;
      * RTAB-Map has localised at least once (loc_age_s is not None);
      * the last /rtabmap/localization_pose is younger than loc_max_age_s, OR the wheel odometry shows that the robot has
        hardly moved since that localisation (`still`): a robot that stands still is not lost, and RTAB-Map may well stay quiet.
    """
    if tf_age_s is None or tf_age_s > tf_max_age_s:
        return False
    if loc_age_s is None:
        return False
    return loc_age_s <= loc_max_age_s or bool(still)


def failure_reason(text: str, localised: bool, ros_ok: bool) -> str:
    """Why move_base gave up on a goal, as one of the contract's `failed` reasons (docs/robot_link.md, /api/state)."""
    if not ros_ok:
        return "ros_unavailable"
    if not localised:
        return "lost_localisation"
    t = (text or "").lower()
    if "valid plan" in t:                                   # "Failed to find a valid plan. Even after executing recovery behaviors."
        return "no_path"
    if "oscillat" in t or "valid control" in t:             # "Robot is oscillating..." / "...a valid control could not be found"
        return "blocked"
    return "aborted"


def path_length(points: Sequence[Tuple[float, float]]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def remaining_along(points: Sequence[Tuple[float, float]], x: float, y: float) -> Optional[float]:
    """Metres still to drive along a plan from the robot at (x, y): the way to the nearest plan point plus the rest of the plan."""
    if not points:
        return None
    nearest = min(range(len(points)), key=lambda i: math.hypot(points[i][0] - x, points[i][1] - y))
    return math.hypot(points[nearest][0] - x, points[nearest][1] - y) + path_length(points[nearest:])


def distance_left(plan: Optional[Sequence[Tuple[float, float]]], x: float, y: float, goal_x: float, goal_y: float,
                  plan_end_tolerance_m: float = 0.5) -> float:
    """Distance to the goal: along the plan if it really leads to this goal, else in a straight line."""
    if plan and math.hypot(plan[-1][0] - goal_x, plan[-1][1] - goal_y) <= plan_end_tolerance_m:
        along = remaining_along(plan, x, y)
        if along is not None:
            return round(along, 2)
    return round(math.hypot(goal_x - x, goal_y - y), 2)


def battery_fraction(volts: Optional[float], empty_v: float = 13.2, full_v: float = 16.5) -> Optional[float]:
    """0..1 from the Kobuki's battery voltage (a rough guide: the voltage sags under load), None if unknown."""
    if volts is None or not math.isfinite(volts) or volts <= 0 or full_v <= empty_v:
        return None
    return round(min(1.0, max(0.0, (volts - empty_v) / (full_v - empty_v))), 2)


def finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class GridView:
    """A read-only look at a nav_msgs/OccupancyGrid (row-major, origin = the cell (0, 0)'s corner, no rotation): is a point usable?"""

    OCCUPIED = 65                       # a cell at or above this is a known obstacle (the costmap's own lethal threshold is 100)

    def __init__(self, width: int, height: int, resolution: float, origin_x: float, origin_y: float, data: Sequence[int]):
        self.width, self.height, self.resolution = int(width), int(height), float(resolution)
        self.origin_x, self.origin_y, self.data = float(origin_x), float(origin_y), data

    def cell(self, x: float, y: float) -> Optional[int]:
        """The cell value (-1 unknown, 0..100 chance of an obstacle) at a map point, None if the point is outside the grid."""
        if self.resolution <= 0:
            return None
        cx, cy = int(math.floor((x - self.origin_x) / self.resolution)), int(math.floor((y - self.origin_y) / self.resolution))
        if not (0 <= cx < self.width and 0 <= cy < self.height):
            return None
        index = cy * self.width + cx
        return self.data[index] if index < len(self.data) else None

    def refuses(self, x: float, y: float) -> bool:
        """True if (x, y) is outside the map or inside a known obstacle. Unknown space inside the map is allowed (the planner plans through it)."""
        value = self.cell(x, y)
        return value is None or value >= self.OCCUPIED
