"""How the robot reads the book on the pad, behind one interface so the way of reading can change.

A BookReader listens for BookPlaced on the bus, identifies the book, and publishes BookIdentified (it was read) or
ScanFailed (it could not be, with a reason). Nothing outside this module knows how: the state machine only sees
those two events.

    CameraBookReader   the camera scanner (perception/book_scanner.py): looks for an ISBN barcode in camera frames.
    RfidBookReader     a stub: raises "not implemented" so an RFID reader can be dropped in later by filling it in,
                       with no change anywhere else. Choose the reader with `books.reader` in config/robot.yaml.
"""

from __future__ import annotations


class BookReader:
    """The interface. A subclass subscribes itself to BookPlaced and publishes BookIdentified / ScanFailed."""

    def __init__(self, bus, finder):
        self.bus, self.finder = bus, finder

    @property
    def scanning(self) -> bool:
        """True while a read is in progress (a second BookPlaced is ignored until it finishes)."""
        return False

    async def wait(self) -> None:
        """Wait for the read in progress, if any, to finish."""
        return None


class RfidBookReader(BookReader):
    def __init__(self, bus, finder, **_):
        raise NotImplementedError(
            "RFID book reading is not implemented yet. Fill in robot/perception/book_reader.py RfidBookReader (read the "
            "tag, look the ISBN up with finder.find, publish BookIdentified or ScanFailed), or set books.reader: camera.")


def build_reader(kind: str, bus, camera, finder, timeout_s: float = 5.0) -> BookReader:
    """The reader `books.reader` names: "camera" (default) or "rfid". Raises ValueError for anything else and
    NotImplementedError (with what to do about it) for rfid."""
    kind = (kind or "camera").lower()
    if kind == "camera":
        from robot.perception.book_scanner import CameraBookReader
        return CameraBookReader(bus, camera, finder, timeout_s=timeout_s)
    if kind == "rfid":
        return RfidBookReader(bus, finder)
    raise ValueError(f"books.reader must be camera or rfid, got {kind!r}")
