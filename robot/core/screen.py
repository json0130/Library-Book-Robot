"""The screen composer: decides what the robot's screen shows, next to (or instead of) the face.

The state machine never picks a view. It publishes StateEntered (where it is, what it said, which choices it offers)
and content events (BookIdentified, BookFocused, RecommendationsReady, LocationResolved, ChoicesOffered, StoringBook,
ReturningProgress); this composer turns them into ScreenUpdate events with one of these views:

    face        the face fills the screen (in a quiet zone with a caption of what the robot said)
    book_card   a cover, title, author, a short summary, where it lives, and "Show me the way" / "Something similar"
    book_list   up to three books with their covers and one reason each; tapping one opens its card
    directions  the floor plan with the target section, the robot, an approximate path, the distance in metres
    options     the current choices as big buttons (OFFER and ANYTHING_ELSE, mainly in a quiet zone)
    status      returning books: where to and how many are left

One state can show different views: ANSWER shows a book card for "tell me about it" and directions for "where is it".
A content view keeps small eyes in a corner (layout "split"); the face never goes away completely. Content stays
until the next content or session change, or until screen.content_ttl_s after the last interaction (longer in a quiet
zone, where people read; shorter in a common area, where the speech carries it), then the face comes back.

Every payload is a dataclass with a `to_json()`; ScreenUpdate.payload carries that dict.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from robot.events import (ThinkingEnded, ThinkingStarted, BookFocused, BookIdentified, BookPlaced, ChoicesOffered, LocationResolved,
                          NavigationRequested, RecommendationsReady, ReturningProgress, ScreenUpdate, StateEntered,
                          StoringBook, UserChoice, UserUtterance)

log = logging.getLogger("robot.screen")

VIEWS = ("face", "book_card", "book_list", "directions", "options", "status")
FACE_STATES = ("IDLE", "GREET", "FAREWELL", "PLAN")
RETURNING_STATES = ("NAVIGATING", "AT_SHELF", "ANNOUNCE", "HOMING")
CONTENT_STATES = ("OFFER", "AWAIT_BOOK", "ANSWER", "RECOMMEND", "ANYTHING_ELSE")


# ---------------------------------------------------------------------------------------------- payloads
class _Payload:
    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class FacePayload(_Payload):
    caption: str = ""                       # what the robot said, shown under the face in a quiet zone


@dataclass
class BookCardPayload(_Payload):
    isbn: str
    title: str
    author: str = ""
    summary: str = ""
    cover_url: str = ""
    level: Optional[int] = None
    section: str = ""
    shelf: str = ""
    badge: str = ""                         # e.g. "Storage slot 5" while it is being returned
    buttons: list = field(default_factory=list)      # [{"id", "label"}]


@dataclass
class BookListPayload(_Payload):
    heading: str
    items: list = field(default_factory=list)        # [{"isbn", "title", "author", "reason", "cover_url"}]


@dataclass
class DirectionsPayload(_Payload):
    kind: str                               # book | section | floor
    title: str                              # what it is ("Dune", "the History section", "Level 2")
    level: int
    floor_name: str
    width: float
    height: float
    sections: list = field(default_factory=list)     # [{"name", "x", "y", "w", "h", "highlight"}]
    landmarks: list = field(default_factory=list)    # [{"kind", "name", "x", "y"}]
    robot: Optional[dict] = None            # {"x", "y"} when the robot is on this floor
    start: Optional[dict] = None            # where the drawn path starts {"x", "y", "label"}
    target: Optional[dict] = None           # {"x", "y"}
    path: list = field(default_factory=list)          # [[x, y], ...]: APPROXIMATE, straight or L-shaped
    distance_m: Optional[int] = None        # approximate, along the drawn legs (both floors)
    section: str = ""
    shelf: str = ""
    other_floor: bool = False
    steps: list = field(default_factory=list)         # ["Take the lift to Level 3", ...]
    buttons: list = field(default_factory=list)


@dataclass
class OptionsPayload(_Payload):
    prompt: str
    options: list = field(default_factory=list)      # [{"id", "label"}]


@dataclass
class StatusPayload(_Payload):
    heading: str
    destination: str = ""
    books_left: int = 0
    detail: str = ""


PAYLOADS = {"face": FacePayload, "book_card": BookCardPayload, "book_list": BookListPayload,
            "directions": DirectionsPayload, "options": OptionsPayload, "status": StatusPayload}


def payload_from_json(view: str, data: dict):
    """The typed payload back from its JSON dict (for tests and tools)."""
    return PAYLOADS[view](**data)


# ---------------------------------------------------------------------------------------------- helpers
def cover_url(isbn: str, title: str = "", author: str = "") -> str:
    """The display server serves data/covers/<isbn>.jpg, or a placeholder made from the title and author."""
    query = urllib.parse.urlencode({"title": title or "", "author": author or ""})
    return f"/covers/{isbn}?{query}"


def trim(text: str, limit: int) -> str:
    """Cut at a word boundary with an ellipsis (the page also clamps to two lines)."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:.")
    return cut + "…"


