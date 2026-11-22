"""Reading a book by its RFID tag, through the same events as the camera barcode scan.

The tag reader is an Arduino with an MFRC522 (rfid/rfid_uid.ino, the wiring is in docs/rfid.md) on a USB serial port. Every time a tag is held to it,
it sends one text line. This module turns those lines into the book on the pad:

    a line from the reader              what it is                        how the book is found
    "UID 04 65 B2 42 CD 21 90"          the tag's number (the usual case)  data/rfid_tags.csv: tag number -> ISBN (tools/rfid_register.py fills it)
    "04 65 B2 42 CD 21 90"              the same without the word UID
    "ISBN 9780132350884"                the ISBN written on the tag        used as it is
    "DUNE"                              a title (rfid/rfid_write.ino)       matched against the catalogue titles

Then RfidBookReader does exactly what the camera reader does after it has read a barcode: it publishes BookPlaced (the state machine goes to SCAN)
and BookIdentified with the same catalogue record (section, shelf, map pose ...), or ScanFailed with a reason if the tag is not registered or the
book is unknown. Nothing downstream can tell the two readers apart. With `books.reader: both` the camera and the RFID reader work side by side.

Run `python tools/rfid_register.py --listen` to see what the reader sends and how this module reads it.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

from robot.events import BookPlaced, ScanFailed
from robot.perception.book_reader import BookReader, identified_event
from robot.services.isbn import to_isbn13

log = logging.getLogger("robot.rfid")

UID_BYTES = (4, 7, 10)                       # the lengths of a tag number (MIFARE: 4, 7 or 10 bytes)
DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_BAUD = 9600


@dataclass(frozen=True)
class TagRead:
    kind: str          # "uid" | "isbn" | "title"
    value: str         # uid: the bytes as upper-case hex without separators; isbn: the ISBN-13; title: the text
    raw: str           # the line as it came

    @property
    def shown(self) -> str:
        """For the screen and the log: a tag number as 04 65 B2 ..., anything else as it is."""
        return " ".join(self.value[i:i + 2] for i in range(0, len(self.value), 2)) if self.kind == "uid" else self.value


def normalise_uid(text: str) -> str:
    """'04 65 b2-42:CD' -> '0465B242CD'."""
    return re.sub(r"[^0-9A-Fa-f]", "", text).upper()


def _uid_ok(hex_text: str) -> bool:
    return len(hex_text) % 2 == 0 and len(hex_text) // 2 in UID_BYTES


_HEX_ONLY = re.compile(r"^[0-9A-Fa-f]{2}(?:[ :-]?[0-9A-Fa-f]{2})*$")


def parse_line(line) -> Optional[TagRead]:
    """One line from the reader -> TagRead, or None for an empty line or one that is not a tag (a start-up message, a wrong ISBN)."""
    raw = line.decode("utf-8", errors="replace") if isinstance(line, (bytes, bytearray)) else str(line)
    text = raw.replace("\x00", "").strip()
    if not text:
        return None
    m = re.match(r"^ISBN[\s:=-]*(.+)$", text, re.I)
    if m:
        isbn = to_isbn13(m.group(1))
        if isbn is None:
            log.warning("the RFID reader sent an ISBN that is not valid: %r", text)
        return TagRead("isbn", isbn, raw) if isbn else None
    m = re.match(r"^(?:UID|TAG)[\s:=-]*(.+)$", text, re.I)
    if m:
        uid = normalise_uid(m.group(1))
        if _uid_ok(uid):
            return TagRead("uid", uid, raw)
        log.warning("the RFID reader sent a tag number of an odd length: %r", text)
        return None
    if _HEX_ONLY.match(text) and _uid_ok(normalise_uid(text)):
        return TagRead("uid", normalise_uid(text), raw)
    isbn = to_isbn13(text)
    if isbn:
        return TagRead("isbn", isbn, raw)
    if len(text) >= 2 and re.search(r"[A-Za-z]", text):
        return TagRead("title", text, raw)
    return None


# ---------------------------------------------------------------------------------------------- tag number -> ISBN
def load_tags(path) -> dict:
    """{uid: isbn} from a CSV with the columns uid, isbn (and optionally title, for people). Missing file = no tags. Bad rows are skipped with a warning."""
    tags: dict = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.reader(f) if r and not r[0].lstrip().startswith("#")]
    except FileNotFoundError:
        return tags
    if not rows:
        return tags
    header = [h.strip().lower() for h in rows[0]]
    if "uid" not in header or "isbn" not in header:
        log.warning("%s: the first row must name the columns uid and isbn", path)
        return tags
    ui, ii = header.index("uid"), header.index("isbn")
    for n, row in enumerate(rows[1:], 2):
        if len(row) <= max(ui, ii):
            continue
        uid, isbn = normalise_uid(row[ui]), to_isbn13(row[ii])
        if not _uid_ok(uid) or not isbn:
            log.warning("%s row %d: %r is not a tag number and a valid ISBN", path, n, row)
            continue
        tags[uid] = isbn
    return tags


def save_tag(path, uid: str, isbn: str, title: str = "") -> None:
    """Add or replace one tag in the CSV (the file and its header are made if they are not there)."""
    uid = normalise_uid(uid)
    if not _uid_ok(uid):
        raise ValueError(f"{uid!r} is not a tag number (4, 7 or 10 bytes of hex)")
    isbn13 = to_isbn13(isbn)
    if not isbn13:
        raise ValueError(f"{isbn!r} is not a valid ISBN")
    path = Path(path)
    rows = []
    if path.is_file():
        with open(path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.reader(f)]
    header = ["uid", "isbn", "title"]
    body = [r for r in rows[1:] if r and normalise_uid(r[0]) != uid] if rows and [h.strip().lower() for h in rows[0][:2]] == ["uid", "isbn"] else []
    body.append([" ".join(uid[i:i + 2] for i in range(0, len(uid), 2)), isbn13, title])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(body)


class TagTable:
    """data/rfid_tags.csv, read again whenever the file changes (a tag registered with tools/rfid_register.py works without a restart)."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self._tags: dict = {}
        self._stamp: object = "unset"

    def get(self, uid: str) -> Optional[str]:
        if self.path is None:
            return None
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp != self._stamp:
            self._stamp, self._tags = stamp, load_tags(self.path)
        return self._tags.get(uid)


