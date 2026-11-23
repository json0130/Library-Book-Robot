"""Builds the robot: drivers -> actuators, core logic on the bus, and the study log.

Event flow (see docs/architecture.md):
    ContextChanged   -> derive Context -> StyleVector -> StyleChanged, actuators restyled
    EmotionDetected  -> remembered (if confident enough) for expression blending
    ReplySentence    -> blend(user emotion, reply tag) -> ExpressionRequested -> face
    every event      -> session log, with the context and style in force
    StateTransition / ScreenUpdate -> the display page and the operator page (when the display server is running)
The dialogue state machine (core/dialogue.py, a table on top of core/fsm.py) subscribes to the same bus; App.run_ticker()
drives its timers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Optional

from robot.actuation import Actuators
from robot.actuation.face import FaceDisplay
from robot.actuation.lights import Lights
from robot.actuation.navigation import Navigation, TurtleBotNavigation
from robot.actuation.neck import Neck
from robot.actuation.pad import Pad
from robot.actuation.printer import Printer
from robot.actuation.storage_ring import DEFAULT_SLOTS, StorageRing
from robot.actuation.voice import Voice
from robot.bus import Bus
from robot.config import ROOT, Config
from robot.core.context import Context, NoiseThresholds, derive_context, resolve_area
from robot.core.dialogue import Dialogue, Settings
from robot.core.screen import VIEWS, ScreenComposer
from robot.core.expression import blend
from robot.core.session_log import SessionLog
from robot.core.zones import ZoneResolver, section_zones
from robot.core.storage import Storage
from robot.core.style import StylePolicy, StyleVector
from robot.drivers import DriverNotImplemented
from robot.events import (ContextChanged, EmotionDetected, ExpressionRequested, NavigationRequested, NavProgress,
                          NoiseReading, PoseUpdated, ReplySentence, ScreenUpdate, StyleChanged)
from robot.perception.book_reader import BookReader, build_reader
from robot.services.book_llm import BookAssistant
from robot.services.book_graph import BookGraph
from robot.services.books import BookFinder
from robot.services.llm import LLMClient

log = logging.getLogger("robot.app")

SAMPLED_EVENTS = (PoseUpdated, NoiseReading, NavProgress)      # arrive about twice a second: the study log keeps one in LOG_SAMPLE_S
LOG_SAMPLE_S = 5.0


@dataclass
class Drivers:
    esp32: object
    audio: object
    camera: object
    scanner_camera: object = None       # camera on the scan pad; None means the main camera


def fake_drivers() -> Drivers:
    from robot.drivers.fake import FakeAudio, FakeCamera, FakeEsp32
    return Drivers(FakeEsp32(), FakeAudio(), FakeCamera())


def sim_drivers(cfg: Config, use_camera: bool = False, speak: bool = False) -> Drivers:
    """Fake hardware for the simulator, except the webcam (use_camera) and the speaker (speak) when
    asked for. Raises OSError, RuntimeError or ValueError, with a readable message, if one is missing."""
    drivers = fake_drivers()
    dev = cfg.robot.get("devices", {})
    if use_camera:
        from robot.drivers.camera import Camera
        drivers.camera = Camera(int(cfg.books["scanner_camera_index"]), dev.get("camera_width"),
                                dev.get("camera_height"), background=True)
    if speak:
        from robot.drivers.audio import Audio
        drivers.audio = Audio(dev.get("speaker"), dev.get("mic"))
    return drivers


def real_drivers(cfg: Config) -> Drivers:
    """The hardware drivers. Raises DriverNotImplemented naming every driver that is missing."""
    from robot.drivers.audio import Audio
    from robot.drivers.camera import Camera
    from robot.drivers.esp32 import Esp32
    dev = cfg.robot.get("devices", {})
    size = (dev.get("camera_width"), dev.get("camera_height"))
    made, missing = {}, []
    for name, make in (("esp32", lambda: Esp32(dev.get("esp32_port", "/dev/ttyUSB0"), dev.get("esp32_baud", 115200))),
                       ("audio", lambda: Audio(dev.get("speaker"), dev.get("mic"))),
                       ("camera", lambda: Camera(int(dev.get("camera_index", 0)), *size, background=True))):
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
        made["scanner_camera"] = Camera(scan_index, *size, background=True)
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
    scanner: Optional[BookReader] = None          # the BookReader (camera scanner or RFID)
    assistant: object = None                      # services.book_llm.BookAssistant, or None (catalogue text only)
    storage: Optional[Storage] = None             # which book is in which slot of the storage ring
    plan: object = None                           # services.floorplan.FloorPlan for directions, or None
    screen: Optional[ScreenComposer] = None       # decides what the screen shows (core/screen.py)
    link: object = None                           # drivers.turtlebot_link.TurtleBotLink when navigation.backend is turtlebot, else None
    zones: Optional[ZoneResolver] = None          # pose + noise + frozen -> zone (core/zones.py); only with a link
    zone_source: str = "map"                      # what decided the zone now: map | noise | frozen | forced
    forced_zone: Optional[str] = None             # film mode (`sim --zone`): this zone for the whole take, whatever the map, noise or operator say
    listener: object = None                       # perception.listener.Listener (push-to-talk microphone), or None
    heard: dict = field(default_factory=lambda: {"mic": "idle", "text": ""})   # what the operator page shows about the microphone
    counter: object = None                        # perception.people.PeopleCounter when faces are watched, else None
    active: bool = True                           # False = stopped (the operator page's Start/Stop): the camera shows, but no face makes
                                                  # it say hello and no book is taken until Start

    def __post_init__(self) -> None:
        default_zone = self.cfg.zones.get("default_zone", "common")
        self.context = derive_context(default_zone, 1, None, self.thresholds, default_zone)
        self.style = self.policy.style_for(self.context, self.fixed)
        cooldown = float((self.cfg.robot.get("dialogue") or {}).get("greet_cooldown_s", 120))
        self.dialogue = Dialogue(self.actuators, lambda: self.style, self.bus.publish, self.finder, self.assistant,
                                 greet_cooldown_s=cooldown, storage=self.storage,
                                 settings=Settings.from_config(self.cfg), get_zone=lambda: self.context.zone,
                                 plan=self.plan)
        self.storage = self.dialogue.storage
        self.screen = ScreenComposer(self.bus.publish, self.finder, self.plan, self.cfg.screen, lambda: self.style,
                                     lambda: self.dialogue.state)
        # order matters: log first, then update context/style, then the dialogue reacts
        self.bus.subscribe(object, self._log)
        self.bus.subscribe(ContextChanged, self._on_context)
        self.bus.subscribe(EmotionDetected, self._on_emotion)
        self.bus.subscribe(ReplySentence, self._on_reply)
        self.bus.subscribe(ExpressionRequested, self._on_expression)
        self.bus.subscribe(NavigationRequested, self._on_navigation)
        self.bus.subscribe(PoseUpdated, self._on_pose)
        self.bus.subscribe(NoiseReading, self._on_noise)
        self.bus.subscribe(ScreenUpdate, self._on_screen)
        self.dialogue.attach(self.bus)
        self.screen.attach(self.bus)
        self._log_sampled: dict = {}
        if self.link is not None and self.zones is None:
            turtlebot = self.cfg.turtlebot
            self.zones = ZoneResolver(self.cfg, self.plan, section_zones(getattr(self.finder, "graph", None)),
                                      lambda: self.dialogue.fsm.is_in("ENGAGED"),
                                      level=None if turtlebot.get("floor") is None else int(turtlebot["floor"]))
        if self.finder is not None:
            books = self.cfg.books
            # subscribed after the dialogue, so the dialogue is already "scanning" when the scan starts
            self.scanner = build_reader(books.get("reader", "camera"), self.bus, self.scanner_camera, self.finder,
                                        timeout_s=float(books["scan_timeout_s"]), rfid=books.get("rfid"), path=self.cfg.path)

    # ---- handlers ----
    def _log(self, event) -> None:
        if isinstance(event, SAMPLED_EVENTS):
            now = time.monotonic()
            if now - self._log_sampled.get(type(event), -1e9) < LOG_SAMPLE_S:
                return
            self._log_sampled[type(event)] = now
        if self.session_log is not None:
            self.session_log.write(event, self.context, self.style, fixed=self.fixed)

    async def _restyle(self) -> None:
        style = self.policy.style_for(self.context, self.fixed)
        if style != self.style:
            self.style = style
            self.actuators.apply_style(style)
            await self.bus.publish(StyleChanged(style, self.context))
            await self.bus.publish(ExpressionRequested(style.eye_expression))
        self.show_state()

    def state_info(self) -> dict:
        """What the operator page shows: where the machine is, the zone, the mode and the storage."""
        d = self.dialogue
        return {"state": d.state, "active": self.active, "zone": self.context.zone, "social": self.context.social, "fixed": self.fixed,
                "stored": len(d.storage), "slots": d.storage.size,
                "books": [{"slot": b.slot, "title": b.title, "shelf": b.shelf, "section": b.section}
                          for b in d.storage.stored()], "style": self.style.summary(),
                "states": list(d.fsm.spec.leaves()), "tree": d.state_tree(), "leaf": d.leaf,
                "next": d.next_actions(), "view": self.screen.current.view if self.screen and self.screen.current else "face",
                "views": list(VIEWS), "guide": getattr(self, "guide_card", None), "link": self.link_info(),
                "operator_note": getattr(self, "operator_note", None),
                "mic": self.heard["mic"] if self.listener is not None else "none", "heard": self.heard["text"]}

    def mic_status(self, kind: str, text: str = "") -> None:
        """The listener says what it is doing (listening, hearing, transcribing, idle): show it on the operator page."""
        self.heard = {"mic": kind, "text": text or (self.heard["text"] if kind in ("hearing", "transcribing") else "")}
        self.show_state()

    def link_info(self) -> dict:
        """The base link for the operator page: the link, the pose, the drive, the noise, the zone and what was skipped."""
        info = {"backend": "turtlebot" if self.link is not None else "sim", "returning": self.dialogue.returning_info()}
        if self.link is not None:
            info.update(self.link.info())
        if self.zones is not None:
            info["zone"] = self.zones.info()
            info["zone"]["source"] = self.zone_source
        return info

    def show_state(self) -> bool:
        return self.actuators.face.show_state(self.state_info())

    def _on_screen(self, event: ScreenUpdate) -> None:
        """The screen composer chose what to show: send it to the page, and tell the operator page where the machine is."""
        update = asdict(event)
        update["options"] = list(event.options)
        log.info("[screen] %s view=%s (%s, %s) %s", event.state_path, event.view, event.layout, event.zone,
                 describe_view(event.view, event.payload) or (repr(event.text) if event.text else ""))
        self.actuators.face.show_screen(update)
        self.show_state()

    async def run_ticker(self, interval_s: float = 0.2) -> None:
        """Drive the state machine's timers until cancelled."""
        while True:
            try:
                await self.dialogue.tick()
                await self.screen.tick()
                await self.refresh_zone()
            except Exception:                        # a failing action must not stop the clock
                log.exception("state machine tick failed")
            await asyncio.sleep(interval_s)

    async def _on_context(self, event: ContextChanged) -> None:
        zone = resolve_area(event.zone, self.cfg.zones)
        noise_db, shown_noise = event.noise_db, None
        if self.forced_zone is not None:                # film mode: the take's zone, never the map's or the noise's
            zone, noise_db, shown_noise, self.zone_source = self.forced_zone, None, event.noise_db, "forced"
        elif self.zones is not None:                    # with a base: the pose, the noise and the visit decide (core/zones.py)
            if event.zone_source is None:               # typed, the operator, the head count: the fallback map zone
                self.zones.set_typed(zone)
            decision = self.zones.decide()
            zone, self.zone_source = decision.zone, decision.source
            if event.zone_source is not None:           # already decided: the noise is the median, shown but not applied twice
                noise_db, shown_noise = None, event.noise_db
        self.context = derive_context(zone, event.group_size, noise_db, self.thresholds,
                                      self.cfg.zones.get("default_zone", "common"))
        if shown_noise is not None:
            self.context = replace(self.context, noise_db=shown_noise)
        await self._restyle()

    async def refresh_zone(self) -> None:
        """Re-decide the zone from the pose and the noise (and the visit: frozen while one lasts); announce a change."""
        if self.zones is None or self.forced_zone is not None:
            return
        decision = self.zones.decide()
        self.zone_source = decision.source
        if decision.zone != self.context.zone:
            log.info("[zone] %s (%s%s)", decision.zone, decision.source, f", {decision.area}" if decision.area else "")
            await self.bus.publish(ContextChanged(decision.zone, self.context.group_size, decision.noise_db,
                                                  zone_source=decision.source))

    async def force_zone(self, zone: str) -> None:
        """Film mode (`sim --zone quiet|common`): hold this zone for the whole run, over the sound map, the noise and the operator."""
        if zone not in ("quiet", "common"):
            raise ValueError(f"zone {zone!r}: quiet or common")
        self.forced_zone = zone
        await self.bus.publish(ContextChanged(zone, self.context.group_size))

    async def _on_pose(self, event: PoseUpdated) -> None:
        if self.zones is not None:
            self.zones.on_pose(event.x, event.y, event.localised)
            await self.refresh_zone()

    async def _on_noise(self, event: NoiseReading) -> None:
        if self.zones is not None and self.zones.on_noise(event):
            await self.refresh_zone()

    async def start_link(self) -> None:
        """Start talking to the base (a no-op without one). Call from the running event loop."""
        if self.link is not None:
            self.actuators.nav.set_speed_limit(self.style.speed_limit)      # the style in force from the start, not only after a change
            await self.link.start(self.bus)

    async def stop_link(self) -> None:
        if self.link is not None:
            await self.link.stop()

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

    def _on_navigation(self, event: NavigationRequested) -> None:
        self.actuators.nav.go_to_pose(event.x, event.y, event.yaw, event.floor)


