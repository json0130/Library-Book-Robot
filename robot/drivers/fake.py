"""Fake drivers with the same methods as the real ones; they log what they would do."""

from __future__ import annotations

import logging

log = logging.getLogger("robot.fake")


class FakeEsp32:
    def __init__(self, port: str = "fake", baud: int = 115200):
        self.sent: list[tuple[str, dict]] = []

    def send(self, cmd: str, **params) -> None:
        self.sent.append((cmd, params))
        args = " ".join(f"{k}={v}" for k, v in params.items())
        log.info("[esp32] %s %s", cmd, args)

    def close(self) -> None:
        pass


class FakeAudio:
    def __init__(self, speaker=None, mic=None):
        self.played: list[tuple[int, float]] = []

    def play(self, wav: bytes, volume: float = 1.0) -> None:
        self.played.append((len(wav), volume))
        log.info("[audio] play %d bytes at volume %.2f", len(wav), volume)

    def record(self, seconds: float) -> bytes:
        log.info("[audio] record %.1fs (fake: silence)", seconds)
        return b""


class FakeFrame:
    """A pretend camera frame that carries the ISBN of the pretend book on the pad (or None)."""

    def __init__(self, isbn=None):
        self.isbn = isbn


class FakeCamera:
    """Shows whatever it was told to: show(isbn=...) puts a pretend book with that barcode on the pad,
    show(image=...) a real image (numpy array) that the real decoder will read, clear() an empty pad.

    `static` tells the scanner the picture will not change, so it gives up after one frame
    instead of waiting for the timeout.
    """

    static = True

    def __init__(self, index: int = 0):
        self._isbn = None
        self._image = None

    def show(self, isbn=None, image=None) -> None:
        self._isbn, self._image = isbn, image

    def clear(self) -> None:
        self.show()

    def read_frame(self):
        log.info("[camera] read frame (fake)")
        return self._image if self._image is not None else FakeFrame(self._isbn)

    @staticmethod
    def decode(frame):
        """Barcode decoder for fake frames; real images go through the real decoder."""
        if isinstance(frame, FakeFrame):
            return frame.isbn
        from robot.perception.book_scanner import decode_isbn
        return decode_isbn(frame)

    def read_jpeg(self, quality: int = 85) -> bytes:
        log.info("[camera] read frame (fake: empty)")
        return b""

    def close(self) -> None:
        pass
