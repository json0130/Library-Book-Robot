"""Try the book barcode scanner without the robot.

    python tools/book_scan_test.py --list                  # the catalogue
    python tools/book_scan_test.py --image photo.jpg       # decode a photo, show the catalogue match
    python tools/book_scan_test.py --live                  # webcam with an overlay, q quits
    python tools/book_scan_test.py --live --append isbns.txt   # collect ISBNs by scanning a pile of books

--append FILE adds every newly scanned ISBN to FILE, one per line, never twice (ISBNs already in the file count as
seen, so you can stop and carry on later), with a short beep and a printed line showing the book's title from the
catalogue or Open Library when there is one. The file is the input of tools/import_books.py.

Needs pyzbar and OpenCV: sudo apt install libzbar0 python3-opencv && pip install pyzbar
Test pictures: python tools/make_test_barcodes.py
"""

from __future__ import annotations

import argparse
import io
import math
import struct
import sys
import threading
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/...`

from robot.app import build_finder  # noqa: E402
from robot.config import load_config  # noqa: E402
from robot.perception.book_scanner import DecoderUnavailable, decode_isbn  # noqa: E402
from robot.services.books import CatalogError, OpenLibrary  # noqa: E402
from robot.services.isbn import to_isbn13  # noqa: E402


class IsbnCollector:
    """The ISBNs collected in a text file, one per line. Reads what is already there (comments and blank lines
    are kept as they are), adds new ISBNs on the end as they come, and never writes one twice."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.seen: set = set()
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                code = to_isbn13(line.split("#", 1)[0].strip())
                if code:
                    self.seen.add(code)

    def __len__(self) -> int:
        return len(self.seen)

    def add(self, isbn: str) -> bool:
        """Append the ISBN (as ISBN-13) unless it is already collected or not valid. True if it was new."""
        code = to_isbn13(isbn)
        if code is None or code in self.seen:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_newline = self.path.is_file() and self.path.stat().st_size > 0 and not self.path.read_bytes().endswith(b"\n")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(("\n" if needs_newline else "") + code + "\n")
        self.seen.add(code)
        return True


def beep_wav(seconds: float = 0.12, hz: float = 880.0, rate: int = 22050) -> bytes:
    """A short, soft beep as a WAV (16-bit mono), with a quick fade so it doesn't click."""
    frames = int(seconds * rate)
    samples = (int(9000 * math.sin(2 * math.pi * hz * i / rate) * min(1.0, i / 200, (frames - i) / 200))
               for i in range(frames))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", s) for s in samples))
    return buf.getvalue()


class Beeper:
    """Plays beep_wav() through the robot's speaker without holding anything up; if there is no usable speaker,
    rings the terminal bell instead."""

    def __init__(self, audio=None):
        self.audio, self.wav = audio, beep_wav()

    @classmethod
    def from_config(cls, cfg) -> "Beeper":
        from robot.drivers.audio import Audio
        try:
            return cls(Audio(cfg.robot.get("devices", {}).get("speaker")))
        except (RuntimeError, ValueError, OSError):
            return cls(None)

    def beep(self) -> None:
        def play():
            try:
                self.audio.play(self.wav, 0.6)
            except Exception:                                  # a missing speaker must not stop the scanning
                print("\a", end="", flush=True)
        if self.audio is None:
            print("\a", end="", flush=True)
        else:
            threading.Thread(target=play, daemon=True).start()


def title_of(finder, online: OpenLibrary | None, isbn: str) -> str | None:
    """The book's title from the catalogue, else Open Library (cached, 3 s timeout, never raises), else None."""
    book = finder.catalog.lookup(isbn) or (online.lookup(isbn) if online is not None else None)
    return book.title if book else None


def register(isbn: str, collector: IsbnCollector, beeper: Beeper | None) -> bool:
    """A scanned ISBN goes into the collector's file now, with a beep if it is new. True if it was new."""
    new = collector.add(isbn)
    if new and beeper is not None:
        beeper.beep()
    return new


def announcement(isbn: str, new: bool, collector: IsbnCollector, finder, online: OpenLibrary | None) -> str:
    """The line printed for a collected ISBN: "+ <isbn>  <title>" if new, "= ..." if it was already there."""
    title = title_of(finder, online, isbn)
    note = "" if new else f"  (already in {collector.path})"
    return f"{'+' if new else '='} {isbn}  {title or '(no title found)'}{note}"


def describe(isbn: str, book) -> str:
    if book is None:
        return f"ISBN {isbn}: not in the catalogue and not found online"
    where = "catalogue" if book.found_in_catalog else "online (Open Library)"
    lines = [f"ISBN {isbn}: {book.title} by {book.author or 'unknown'}  [{where}]"]
    if book.found_in_catalog:
        lines.append(f"  shelf {book.shelf}, storage slot {book.slot}")
        lines.append(f"  {book.summary}")
    return "\n".join(lines)


def run_image(finder, path: Path) -> int:
    import cv2
    frame = cv2.imread(str(path))
    if frame is None:
        print(f"error: cannot read an image from {path}", file=sys.stderr)
        return 1
    isbn = decode_isbn(frame)
    if isbn is None:
        print("No ISBN barcode found.")
        return 1
    print(describe(isbn, finder.find(isbn)))
    return 0


def run_live(finder, camera_index: int, size: tuple | None = None, collector: IsbnCollector | None = None,
             beeper: Beeper | None = None, online: OpenLibrary | None = None) -> int:
    """Webcam view with the scanner running beside it.

    The picture is drawn at the camera's own rate from its newest frame; decoding happens on a
    second thread, on the newest frame whenever it is free, so a slow decode never holds the
    picture back or makes it lag.

    With a collector, each new valid ISBN is appended to its file straight away and beeps; its title (from the
    catalogue, else Open Library) is looked up and printed on another thread, so a slow lookup never delays
    the next scan.
    """
    import time
    import cv2
    from robot.drivers.camera import Camera
    dev = load_config().robot.get("devices", {})
    width, height = size or (dev.get("camera_width"), dev.get("camera_height"))
    camera = Camera(camera_index, width, height, background=True)
    print(f"Camera {camera_index}: {camera.width}x{camera.height}")
    state = {"label": "looking for a barcode...", "last": None, "busy": False, "decode_ms": 0.0}

    def announce(isbn: str, new: bool) -> None:
        """Print the collected ISBN with its title (catalogue first, then Open Library) when there is one."""
        line = announcement(isbn, new, collector, finder, online)
        print(line, flush=True)
        state["label"] = f"{line}  [{len(collector)} collected]"

    def scan(frame):
        t0 = time.perf_counter()
        try:
            isbn = decode_isbn(frame)
            if isbn and isbn != state["last"] and collector is not None:
                state["last"] = isbn
                new = register(isbn, collector, beeper)
                state["label"] = f"{isbn}  looking up the title..."
                threading.Thread(target=announce, args=(isbn, new), daemon=True).start()
            elif isbn and isbn != state["last"]:
                state["last"] = isbn
                book = finder.find(isbn)
                print(describe(isbn, book), flush=True)
                state["label"] = f"{isbn}  {book.title if book else 'unknown book'}"
        except Exception as exc:                   # keep the view alive; say what went wrong
            state["label"] = f"scan error: {exc}"
        state["decode_ms"] = (time.perf_counter() - t0) * 1000
        state["busy"] = False

    print("Hold a book's barcode up to the camera. Press q in the window to quit.")
    shown, started = 0, time.perf_counter()
    try:
        while True:
            frame = camera.read_frame()
            if not state["busy"]:
                state["busy"] = True
                threading.Thread(target=scan, args=(frame.copy(),), daemon=True).start()
            shown += 1
            fps = shown / max(time.perf_counter() - started, 1e-6)
            view = frame.copy()
            cv2.putText(view, state["label"], (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 0), 2)
            cv2.putText(view, f"{fps:.0f} fps, scan {state['decode_ms']:.0f} ms", (10, view.shape[0] - 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            cv2.imshow("book scanner", view)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                return 0
            if shown == 60:                        # report the rate over a fresh window
                shown, started = 0, time.perf_counter()
    finally:
        camera.close()
        cv2.destroyAllWindows()


def parse_size(text: str) -> tuple:
    try:
        w, h = text.lower().split("x")
        return int(w), int(h)
    except ValueError:
        raise argparse.ArgumentTypeError(f"size must look like 640x480, got {text!r}") from None


def main(argv=None) -> int:
    cfg = load_config()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--image", type=Path, help="decode this photo")
    mode.add_argument("--live", action="store_true", help="decode the webcam")
    mode.add_argument("--list", action="store_true", help="print the catalogue")
    p.add_argument("--camera", type=int, default=int(cfg.books["scanner_camera_index"]), help="webcam index")
    p.add_argument("--append", type=Path, metavar="FILE",
                   help="with --live: add each newly scanned ISBN to FILE (one per line, no duplicates), beep, and\n"
                        "print its title; the file works as the input of tools/import_books.py")
    p.add_argument("--size", type=parse_size, metavar="WxH",
                   help="camera resolution for --live, e.g. 640x480 (smooth, 30 fps) or 1280x720 (sharper, "
                        "about 8 fps on this webcam); default from config/robot.yaml")
    args = p.parse_args(argv)
    if args.append and not args.live:
        p.error("--append only works with --live")
    try:
        finder = build_finder(cfg)
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.list:
        for b in finder.catalog.books:
            print(f"{b.isbn13}  {b.title} ({b.author}) [{b.genre}] shelf {b.shelf}, slot {b.slot}")
        return 0
    try:
        if args.image:
            return run_image(finder, args.image)
        collector = IsbnCollector(args.append) if args.append else None
        if collector is not None:
            print(f"Collecting ISBNs into {args.append} ({len(collector)} already there). New ones beep.", flush=True)
        return run_live(finder, args.camera, args.size, collector,
                        Beeper.from_config(cfg) if collector is not None else None,
                        OpenLibrary(cfg.path(cfg.books["cache_dir"])) if collector is not None else None)
    except DecoderUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
