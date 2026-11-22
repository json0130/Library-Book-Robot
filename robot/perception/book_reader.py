"""How the robot reads the book on the pad, behind one interface so the way of reading can change.

A BookReader identifies the book on the pad and publishes BookIdentified (it was read) or ScanFailed (it could not be, with a reason).
Nothing outside this module knows how: the state machine only sees BookPlaced, then one of those two events.

    CameraBookReader   the camera scanner (perception/book_scanner.py): BookPlaced -> looks for an ISBN barcode in camera frames.
    RfidBookReader     an RFID reader on a serial port (perception/rfid.py): a tag held to it -> BookPlaced + BookIdentified.
    CompositeBookReader  both at once (`books.reader: both`): the same events whichever way the book was read.

Choose the reader with `books.reader` (camera | rfid | both) in config/robot.yaml.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional


class BookReader:
    """The interface. A subclass publishes BookIdentified / ScanFailed (the camera one after BookPlaced, the RFID one together with it)."""

    def __init__(self, bus, finder):
        self.bus, self.finder = bus, finder
        self.ready: Callable[[], bool] = lambda: True

    def set_ready(self, ready: Callable[[], bool]) -> None:
        """`ready()` says whether the robot can take a book now (the state machine is in a state that expects one)."""
        self.ready = ready

    @property
    def scanning(self) -> bool:
        """True while a read is in progress (a second BookPlaced is ignored until it finishes)."""
        return False

    async def wait(self) -> None:
        """Wait for the read in progress, if any, to finish."""
        return None

    async def run(self) -> None:
        """Whatever has to keep running in the background (the RFID reader's serial port). The camera reader needs nothing."""
        return None


def identified_event(finder, isbn: str, book):
    """The BookIdentified for an ISBN and the catalogue's answer (a Book, or None for a valid ISBN nobody knows). Both readers build it here,
    so a book read by RFID and the same book read by barcode are the same event: section, shelf, map pose and all."""
    from robot.events import BookIdentified
    if book is None:
        return BookIdentified(isbn=isbn)
    place = finder.locate(book.isbn13) if book.found_in_catalog else None          # section, floor, pose...
    return BookIdentified(isbn=isbn, title=book.title, author=book.author or None, summary=book.summary,
                          shelf=(place.shelf if place else None) or book.shelf, slot=book.slot,
                          found_in_catalog=book.found_in_catalog, call_number=book.call_number or None,
                          section=place.section if place else None, floor=place.floor if place else None,
                          zone=place.zone if place else None, map_x=place.map_x if place else None,
                          map_y=place.map_y if place else None, map_yaw=place.map_yaw if place else None)


class CompositeBookReader(BookReader):
    """Several readers behind the one interface: busy while any of them is, `run` runs them all."""

    def __init__(self, bus, finder, readers):
        super().__init__(bus, finder)
        self.readers = list(readers)
        self.rfid = next((r for r in self.readers if hasattr(r, "handle")), None)       # the RFID reader, if there is one (the sim's `tag` command)

    def set_ready(self, ready) -> None:
        super().set_ready(ready)
        for reader in self.readers:
            reader.set_ready(ready)

    @property
    def scanning(self) -> bool:
        return any(r.scanning for r in self.readers)

    async def wait(self) -> None:
        await asyncio.gather(*(r.wait() for r in self.readers))

    async def run(self) -> None:
        await asyncio.gather(*(r.run() for r in self.readers))


def __getattr__(name):                                  # `from robot.perception.book_reader import RfidBookReader` still works
    if name == "RfidBookReader":
        from robot.perception.rfid import RfidBookReader
        return RfidBookReader
    raise AttributeError(name)


def build_reader(kind: str, bus, camera, finder, timeout_s: float = 5.0, rfid: Optional[dict] = None, rfid_source=None,
                 path: Callable[[str], object] = lambda p: p) -> Optional[BookReader]:
    """The reader `books.reader` names: "camera" (default), "rfid" or "both". Raises ValueError for anything else.
    rfid: the `books.rfid` settings (port, baud, tags_path, gap_s); rfid_source replaces the serial port (tests, a demo); path resolves
    a relative file name. The camera reader needs `camera`; with `both` and no camera only the RFID reader is made."""
    kind = (kind or "camera").lower()
    if kind not in ("camera", "rfid", "both"):
        raise ValueError(f"books.reader must be camera, rfid or both, got {kind!r}")
    readers = []
    if kind in ("camera", "both") and camera is not None:
        from robot.perception.book_scanner import CameraBookReader
        readers.append(CameraBookReader(bus, camera, finder, timeout_s=timeout_s))
    if kind in ("rfid", "both"):
        from robot.perception.rfid import RfidBookReader, SerialTagSource
        cfg = rfid or {}
        source = rfid_source or SerialTagSource.from_config(cfg)
        readers.append(RfidBookReader(bus, finder, source, path(cfg.get("tags_path") or "data/rfid_tags.csv"), gap_s=float(cfg.get("gap_s", 2.5))))
    if not readers:
        return None
    return readers[0] if len(readers) == 1 else CompositeBookReader(bus, finder, readers)