def build_finder(cfg: Config) -> BookFinder:
    """The catalogue and the book graph built from the data files in config/robot.yaml (`books:`), with the
    optional Open Library fallback. Raises books.CatalogError with a clear message if a data file is bad."""
    books = cfg.books
    graph = BookGraph.from_files(cfg.path(books["catalog_path"]), cfg.path(books["topics_path"]),
                                 cfg.path(books["shelves_path"]), cfg.path(books["links_path"]),
                                 weights=books["similarity"])
    return BookFinder(graph.catalog, online_fallback=bool(books["online_fallback"]),
                      cache_dir=cfg.path(books["cache_dir"]), graph=graph)


def build_assistant(cfg: Config) -> BookAssistant:
    """The LLM helper for the book flow, talking to the AI server in config/robot.yaml.
    Uses ai_server.llm_timeout_s (short: a visitor is waiting), not the 300 s of the chat tool."""
    server = cfg.robot["ai_server"]
    return BookAssistant(LLMClient(cfg.ai_host, cfg.ai_port, float(server.get("llm_timeout_s", 20))))


def describe_view(view: str, p: dict) -> str:
    """One line about a view's content, for the terminal and the log."""
    if view == "book_card":
        return f"{p.get('title')!r}" + (f" [{p['badge']}]" if p.get("badge") else "") + \
            (f" level {p.get('level')} {p.get('section')} {p.get('shelf')}" if p.get("section") else "")
    if view == "book_list":
        return f"{p.get('heading')!r}: " + " | ".join(i["title"] for i in p.get("items", []))
    if view == "directions":
        return (f"{p.get('title')!r} level {p.get('level')}, about {p.get('distance_m')} m: "
                + " / ".join(p.get("steps", [])))
    if view == "options":
        return f"{p.get('prompt')!r}: " + " | ".join(o["label"] for o in p.get("options", []))
    if view == "status":
        return f"{p.get('heading')} to {p.get('destination')}, {p.get('books_left')} left"
    return repr(p.get("caption")) if p.get("caption") else ""


