"""Film mode: what the robot did and when, for editing the competition video (tools/video/, docs/video_shot_list.md).

Nothing here runs unless the simulator is started with `--film TAKE` (the event log) or `--record PATH` (the camera video);
without those flags the robot behaves exactly as before.

    FilmLog        subscribes to the bus and writes recordings/<take>/events.jsonl: one JSON object per line,
                   {"t": time.time(), "type": ..., ...}. The video editor lines clips up on these times
                   (storyboard "event:rfid_read-2s") and captions every word Stuart says from the "reply" lines.
    FrameRecorder  writes the camera frames the camera feed already draws (faces boxed) to an mp4 at a steady frame rate,
                   so the webcam is not opened twice.

Event types: person_detected, greeting, speech_in, reply (text, channel speech|screen), speak (a sentence as it starts playing, with
its length: the captions' timing), screen_text (what a quiet zone shows
instead of saying it), rfid_read (title: the book on the pad was identified; the prototype reads it by camera today, RFID in the
final design), zone (quiet|common, and what decided it), goal_sent, goal_reached, goal_failed, unload, sound_level (dB or dBFS,
from the base), state (each state entered), start and stop.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from robot.events import (BookIdentified, NavArrived, NavFailed, NavigationRequested, NoiseReading, PersonEngaged,
                          ReplySentence, ReturningProgress, ScreenUpdate, StateEntered, UserChoice, UserUtterance)

log = logging.getLogger("robot.film")

ROOT = Path(__file__).resolve().parent.parent
RECORDINGS = ROOT / "recordings"


def take_dir(take: str, root: Path = RECORDINGS) -> Path:
    """recordings/<take>/ (made if missing). A take name is a plain word: letters, digits, - and _."""
    if not take or not all(c.isalnum() or c in "-_" for c in take):
        raise ValueError(f"take name {take!r}: use letters, digits, - and _ only")
    d = root / take
    d.mkdir(parents=True, exist_ok=True)
    return d


class FilmLog:
    """Appends one line per interesting event to `path`. get_zone / get_channels read the zone and the style's channels in force,
    so a reply is marked as spoken or only shown. Thread-safe (the noise and the camera live on other threads)."""

    SOUND_EVERY_S = 0.5                       # sound readings arrive about twice a second; keep at most this often

    def __init__(self, path: Path, get_zone: Callable[[], str] = lambda: "", get_channels: Callable[[], tuple] = lambda: (),
                 get_zone_source: Callable[[], str] = lambda: "", clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.get_zone, self.get_channels, self.get_zone_source, self.clock = get_zone, get_channels, get_zone_source, clock
        self._lock = threading.Lock()
        self._file = self.path.open("a", encoding="utf-8")
        self._zone: Optional[str] = None
        self._last_sound = -1e9
        self._last_screen = ""
        self._last_state = ""

    # ---- writing ----
    def write(self, kind: str, **fields) -> dict:
        row = {"t": round(self.clock(), 3), "type": kind, **{k: v for k, v in fields.items() if v is not None}}
        with self._lock:
            if self._file.closed:
                return row
            self._file.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._file.flush()
        return row

    def start(self, **meta) -> None:
        self.write("start", **meta)
        self.check_zone()

    def close(self) -> None:
        self.write("stop")
        with self._lock:
            self._file.close()

    def check_zone(self) -> None:
        """Log the zone whenever the one in force changes (whatever changed it: the map, the noise, the operator, --zone)."""
        zone = self.get_zone()
        if zone and zone != self._zone:
            self._zone = zone
            self.write("zone", zone=zone, source=self.get_zone_source() or None)

    # ---- the bus ----
    def attach(self, bus) -> None:
        bus.subscribe(object, self.on_event, first=True)              # see each event when it happens, before anyone handles it

    def on_speak(self, text: str, seconds: float) -> None:
        """Voice.on_sentence: a sentence starts playing through the speaker now and lasts `seconds` (the captions use this)."""
        self.write("speak", text=text, seconds=round(seconds, 2), zone=self.get_zone())

    def on_event(self, event) -> None:
        try:
            self._on_event(event)
        except Exception:                      # the film log must never disturb the robot
            log.exception("film log: could not write %s", type(event).__name__)

    def _on_event(self, event) -> None:
        if isinstance(event, PersonEngaged):
            self.write("person_detected", persons=list(event.person_ids) or None, source=event.source)
        elif isinstance(event, StateEntered):
            leaf = event.state_path.rsplit(".", 1)[-1]
            self.write("state", state=event.state_path, zone=event.zone)
            if leaf == "GREET" and self._last_state != event.state_path:      # the state is re-announced while in it: once
                self.write("greeting", zone=event.zone, text=event.text or None)
            self._last_state = event.state_path
        elif isinstance(event, UserUtterance):
            self.write("speech_in", text=event.text, source=event.source)
        elif isinstance(event, UserChoice):
            self.write("choice", choice=event.choice, source=event.source)
        elif isinstance(event, ReplySentence):
            spoken = "speech" in tuple(self.get_channels() or ())
            self.write("reply", text=event.text, channel="speech" if spoken else "screen", zone=self.get_zone())
        elif isinstance(event, ScreenUpdate):
            self._on_screen(event)
        elif isinstance(event, BookIdentified):
            self.write("rfid_read", title=event.title, isbn=event.isbn, section=event.section)
        elif isinstance(event, NavigationRequested):
            self.write("goal_sent", x=round(event.x, 2), y=round(event.y, 2), title=event.title or None,
                       section=event.section, shelf=event.shelf)
        elif isinstance(event, NavArrived):
            self.write("goal_reached", goal=event.goal_id)
        elif isinstance(event, NavFailed):
            self.write("goal_failed", goal=event.goal_id, reason=event.reason)
        elif isinstance(event, ReturningProgress):
            if event.phase == "unloading":
                self.write("unload", section=event.section, shelf=event.shelf, books_left=event.books_left)
            elif event.phase == "driving":
                self.write("returning", section=event.section, books_left=event.books_left)
        elif isinstance(event, NoiseReading):
            now = self.clock()
            if now - self._last_sound >= self.SOUND_EVERY_S:
                self._last_sound = now
                self.write("sound_level", db=round(event.overall, 1), unit=event.unit, calibrated=event.calibrated,
                           robot_noise=event.robot_noise or None)
        self.check_zone()

    def _on_screen(self, event: ScreenUpdate) -> None:
        """In a quiet zone the words are read on the screen, not heard: log what it shows (once per new text)."""
        if event.zone != "quiet":
            return
        payload = event.payload or {}
        text = payload.get("message") or payload.get("caption") or event.text or ""
        if text and text != self._last_screen:
            self._last_screen = text
            self.write("screen_text", text=text, view=event.view)


class FrameRecorder:
    """Writes frames (BGR numpy arrays) to an mp4 at a steady `fps`: a frame is repeated or dropped so that the video's time
    matches the wall clock, whatever rate the frames come at. The size is fixed by the first frame. meta.json-style start time
    in `started` (time.time()). Thread-safe; close() finishes the file."""

    def __init__(self, path, fps: float = 15.0, clock: Callable[[], float] = time.time):
        self.path, self.fps, self.clock = Path(path), float(fps), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = None
        self._size = None
        self._lock = threading.Lock()
        self.started: Optional[float] = None
        self.frames = 0

    def _open(self, frame) -> None:
        import cv2
        h, w = frame.shape[:2]
        self._size = (w, h)
        for fourcc in ("avc1", "mp4v"):                      # H.264 if this OpenCV has it, else MPEG-4 part 2 (the editor re-encodes)
            writer = cv2.VideoWriter(str(self.path), cv2.VideoWriter_fourcc(*fourcc), self.fps, self._size)
            if writer.isOpened():
                self._writer = writer
                log.info("recording the camera to %s (%s, %dx%d, %g fps)", self.path, fourcc, w, h, self.fps)
                return
            writer.release()
        raise RuntimeError(f"OpenCV cannot write an mp4 to {self.path}")

    def write(self, frame) -> None:
        """Add a frame seen now. Called from the camera feed's thread."""
        if frame is None:
            return
        with self._lock:
            now = self.clock()
            if self._writer is None:
                self._open(frame)
                self.started = now
            if (frame.shape[1], frame.shape[0]) != self._size:
                import cv2
                frame = cv2.resize(frame, self._size)
            due = int((now - self.started) * self.fps) + 1         # frames there should be by now
            while self.frames < due:
                self._writer.write(frame)
                self.frames += 1

    def close(self) -> None:
        """Finish the mp4 and write <path>.json with when its first frame was taken, so the editor can line it up with the
        events (tools/video/edit.py: "event:rfid_read-2s")."""
        with self._lock:
            if self._writer is not None:
                self._writer.release()
                self._writer = None
                meta = {"file": self.path.name, "started": self.started, "fps": self.fps, "frames": self.frames,
                        "clock": "time.time()"}
                self.path.with_suffix(self.path.suffix + ".json").write_text(json.dumps(meta, indent=2))
