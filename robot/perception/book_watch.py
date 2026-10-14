"""Starts a scan when a book's barcode is shown to the camera: a stand-in for the pad's weight sensor.

The real robot will publish BookPlaced when the pad's weight sensor fires (see book_scanner.py). Until
then, with only a camera, this watches it several times a second and publishes the same BookPlaced when
it sees a valid ISBN barcode, so holding a book up is enough. BookScanner then does the full read.

A book that is still in view doesn't start another scan: it has to leave the view for `rearm_after_s`
seconds, or a different ISBN has to appear. Nothing starts while a scan or an action is in progress.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from robot.events import BookPlaced
from robot.perception.book_scanner import DecoderUnavailable, quick_isbn

log = logging.getLogger("robot.bookwatch")


class BookWatcher:
    def __init__(self, bus, camera, ready: Callable[[], bool], interval_s: float = 0.4,
                 rearm_after_s: float = 3.0, probe: Callable = quick_isbn, clock=time.monotonic):
        """ready(): True when the robot can take a new book (the dialogue is not scanning or acting)."""
        self.bus, self.camera, self.ready = bus, camera, ready
        self.interval_s, self.rearm_after_s = interval_s, rearm_after_s
        self.probe, self.clock = probe, clock
        self.current: Optional[str] = None      # the ISBN that last started a scan and is still in view
        self._last_seen = 0.0

    def _look(self) -> Optional[str]:
        return self.probe(self.camera.peek())

    async def step(self) -> Optional[str]:
        """Look once; returns the ISBN if this look started a scan. Raises OSError if the camera fails,
        DecoderUnavailable if pyzbar is missing."""
        isbn = await asyncio.get_running_loop().run_in_executor(None, self._look)
        now = self.clock()
        if isbn is None:
            if self.current is not None and now - self._last_seen >= self.rearm_after_s:
                self.current = None
            return None
        self._last_seen = now
        if isbn == self.current or not self.ready():
            return None
        self.current = isbn
        log.info("[camera] barcode %s in view: scanning", isbn)
        await self.bus.publish(BookPlaced())
        return isbn

    async def run(self) -> None:
        """Watch forever. A camera hiccup is retried; a missing decoder library stops the watch."""
        last_error = None
        while True:
            try:
                await self.step()
                last_error = None
            except DecoderUnavailable as exc:
                log.warning("[camera] can't watch for barcodes: %s", exc)
                return
            except OSError as exc:
                if str(exc) != last_error:
                    log.warning("[camera] barcode watch failed (%s); will keep trying", exc)
                    last_error = str(exc)
            await asyncio.sleep(self.interval_s)
