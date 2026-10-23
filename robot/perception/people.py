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

EngagementDetector turns the same detections into the events the state machine runs on:

    PersonEngaged       a face at least `engagement.min_face_px` tall (close, about 1.5 m) for `engagement.dwell_s`
    PersonNearbyBusy    a face for that long that is never big enough (far away, or not coming closer): near, not engaging
    PersonLeft          no face for `engagement.left_after_s`

The server's face detector only finds faces that look at the camera, so "a face" is a frontal face. A person who
turns away simply stops being seen, and they are only reported gone after `left_after_s`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from robot.events import ContextChanged, PersonEngaged, PersonLeft, PersonNearbyBusy

log = logging.getLogger("robot.people")


def names(ids) -> str:
    return " and ".join(f"person {i}" for i in ids) if len(ids) <= 2 else \
        ", ".join(f"person {i}" for i in ids[:-1]) + f" and person {ids[-1]}"


class EngagementDetector:
    """update(detections) is called once per camera sample and returns the events that sample caused, as
    [("engaged", person_ids), ("busy", person_ids), ("left", ())]. Each is reported once per arrival."""

    def __init__(self, min_face_px: float = 60, dwell_s: float = 1.5, left_after_s: float = 5.0, clock=time.monotonic,
                 require_close: bool = True):
        """require_close False: any face engages (the closeness check is not in use yet), after dwell_s."""
        self.min_face_px, self.dwell_s, self.left_after_s, self.clock = min_face_px, dwell_s, left_after_s, clock
        self.require_close = require_close
        self.state = "absent"                       # absent | nearby | engaged
        self._near_since: Optional[float] = None    # a face that is big enough has been in view since
        self._far_since: Optional[float] = None     # a face that is too small has been in view since
        self._last_seen: Optional[float] = None
        self._last_update: Optional[float] = None   # when update() last ran

    @classmethod
    def from_config(cls, settings: dict, clock=time.monotonic) -> "EngagementDetector":
        """From config/robot.yaml `engagement:` (keys the detector does not use, like leave_grace_s, are skipped)."""
        keys = ("min_face_px", "dwell_s", "left_after_s", "require_close")
        return cls(clock=clock, **{k: settings[k] for k in keys if k in settings})

    STALL_S = 3.0               # a gap this long between samples is the robot being busy, not the person being gone

    @staticmethod
    def size(detection: dict) -> int:
        top, _, bottom, _ = detection["box"]
        return bottom - top

    def update(self, detections: list) -> list:
        now = self.clock()
        events = []
        if self._last_update is not None and now - self._last_update > self.STALL_S and self._last_seen is not None:
            self._last_seen += now - self._last_update      # the robot was busy (speaking...), not nobody there
        self._last_update = now
        big = [d for d in detections if not self.require_close or self.size(d) >= self.min_face_px]
        if detections:
            self._last_seen = now
        if big:
            self._far_since = None
            self._near_since = self._near_since if self._near_since is not None else now
            if self.state != "engaged" and now - self._near_since >= self.dwell_s:
                self.state = "engaged"
                events.append(("engaged", tuple(sorted({d["person"] for d in big if d.get("person")}))))
        elif detections:                              # faces, but none close enough
            self._near_since = None
            self._far_since = self._far_since if self._far_since is not None else now
            if self.state == "absent" and now - self._far_since >= self.dwell_s:
                self.state = "nearby"
                events.append(("busy", tuple(sorted({d["person"] for d in detections if d.get("person")}))))
        else:                                         # nobody in view
            self._near_since = self._far_since = None
            if self.state != "absent" and self._last_seen is not None and now - self._last_seen >= self.left_after_s:
                self.state = "absent"
                events.append(("left", ()))
        return events


EVENT_FOR = {"engaged": PersonEngaged, "busy": PersonNearbyBusy}


class PeopleCounter:
    def __init__(self, bus, camera, vision, map_zone: str = "common", interval_s: float = 1.0,
                 arrive_frames: int = 2, leave_after_s: float = 8.0,
                 on_change: Optional[Callable[[int, tuple], Awaitable]] = None, clock=time.monotonic,
                 identify: Optional[Callable] = None, engagement: Optional[EngagementDetector] = None):
        self.bus, self.camera, self.vision = bus, camera, vision
        self.map_zone = map_zone
        self.interval_s = interval_s
        self.arrive_frames = max(1, int(arrive_frames))
        self.leave_after_s = leave_after_s
        self.on_change = on_change or self._publish
        self.clock = clock
        self.identify = identify
        self.engagement = engagement                # optional: also report PersonEngaged / PersonNearbyBusy / PersonLeft
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
        if self.engagement is not None:
            for kind, person_ids in self.engagement.update(detections):
                log.info("[camera] %s%s", {"engaged": "someone is engaging", "busy": "someone is near but busy",
                                           "left": "they have left"}[kind],
                         f" ({', '.join(f'person {p}' for p in person_ids)})" if person_ids else "")
                await self.bus.publish(PersonLeft() if kind == "left" else EVENT_FOR[kind](person_ids))
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
