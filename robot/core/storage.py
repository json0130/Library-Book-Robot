"""The storage ring as data: which slot holds which returned book, until the robot puts it back on its shelf.

    storage = Storage(slots=8, path="data/state/storage.json")
    slot = storage.reserve(preferred=4)           # a free slot, the preferred one if it is free
    storage.commit(slot, StoredBook(isbn=..., title=..., shelf=..., ...))      # the pad dropped the book in
    storage.release(slot)                         # the book was put back on its shelf
    storage.stops()                               # the books grouped by shelf, one stop per shelf, in driving order

A slot holds one book or nothing. A reserved slot is promised to a book that is being stored but has not been
dropped yet; it is not saved and not counted as stored. What is stored is written to the file after every change
(atomically, so a power cut cannot leave half a file) and read back at start-up, so a restart does not lose the
stored books. The file holds books and shelves only, never anything about visitors.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("robot.storage")


@dataclass
class StoredBook:
    isbn: str
    title: str = ""
    shelf: Optional[str] = None
    section: Optional[str] = None
    floor: Optional[int] = None
    zone: Optional[str] = None
    map_x: Optional[float] = None
    map_y: Optional[float] = None
    map_yaw: Optional[float] = None
    slot: Optional[int] = None
    stored_at: float = field(default_factory=time.time)

    @property
    def has_pose(self) -> bool:
        return None not in (self.map_x, self.map_y, self.map_yaw)


@dataclass
class ShelfStop:
    """One stop of a returning trip: a shelf, where it is, and the slots whose books belong on it."""
    shelf: str
    section: Optional[str]
    floor: Optional[int]
    zone: Optional[str]
    x: float
    y: float
    yaw: float
    slots: list = field(default_factory=list)
    titles: list = field(default_factory=list)


class Storage:
    def __init__(self, slots: int = 8, path: Optional[Path | str] = None):
        if slots < 1:
            raise ValueError("the ring needs at least one slot")
        self.size = slots
        self.path = Path(path) if path else None
        self.books: list[Optional[StoredBook]] = [None] * slots
        self._reserved: set = set()
        if self.path is not None:
            self.load()

    # ---- what is in it ----
    def __len__(self) -> int:
        return sum(b is not None for b in self.books)

    @property
    def empty(self) -> bool:
        return len(self) == 0

    @property
    def full(self) -> bool:
        return not self.free_slots()

    def free_slots(self) -> list:
        """Slots with no book and no promise of one."""
        return [i for i, b in enumerate(self.books) if b is None and i not in self._reserved]

    def stored(self) -> list:
        return [b for b in self.books if b is not None]

    def returnable(self) -> list:
        """The stored books the robot can drive to a shelf for (those with a map position)."""
        return [b for b in self.stored() if b.has_pose]

    def slot_of(self, isbn: str) -> Optional[int]:
        return next((i for i, b in enumerate(self.books) if b is not None and b.isbn == isbn), None)

    # ---- changing it ----
    def reserve(self, preferred: Optional[int] = None) -> Optional[int]:
        """Promise a free slot to a book being stored: `preferred` if it is free, else the lowest free one.
        None if the ring is full."""
        free = self.free_slots()
        if not free:
            return None
        slot = preferred if preferred in free else free[0]
        self._reserved.add(slot)
        return slot

    def unreserve(self, slot: Optional[int]) -> None:
        self._reserved.discard(slot)

    def commit(self, slot: int, book: StoredBook) -> None:
        """The book is in the slot now: it counts as stored and is saved."""
        if not 0 <= slot < self.size:
            raise ValueError(f"slot must be 0..{self.size - 1}")
        if self.books[slot] is not None:
            raise ValueError(f"slot {slot} already holds {self.books[slot].title!r}")
        book.slot = slot
        self.books[slot] = book
        self._reserved.discard(slot)
        self.save()

    def release(self, slot: int) -> Optional[StoredBook]:
        """The book in the slot was taken out (put back on its shelf). Returns it."""
        book, self.books[slot] = self.books[slot], None
        self._reserved.discard(slot)
        self.save()
        return book

    def clear(self) -> None:
        self.books = [None] * self.size
        self._reserved.clear()
        self.save()

    # ---- the queue for putting books back ----
    def stops(self, first: Optional[str] = None) -> list:
        """The returnable books grouped by shelf, one ShelfStop per shelf, ordered by floor then shelf id, with
        the shelf `first` (the one the robot is heading for) moved to the front."""
        by_shelf: dict[str, ShelfStop] = {}
        for book in self.returnable():
            key = book.shelf or f"{book.map_x},{book.map_y}"
            stop = by_shelf.setdefault(key, ShelfStop(key, book.section, book.floor, book.zone,
                                                      book.map_x, book.map_y, book.map_yaw))
            stop.slots.append(book.slot)
            stop.titles.append(book.title)
        ordered = sorted(by_shelf.values(), key=lambda s: (s.floor if s.floor is not None else 99, s.shelf))
        if first is not None:
            ordered.sort(key=lambda s: s.shelf != first)                    # stable: only moves `first` forward
        return ordered

    # ---- saving ----
    def save(self) -> None:
        if self.path is None:
            return
        data = {"slots": self.size, "books": [asdict(b) if b else None for b in self.books]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".storage-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not save the storage to %s: %s", self.path, exc)

    def load(self) -> None:
        """Read the saved books. A missing file is an empty ring; an unreadable one is set aside (as .bad) and the
        ring starts empty, so a corrupt file never stops the robot starting. Books beyond the ring's size are kept
        out and logged."""
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            saved = data["books"]
            if not isinstance(saved, list):
                raise ValueError("books must be a list")
            books = [StoredBook(**b) if b else None for b in saved]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            bad = self.path.with_suffix(".bad")
            log.warning("the saved storage %s is unreadable (%s); starting empty, the file is kept as %s", self.path, exc, bad.name)
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            return
        for i, book in enumerate(books):
            if book is None:
                continue
            if i >= self.size:
                log.warning("saved slot %d is outside the %d-slot ring; %s is not loaded", i, self.size, book.title)
                continue
            book.slot = i
            self.books[i] = book
