"""Serial link to the ESP32 that runs lights, neck, storage ring, printer and base motors.

Protocol (planned, see firmware/esp32/README.md): newline-delimited JSON at 115200 baud,
one command per line, e.g. {"id": 7, "cmd": "light", "color": "#3a6fd8", "brightness": 0.2},
answered by {"id": 7, "ok": true}. Not implemented yet: the firmware does not exist.
"""

from __future__ import annotations

from robot.drivers import DriverNotImplemented


class Esp32:
    def __init__(self, port: str, baud: int = 115200):
        # When written: `import serial` here (pyserial, optional extra), so the sim never needs it.
        raise DriverNotImplemented("esp32", f"serial link on {port}; protocol in firmware/esp32/README.md")

    def send(self, cmd: str, **params) -> None:
        """Send one command and wait for its acknowledgement."""
        raise NotImplementedError

    def close(self) -> None:
        pass