def short_reason(because: str, limit: int) -> str:
    """"because, like Cosmos, it's about popular science" -> "Like Cosmos, it's about popular science"."""
    text = (because or "").strip()
    text = text[len("because"):].lstrip(" ,") if text.lower().startswith("because") else text
    return trim(text[:1].upper() + text[1:], limit)


def l_path(start: tuple, end: tuple) -> list:
    """APPROXIMATE: an L-shaped walk, first along x then along y (a straight line when they share a row or a column).
    There is no real path planning: shelves and walls are not avoided."""
    (x0, y0), (x1, y1) = start, end
    points = [[x0, y0]]
    if x0 != x1 and y0 != y1:
        points.append([x1, y0])
    points.append([x1, y1])
    return [[round(x, 2), round(y, 2)] for x, y in points]


def l_length(start: tuple, end: tuple) -> float:
    return abs(end[0] - start[0]) + abs(end[1] - start[1])


def directions(plan, kind: str, title: str, level: int, robot: Optional[tuple], section: str = "", shelf: str = "",
               target: Optional[tuple] = None) -> Optional[DirectionsPayload]:
    """The directions payload to `target` (x, y) on `level` (or to the floor itself when kind is "floor"), from the
    robot at (level, x, y). None if the plan has no such floor."""
    floor = plan.floor(level) if plan is not None else None
    if floor is None:
        return None
    if target is None and section:
        found = floor.section(section)
        target = found.centre if found else None
    robot_here = robot is not None and robot[0] == level
    other = robot is not None and robot[0] != level
    lift_here = floor.landmark("lift")
    steps, distance = [], 0.0
    start = None
    if robot_here:
        start = {"x": robot[1], "y": robot[2], "label": "You are here"}
    elif other and lift_here is not None:
        start = {"x": lift_here.x, "y": lift_here.y, "label": "Lift"}
        steps.append(f"Take the lift to Level {level}")
        lift_there = plan.lift(robot[0])
        if lift_there is not None:
            distance += l_length((robot[1], robot[2]), (lift_there.x, lift_there.y))
    elif other:
        steps.append(f"Go up to Level {level}" if level > robot[0] else f"Go down to Level {level}")
    path = []
    if start is not None and target is not None:
        path = l_path((start["x"], start["y"]), target)
        distance += l_length((start["x"], start["y"]), target)
    if kind == "floor":
        names = ", ".join(s.name for s in floor.sections) or "no sections yet"
        steps.append(f"{floor.name}: {names}")
    else:
        where = f"the {section} section" if section else title
        steps.append(f"Then go to {where}" + (f", shelf {shelf}" if shelf else "") if other else
                     f"Go to {where}" + (f", shelf {shelf}" if shelf else ""))
    highlight = {section.lower()} if section else ({s.name.lower() for s in floor.sections} if kind == "floor" else set())
    return DirectionsPayload(
        kind=kind, title=title, level=level, floor_name=floor.name, width=floor.width, height=floor.height,
        sections=[{"name": s.name, "x": s.x, "y": s.y, "w": s.w, "h": s.h, "highlight": s.name.lower() in highlight}
                  for s in floor.sections],
        landmarks=[{"kind": m.kind, "name": m.name, "x": m.x, "y": m.y} for m in floor.landmarks],
        robot={"x": robot[1], "y": robot[2]} if robot_here else None, start=start,
        target={"x": target[0], "y": target[1]} if target is not None else None, path=path,
        distance_m=round(distance) if path or distance else None, section=section, shelf=shelf, other_floor=other,
        steps=steps, buttons=[{"id": "guide", "label": "Guide me"}] if kind != "floor" else [])