# ---------------------------------------------------------------------------------------------- the serial port
class SerialTagSource:
    """The lines the reader sends, from a USB serial port, as an async iterator. A missing or unplugged reader is not an error for the robot: it is
    reported once and tried again every `retry_s` seconds (the camera reader keeps working meanwhile)."""

    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD, retry_s: float = 10.0, opener: Optional[Callable] = None):
        self.port, self.baud, self.retry_s = port, int(baud), float(retry_s)
        self._opener = opener or self._open_serial
        self.connected = False

    @classmethod
    def from_config(cls, cfg: dict) -> "SerialTagSource":
        return cls(str(cfg.get("port") or DEFAULT_PORT), int(cfg.get("baud") or DEFAULT_BAUD), float(cfg.get("retry_s", 10.0)))

    def _open_serial(self):
        import serial                                      # pyserial: pip install pyserial (pyproject extra `serial`)
        return serial.Serial(port=self.port, baudrate=self.baud, timeout=1.0)

    async def lines(self) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        warned = False

        def reader(handle):
            try:
                while not stop.is_set():
                    line = handle.readline()
                    if line:
                        loop.call_soon_threadsafe(queue.put_nowait, line)
            except Exception as exc:                        # unplugged: end this connection, the loop below reconnects
                loop.call_soon_threadsafe(queue.put_nowait, exc)

        try:
            while True:
                try:
                    handle = self._opener()
                except Exception as exc:                    # not plugged in, no permission, pyserial missing
                    (log.debug if warned else log.warning)("RFID reader not available on %s (%s); trying again every %g s", self.port, exc, self.retry_s)
                    warned = True
                    await asyncio.sleep(self.retry_s)
                    continue
                self.connected, warned = True, False
                log.info("RFID reader connected on %s", self.port)
                thread = threading.Thread(target=reader, args=(handle,), daemon=True, name="rfid-serial")
                thread.start()
                try:
                    while True:
                        item = await queue.get()
                        if isinstance(item, Exception):
                            log.warning("RFID reader on %s stopped: %s", self.port, item)
                            break
                        yield item
                finally:
                    self.connected = False
                    stop.set()
                    try:
                        handle.close()
                    except Exception:
                        pass
                    stop.clear()
                    queue = asyncio.Queue()
                await asyncio.sleep(self.retry_s)
        finally:
            stop.set()


