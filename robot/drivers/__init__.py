"""Hardware drivers. Each real driver has a fake in fake.py with the same methods, so the
simulator and tests run without hardware. pyserial and OpenCV are imported only inside the
real drivers that need them."""

from __future__ import annotations


class DriverNotImplemented(RuntimeError):
    """A real driver the robot needs has not been written yet."""

    def __init__(self, name: str, detail: str = "", missing: list | None = None):
        self.name = name
        self.missing = missing or [self]          # every missing driver, when several are reported
        super().__init__(f"{name} driver is not implemented yet" + (f" ({detail})" if detail else ""))
