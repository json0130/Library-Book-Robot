"""Which zone (quiet or common) the robot is in, when it can drive and listen: the pose, the noise, and who is talking to it.

Precedence, from the first that applies:

    1. frozen      while a visitor is being helped (the dialogue is in ENGAGED) the zone does not change, so the style of a
                   conversation never flips half-way through it. What it was just before the visit is kept.
    2. noise       the base's microphones (perception/noise.py): a room that has been loud for a while is common, one that has
                   been quiet for a while is quiet, whatever the map says (with hysteresis; no answer until there is enough data).
    3. map         `zone_from_pose: true` and the base localised: the zone of the floor-plan section the base is in (the
                   section's own `zone:` in data/floorplan.yaml, else the zone of its shelves in shelves.csv); outside every
                   section the `default_zone` of config/zones.yaml. Otherwise the zone that was typed or set by the operator
                   (the sim's `quiet alone`, the operator page's Quiet / Common buttons), which is also what the robot starts with.

So moving from the quiet reading room to the common foyer switches the style while the robot is IDLE or RETURNING; a noisy
quiet-section is treated as common; and nothing changes while it talks to someone. Frozen and noise are both lifted the
moment the visit ends (the next refresh), so the robot follows the room again.

This module is used only when the robot has a link to the base (`navigation.backend: turtlebot`); the simulator keeps its
typed zone and the older instantaneous override (`noise <dB>`), exactly as before.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable, Optional

from robot.perception.noise import NoiseFilter


@dataclass(frozen=True)
class ZoneDecision:
    zone: str
    source: str                          # frozen | noise | map
    map_zone: str
    noise_zone: Optional[str] = None
    noise_db: Optional[float] = None
    area: str = ""                       # the section the base is in, when the zone comes from the pose
    frozen: bool = False


def section_zones(graph) -> dict:
    """{section name (lower case): the zone most of its shelves have}, from shelves.csv."""
    votes: dict = {}
    for shelf in getattr(graph, "shelves", {}).values():
        if shelf.section and shelf.zone:
            counts = votes.setdefault(shelf.section.lower(), {})
            counts[shelf.zone] = counts.get(shelf.zone, 0) + 1
    return {name: max(counts, key=lambda z: (counts[z], z == "quiet")) for name, counts in votes.items()}


class ZoneResolver:
    def __init__(self, cfg, plan=None, sections: Optional[dict] = None, is_frozen: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.monotonic, level: Optional[int] = None):
        zones = cfg.zones or {}
        self.default = zones.get("default_zone", "common")
        self.zone_from_pose = bool(zones.get("zone_from_pose", False))
        self.dwell_s = float(zones.get("zone_dwell_s", 3.0))
        self.noise = NoiseFilter(cfg.noise, clock)
        self.plan, self.sections, self.is_frozen, self.clock = plan, dict(sections or {}), is_frozen, clock
        self.level = level
        self.typed = self.default                    # the zone typed, set by the operator, or the starting one
        self.pose: Optional[tuple] = None            # (x, y, localised)
        self._map: Optional[tuple] = None            # (zone, area) in force from the pose
        self._candidate: Optional[tuple] = None      # (zone, area, since) waiting out zone_dwell_s
        self._held: Optional[ZoneDecision] = None    # the last decision made while not frozen

    # ---- inputs ----
    def set_typed(self, zone: str) -> None:
        self.typed = zone

    def on_pose(self, x: float, y: float, localised: bool) -> None:
        self.pose = (x, y, bool(localised))

    def on_noise(self, reading) -> bool:
        return self.noise.add(reading)

    # ---- looking the position up ----
    def _level(self) -> Optional[int]:
        if self.level is not None:
            return self.level
        if self.plan is not None and self.plan.station is not None:
            return self.plan.station.level
        return next(iter(self.plan.floors), None) if self.plan is not None and self.plan.floors else None

    def zone_at(self, x: float, y: float) -> tuple:
        """(zone, section name) at a point of the base's floor: the section's zone, else the default zone."""
        floor = self.plan.floor(self._level()) if self.plan is not None and self._level() is not None else None
        if floor is not None:
            for section in floor.sections:
                if section.contains(x, y):
                    zone = getattr(section, "zone", None) or self.sections.get(section.name.lower())
                    return (zone or self.default), section.name
        return self.default, ""

    def _from_pose(self) -> Optional[tuple]:
        """(zone, area) from the pose once it has stayed in a new zone for zone_dwell_s; None when the pose cannot be used."""
        if not self.zone_from_pose or self.pose is None or not self.pose[2]:
            return None
        zone, area = self.zone_at(self.pose[0], self.pose[1])
        now = self.clock()
        if self._map is None:
            self._map, self._candidate = (zone, area), None
        elif zone != self._map[0]:
            if self._candidate is None or self._candidate[0] != zone:
                self._candidate = (zone, area, now)
            if now - self._candidate[2] >= self.dwell_s:
                self._map, self._candidate = (zone, area), None
        else:
            self._map, self._candidate = (zone, area), None      # same zone: only the area name moves
        return self._map

    # ---- the answer ----
    def decide(self) -> ZoneDecision:
        if self.is_frozen() and self._held is not None:
            return replace(self._held, source="frozen", frozen=True)
        pose_zone = self._from_pose()
        map_zone, area = pose_zone if pose_zone else (self.typed, "")
        noise_zone = self.noise.zone
        zone, source = (noise_zone, "noise") if noise_zone else (map_zone, "map")
        decision = ZoneDecision(zone, source, map_zone, noise_zone, self.noise.median, area)
        if not self.is_frozen():
            self._held = decision
        return decision

    def info(self) -> dict:
        d = self.decide()
        return {"zone": d.zone, "source": d.source, "map_zone": d.map_zone, "area": d.area, "noise_zone": d.noise_zone,
                "from_pose": self.zone_from_pose, "noise": self.noise.info()}
