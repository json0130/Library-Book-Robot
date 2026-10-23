"""The scenario clock: the robot's own timers (auto-return delay, simulated drives, timeouts) run `speed` times faster,
while the LLM and the network keep their real latency (that is measured with the real clock)."""

from __future__ import annotations

import asyncio
import time


class ScaledClock:
    """now() grows `speed` times faster than real time. Pass it as the dialogue's and the screen composer's clock."""

    def __init__(self, speed: float = 1.0, start: float = 1000.0, real=time.monotonic):
        if speed <= 0:
            raise ValueError("speed must be more than 0")
        self.speed, self.real = float(speed), real
        self._r0, self._v0 = real(), start

    def __call__(self) -> float:
        return self._v0 + (self.real() - self._r0) * self.speed

    def real_seconds(self, virtual_seconds: float) -> float:
        return virtual_seconds / self.speed

    async def sleep(self, virtual_seconds: float) -> None:
        """Wait `virtual_seconds` of robot time."""
        await asyncio.sleep(max(0.0, virtual_seconds) / self.speed)
