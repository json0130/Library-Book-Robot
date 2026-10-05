"""Reads the ISBN barcode of the book on the pad: BookPlaced -> scan -> BookScanned or ScanFailed.

    decode_isbn(frame)    one camera frame -> ISBN-13 string or None (needs pyzbar and OpenCV)
    BookScanner           listens for BookPlaced, grabs frames until a valid ISBN is read or the
                          timeout runs out, then publishes BookScanned (with the catalogue record)
                          or ScanFailed

Hook for the hardware: the pad's weight sensor (via the ESP32 driver, not built yet) only has to
`await bus.publish(BookPlaced())`. Nothing here needs to change when that arrives.

pyzbar and OpenCV are imported inside decode_isbn, so the simulator and tests that use fake
frames run without them. Install: sudo apt install libzbar0 python3-opencv && pip install pyzbar
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Iterator, Optional

from robot.events import BookPlaced, BookScanned, ScanFailed
from robot.services.isbn import to_isbn13

log = logging.getLogger("robot.scanner")

IGNORED_SYMBOLS = {"EAN2", "EAN5"}      # price add-ons are not the book's code


class DecoderUnavailable(RuntimeError):
    """pyzbar or OpenCV is not installed."""


def _load_decoder():
    try:
        import cv2
        from pyzbar import pyzbar
    except ImportError as exc:
        raise DecoderUnavailable(
            f"{exc}. Install with: sudo apt install libzbar0 python3-opencv && pip install pyzbar") from exc
    return cv2, pyzbar


def _variants(frame, cv2) -> Iterator[tuple[str, object]]:
    """The images to try, cheapest first: gray, contrast boosted, upscaled, then rotated copies."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if getattr(frame, "ndim", 2) == 3 else frame
    yield "grayscale", gray
    yield "clahe", cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    yield "upscaled 2x", cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    for name, code in (("rotated 90", cv2.ROTATE_90_CLOCKWISE), ("rotated 180", cv2.ROTATE_180),
                       ("rotated 270", cv2.ROTATE_90_COUNTERCLOCKWISE)):
        yield name, cv2.rotate(gray, code)


def decode_isbn(frame) -> Optional[str]:
    """The ISBN-13 of the first valid ISBN barcode in a frame, or None.

    Tries the plain grayscale image, a contrast-boosted one, a 2x upscale, then the 90, 180 and 270
    degree rotations (books lie in any orientation). Other codes, like library stickers, are
    logged at debug level and ignored. Raises DecoderUnavailable if pyzbar or OpenCV is missing.
    """
    cv2, pyzbar = _load_decoder()
    for name, image in _variants(frame, cv2):
        for symbol in pyzbar.decode(image):
            if symbol.type in IGNORED_SYMBOLS:
                continue
            text = symbol.data.decode("ascii", errors="replace")
            isbn = to_isbn13(text)
            if isbn:
                log.debug("ISBN %s found (%s, %s)", isbn, symbol.type, name)
                return isbn
            log.debug("ignoring non-ISBN %s code %r (%s)", symbol.type, text, name)
    return None


class BookScanner:
    """Scans when a book is placed. Frames come from `camera.read_frame()`.

    finder: services.books.BookFinder (catalogue, then the online fallback).
    decode: frame -> ISBN or None. Defaults to the camera's own `decode` if it has one (the fake
    camera does), else decode_isbn.
    """

    def __init__(self, bus, camera, finder, timeout_s: float = 5.0, poll_s: float = 0.1,
                 decode: Optional[Callable] = None):
        self.bus, self.camera, self.finder = bus, camera, finder
        self.timeout_s, self.poll_s = timeout_s, poll_s
        self.decode = decode or getattr(camera, "decode", decode_isbn)
        self._task: Optional[asyncio.Task] = None
        bus.subscribe(BookPlaced, self.on_book_placed)

    async def on_book_placed(self, event: BookPlaced) -> None:
        """Start a scan in the background so the publisher (the weight sensor) is not held up."""
        if self.scanning:
            log.debug("book placed during a scan; ignored")
            return
        self._task = asyncio.create_task(self.scan())

    @property
    def scanning(self) -> bool:
        return self._task is not None and not self._task.done()

    async def wait(self) -> None:
        """Wait for the scan in progress (if any) to finish."""
        if self._task is not None:
            await asyncio.shield(self._task)

    async def scan(self):
        """Look for a barcode for up to timeout_s; publish and return BookScanned or ScanFailed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        reason = f"no ISBN barcode found within {self.timeout_s:g} s"
        try:
            while True:
                frame = await loop.run_in_executor(None, self.camera.read_frame)
                isbn = await loop.run_in_executor(None, self.decode, frame)
                if isbn:
                    book = await loop.run_in_executor(None, self.finder.find, isbn)
                    result = self._scanned(isbn, book)
                    break
                if getattr(self.camera, "static", False) or loop.time() >= deadline:
                    result = ScanFailed(reason)
                    break
                await asyncio.sleep(self.poll_s)
        except DecoderUnavailable as exc:
            result = ScanFailed(f"barcode decoding unavailable: {exc}")
        except (OSError, RuntimeError) as exc:
            log.exception("scan failed")
            result = ScanFailed(f"camera error: {exc}")
        await self.bus.publish(result)
        return result

    @staticmethod
    def _scanned(isbn: str, book) -> BookScanned:
        if book is None:                           # a valid ISBN nobody knows: still a successful read
            return BookScanned(isbn=isbn)
        return BookScanned(isbn=isbn, title=book.title, author=book.author or None, summary=book.summary,
                           shelf=book.shelf, slot=book.slot, found_in_catalog=book.found_in_catalog)
