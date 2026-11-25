"""The scan pad's vibration motor, through the ESP32: a pulse tells the book to slide into its storage slot."""

from __future__ import annotations


class Pad:
    def __init__(self, esp32):
        self.esp32 = esp32

    def vibrate(self, ms: int = 400) -> None:
        self.esp32.send("pad_vibrate", ms=int(max(0, min(5000, ms))))