def build_plan(cfg: Config):
    """The floor plan from books.floorplan_path, or None (with a warning) if it is missing or wrong: the robot
    still works, only the directions view has no map."""
    from robot.services.floorplan import FloorPlan, FloorPlanError
    path = cfg.path(cfg.books.get("floorplan_path") or "data/floorplan.yaml")
    if not path.is_file():
        log.warning("no floor plan at %s: directions are shown without a map", path)
        return None
    try:
        return FloorPlan.load(path)
    except FloorPlanError as exc:
        log.warning("the floor plan is not usable (%s): directions are shown without a map", exc)
        return None


def build_storage(cfg: Config, persist: bool = True) -> Storage:
    """The storage ring's data: the slot count from config (else the ring's own) and, if `persist`, the file it is
    saved to (data/state/storage.json)."""
    settings = cfg.storage
    path = cfg.path(settings["path"]) if persist and settings.get("path") else None
    return Storage(int(settings.get("slots") or DEFAULT_SLOTS), path)


def build_link(cfg: Config, env: Optional[dict] = None, base_url: Optional[str] = None):
    """The link to the base from config/robot.yaml `turtlebot:` and the environment's ROBOT_LINK_TOKEN. Raises
    turtlebot_link.LinkConfigError (say what is missing) if there is no token."""
    from robot.drivers.turtlebot_link import LinkSettings, TurtleBotLink
    return TurtleBotLink(LinkSettings.from_config(cfg, env, base_url))


