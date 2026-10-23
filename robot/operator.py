"""Operator controls: what a person running a live test can do besides what the sensors do.

The same commands are typed in the simulator's terminal and sent from the /operator page of the display server (which
the robot picks up through robot/display_link.py), so there is one implementation:

    state                 where the machine is, the zone, what is in storage
    goto <state>          force the machine into a state (OFFER, ENGAGED.SCAN, NAVIGATING...), filling in what it needs:
                          a sample book for SCAN, STORE, ANSWER and RECOMMEND, two stored books for RETURNING
    zone quiet|common     change the zone (a style setting, not a state)
    engage | busy | leave a person engages / is near but busy / walks away (PersonEngaged / PersonNearbyBusy / PersonLeft)
    dropped               the pad dropped the book into its slot (BookDropped)
    pick <choice>         press a screen button for the visitor: return ask recommend more guide done
    book [isbn|none]      place a book on the pad (scanned by the camera or, in the sim, a fake one)
    say <text>            the visitor says this
    reset [all]           back to IDLE with nothing in progress; `all` also empties the storage
    screen <view> [isbn | section]   force a screen view with sample data (face book_card book_list directions
                          options status); `screen face` goes back to the face

Events made here carry source="operator", which the session log shows with every state transition.
"""

from __future__ import annotations

import random
from typing import Callable, Optional

from robot.core.fsm import SpecError
from robot.events import (BookDropped, BookPlaced, ContextChanged, PersonEngaged, PersonLeft, PersonNearbyBusy,
                          UserChoice, UserUtterance)
from robot.services.isbn import to_isbn13

COMMANDS = ("state", "goto", "zone", "engage", "busy", "leave", "dropped", "pick", "book", "say", "reset", "screen")
PICKS = ("return", "ask", "recommend", "more", "guide", "done", "another")
ZONES = ("quiet", "common")
GROUP_SIZES = {"alone": 1, "group": 3}


class Operator:
    def __init__(self, app, say: Callable[[str], None] = lambda text: None, rng: Optional[random.Random] = None):
        self.app = app
        self.say = say
        self.rng = rng or random.Random()
        self.zone = app.cfg.zones.get("default_zone", "common")
        self.group_size = 1
        self.person_ids: tuple = ()                 # who the camera tells apart (person 1...), () when typed
        self.noise_db: Optional[float] = None

    # ---- the context the sim and the operator page set ----
    async def context_changed(self) -> None:
        await self.app.bus.publish(ContextChanged(self.zone, self.group_size, self.noise_db, self.person_ids))

    async def set_zone(self, zone: str, group: Optional[str] = None) -> None:
        self.zone = zone
        if group is not None:
            self.group_size, self.noise_db, self.person_ids = GROUP_SIZES[group], None, ()
        await self.context_changed()

    # ---- books ----
    async def place_book(self, args: list) -> bool:
        """book [isbn|none]: scan the book on the pad. With the real webcam, scan whatever it sees; with
        the fake camera, show it a book first. False if the input was bad."""
        camera = self.app.scanner_camera
        if not hasattr(camera, "show"):                     # the real webcam
            if args:
                self.say("  `book <isbn>` and `book none` need the fake camera (run without --camera)")
                return False
            self.say("  scanning... hold the book's barcode to the camera")
            await self.run_scan()
            return True
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

    async def run_scan(self) -> None:
        await self.app.bus.publish(BookPlaced(source="operator"))
        if self.app.scanner is not None:
            await self.app.scanner.wait()

    # ---- the commands ----
    def describe(self) -> str:
        d = self.app.dialogue
        stored = ", ".join(f"{b.title} (slot {b.slot})" for b in d.storage.stored()) or "empty"
        return f"state {d.state} | zone {self.app.context.zone}{' (fixed mode)' if self.app.fixed else ''} | storage: {stored}"

    async def run(self, command: str, arg: str = "") -> bool:
        """Run one operator command; returns False if it was not one or the argument was bad (the reason is said)."""
        command, arg = command.lower(), arg.strip()
        bus, d = self.app.bus, self.app.dialogue
        if command == "state":
            self.say("  " + self.describe())
        elif command == "goto":
            if not arg:
                self.say("  usage: goto <state>; states: " + ", ".join(d.fsm.spec.leaves()))
                return False
            try:
                path = await d.goto(arg)
            except SpecError as exc:
                self.say(f"  {exc}")
                return False
            self.say(f"  now in {path}")
        elif command == "zone":
            if arg.lower() not in ZONES:
                self.say("  usage: zone quiet|common")
                return False
            await self.set_zone(arg.lower())
        elif command == "engage":
            if self.group_size == 0:
                self.group_size = 1
                await self.context_changed()
            await bus.publish(PersonEngaged(self.person_ids, source="operator"))
        elif command == "busy":
            await bus.publish(PersonNearbyBusy(self.person_ids, source="operator"))
        elif command == "leave":
            self.group_size, self.person_ids = 0, ()
            await self.context_changed()
            await bus.publish(PersonLeft(source="operator"))
        elif command == "dropped":
            await bus.publish(BookDropped(source="operator"))
        elif command == "pick":
            content = arg.split(":", 1)[0].lower() in ("book", "where", "guide", "similar", "section", "floor")
            if arg.lower() not in PICKS and not (content and ":" in arg):
                self.say("  usage: pick " + "|".join(PICKS) + " | book:<isbn> | where:<isbn> | similar:<isbn> | "
                         "guide:<isbn> | section:<name> | floor:<n>")
                return False
            await bus.publish(UserChoice(arg if content else arg.lower(), source="operator"))
        elif command == "book":
            return await self.place_book(arg.split())
        elif command == "say":
            if not arg:
                self.say("  usage: say <text>")
                return False
            await bus.publish(UserUtterance(arg, source="operator"))
        elif command == "screen":
            view, _, rest = arg.partition(" ")
            if not view or self.app.screen is None:
                self.say("  usage: screen face|book_card|book_list|directions|options|status [isbn | section]")
                return False
            try:
                update = await self.app.screen.force(view, rest)
            except ValueError as exc:
                self.say(f"  {exc}")
                return False
            self.say(f"  screen: {update.view}")
        elif command == "reset":
            await d.reset(everything=arg.lower() == "all")
            self.say("  reset: IDLE" + (", storage emptied" if arg.lower() == "all" else ""))
        else:
            return False
        return True
