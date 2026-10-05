"""LED ring and the book spotlight, through the ESP32."""

from __future__ import annotations


class Lights:
    def __init__(self, esp32):
        self.esp32 = esp32

    def set(self, color: str, brightness: float) -> None:
        self.esp32.send("light", color=color, brightness=round(max(0.0, min(1.0, brightness)), 2))

    def spotlight(self, on: bool) -> None:
        self.esp32.send("spotlight", on=bool(on))