def build_app(cfg: Config, drivers: Drivers, face: Optional[FaceDisplay] = None, speech=None,
              log_sessions: bool = True, fixed: bool = False, finder: Optional[BookFinder] = None,
              assistant=None, persist_storage: bool = True, link=None) -> App:
    """Wire drivers and services into an App. speech: services.speech.Speech, or None to only log.
    finder: the book lookup; built from config/robot.yaml `books:` if not given.
    assistant: the LLM helper (build_assistant); None answers from the catalogue text alone.
    persist_storage: keep the stored books in data/state/storage.json (tests turn this off).
    link: the TurtleBotLink to drive with; made from the config when `navigation.backend` is turtlebot and none is given.
    The link is only built here: `await app.start_link()` starts it, from inside the event loop."""
    display = cfg.display
    face = face or FaceDisplay(display["host"], int(display["port"]), background=True)
    voice_cfg = cfg.robot.get("voice", {})
    plan = build_plan(cfg)
    if link is None and cfg.navigation["backend"] == "turtlebot":
        link = build_link(cfg)
    turtlebot = cfg.turtlebot
    nav = (TurtleBotNavigation(link, plan, float(turtlebot["max_goal_distance_m"]),
                               None if turtlebot.get("floor") is None else int(turtlebot["floor"]))
           if link is not None else Navigation(drivers.esp32))
    voice = Voice(drivers.audio, speech.synthesize if speech else None, face,
                  float(voice_cfg.get("whisper_volume", 0.35)),
                  ROOT / "data" / "tts_cache" if speech else None, speech.cache_tag if speech else "",
                  on_speaking=link.set_speaking if link is not None else None)
    actuators = Actuators(
        voice=voice, face=face, lights=Lights(drivers.esp32), neck=Neck(drivers.esp32),
        ring=StorageRing(drivers.esp32, slots=int(cfg.storage.get("slots") or DEFAULT_SLOTS)), printer=Printer(drivers.esp32),
        nav=nav, pad=Pad(drivers.esp32))
    session_log = None
    if log_sessions:
        session_log = SessionLog(cfg.path(cfg.robot.get("logging", {}).get("session_dir", "data/logs")))
    return App(cfg=cfg, bus=Bus(), actuators=actuators, policy=StylePolicy(cfg.styles),
               thresholds=NoiseThresholds.from_zones(cfg.zones), session_log=session_log, fixed=fixed,
               finder=finder or build_finder(cfg), scanner_camera=drivers.scanner_camera or drivers.camera,
               assistant=assistant, storage=build_storage(cfg, persist_storage), plan=plan, link=link)
