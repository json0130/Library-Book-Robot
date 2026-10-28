"""Keyboard simulator: drive the robot's core logic by typing. Hardware is fake unless asked for.

    python -m robot sim
    quiet alone | common group   set the map zone (or an area from zones.yaml) and alone/group
    noise 62 | noise off         a noise reading in dB (may override the zone) / clear it
    book                         place a book on the pad: scanned by the webcam with --camera, else a random
                                 sample book is shown to the fake camera
    book <isbn>                  fake camera only: place the book with this ISBN (catalogue book or any valid ISBN)
    book none                    fake camera only: place a book whose barcode can't be read (scan fails)
    scan <image path>            fake camera only: the real barcode decoder reads a photo of a book
    return | explain | recommend say that to the robot once it has offered
    guide                        ask the robot to guide you to the shelf it last mentioned (after return,
                                 recommend or where); the fake base just logs the NavigationRequested pose
    where <title or isbn>        ask where a book lives (the title is matched loosely: `where dune`)
    reload                       read data/catalog.csv, topics.yaml, shelves.csv and curated_links.csv again
    <anything else you type>     the visitor says it ("can you explain it?"); `say <text>` also works
    feel <emotion> [confidence]  the camera sees this emotion on the visitor (confidence 0..1)
    leave                        everyone walks away (also PersonLeft)
    fixed on | fixed off         baseline condition: one style everywhere
    Operator controls (the same as the /operator page of the display server; robot/operator.py):
    state                        where the state machine is, the zone and what is in storage
    goto <state>                 force a state (OFFER, SCAN, STORE, NAVIGATING...), filling in a sample book or two
                                 stored books where it needs them
    zone quiet | zone common     change the zone
    engage | busy                a person engages / is near but busy (PersonEngaged / PersonNearbyBusy)
    dropped                      the pad dropped the book into its slot (BookDropped)
    pick return|ask|recommend|more|guide|done|surprise   press that screen button
    pick book:<isbn> | similar:<isbn> | guide:<isbn> | section:<name> | topic:<name>   tap a book, a card button or a choice
    screen <view> [isbn | section]  show a screen view with sample data: face book_card book_list directions
                                 options status (`screen face` goes back to the face)
    reset | reset all            back to IDLE (`all` also empties the storage)
    wait <seconds> | wait idle   let the timers run (for scripted runs)
    help, quit

Options: --llm      the AI server's LLM words explain and recommend (else the catalogue text)
         --camera   scan with the real webcam, and show a live camera window
         --faces    count the people the webcam sees (AI server face detection): the first face makes the
                    robot say hello, and nobody in view for a few seconds ends the visit. Needs --camera
         --auto-scan  start a scan as soon as a book's barcode is held up to the camera: no need to type
                    `book` (the camera stands in for the pad's weight sensor). Needs --camera
         --speak    speak replies through the speaker (TTS from the AI server); the face display's mouth moves
         --live     all of them: webcam, faces, auto-scan, speaker, LLM
         --preview  also open a camera window on the robot (the operator page already shows the camera)
The `--live` flow: step in front of the camera (hello), hold a book's barcode up to it (or type `book`), then type
"can you explain it?" or "can you recommend something?". Replies are spoken only in styles with the speech
channel (a common area, the default); quiet zones print or use other channels instead. With --faces the
head count comes from the camera, so `common group`, `leave` and the like are overridden at the next change.

After each command the sim prints the style and the dialogue state. Expressions go to the face
display if it is running (python -m display.server), otherwise they are only printed.
Without --camera, --speak and --llm no hardware or AI server is needed.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import threading
from typing import Optional, TextIO

from robot.app import App, build_app, build_assistant, sim_drivers
from robot.services.warmup import Warmup
from robot.config import Config
from robot.core.expression import EMOTIONS, normalize
from robot.events import EmotionDetected, UserUtterance
from robot.operator import COMMANDS as OPERATOR_COMMANDS, GROUP_SIZES, Operator
from robot.services.books import CatalogError

CHOICES = ("return", "explain", "recommend")
COMMANDS = {"quit", "exit", "help", "?", "noise", "scan", "feel", "fixed", "guide", "where", "reload", "wait",
            *OPERATOR_COMMANDS, *CHOICES}
SETTLE_S = 120.0                                    # at the end of piped input, how long to let a return trip finish


class Sim:
    def __init__(self, app: App, out: TextIO = sys.stdout, rng: Optional[random.Random] = None):
        self.app = app
        self.out = out
        self.rng = rng or random.Random()
        self.op = Operator(app, self.say, self.rng)

    # the context lives in the Operator, which the /operator page shares
    zone = property(lambda self: self.op.zone, lambda self, v: setattr(self.op, "zone", v))
    group_size = property(lambda self: self.op.group_size, lambda self, v: setattr(self.op, "group_size", v))
    person_ids = property(lambda self: self.op.person_ids, lambda self, v: setattr(self.op, "person_ids", v))
    noise_db = property(lambda self: self.op.noise_db, lambda self, v: setattr(self.op, "noise_db", v))

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def status(self) -> None:
        c = self.app.context
        noise = f", noise {c.noise_db:g} dB" if c.noise_db is not None else ""
        self.say(f"  context: {c.zone} {c.social} (map {self.zone}, {c.group_size} {'person' if c.group_size == 1 else 'people'}{noise})"
                 + ("  [fixed mode]" if self.app.fixed else ""))
        self.say(f"  style:   {self.app.style.summary()}")
        stored = len(self.app.dialogue.storage)
        self.say(f"  dialogue: {self.app.dialogue.state}" + (f"   (storage: {stored} book{'s' * (stored != 1)})" if stored else ""))

    async def context_changed(self) -> None:
        await self.op.context_changed()

    async def people_changed(self, count: int, person_ids: tuple = ()) -> None:
        """The camera's confirmed people changed (PeopleCounter): same as typing the head count."""
        changed = (count, person_ids) != (self.group_size, self.person_ids)
        self.group_size, self.person_ids = count, person_ids
        await self.context_changed()
        if changed and count != getattr(self, "_shown_count", None):      # print the style only when the count changes
            self._shown_count = count
            self.status()

    async def place_book(self, args: list) -> bool:
        return await self.op.place_book(args)

    async def run_scan(self) -> None:
        await self.op.run_scan()

    async def wait(self, arg: str) -> None:
        """wait <seconds> or wait idle: let the state machine's timers run."""
        loop = asyncio.get_running_loop()
        until = loop.time() + (SETTLE_S if arg == "idle" else float(arg))
        while loop.time() < until and not (arg == "idle" and self.app.dialogue.leaf == "IDLE"):
            await asyncio.sleep(0.1)

    async def settle(self, limit_s: float = SETTLE_S) -> None:
        """At the end of piped input: let a goodbye and a trip putting books back finish (they run on timers)."""
        loop = asyncio.get_running_loop()
        until = loop.time() + limit_s
        d = self.app.dialogue
        while loop.time() < until and (d.leaf == "FAREWELL" or d.fsm.is_in("RETURNING")):
            await asyncio.sleep(0.1)

    async def scan_image(self, path: str) -> bool:
        """scan <path>: the real decoder reads a photo through the fake camera."""
        if not hasattr(self.app.scanner_camera, "show"):
            self.say("  `scan <image>` needs the fake camera (run without --camera)")
            return False
        try:
            import cv2
        except ImportError:
            self.say("  OpenCV is not installed (sudo apt install python3-opencv)")
            return False
        image = cv2.imread(path)
        if image is None:
            self.say(f"  cannot read an image from {path!r}")
            return False
        self.app.scanner_camera.show(image=image)
        await self.run_scan()
        return True

    async def run_scan(self) -> None:
        await self.app.bus.publish(BookPlaced())
        if self.app.scanner is not None:
            await self.app.scanner.wait()

    async def handle(self, line: str) -> bool:
        """Run one command; returns False to quit."""
        words = line.split()
        if not words:
            return True
        cmd, args = words[0].lower(), words[1:]
        rest = line.strip()[len(words[0]):].strip()
        bus = self.app.bus
        if cmd in ("quit", "exit"):
            return False
        if cmd in ("next", "abort") and getattr(self, "guide", None) is not None:
            await self.guide.command(cmd)                    # the guided scenario test: only the tester presses these
            return True
        if cmd in ("help", "?"):
            self.say("\n\n".join(__doc__.split("\n\n")[1:3]))
            return True
        if len(words) == 2 and words[1].lower() in GROUP_SIZES:
            await self.op.set_zone(cmd, words[1].lower())
        elif cmd == "wait" and len(args) == 1:
            try:
                await self.wait(args[0].lower())
            except ValueError:
                self.say("  usage: wait <seconds> | wait idle")
                return True
        elif cmd in OPERATOR_COMMANDS and cmd not in ("book", "say"):
            if not await self.op.run(cmd, rest):
                return True
        elif cmd == "noise" and args:
            if args[0].lower() == "off":
                self.noise_db = None
            else:
                try:
                    self.noise_db = float(args[0])
                except ValueError:
                    self.say("  usage: noise <dB> | noise off")
                    return True
            await self.context_changed()
        elif cmd == "book":
            if not await self.place_book(args):
                return True
        elif cmd == "scan":
            if not (await self.scan_image(rest) if rest else await self.place_book([])):
                return True
        elif cmd in CHOICES and not args:
            await bus.publish(UserUtterance(cmd))
        elif cmd == "guide" and not args:
            await bus.publish(UserUtterance("guide me"))
        elif cmd == "reload" and not args:
            try:
                self.app.finder.reload()
            except (CatalogError, RuntimeError) as exc:
                self.say(f"  could not reload, keeping the old data: {exc}")
            else:
                graph = self.app.finder.graph
                self.say(f"  reloaded: {len(graph.catalog)} books, {len(graph.shelves)} shelves, {len(graph.links)} links")
            return True
        elif cmd == "where" and rest and rest.split()[0].lower() in ("is", "are", "was", "can", "do", "does", "would"):
            await bus.publish(UserUtterance(line.strip()))     # a sentence ("where is it?"), not `where <title>`
        elif cmd == "where" and rest:
            book = self.app.finder.catalog.find_title(rest)
            if book is None:
                self.say(f"  no book in the catalogue matches {rest!r}")
                return True
            await bus.publish(UserUtterance(f"where is {book.title}"))
        elif cmd == "say" and rest:
            await bus.publish(UserUtterance(rest, source="operator"))
        elif cmd == "feel" and args and normalize(args[0]) in EMOTIONS:
            try:
                conf = float(args[1]) if len(args) > 1 else 0.9
            except ValueError:
                conf = 0.9
            await bus.publish(EmotionDetected(normalize(args[0]), conf))
        elif cmd == "fixed" and args and args[0].lower() in ("on", "off"):
            await self.app.set_fixed(args[0].lower() == "on")
        elif cmd in COMMANDS:
            self.say(f"  usage problem with `{cmd}`; type help")
            return True
        else:                                    # not a command: the visitor is talking to the robot
            await bus.publish(UserUtterance(line.strip()))
        self.status()
        return True


