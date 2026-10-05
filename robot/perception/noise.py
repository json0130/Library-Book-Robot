"""Ambient noise level from the microphone, for the zone override in core/context.py.

Planned: record short windows, convert to an approximate dB level with level_db(), average
over ~10 s and publish ContextChanged with noise_db. The dB scale needs calibrating with a
sound meter on the robot's mic. Not implemented beyond level_db().
"""

from __future__ import annotations

import math


def level_db(rms: float, reference_rms: float = 1.0, offset_db: float = 0.0) -> float:
    """Uncalibrated level in dB for an RMS of 16-bit samples: 20*log10(rms/reference) + offset."""
    return 20.0 * math.log10(max(rms, 1e-9) / reference_rms) + offset_db


class NoiseMeter:
    def __init__(self, bus, audio, interval_s: float = 10.0):
        self.bus, self.audio, self.interval_s = bus, audio, interval_s

    async def run(self) -> None:
        raise NotImplementedError("noise measurement is not implemented yet")
