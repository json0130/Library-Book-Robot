"""RFID book reading: the tag reader's lines, tag -> ISBN, and that a tag read produces the same events as a camera barcode scan."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from robot.bus import Bus
from robot.drivers.fake import FakeCamera
from robot.events import BookIdentified, BookPlaced, ScanFailed
from robot.perception import rfid
from robot.perception.book_reader import CompositeBookReader, build_reader
from robot.perception.book_scanner import BookScanner
from robot.perception.rfid import RfidBookReader, SerialTagSource, TagTable, load_tags, normalise_uid, parse_line, save_tag
from robot.services.books import BookFinder
from tests.helpers.sample import sample_graph

HITCH = "9780345391803"
UID = "04 65 B2 42 CD 21 90"


def finder():
    graph = sample_graph()
    return BookFinder(graph.catalog, online_fallback=False, graph=graph)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Source:
    """A reader that is not there: lines are pushed by hand."""

    def __init__(self):
        self.queue = asyncio.Queue()

    async def lines(self):
        while True:
            yield await self.queue.get()


class ParseTest(unittest.TestCase):
    def test_the_lines_the_reader_sends(self):
        self.assertEqual(parse_line(b"UID 04 65 B2 42 CD 21 90\r\n").value, "0465B242CD2190")
        self.assertEqual(parse_line("uid: 1d 5c 7b 36").value, "1D5C7B36")
        self.assertEqual(parse_line("1D 5C 7B 36").kind, "uid")                       # without the word UID
        self.assertEqual(parse_line("1D5C7B36").kind, "uid")
        self.assertEqual(parse_line("UID 04 65 B2 42 CD 21 90").shown, "04 65 B2 42 CD 21 90")
        isbn = parse_line("ISBN 978-0-345-39180-3")
        self.assertEqual((isbn.kind, isbn.value), ("isbn", HITCH))
        self.assertEqual(parse_line(HITCH).kind, "isbn")                             # a bare ISBN is not mistaken for a tag number
        self.assertEqual(parse_line("0345391802").kind, "isbn")                       # ISBN-10 too
        title = parse_line("DUNE\n")
        self.assertEqual((title.kind, title.value), ("title", "DUNE"))               # rfid_write.ino sends titles

    def test_noise_and_bad_lines_are_not_tags(self):
        for line in ("", "   ", b"\x00\x00", "ISBN 9780345391804", "UID 04 65", "12", "Ready!!"[:0]):
            self.assertIsNone(parse_line(line), line)
        self.assertEqual(parse_line("Ready").kind, "title")                          # a start-up message looks like a title: it is simply "no such book"


class TagFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "tags.csv"

    def test_save_load_replace_and_the_reload_when_the_file_changes(self):
        save_tag(self.path, UID, HITCH, "The Hitchhiker's Guide")
        save_tag(self.path, "1d5c7b36", "0345391802")
        self.assertEqual(load_tags(self.path), {"0465B242CD2190": HITCH, "1D5C7B36": HITCH})
        save_tag(self.path, "1D 5C 7B 36", "9780553418026")                          # the same tag again: replaced, not doubled
        self.assertEqual(load_tags(self.path)["1D5C7B36"], "9780553418026")
        self.assertEqual(len(load_tags(self.path)), 2)
        table = TagTable(self.path)
        self.assertEqual(table.get("1D5C7B36"), "9780553418026")
        save_tag(self.path, "AA BB CC DD", HITCH)
        self.assertEqual(table.get("AABBCCDD"), HITCH)                               # picked up without a restart
        self.assertEqual(TagTable(self.dir / "missing.csv").get("AABBCCDD"), None)

    def test_bad_input_is_refused_and_bad_rows_are_skipped(self):
        with self.assertRaises(ValueError):
            save_tag(self.path, "12 34", HITCH)
        with self.assertRaises(ValueError):
            save_tag(self.path, UID, "123")
        self.path.write_text("uid,isbn,title\n04 65 B2 42,9780345391803,ok\nnot a uid,9780345391803,x\n04 65 B2 43,12345,bad isbn\n# comment\n")
        self.assertEqual(load_tags(self.path), {"0465B242": HITCH})
        self.assertEqual(normalise_uid("04-65:b2 42"), "0465B242")


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.tags = self.dir / "tags.csv"
        save_tag(self.tags, UID, HITCH)
        self.clock = Clock()

    def run_reader(self, *lines, reader_kwargs=None, ready=None):
        async def go():
            bus, seen = Bus(), []
            bus.subscribe(object, seen.append)
            reader = RfidBookReader(bus, finder(), Source(), self.tags, clock=self.clock, **(reader_kwargs or {}))
            if ready:
                reader.set_ready(ready)
            results = []
            for line in lines:
                results.append(await reader.handle(line))
                self.clock.t += 10                                                  # each line is a separate presentation
            return seen, results
        return asyncio.run(go())

    def test_a_registered_tag_publishes_book_placed_then_the_same_book_identified_as_the_camera_does(self):
        seen, results = self.run_reader(f"UID {UID}\n")
        self.assertEqual([type(e) for e in seen], [BookPlaced, BookIdentified])
        self.assertEqual(seen[0].source, "rfid")
        rfid_book = seen[1]
        self.assertEqual((rfid_book.isbn, rfid_book.title), (HITCH, "The Hitchhiker's Guide to the Galaxy"))

        async def by_camera():                                                      # the same book by barcode
            bus, got = Bus(), []
            bus.subscribe(BookIdentified, got.append)
            camera = FakeCamera()
            camera.show(isbn=HITCH)
            reader = BookScanner(bus, camera, finder(), timeout_s=1)
            await bus.publish(BookPlaced())
            await reader.wait()
            return got[0]
        cam_book = asyncio.run(by_camera())
        for field in ("isbn", "title", "author", "summary", "shelf", "slot", "found_in_catalog", "call_number", "section", "floor", "zone", "map_x", "map_y", "map_yaw"):
            self.assertEqual(getattr(rfid_book, field), getattr(cam_book, field), field)

    def test_an_isbn_on_the_tag_and_a_title_from_the_first_sketch_work_too(self):
        seen, _ = self.run_reader(f"ISBN {HITCH}", "Hitchhiker's Guide to the Galaxy")
        books = [e for e in seen if isinstance(e, BookIdentified)]
        self.assertEqual([b.isbn for b in books], [HITCH, HITCH])

    def test_an_unregistered_tag_or_unknown_title_is_a_failed_scan_with_a_reason(self):
        seen, _ = self.run_reader("UID AA BB CC DD", "Zzyzx Qwerty Handbook")
        failed = [e for e in seen if isinstance(e, ScanFailed)]
        self.assertEqual(len(failed), 2)
        self.assertIn("not registered", failed[0].reason)
        self.assertIn("rfid_register.py", failed[0].reason)
        self.assertIn("not a title in the catalogue", failed[1].reason)
        self.assertEqual([type(e) for e in seen][:2], [BookPlaced, ScanFailed])

    def test_a_tag_that_stays_on_the_reader_is_one_book_but_held_again_later_is_a_new_one(self):
        async def go():
            bus, seen = Bus(), []
            bus.subscribe(BookIdentified, seen.append)
            reader = RfidBookReader(bus, finder(), Source(), self.tags, gap_s=2.5, clock=self.clock)
            for dt in (0, 1, 1, 1, 1):                                              # sent every second while it stays
                self.clock.t += dt
                await reader.handle(f"UID {UID}")
            first = len(seen)
            self.clock.t += 5                                                       # taken away, held again
            await reader.handle(f"UID {UID}")
            return first, len(seen)
        self.assertEqual(asyncio.run(go()), (1, 2))

    def test_a_tag_is_ignored_when_the_robot_is_not_expecting_a_book_and_can_be_held_again(self):
        busy = {"yes": True}
        seen, results = self.run_reader(f"UID {UID}", f"UID {UID}", ready=lambda: not busy["yes"])
        self.assertEqual((seen, results), ([], [None, None]))
        busy["yes"] = False
        seen, results = self.run_reader(f"UID {UID}", ready=lambda: not busy["yes"])
        self.assertEqual(len(seen), 2)

    def test_non_tag_lines_are_ignored_and_the_reader_is_busy_only_while_reading(self):
        async def go():
            bus = Bus()
            reader = RfidBookReader(bus, finder(), Source(), self.tags, clock=self.clock)
            before = reader.scanning
            self.assertIsNone(await reader.handle("\x00\x00"))
            await reader.handle(f"UID {UID}")
            return before, reader.scanning
        self.assertEqual(asyncio.run(go()), (False, False))

    def test_the_camera_ignores_a_book_placed_by_the_rfid_reader_so_both_can_run_together(self):
        async def go():
            bus, seen = Bus(), []
            bus.subscribe(object, seen.append)
            camera = FakeCamera()
            camera.show(isbn="9780553418026")                                       # something else in front of the camera
            source = Source()
            both = build_reader("both", bus, camera, finder(), timeout_s=1, rfid_source=source, rfid={"tags_path": str(self.tags), "gap_s": 2.5},
                                path=lambda p: p)
            self.assertIsInstance(both, CompositeBookReader)
            await both.readers[1].handle(f"UID {UID}")
            await both.wait()
            await asyncio.sleep(0.2)
            return both, seen
        both, seen = asyncio.run(go())
        self.assertEqual([type(e).__name__ for e in seen], ["BookPlaced", "BookIdentified"])           # no camera scan, no late ScanFailed
        self.assertFalse(both.scanning)

    def test_the_camera_still_reads_a_barcode_when_both_are_on(self):
        async def go():
            bus, seen = Bus(), []
            bus.subscribe(BookIdentified, seen.append)
            camera = FakeCamera()
            camera.show(isbn=HITCH)
            both = build_reader("both", bus, camera, finder(), timeout_s=1, rfid_source=Source(), rfid={"tags_path": str(self.tags)})
            await bus.publish(BookPlaced())                                         # what the watcher publishes for a barcode in view
            await both.wait()
            return seen
        self.assertEqual([b.isbn for b in asyncio.run(go())], [HITCH])

    def test_run_reads_the_source_until_it_ends(self):
        async def go():
            bus, seen = Bus(), []
            bus.subscribe(BookIdentified, seen.append)
            source = Source()
            reader = RfidBookReader(bus, finder(), source, self.tags, clock=self.clock)
            task = asyncio.create_task(reader.run())
            await source.queue.put(f"UID {UID}\n".encode())
            for _ in range(50):
                await asyncio.sleep(0.02)
                if seen:
                    break
            task.cancel()
            return seen
        self.assertEqual([b.isbn for b in asyncio.run(go())], [HITCH])


class SerialSourceTest(unittest.TestCase):
    def test_a_missing_reader_is_retried_not_fatal_and_lines_come_through_once_it_is_there(self):
        class Port:
            def __init__(self):
                self.lines = [b"UID 04 65 B2 42\r\n", b"DUNE\n"]
                self.closed = False

            def readline(self):
                if self.lines:
                    return self.lines.pop(0)
                import time
                time.sleep(0.05)
                return b""

            def close(self):
                self.closed = True
        attempts = []
        port = Port()

        def opener():
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("no such device")
            return port

        async def go():
            source = SerialTagSource("/dev/none", retry_s=0.05, opener=opener)
            got = []
            with self.assertLogs("robot.rfid", level="DEBUG") as logs:
                async for line in source.lines():
                    got.append(line)
                    if len(got) == 2:
                        break
            return got, logs.output
        got, logs = asyncio.run(go())
        self.assertEqual(got, [b"UID 04 65 B2 42\r\n", b"DUNE\n"])
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sum("not available" in l and l.startswith("WARNING") for l in logs), 1)           # said once, then quietly

    def test_the_settings_come_from_the_config(self):
        source = SerialTagSource.from_config({"port": "/dev/ttyUSB3", "baud": 115200, "retry_s": 3})
        self.assertEqual((source.port, source.baud, source.retry_s), ("/dev/ttyUSB3", 115200, 3.0))
        self.assertEqual(SerialTagSource.from_config({}).port, rfid.DEFAULT_PORT)


if __name__ == "__main__":
    unittest.main()
