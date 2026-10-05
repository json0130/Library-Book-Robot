"""Base motion: the style's speed limit and going to named areas. Stub: no mapping or planning yet."""

from __future__ import annotations


class Navigation:
    def __init__(self, esp32):
        self.esp32 = esp32

    def set_speed_limit(self, mps: float) -> None:
        self.esp32.send("speed_limit", mps=round(max(0.0, mps), 2))

    def go_to(self, area: str) -> None:
        """Drive to a named area from config/zones.yaml. Not implemented: needs localisation."""
        raise NotImplementedError("navigation is not implemented yet")
