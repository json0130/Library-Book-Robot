"""Keyboard simulator: drive the robot's core logic by typing, with fake hardware.

    python -m robot sim
    quiet alone | common group   set the map zone (or an area from zones.yaml) and alone/group
    noise 62 | noise off         a noise reading in dB (may override the zone) / clear it
    book                         place a random sample book on the pad (the fake camera shows its barcode)
    book <isbn>                  place the book with this ISBN (any catalogue book, or any valid ISBN)
    book none                    place a book whose barcode can't be read (scan fails)
    scan <image path>            place a book and let the real barcode decoder read a photo of it
    return | explain | recommend say that to the robot when it has offered
    say <text>                   the visitor says something
    feel <emotion> [confidence]  the camera sees this emotion on the visitor (confidence 0..1)
    leave                        everyone walks away
    fixed on | fixed off         baseline condition: one style everywhere
    help, quit

After each command the sim prints the style and the dialogue state. Expressions go to the face
display if it is running (python -m display.server), otherwise they are only printed.
No hardware or AI server is needed.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
from typing import Optional, TextIO

from robot.app import App, build_app, fake_drivers
from robot.config import Config
from robot.core.expression import EMOTIONS, normalize
from robot.events import BookPlaced, ContextChanged, EmotionDetected, UserUtterance
from robot.services.isbn import to_isbn13

GROUP_SIZES = {"alone": 1, "group": 3}
CHOICES = ("return", "explain", "recommend")


class Sim:
    def __init__(self, app: App, out: TextIO = sys.stdout, rng: Optional[random.Random] = None):
        self.app = app
        self.out = out
        self.rng = rng or random.Random()
        self.zone = app.cfg.zones.get("default_zone", "common")
        self.group_size = 1
        self.noise_db: Optional[float] = None

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def status(self) -> None:
        c = self.app.context
        noise = f", noise {c.noise_db:g} dB" if c.noise_db is not None else ""
        self.say(f"  context: {c.zone} {c.social} (map {self.zone}, {c.group_size} {'person' if c.group_size == 1 else 'people'}{noise})"
                 + ("  [fixed mode]" if self.app.fixed else ""))
        self.say(f"  style:   {self.app.style.summary()}")
        self.say(f"  dialogue: {self.app.dialogue.state}")

    async def context_changed(self) -> None:
        await self.app.bus.publish(ContextChanged(self.zone, self.group_size, self.noise_db))

    async def place_book(self, args: list) -> bool:
        """book [isbn|none]: show the fake camera a book, then run the scan. False if the input was bad."""
        camera = self.app.scanner_camera
        if not hasattr(camera, "show"):
            self.say("  `book` needs the fake camera; use `scan <image>` or the real pad")
            return False
        if args and args[0].lower() == "none":
            camera.clear()
        elif args:
            isbn = to_isbn13("".join(args))
            if isbn is None:
                self.say(f"  {' '.join(args)!r} is not a valid ISBN-10 or ISBN-13")
                return False
            camera.show(isbn=isbn)
        else:
            camera.show(isbn=self.rng.choice(self.app.finder.catalog.books).isbn13)
        await self.run_scan()
        return True

    async def scan_image(self, path: str) -> bool:
        """scan <path>: the real decoder reads a photo through the fake camera."""
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
        if cmd in ("help", "?"):
            self.say(__doc__.split("\n\n")[1])
            return True
        if len(words) == 2 and words[1].lower() in GROUP_SIZES:
            self.zone, self.group_size, self.noise_db = cmd, GROUP_SIZES[words[1].lower()], None
            await self.context_changed()
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
        elif cmd == "leave":
            self.group_size = 0
            await self.context_changed()
        elif cmd == "book":
            if not await self.place_book(args):
                return True
        elif cmd == "scan" and rest:
            if not await self.scan_image(rest):
                return True
        elif cmd in CHOICES and not args:
            await bus.publish(UserUtterance(cmd))
        elif cmd == "say" and rest:
            await bus.publish(UserUtterance(rest))
        elif cmd == "feel" and args and normalize(args[0]) in EMOTIONS:
            try:
                conf = float(args[1]) if len(args) > 1 else 0.9
            except ValueError:
                conf = 0.9
            await bus.publish(EmotionDetected(normalize(args[0]), conf))
        elif cmd == "fixed" and args and args[0].lower() in ("on", "off"):
            await self.app.set_fixed(args[0].lower() == "on")
        else:
            self.say("  unknown command; type help")
            return True
        self.status()
        return True


async def run_sim(cfg: Config, stdin: TextIO = sys.stdin, out: TextIO = sys.stdout,
                  log_sessions: bool = True) -> int:
    logging.basicConfig(level=logging.INFO, format="  %(message)s", stream=out, force=True)
    app = build_app(cfg, fake_drivers(), log_sessions=log_sessions)
    sim = Sim(app, out)
    sim.say("Robot simulator (fake hardware). Type help for commands, quit to exit.")
    if app.session_log:
        sim.say(f"Logging this session to {app.session_log.path}")
    sim.status()
    loop = asyncio.get_running_loop()
    while True:
        if stdin.isatty():
            print("> ", end="", file=out, flush=True)
        line = await loop.run_in_executor(None, stdin.readline)
        if not line:
            break
        if not stdin.isatty():
            sim.say(f"> {line.rstrip()}")
        if not await sim.handle(line):
            break
    return 0