# ---------------------------------------------------------------------------------------------- the composer
class ScreenComposer:
    def __init__(self, publish: Callable, finder=None, plan=None, settings: Optional[dict] = None,
                 get_style: Callable = lambda: None, state_of: Callable[[], str] = lambda: "IDLE",
                 clock: Callable[[], float] = time.monotonic):
        self.publish, self.finder, self.plan = publish, finder, plan
        self.settings = settings or {}
        self.get_style, self.state_of, self.clock = get_style, state_of, clock
        self.path, self.zone, self.text, self.options, self.title = "IDLE", "common", "", (), ""
        self.choices: tuple = ()
        self.repair = False
        self.thinking = False
        self.content: Optional[tuple] = None          # (view, payload) for the current content
        self.until: Optional[float] = None            # when the content goes back to the face
        self.book: Optional[BookIdentified] = None    # the book on the pad this visit
        self.storing: Optional[StoringBook] = None
        self.progress: Optional[ReturningProgress] = None
        self.nav: Optional[tuple] = None              # (level, x, y) of the last navigation target
        self.current: Optional[ScreenUpdate] = None

    def attach(self, bus) -> None:
        bus.subscribe(StateEntered, self.on_state)
        bus.subscribe(BookIdentified, self.on_book)
        bus.subscribe(BookFocused, self.on_focus)
        bus.subscribe(RecommendationsReady, self.on_recommendations)
        bus.subscribe(LocationResolved, self.on_location)
        bus.subscribe(ChoicesOffered, self.on_choices)
        bus.subscribe(StoringBook, self.on_storing)
        bus.subscribe(ReturningProgress, self.on_progress)
        bus.subscribe(NavigationRequested, self.on_navigation)
        bus.subscribe(ThinkingStarted, self.on_thinking)
        bus.subscribe(ThinkingEnded, self.on_thinking)
        for kind in (UserUtterance, UserChoice, BookPlaced):
            bus.subscribe(kind, self.on_interaction)

    # ---- settings per zone ----
    def ttl(self) -> float:
        ttl = self.settings.get("content_ttl_s", {"quiet": 30, "common": 15})
        return float(ttl.get(self.zone, 15) if isinstance(ttl, dict) else ttl)

    def summary_chars(self) -> int:
        chars = self.settings.get("summary_chars", {"quiet": 180, "common": 110})
        return int(chars.get(self.zone, 110) if isinstance(chars, dict) else chars)

    @property
    def leaf(self) -> str:
        return self.path.split(".")[-1]

    def touch(self) -> bool:
        style = self.get_style()
        return bool(getattr(style, "touch_buttons", self.zone == "quiet"))

    # ---- events ----
    async def on_state(self, event: StateEntered) -> None:
        if self.state_of() and self.state_of() != event.state_path:
            return                          # the machine has moved on: a newer StateEntered follows, don't undo its content
        self.path, self.zone, self.text = event.state_path, event.zone, event.text
        self.options, self.title = tuple(event.options), event.title
        self.choices = tuple(event.choices or event.options)
        self.repair = event.repair
        leaf = self.leaf
        self.thinking = False
        if leaf in ("IDLE", "GREET") or (leaf == "FAREWELL"):
            self.content, self.until, self.book, self.storing = None, None, None, None
        if leaf in ("SCAN", "AWAIT_BOOK"):
            self.content = None                       # a new book or a new question: what was shown is about something else
        if leaf not in RETURNING_STATES and leaf != "PLAN":
            self.progress = None
        await self.show()

    async def on_book(self, event: BookIdentified) -> None:
        self.book = event
        if self.storing is not None and self.storing.isbn == event.isbn:
            return                      # the dialogue already took it to STORE (it hears the event first): keep the slot card
        if event.found_in_catalog:
            self.set_content("book_card", self.book_card(event.isbn, pad=True))

    async def on_focus(self, event: BookFocused) -> None:
        card = self.book_card(event.isbn)
        if card is not None:
            self.set_content("book_card", card)
            await self.show()

    async def on_recommendations(self, event: RecommendationsReady) -> None:
        items = []
        for pick in event.picks[:3]:
            book = self.lookup(pick["isbn"])
            if book is None:
                continue
            items.append({"isbn": book.isbn13, "title": book.title, "author": book.author or "",
                          "reason": short_reason(pick.get("because", ""), 90 if self.zone == "quiet" else 70),
                          "cover_url": cover_url(book.isbn13, book.title, book.author)})
        if items:
            source = self.lookup(event.source_isbn) if event.source_isbn else None
            heading = (f"If you like {source.title}" if source else
                       f"About {event.interest}" if event.interest else "You might enjoy")
            self.set_content("book_list", BookListPayload(heading, items))
            await self.show()

    async def on_location(self, event: LocationResolved) -> None:
        payload = self.directions_for(event)
        if payload is not None:
            self.set_content("directions", payload)
            await self.show()

    async def on_choices(self, event: ChoicesOffered) -> None:
        self.set_content("options", OptionsPayload(event.prompt, [dict(c) for c in event.choices]))
        await self.show()

    async def on_thinking(self, event) -> None:
        """The LLM is being asked (or has answered): the screen shows a progress indicator in both zones."""
        self.thinking = isinstance(event, ThinkingStarted)
        await self.show()

    async def on_storing(self, event: StoringBook) -> None:
        """The card with its slot during STORE; afterwards it stays as content, with "Show me the way"."""
        self.storing = event
        card = self.book_card(event.isbn)
        if card is not None:
            card.badge = f"In storage slot {event.slot}"
            card.buttons = []                           # they handed it in: nothing to ask about it
            self.set_content("book_card", card)

    async def on_progress(self, event: ReturningProgress) -> None:
        self.progress = event
        if self.leaf in RETURNING_STATES:
            await self.show()

    def on_navigation(self, event: NavigationRequested) -> None:
        if event.floor is not None:
            self.nav = (int(event.floor), event.x, event.y)
        elif self.plan is not None and self.plan.station is not None and event.title == "home":
            self.nav = (self.plan.station.level, event.x, event.y)

    def on_interaction(self, event) -> None:
        if self.content is not None:
            self.until = self.clock() + self.ttl()

    async def tick(self) -> bool:
        """Back to the face when the content's time is up; True if it changed. While a visitor is being helped the
        map, the list or the card stays up (it is what the robot is talking about); it clears with the next content, a
        new question or the end of the visit."""
        engaged = self.path.startswith("ENGAGED") and self.leaf != "FAREWELL"
        if self.content is not None and self.until is not None and self.clock() >= self.until and not engaged:
            self.content, self.until = None, None
            await self.show()
            return True
        return False

    # ---- building views ----
    def set_content(self, view: str, payload) -> None:
        self.content = (view, payload)
        self.until = self.clock() + self.ttl()

    def lookup(self, isbn: str):
        return self.finder.catalog.lookup(isbn) if self.finder is not None and isbn else None

    def book_card(self, isbn: str, pad: bool = False) -> Optional[BookCardPayload]:
        book = self.lookup(isbn)
        if book is None:
            return None
        place = self.finder.locate(book.isbn13)
        return BookCardPayload(
            isbn=book.isbn13, title=book.title, author=book.author or "",
            summary=trim(book.summary or "", self.summary_chars()), cover_url=cover_url(book.isbn13, book.title, book.author),
            level=place.floor if place else None, section=(place.section if place else "") or "",
            shelf=(place.shelf if place else "") or "",
            buttons=[{"id": f"guide:{book.isbn13}", "label": "Show me the way"},
                     {"id": f"similar:{book.isbn13}", "label": "Something similar"}])

    def robot_position(self) -> Optional[tuple]:
        """The station when idle or with a visitor; during RETURNING the last (simulated) navigation target."""
        if "RETURNING" in self.path and self.nav is not None:
            return self.nav
        station = self.plan.station if self.plan is not None else None
        return (station.level, station.x, station.y) if station else None

    def directions_for(self, event: LocationResolved) -> Optional[DirectionsPayload]:
        if self.plan is None or event.floor is None:
            return None
        target = (event.x, event.y) if event.x is not None and event.y is not None else None
        title = event.title or (f"the {event.section} section" if event.section else f"Level {event.floor}")
        return directions(self.plan, event.kind, title, int(event.floor), self.robot_position(),
                          event.section or "", event.shelf or "", target)

    def compose(self) -> tuple:
        """(view, payload) for where the machine is and what has been shown."""
        leaf = self.leaf
        if leaf in FACE_STATES:
            return "face", FacePayload(self.text if self.zone == "quiet" else "")
        if leaf in RETURNING_STATES:
            p = self.progress
            heading = {"NAVIGATING": "Returning books", "AT_SHELF": "Putting books back", "ANNOUNCE": "Excuse me",
                       "HOMING": "Heading home"}[leaf]
            destination = "home" if leaf == "HOMING" else (f"the {p.section} section" if p and p.section else
                                                         (f"shelf {p.shelf}" if p and p.shelf else ""))
            detail = self.text if leaf == "ANNOUNCE" else (f"Shelf {p.shelf}, level {p.floor}" if p and p.shelf else "")
            return "status", StatusPayload(heading, destination, p.books_left if p else 0, detail)
        if leaf == "SCAN":
            return "face", FacePayload("Reading the book…" if self.zone == "quiet" else "")
        if leaf == "STORE" and self.storing is not None:
            card = self.book_card(self.storing.isbn) or BookCardPayload(self.storing.isbn, self.storing.title)
            card.badge, card.buttons = f"Storage slot {self.storing.slot}", []
            return "book_card", card
        if self.repair and self.choices and leaf in ("OFFER", "AWAIT_BOOK", "ANYTHING_ELSE"):
            return "options", OptionsPayload(self.text if self.zone == "quiet" else "You can tap:", [dict(o) for o in self.choices])
        if self.content is not None:
            return self.content
        if self.choices and (leaf in ("OFFER", "ANYTHING_ELSE") or (leaf == "AWAIT_BOOK" and self.touch())):
            # the state's choices as buttons in every zone: the screen follows the conversation by itself
            prompt = self.text if self.zone == "quiet" else (self.title or "How can I help?")   # speech carries the words
            return "options", OptionsPayload(prompt, [dict(o) for o in self.choices])
        return "face", FacePayload(self.text if self.zone == "quiet" else "")

    async def show(self, view: Optional[str] = None, payload=None) -> ScreenUpdate:
        self.path = self.state_of() or self.path          # content can arrive while the machine enters a state
        if view is None:
            view, payload = self.compose()
        content = view != "face"
        update = ScreenUpdate(self.path, self.zone, "split" if content else "full_face", self.title,
                              self.text, tuple(self.options), view, payload.to_json(),
                              self.ttl() if content else None, self.thinking)
        self.current = update
        log.debug("[screen] %s %s", update.state_path, view)
        if self.publish is not None:
            await self.publish(update)
        return update

    # ---- the operator's `screen <view> [isbn | section]` ----
    async def force(self, view: str, arg: str = "") -> ScreenUpdate:
        """Show a view with sample (or the given) data, as if content had arrived. `face` clears the content."""
        view = view.strip().lower()
        if view not in VIEWS:
            raise ValueError(f"no view called {view!r}; views: {', '.join(VIEWS)}")
        books = self.finder.catalog.books if self.finder is not None else []
        isbn = arg.strip() if arg.strip().isdigit() else (books[0].isbn13 if books else "")
        if view == "face":
            self.content, self.until = None, None
            return await self.show()
        if view == "book_card":
            payload = self.book_card(isbn) or BookCardPayload(isbn, "Sample book")
        elif view == "book_list":
            picks = self.finder.recommend(isbn, 3) if self.finder is not None and isbn else []
            payload = BookListPayload(f"If you like {self.lookup(isbn).title}" if self.lookup(isbn) else "You might enjoy",
                                      [{"isbn": p.book.isbn13, "title": p.book.title, "author": p.book.author or "",
                                        "reason": short_reason(p.because, 70), "cover_url": cover_url(p.book.isbn13, p.book.title,
                                                                                              p.book.author)}
                                       for p in picks])
        elif view == "directions":
            payload = self.forced_directions(arg)
        elif view == "options":
            payload = OptionsPayload("How can I help?", [{"id": "return", "label": "Return a book"},
                                                         {"id": "ask", "label": "Ask about a book"},
                                                         {"id": "recommend", "label": "Get a recommendation"}])
        else:
            payload = StatusPayload("Returning books", "the Science section", 2, "Shelf 3-SCI-A, level 3")
        if payload is None:
            raise ValueError(f"nothing to show for {arg!r}")
        self.set_content(view, payload)
        return await self.show(view, payload)

    def forced_directions(self, arg: str) -> Optional[DirectionsPayload]:
        arg = arg.strip()
        if arg.isdigit() and self.finder is not None and self.lookup(arg):
            book, place = self.lookup(arg), self.finder.locate(arg)
            if place and place.floor is not None:
                return directions(self.plan, "book", book.title, place.floor, self.robot_position(),
                                  place.section or "", place.shelf or "",
                                  (place.map_x, place.map_y) if place.map_x is not None else None)
        found = self.plan.where_section(arg or "Science") if self.plan is not None else None
        if found is None and self.plan is not None and self.plan.floors:
            floor = next(iter(self.plan.floors.values()))
            found = (floor, floor.sections[0]) if floor.sections else None
        if found is None:
            return None
        floor, section = found
        return directions(self.plan, "section", f"the {section.name} section", floor.level, self.robot_position(),
                          section.name)
