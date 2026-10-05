"""Neck pose (lowered in quiet zones, upright in common areas) and gaze, through the ESP32."""

from __future__ import annotations

POSES = ("lowered", "upright")


class Neck:
    def __init__(self, esp32):
        self.esp32 = esp32

    def pose(self, name: str) -> None:
        if name not in POSES:
            raise ValueError(f"unknown neck pose {name!r}")
        self.esp32.send("neck", pose=name)

    def look_at(self, target: str) -> None:
        """Turn the head toward a target: "visitor", "pad" (the book), or "forward"."""
        self.esp32.send("gaze", target=target)
