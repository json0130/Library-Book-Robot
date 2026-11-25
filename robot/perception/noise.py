"""Ambient noise level, for the zone override in core/context.py and core/zones.py.

The base's microphones (the Dell's `noise_logger`) send a NoiseReading about twice a second through the link
(docs/robot_link.md). `NoiseFilter` turns that stream into a steady answer: is this room loud (common) or quiet?

    robot_noise      readings taken while the robot itself was talking or driving say nothing about the room: ignored.
    calibrated       only a reading in calibrated dB can be compared with the thresholds in config/zones.yaml. A raw dBFS
                     reading (calibrated = false) is ignored, or compared with `noise.raw_thresholds`, as
                     `noise.uncalibrated` says (ignore | raw).
    rolling median   over `noise.window_s` (45 s): a door slam or one burst of speech does not change the room.
    hysteresis       the room becomes common when the median is above `to_common_above_db` (55) and quiet again only when
                     it is below `to_quiet_below_db` (48); in between, the last answer stands. No answer (None) until
                     `min_samples` readings are in, and again when the readings stop (the window empties).

`level_db` and `NoiseMeter` are the older plan for measuring with the robot's own microphone (not implemented).
"""

from __future__ import annotations

import math
import statistics
import time
from collections import deque
from typing import Callable, Optional


class NoiseFilter:
    def __init__(self, settings: dict, clock: Callable[[], float] = time.monotonic):
        self.settings = dict(settings)
        self.clock = clock
        self.window: deque = deque()                 # (our time, level)
        self._zone: Optional[str] = None
        self._calibrated: Optional[bool] = None
        self.unit = ""
        self.accepted = 0
        self.ignored = {"robot_noise": 0, "uncalibrated": 0, "disabled": 0, "bad": 0}

    # ---- settings ----
    @property
    def window_s(self) -> float:
        return float(self.settings.get("window_s", 45))

    @property
    def min_samples(self) -> int:
        return int(self.settings.get("min_samples", 6))

    def thresholds(self, calibrated: bool) -> Optional[tuple]:
        """(to_common_above, to_quiet_below) for a reading of this kind, or None if such readings are not used."""
        if calibrated:
            return float(self.settings.get("to_common_above_db", 55.0)), float(self.settings.get("to_quiet_below_db", 48.0))
        if str(self.settings.get("uncalibrated", "ignore")) != "raw":
            return None
        raw = self.settings.get("raw_thresholds") or {}
        return float(raw.get("to_common_above_db", -35.0)), float(raw.get("to_quiet_below_db", -45.0))

    # ---- readings in ----
    def add(self, reading) -> bool:
        """Take a NoiseReading (anything with overall, calibrated, robot_noise). True if it counted."""
        if not self.settings.get("enabled", True):
            self.ignored["disabled"] += 1
            return False
        level = reading.overall
        if not isinstance(level, (int, float)) or not math.isfinite(level):
            self.ignored["bad"] += 1
            return False
        if reading.robot_noise:
            self.ignored["robot_noise"] += 1
            return False
        if self.thresholds(bool(reading.calibrated)) is None:
            self.ignored["uncalibrated"] += 1
            return False
        if self._calibrated is not None and bool(reading.calibrated) != self._calibrated:
            self.window.clear()                      # dB and dBFS must never be mixed in one median
            self._zone = None
        self._calibrated, self.unit = bool(reading.calibrated), str(getattr(reading, "unit", "") or "")
        now = self.clock()
        self.window.append((now, float(level)))
        self.accepted += 1
        self._prune(now)
        self._decide()
        return True

    def _prune(self, now: float) -> None:
        while self.window and now - self.window[0][0] > self.window_s:
            self.window.popleft()
        if len(self.window) < self.min_samples:
            self._zone = None                        # too little to say anything: readings stopped, or just started

    def _decide(self) -> None:
        median = self.median
        limits = self.thresholds(bool(self._calibrated))
        if median is None or limits is None:
            return
        common_above, quiet_below = limits
        if median > common_above:
            self._zone = "common"
        elif median < quiet_below:
            self._zone = "quiet"

    # ---- answers out ----
    @property
    def median(self) -> Optional[float]:
        self._prune(self.clock())
        if len(self.window) < self.min_samples:
            return None
        return round(statistics.median(level for _, level in self.window), 2)

    @property
    def zone(self) -> Optional[str]:
        """"common", "quiet", or None (no answer: the map zone stands)."""
        self._prune(self.clock())
        return self._zone

    def info(self) -> dict:
        """What the operator page shows."""
        return {"median": self.median, "samples": len(self.window), "zone": self.zone, "calibrated": self._calibrated,
                "unit": self.unit, "accepted": self.accepted, "ignored": dict(self.ignored)}


def level_db(rms: float, reference_rms: float = 1.0, offset_db: float = 0.0) -> float:
    """Uncalibrated level in dB for an RMS of 16-bit samples: 20*log10(rms/reference) + offset."""
    return 20.0 * math.log10(max(rms, 1e-9) / reference_rms) + offset_db


class NoiseMeter:
    def __init__(self, bus, audio, interval_s: float = 10.0):
        self.bus, self.audio, self.interval_s = bus, audio, interval_s

    async def run(self) -> None:
        raise NotImplementedError("measuring with the robot's own microphone is not implemented; the base's microphones come through the link")
