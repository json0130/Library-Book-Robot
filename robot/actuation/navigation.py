"""Base motion: the style's speed limit and going to a place. Stub: no mapping or planning yet; the ESP32
driver will carry `navigate` out, and the fake driver (the simulator) just logs it."""

from __future__ import annotations


class Navigation:
    def __init__(self, esp32):
        self.esp32 = esp32

    def set_speed_limit(self, mps: float) -> None:
        self.esp32.send("speed_limit", mps=round(max(0.0, mps), 2))

    def go_to_pose(self, x: float, y: float, yaw: float) -> None:
        """Drive to a map pose (metres, metres, radians), e.g. a shelf from data/shelves.csv."""
        self.esp32.send("navigate", x=round(x, 2), y=round(y, 2), yaw=round(yaw, 2))

    def go_to(self, area: str) -> None:
        """Drive to a named area from config/zones.yaml. Not implemented: needs localisation."""
        raise NotImplementedError("navigation is not implemented yet")
