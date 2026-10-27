"""Thermal printer for slips (book details, a recommendation), through the ESP32.
Used instead of speech in quiet zones."""

from __future__ import annotations


class Printer:
    def __init__(self, esp32, width: int = 32):
        self.esp32 = esp32
        self.width = width

    def print_slip(self, text: str) -> None:
        self.esp32.send("print", text=text[:500])
