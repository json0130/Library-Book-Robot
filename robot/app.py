"""Builds the robot: drivers -> actuators, core logic on the bus, and the study log.

Event flow (see docs/architecture.md):
    ContextChanged   -> derive Context -> StyleVector -> StyleChanged, actuators restyled
    EmotionDetected  -> remembered (if confident enough) for expression blending
    ReplySentence    -> blend(user emotion, reply tag) -> ExpressionRequested -> face
    every event      -> session log, with the context and style in force
The dialogue state machine subscribes to the same bus (core/dialogue.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from robot.actuation import Actuators
from robot.actuation.face import FaceDisplay
from robot.actuation.lights import Lights
from robot.actuation.navigation import Navigation
from robot.actuation.neck import Neck
from robot.actuation.printer import Printer
from robot.actuation.storage_ring import StorageRing
from robot.actuation.voice import Voice
from robot.bus import Bus
from robot.config import Config
from robot.core.context import Context, NoiseThresholds, derive_context, resolve_area
from robot.core.dialogue import Dialogue
from robot.core.expression import blend
from robot.core.session_log import SessionLog
from robot.core.style import StylePolicy, StyleVector
from robot.drivers import DriverNotImplemented
from robot.events import (ContextChanged, EmotionDetected, ExpressionRequested, ReplySentence,
                          StyleChanged)
from robot.perception.book_scanner import BookScanner
from robot.services.books import BookFinder, Catalog

log = logging.getLogger("robot.app")


@dataclass
class Drivers:
    esp32: object
    audio: object
    camera: object
    scanner_camera: object = None       # camera on the scan pad; None means the main camera


def fake_drivers() -> Drivers:
    from robot.drivers.fake import FakeAudio, FakeCamera, FakeEsp32
    return Drivers(FakeEsp32(), FakeAudio(), FakeCamera())


def real_drivers(cfg: Config) -> Drivers:
    """The hardware drivers. Raises DriverNotImplemented naming every driver that is missing."""
    from robot.drivers.audio import Audio
    from robot.drivers.camera import Camera
    from robot.drivers.esp32 import Esp32
    dev = cfg.robot.get("devices", {})
    made, missing = {}, []
    for name, make in (("esp32", lambda: Esp32(dev.get("esp32_port", "/dev/ttyUSB0"), dev.get("esp32_baud", 115200))),
                       ("audio", lambda: Audio(dev.get("speaker"), dev.get("mic"))),
                       ("camera", lambda: Camera(int(dev.get("camera_index", 0))))):
        try:
            made[name] = make()
        except DriverNotImplemented as exc:
            missing.append(exc)
    if missing:
        for driver in made.values():             # release what did open (e.g. the camera)
            close = getattr(driver, "close", None)
            if close:
                close()
        raise DriverNotImplemented(", ".join(e.name for e in missing), missing=missing)
    scan_index = int(cfg.books["scanner_camera_index"])
    if scan_index != int(dev.get("camera_index", 0)):       # a separate camera on the scan pad
        made["scanner_camera"] = Camera(scan_index)
    return Drivers(**made)


@dataclass
class App:
    cfg: Config
    bus: Bus
    actuators: Actuators
    policy: StylePolicy
    thresholds: NoiseThresholds
    session_log: Optional[SessionLog] = None
    fixed: bool = False
    context: Context = field(default_factory=lambda: Context("common"))
    style: StyleVector = field(default_factory=StyleVector)
    user_emotion: Optional[tuple] = None          # (emotion, confidence 0..1)
    dialogue: Optional[Dialogue] = None
    finder: Optional[BookFinder] = None           # catalogue + online fallback
    scanner_camera: object = None                 # camera pointed at the scan pad
    scanner: Optional[BookScanner] = None

    def __post_init__(self) -> None:
        default_zone = self.cfg.zones.get("default_zone", "common")
        self.context = derive_context(default_zone, 1, None, self.thresholds, default_zone)
        self.style = self.policy.style_for(self.context, self.fixed)
        self.dialogue = Dialogue(self.actuators, lambda: self.style, self.bus.publish, self.finder)
        # order matters: log first, then update context/style, then the dialogue reacts
        self.bus.subscribe(object, self._log)
        self.bus.subscribe(ContextChanged, self._on_context)
        self.bus.subscribe(EmotionDetected, self._on_emotion)
        self.bus.subscribe(ReplySentence, self._on_reply)
        self.bus.subscribe(ExpressionRequested, self._on_expression)
        self.dialogue.attach(self.bus)
        if self.scanner_camera is not None and self.finder is not None:
            books = self.cfg.books
            # subscribed after the dialogue, so the dialogue is already "scanning" when the scan starts
            self.scanner = BookScanner(self.bus, self.scanner_camera, self.finder,
                                       timeout_s=float(books["scan_timeout_s"]))

    # ---- handlers ----
    def _log(self, event) -> None:
        if self.session_log is not None:
            self.session_log.write(event, self.context, self.style, fixed=self.fixed)

    async def _restyle(self) -> None:
        style = self.policy.style_for(self.context, self.fixed)
        if style != self.style:
            self.style = style
            self.actuators.apply_style(style)
            await self.bus.publish(StyleChanged(style, self.context))
            await self.bus.publish(ExpressionRequested(style.eye_expression))

    async def _on_context(self, event: ContextChanged) -> None:
        zone = resolve_area(event.zone, self.cfg.zones)
        self.context = derive_context(zone, event.group_size, event.noise_db, self.thresholds,
                                      self.cfg.zones.get("default_zone", "common"))
        await self._restyle()

    async def set_fixed(self, on: bool) -> None:
        self.fixed = bool(on)
        await self._restyle()

    def _on_emotion(self, event: EmotionDetected) -> None:
        threshold = self.cfg.robot.get("thresholds", {}).get("emotion_min_confidence", 0.5)
        self.user_emotion = (event.emotion, event.confidence) if event.confidence >= threshold else None

    async def _on_reply(self, event: ReplySentence) -> None:
        user, conf = self.user_emotion or (None, 0.0)
        await self.bus.publish(ExpressionRequested(blend(user, conf, event.emotion_tag)))

    def _on_expression(self, event: ExpressionRequested) -> None:
        self.actuators.face.show_emotion(event.emotion, event.hold_s)


def build_finder(cfg: Config) -> BookFinder:
    """The catalogue (data/catalog.csv) with the optional Open Library fallback.
    Raises books.CatalogError with a clear message if the catalogue file is bad."""
    books = cfg.books
    catalog = Catalog.load(cfg.path(books["catalog_path"]))
    return BookFinder(catalog, online_fallback=bool(books["online_fallback"]),
                      cache_dir=cfg.path(books["cache_dir"]))


def build_app(cfg: Config, drivers: Drivers, face: Optional[FaceDisplay] = None, speech=None,
              log_sessions: bool = True, fixed: bool = False, finder: Optional[BookFinder] = None) -> App:
    """Wire drivers and services into an App. speech: services.speech.Speech, or None to only log.
    finder: the book lookup; built from config/robot.yaml `books:` if not given."""
    display = cfg.robot.get("display", {})
    face = face or FaceDisplay(display.get("host", "127.0.0.1"), int(display.get("port", 8765)))
    voice_cfg = cfg.robot.get("voice", {})
    actuators = Actuators(
        voice=Voice(drivers.audio, speech.synthesize if speech else None, face,
                    float(voice_cfg.get("whisper_volume", 0.35))),
        face=face, lights=Lights(drivers.esp32), neck=Neck(drivers.esp32),
        ring=StorageRing(drivers.esp32), printer=Printer(drivers.esp32), nav=Navigation(drivers.esp32))
    session_log = None
    if log_sessions:
        session_log = SessionLog(cfg.path(cfg.robot.get("logging", {}).get("session_dir", "data/logs")))
    return App(cfg=cfg, bus=Bus(), actuators=actuators, policy=StylePolicy(cfg.styles),
               thresholds=NoiseThresholds.from_zones(cfg.zones), session_log=session_log, fixed=fixed,
               finder=finder or build_finder(cfg), scanner_camera=drivers.scanner_camera or drivers.camera)