def build_identify(cfg: Config, sim: "Sim"):
    """The PersonTracker that gives faces person numbers, or None (with the reason said) if it can't run."""
    seen = cfg.robot.get("perception") or {}
    if not seen.get("identify", True):
        return None
    from robot.perception.identity import EmbedderUnavailable, FaceEmbedder, PersonTracker
    try:
        embedder = FaceEmbedder(cfg.path(seen.get("face_model", "models/face/face_recognition_sface_2021dec.onnx")))
    except EmbedderUnavailable as exc:
        sim.say(f"  can't tell people apart ({exc}); counting faces only")
        return None
    sim.say("  telling people apart: person 1, person 2... (kept in memory for this run only)")
    return PersonTracker(embedder, float(seen.get("identity_threshold", 0.40)))


async def read_line(loop: asyncio.AbstractEventLoop, stdin: TextIO) -> str:
    """stdin.readline() without the loop's default executor: that one is joined when asyncio.run() shuts
    down, so a read blocked on the keyboard made Ctrl+C hang until Enter was pressed."""
    future = loop.create_future()

    def deliver(line: str) -> None:
        if not future.done():
            future.set_result(line)

    def read() -> None:
        line = stdin.readline()
        try:
            loop.call_soon_threadsafe(deliver, line)
        except RuntimeError:                     # the loop is already closed: nobody is waiting
            pass

    threading.Thread(target=read, name="stdin", daemon=True).start()
    return await future


