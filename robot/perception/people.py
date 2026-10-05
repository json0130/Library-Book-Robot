"""Counts the people with the robot from camera frames and publishes ContextChanged.

Planned: every second, run face detection (services.vision.Vision.detect) on a camera frame,
smooth the count over a few frames so one missed face doesn't flip alone/group, and publish
ContextChanged(zone=<map zone>, group_size=<count>) when the count changes. Not implemented.
"""

from __future__ import annotations


class PeopleCounter:
    def __init__(self, bus, camera, vision, map_zone: str = "common", interval_s: float = 1.0):
        self.bus, self.camera, self.vision = bus, camera, vision
        self.map_zone = map_zone
        self.interval_s = interval_s

    async def run(self) -> None:
        raise NotImplementedError("people counting is not implemented yet")
