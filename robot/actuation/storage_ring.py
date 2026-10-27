"""Rotating storage ring that holds returned books, through the ESP32. Stub: slots are not mapped yet."""

from __future__ import annotations


DEFAULT_SLOTS = 8                # slots 0..7; tools/catalog_check.py checks the catalogue against this


class StorageRing:
    def __init__(self, esp32, slots: int = DEFAULT_SLOTS):
        self.esp32 = esp32
        self.slots = slots

    def rotate_to(self, slot: int) -> None:
        if not 0 <= slot < self.slots:
            raise ValueError(f"slot must be 0..{self.slots - 1}")
        self.esp32.send("ring", slot=slot)
