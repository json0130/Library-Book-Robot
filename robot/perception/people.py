"""Counts the people with the robot from camera frames and publishes ContextChanged.

Every `interval_s` one frame goes to the AI server's face detection (services.vision.Vision.detect).
What is reported is smoothed so one missed or spurious face doesn't flip alone/group, or make the robot
forget a visitor who looks down at a book.

With `identify` (perception.identity.PersonTracker) each face is also given a person number, and presence is
tracked per person:

    a person appears      confirmed after `arrive_frames` samples in a row
    a person is missing   gone once they have been missing for `leave_after_s`

so a visitor who leaves and comes back is the same person 1, and a new face is person 2. Without it only
the head count is smoothed:

    more faces than now   confirmed after `arrive_frames` samples in a row (the smallest count seen)
    fewer faces than now  confirmed once the lower count has lasted `leave_after_s`

When the confirmed people change, on_change(count, person_ids) is called (person_ids is () without
`identify`). The default publishes ContextChanged(map_zone, count, person_ids=...); the dialogue then greets
a person it hasn't greeted yet and resets when the count reaches 0. The newest raw detections, with a
"person" number each when known, stay in `detections` for the live view to draw.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from robot.events import ContextChanged

log = logging.getLogger("robot.people")


def names(ids) -> str:
    return " and ".join(f"person {i}" for i in ids) if len(ids) <= 2 else \
        ", ".join(f"person {i}" for i in ids[:-1]) + f" and person {ids[-1]}"


class PeopleCounter:
    def __init__(self, bus, camera, vision, map_zone: str = "common", interval_s: float = 1.0,
                 arrive_frames: int = 2, leave_after_s: float = 8.0,
                 on_change: Optional[Callable[[int, tuple], Awaitable]] = None, clock=time.monotonic,
                 identify: Optional[Callable] = None):
        self.bus, self.camera, self.vision = bus, camera, vision
        self.map_zone = map_zone
        self.interval_s = interval_s
        self.arrive_frames = max(1, int(arrive_frames))
        self.leave_after_s = leave_after_s
        self.on_change = on_change or self._publish
        self.clock = clock
        self.identify = identify
        self.count = 0                      # the confirmed head count
        self.person_ids: tuple = ()         # the confirmed people (with `identify`)
        self.detections: list[dict] = []    # raw detections of the newest frame ({"box": [t, r, b, l], ...})
        self.detected_at: Optional[float] = None
        self._more = 0                      # consecutive samples with more faces than `count`
        self._more_min = 0                  # ...and the fewest faces among them
        self._fewer_since: Optional[float] = None
        self._streak: dict[int, int] = {}           # per person: samples in a row seen, until confirmed
        self._missing_since: dict[int, float] = {}  # per confirmed person: when they dropped out of view
        self._last_error: Optional[str] = None

    async def _publish(self, count: int, person_ids: tuple = ()) -> None:
        await self.bus.publish(ContextChanged(self.map_zone, count, person_ids=person_ids))

    def _grab(self):
        """One frame -> (detections, person numbers or None). Blocking (camera, a server round trip,
        embeddings): run in a thread."""
        cam = self.camera
        frame = None
        if self.identify is not None and hasattr(cam, "peek") and hasattr(cam, "encode_jpeg"):
            frame = cam.peek()
            jpg = cam.encode_jpeg(frame)
        else:
            peek = getattr(cam, "peek_jpeg", None)
            jpg = peek() if peek else cam.read_jpeg()
        detections = self.vision.detect(jpg)
        people = self.identify(frame, detections) if frame is not None and detections else None
        return detections, people

    async def step(self) -> int:
        """Take one sample and return the confirmed count. Raises OSError/EOFError/ValueError if the
        camera or the server fails."""
        detections, people = await asyncio.get_running_loop().run_in_executor(None, self._grab)
        if people is not None:
            for d, person in zip(detections, people):
                if person is not None:
                    d["person"] = person
        self.detections, self.detected_at = detections, self.clock()
        if self.identify is not None:
            await self._update_people({p for p in (people or []) if p is not None})
        else:
            await self._update(len(detections))
        return self.count

    # ---- with person numbers ----
    async def _update_people(self, seen: set) -> None:
        now = self.clock()
        present = set(self.person_ids)
        changed = False
        for person in seen:
            self._missing_since.pop(person, None)
            if person in present:
                continue
            self._streak[person] = self._streak.get(person, 0) + 1
            if self._streak[person] >= self.arrive_frames:
                present.add(person)
                self._streak.pop(person)
                changed = True
        for person in list(self._streak):
            if person not in seen:                          # an unconfirmed face that didn't repeat
                del self._streak[person]
        for person in list(present - seen):
            since = self._missing_since.setdefault(person, now)
            if now - since >= self.leave_after_s:
                present.discard(person)
                del self._missing_since[person]
                changed = True
        if changed:
            await self._set(len(present), tuple(sorted(present)))

    # ---- head count only ----
    async def _update(self, seen: int) -> None:
        now = self.clock()
        if seen > self.count:
            self._fewer_since = None
            self._more_min = seen if self._more == 0 else min(self._more_min, seen)
            self._more += 1
            if self._more >= self.arrive_frames:
                await self._set(self._more_min)
            return
        self._more = 0
        if seen == self.count:
            self._fewer_since = None
            return
        if self._fewer_since is None:
            self._fewer_since = now
        if now - self._fewer_since >= self.leave_after_s:
            await self._set(seen)

    async def _set(self, count: int, person_ids: tuple = ()) -> None:
        self.count, self.person_ids = count, person_ids
        self._more, self._fewer_since = 0, None
        if person_ids:
            log.info("[camera] %s in view", names(person_ids))
        else:
            log.info("[camera] %s", f"{count} face(s) in view" if count else "nobody in view")
        await self.on_change(count, person_ids)

    async def run(self) -> None:
        """Sample forever. A camera or server failure is logged once and retried, not fatal."""
        while True:
            try:
                await self.step()
                self._last_error = None
            except (OSError, EOFError, ValueError) as exc:
                if str(exc) != self._last_error:
                    log.warning("[camera] face detection failed (%s); will keep trying", exc)
                    self._last_error = str(exc)
                self.detections = []
            await asyncio.sleep(self.interval_s)
