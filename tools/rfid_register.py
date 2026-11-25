"""Register RFID tags to books, and see what the tag reader sends.

    python tools/rfid_register.py --listen                      print every line the reader sends and how the robot reads it (Ctrl-C ends)
    python tools/rfid_register.py                               hold a tag to the reader, then type the book's ISBN (or its title): saved in data/rfid_tags.csv
    python tools/rfid_register.py --isbn 9780132350884          the next tag held to the reader is registered to that ISBN
    python tools/rfid_register.py --uid "04 65 B2 42 CD 21 90" --isbn 9780132350884     no reader needed: write the pair straight into the file
    python tools/rfid_register.py --port /dev/ttyACM1 --list    which tags are registered

The robot reads data/rfid_tags.csv again by itself when it changes, so a new tag works without a restart. Stop the robot first if it has the serial
port open (only one program can read it): the tool says so if the port is busy.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from robot.perception.rfid import DEFAULT_BAUD, DEFAULT_PORT, load_tags, normalise_uid, parse_line, save_tag     # noqa: E402
from robot.services.isbn import to_isbn13                                                                             # noqa: E402

TAGS = ROOT / "data" / "rfid_tags.csv"


def read_one(read_line: Callable, say: Callable, timeout_s: float = 60.0):
    """Wait for a tag number (uid) from the reader: the first line that parses as one. None on timeout."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        line = read_line()
        read = parse_line(line) if line else None
        if read is not None and read.kind == "uid":
            return read
        if read is not None:
            say(f"  the reader sent {read.kind} {read.value!r}, not a tag number (rfid_uid.ino sends the number): ignoring")
    return None


def isbn_for(text: str, finder=None) -> Optional[str]:
    """What the person typed -> an ISBN-13: an ISBN, or (with the catalogue) a title."""
    isbn = to_isbn13(text)
    if isbn or finder is None:
        return isbn
    found = finder.titles_in(text)
    return found[0].isbn13 if found else None


def register(read_line: Callable, ask: Callable, say: Callable, tags_path=TAGS, isbn: Optional[str] = None, finder=None, timeout_s: float = 60.0) -> int:
    say("Hold the tag to the reader ...")
    read = read_one(read_line, say, timeout_s)
    if read is None:
        say("no tag number arrived in time: is the reader plugged in and rfid_uid.ino on it?  (--listen shows what it sends)")
        return 1
    known = load_tags(tags_path).get(read.value)
    say(f"tag {read.shown}" + (f" is already registered to {known}" if known else " is new"))
    book = isbn
    while book is None:
        text = ask("ISBN or title of the book this tag is on (Enter = skip): ").strip()
        if not text:
            say("not saved")
            return 1
        book = isbn_for(text, finder)
        if book is None:
            say("  that is not a valid ISBN" + (" or a title in the catalogue" if finder else "") + ", try again")
    title = ""
    if finder is not None:
        found = finder.find(book)
        title = found.title if found else ""
    save_tag(tags_path, read.value, book, title)
    say(f"saved: {read.shown} -> {book}" + (f" ({title})" if title else "") + f" in {tags_path}")
    return 0


def open_port(port: str, baud: int):
    import serial
    ser = serial.Serial(port=port, baudrate=baud, timeout=1.0)
    return ser


def main(argv=None, ask: Callable = input, say: Callable = print, opener: Optional[Callable] = None) -> int:
    ap = argparse.ArgumentParser(description="Register RFID tags to books.")
    ap.add_argument("--port", default=DEFAULT_PORT, help=f"the reader's serial port (default {DEFAULT_PORT})")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--tags", default=str(TAGS), help="the CSV to write (default data/rfid_tags.csv)")
    ap.add_argument("--listen", action="store_true", help="only show what the reader sends")
    ap.add_argument("--list", action="store_true", help="show the registered tags")
    ap.add_argument("--isbn", help="register the next tag to this ISBN (with --uid: no reader needed)")
    ap.add_argument("--uid", help="the tag number, instead of reading it from the reader")
    ap.add_argument("--no-catalog", action="store_true", help="do not load the catalogue (then only an ISBN can be typed, not a title)")
    args = ap.parse_args(argv)
    if args.list:
        tags = load_tags(args.tags)
        for uid, isbn in sorted(tags.items()):
            say(f"{' '.join(uid[i:i + 2] for i in range(0, len(uid), 2)):<24} {isbn}")
        say(f"{len(tags)} tag(s) in {args.tags}")
        return 0
    if args.uid:
        if not args.isbn:
            say("--uid needs --isbn")
            return 2
        try:
            save_tag(args.tags, args.uid, args.isbn)
        except ValueError as exc:
            say(f"not saved: {exc}")
            return 2
        say(f"saved: {normalise_uid(args.uid)} -> {to_isbn13(args.isbn)} in {args.tags}")
        return 0
    try:
        handle = (opener or open_port)(args.port, args.baud)
    except Exception as exc:
        say(f"cannot open the reader on {args.port}: {exc}\n(plugged in? another program, such as the robot or rfid/rfid_read.py, may have the port: stop it first)")
        return 2
    read_line = lambda: handle.readline()
    try:
        if args.listen:
            say(f"listening on {args.port} at {args.baud} baud (Ctrl-C ends). Hold a tag to the reader.")
            try:
                while True:
                    line = read_line()
                    if line:
                        read = parse_line(line)
                        say(f"{line!r:<40} -> " + (f"{read.kind}: {read.shown}" if read else "not a tag (ignored)"))
            except KeyboardInterrupt:
                return 0
        finder = None
        if not args.no_catalog:
            try:
                from robot.app import build_finder
                from robot.config import load_config
                finder = build_finder(load_config())
            except Exception as exc:
                say(f"(catalogue not loaded: {exc}; type an ISBN)")
        return register(read_line, ask, say, args.tags, args.isbn, finder)
    finally:
        try:
            handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
