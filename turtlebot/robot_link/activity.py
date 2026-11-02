"""Is the robot making noise itself? Speaking (the Jetson's voice, /robot_speaking) or moving (the wheels, /odom).

A microphone reading taken then says nothing about the room. The same rule is used by the API (`robot_noise` in /api/state),
the noise logger (no row in the CSV) and the noise map (no painted circle), so they agree.

A reading covers the half second before it arrives (`chunk_s`), so a reading counts as the robot's own if there was activity
at any time in `chunk_s + grace_s` before it arrived (1 s by default: the half second measured, plus 0.5 s afterwards).
"""

import math
import threading
import time
from typing import Callable, Optional


class RobotActivity:
    def __init__(self, grace_s: float = 0.5, chunk_s: float = 0.5, speaking_stale_s: float = 30.0,
                 moving_mps: float = 0.02, turning_rps: float = 0.05, odom_max_age_s: float = 1.0,
                 clock: Callable[[], float] = time.time):
        self.grace_s, self.chunk_s, self.speaking_stale_s = grace_s, chunk_s, speaking_stale_s
        self.moving_mps, self.turning_rps, self.odom_max_age_s = moving_mps, turning_rps, odom_max_age_s
        self.clock = clock
        self.lock = threading.Lock()
        self._speaking = False
        self._speaking_since = 0.0
        self._last_active: Optional[float] = None       # when activity was last seen to end or go on
        self._odom_at: Optional[float] = None
        self._moving = False

    # ---- inputs ----
    def set_speaking(self, speaking: bool, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        with self.lock:
            if speaking:
                if not self._speaking:
                    self._speaking_since = now
                self._speaking = True
                self._last_active = now
            elif self._speaking:
                self._speaking = False
                self._last_active = now

    def on_odom(self, vx: float, wz: float, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        moving = (math.isfinite(vx) and abs(vx) > self.moving_mps) or (math.isfinite(wz) and abs(wz) > self.turning_rps)
        with self.lock:
            self._odom_at, self._moving = now, moving
            if moving:
                self._last_active = now

    # ---- answers ----
    def speaking(self, now: Optional[float] = None) -> bool:
        """True while the flag is set. A flag nobody has cleared for speaking_stale_s (the Jetson went away) lapses."""
        now = self.clock() if now is None else now
        with self.lock:
            if self._speaking and now - self._speaking_since > self.speaking_stale_s:
                self._speaking = False
                self._last_active = now
            return self._speaking

    def moving(self, now: Optional[float] = None) -> bool:
        now = self.clock() if now is None else now
        with self.lock:
            return bool(self._moving and self._odom_at is not None and now - self._odom_at <= self.odom_max_age_s)

    def busy(self, now: Optional[float] = None, lookback_s: float = 0.0) -> bool:
        """Speaking or moving now, or was within the last lookback_s seconds."""
        now = self.clock() if now is None else now
        if self.speaking(now) or self.moving(now):
            return True
        with self.lock:
            return self._last_active is not None and now - self._last_active <= lookback_s

    def reading_is_robot_noise(self, now: Optional[float] = None) -> bool:
        """For a microphone reading that arrives now."""
        return self.busy(now, self.chunk_s + self.grace_s)