# ---------------------------------------------------------------------------------------------- the reader
class RfidBookReader(BookReader):
    """Tag held to the reader -> BookPlaced, then BookIdentified or ScanFailed: the camera reader's events, for the same state machine.

    source: an object with `lines()`, an async iterator of lines (SerialTagSource, or a queue in tests).
    tags: path of the CSV with tag number -> ISBN. gap_s: a tag that keeps being read less than this far apart counts as ONE presentation
    (the reader sends the tag again while it stays on it); after a gap, the same tag is a new book.
    """

    def __init__(self, bus, finder, source, tags_path=None, gap_s: float = 2.5, clock: Callable[[], float] = time.monotonic):
        super().__init__(bus, finder)
        self.source, self.tags, self.gap_s, self.clock = source, TagTable(tags_path), float(gap_s), clock
        self._busy = False
        self._last: Optional[tuple] = None             # (value, time last seen)
        self.last_read: Optional[TagRead] = None

    @property
    def scanning(self) -> bool:
        return self._busy

    @property
    def rfid(self) -> "RfidBookReader":                  # the same name a CompositeBookReader gives its RFID part (the sim's `tag` command)
        return self

    async def run(self) -> None:
        async for line in self.source.lines():
            await self.handle(line)

    async def handle(self, line) -> Optional[object]:
        """One line from the reader. Returns the event published (BookIdentified or ScanFailed), or None if it was ignored."""
        read = parse_line(line)
        if read is None:
            log.debug("RFID reader said %r: not a tag", line)
            return None
        now = self.clock()
        same = self._last is not None and self._last[0] == (read.kind, read.value) and now - self._last[1] < self.gap_s
        self._last = ((read.kind, read.value), now)
        if same:
            return None                                # the same tag, still on the reader
        if self._busy or not self.ready():
            log.info("RFID tag %s ignored: the robot is busy (hold it to the reader again in a moment)", read.shown)
            self._last = None
            return None
        self._busy = True
        try:
            self.last_read = read
            result = await self._identify(read)
            await self.bus.publish(BookPlaced(source="rfid"))          # the state machine goes to SCAN, as it does for the camera
            await self.bus.publish(result)
            return result
        finally:
            self._busy = False

    async def _identify(self, read: TagRead):
        loop = asyncio.get_running_loop()
        try:
            if read.kind == "isbn":
                isbn = read.value
            elif read.kind == "uid":
                isbn = self.tags.get(read.value)
                if isbn is None:
                    return ScanFailed(f"RFID tag {read.shown} is not registered to a book yet: run  python tools/rfid_register.py  and hold it to the reader")
            else:
                matches = self.finder.titles_in(read.value)
                if not matches:
                    return ScanFailed(f"RFID tag says {read.value!r}, which is not a title in the catalogue")
                isbn = matches[0].isbn13
            book = await loop.run_in_executor(None, self.finder.find, isbn)
            log.info("RFID tag %s -> %s %s", read.shown, isbn, book.title if book else "(not in the catalogue)")
            return identified_event(self.finder, isbn, book)
        except Exception as exc:                        # the catalogue or the network failed: a failed read, not a crashed robot
            log.exception("RFID read failed")
            return ScanFailed(f"could not look up the RFID tag {read.shown}: {exc}")