async def run_sim(cfg: Config, stdin: TextIO = sys.stdin, out: TextIO = sys.stdout,
                  log_sessions: bool = True, use_llm: bool = False, use_camera: bool = False,
                  speak: bool = False, use_faces: bool = False, preview: bool = False,
                  auto_scan: bool = False, settle_s: float = SETTLE_S, persist_storage: bool = True,
                  local_faces: bool = False, scenario_guide=None, initial_zone: Optional[str] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="  %(message)s", stream=out, force=True)
    for wanted, flag in ((use_faces, "--faces"), (auto_scan, "--auto-scan")):
        if wanted and not use_camera:
            print(f"{flag} needs the real webcam: add --camera (or use --live)", file=sys.stderr)
            return 1
    try:
        drivers = sim_drivers(cfg, use_camera=use_camera, speak=speak)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Cannot start the simulator: {exc}", file=sys.stderr)
        return 1
    speech = None
    if speak:
        from robot.services.speech import Speech
        speech = Speech.from_config(cfg)
    assistant = build_assistant(cfg) if use_llm else None
    speaker = cfg.robot.get("devices", {}).get("speaker") or "default"
    counter = window = counter_task = watch_task = ticker = link_task = feed_task = warm_task = None
    try:
        app = build_app(cfg, drivers, speech=speech, log_sessions=log_sessions, assistant=assistant,
                        persist_storage=persist_storage)
        sim = Sim(app, out)
        sim.guide = None                                     # the guided scenario test (robot/scenarios/live.py), if any
        sim.say("Robot simulator. Type help for commands, quit to exit.")
        if initial_zone:
            await sim.op.set_zone(initial_zone)
        sim.say(f"  camera: {'real webcam' if use_camera else 'fake'} | speaker: "
                f"{'real (' + speaker + '), TTS from ' + cfg.ai_host if speak else 'none, replies are only printed'}"
                f" | explain/recommend: {'LLM at ' + cfg.ai_host if use_llm else 'catalogue text'}"
                f" | faces: {('OpenCV on the robot' if local_faces else 'AI server at ' + cfg.ai_host) if use_faces else 'typed (common alone, ...)'}"
                f" | scan: {'when a barcode is shown' if auto_scan else 'when you type book'}")
        if app.session_log:
            sim.say(f"Logging this session to {app.session_log.path}")
        sim.status()
        if app.dialogue.storage.stored():
            sim.say(f"  {len(app.dialogue.storage)} book(s) are still in storage from last time "
                    "(`reset all` empties it)")
        ticker = asyncio.create_task(app.run_ticker())
        if use_llm:                                         # one silent request now, and keep-alives while idle (services/warmup.py)
            warm_task = asyncio.create_task(Warmup.from_app(app).run())
        if speak and speech is not None:
            problem = await asyncio.get_running_loop().run_in_executor(None, speech.check)
            if problem:
                sim.say(f"  WARNING: {problem}")
        if speak:                                           # make the fixed lines' speech now, so the hello is instant
            from robot.core import dialogue as lines
            threading.Thread(target=app.actuators.voice.prepare, name="tts-prepare", daemon=True, args=([
                lines.HELLO, lines.OFFER_NO_BOOK, lines.GOODBYE_SPOKEN, lines.THANKS_SPOKEN, lines.RETURNING_OPENING,
                app.dialogue.thinking_text, "One moment, please.", "Sorry, I didn't catch that.",
                "Is there anything else I can help with?", "Place the book on my pad and I'll take it back.",
                "Which book? Tell me its title, or place it on my pad.",
                "Happy to! What kind of books do you like? Tell me a topic, a title or an author, "
                "or place a book you like on my pad."],)).start()
        from robot.display_link import DisplayLink
        shown = cfg.display
        link_task = asyncio.create_task(DisplayLink(app.bus, sim.op, shown["host"], int(shown["port"]),
                                                    float(shown["poll_s"])).run())
        if use_faces:
            from robot.perception.people import EngagementDetector, PeopleCounter
            from robot.services.vision import Vision
            seen = cfg.robot.get("perception") or {}
            identify = build_identify(cfg, sim)
            if local_faces or seen.get("detector") == "local":
                from robot.services.local_faces import LocalFaceDetector, LocalFacesUnavailable
                try:
                    detector = LocalFaceDetector()
                except LocalFacesUnavailable as exc:
                    print(f"Cannot detect faces locally: {exc}", file=sys.stderr)
                    return 1
                sim.say("  detecting faces on the robot (OpenCV), not on the AI server")
            else:
                detector = Vision(cfg.ai_host, cfg.ai_port, timeout=10)
            counter = PeopleCounter(app.bus, drivers.camera, detector,
                                    identify=identify, engagement=EngagementDetector.from_config(cfg.engagement),
                                    map_zone=sim.zone, interval_s=float(seen.get("face_interval_s", 1.0)),
                                    arrive_frames=int(seen.get("arrive_frames", 2)),
                                    leave_after_s=float(seen.get("leave_after_s", 8.0)),
                                    on_change=sim.people_changed)
            counter_task = asyncio.create_task(counter.run())
            sim.say("  watching for faces: step in front of the camera and the robot says hello")
        if use_camera:
            from robot.camera_feed import CameraFeed
            feed_task = asyncio.create_task(CameraFeed(drivers.camera, counter, shown["host"], int(shown["port"]),
                                                       float(shown["camera_fps"]), int(shown["camera_width"])).run())
        if auto_scan:
            from robot.perception.book_watch import BookWatcher
            ready = lambda: (app.dialogue.leaf in ("IDLE", "OFFER", "AWAIT_BOOK", "ANSWER", "ANYTHING_ELSE")
                             and not (app.scanner is not None and app.scanner.scanning))
            watch_task = asyncio.create_task(BookWatcher(app.bus, app.scanner_camera, ready).run())
            sim.say("  watching for books: hold a barcode up to the camera")
        if use_camera and preview:
            from robot.sim.preview import Preview, display_available
            if display_available():
                window = Preview(drivers.camera, counter,
                                 status=lambda: f"people: {sim.group_size} | dialogue: {app.dialogue.state}")
                window.start()
            else:
                sim.say("  no display found: not showing the camera window")
        guide_task = None
        if scenario_guide is not None:
            sim.guide = scenario_guide(app, sim, drivers)
            guide_task = asyncio.create_task(sim.guide.run())
        loop = asyncio.get_running_loop()
        while True:
            if stdin.isatty():
                print("> ", end="", file=out, flush=True)
            line = await read_line(loop, stdin)
            if not line:
                break
            if not stdin.isatty():
                sim.say(f"> {line.rstrip()}")
            if not await sim.handle(line):
                break
        if guide_task is not None and not stdin.isatty():
            await asyncio.wait_for(sim.guide.done.wait(), 5)
        if not stdin.isatty() and settle_s > 0:
            await sim.settle(settle_s)
        return 0
    finally:
        for task in (counter_task, watch_task, ticker, link_task, feed_task, warm_task, locals().get("guide_task")):
            if task is not None:
                task.cancel()
        if window is not None:
            window.stop()
        for camera in (drivers.camera, drivers.scanner_camera):
            if camera is not None and hasattr(camera, "close"):
                camera.close()
