import contextlib
import io
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

from robot.services.books import Book, Catalog
from tests.helpers.ean13 import check_digit
from tools import book_scan_test as tool


def isbn(n):
    body = "978" + f"{n:09d}"
    return body + check_digit(body)


class FakeFinder:
    def __init__(self, *books):
        self.catalog = Catalog(books)


class FakeOnline:
    """Stands in for OpenLibrary: knows the titles in `known` and records what it was asked."""

    def __init__(self, known=None):
        self.known, self.asked = known or {}, []

    def lookup(self, code):
        self.asked.append(code)
        return Book(code, self.known[code], found_in_catalog=False) if code in self.known else None


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "isbns.txt"

    def lines(self):
        return self.path.read_text(encoding="utf-8").splitlines()

    def test_new_isbns_are_appended_one_per_line_and_never_twice(self):
        c = tool.IsbnCollector(self.path)
        self.assertTrue(c.add(isbn(1)))
        self.assertTrue(c.add(isbn(2)))
        self.assertFalse(c.add(isbn(1)))                                                # scanned again
        self.assertEqual(self.lines(), [isbn(1), isbn(2)])
        self.assertEqual(len(c), 2)

    def test_the_file_and_its_folder_are_created(self):
        c = tool.IsbnCollector(Path(self.dir.name) / "deep" / "er" / "list.txt")
        c.add(isbn(1))
        self.assertEqual((Path(self.dir.name) / "deep" / "er" / "list.txt").read_text(encoding="utf-8"), isbn(1) + "\n")

    def test_what_is_already_in_the_file_counts_as_seen_and_is_kept(self):
        self.path.write_text(f"# my pile\n{isbn(1)}\n\n  {isbn(2)}   # blue book\n", encoding="utf-8")
        c = tool.IsbnCollector(self.path)
        self.assertEqual(len(c), 2)
        self.assertFalse(c.add(isbn(1)))
        self.assertFalse(c.add(isbn(2)))
        self.assertTrue(c.add(isbn(3)))
        self.assertEqual(self.lines(), ["# my pile", isbn(1), "", f"  {isbn(2)}   # blue book", isbn(3)])

    def test_an_isbn_10_of_a_collected_book_is_not_added_again(self):
        digits = isbn(1)[3:12]
        check = (11 - sum((10 - i) * int(d) for i, d in enumerate(digits)) % 11) % 11
        isbn10 = digits + ("X" if check == 10 else str(check))
        self.path.write_text(isbn(1) + "\n", encoding="utf-8")
        self.assertFalse(tool.IsbnCollector(self.path).add(isbn10))

    def test_a_file_with_no_final_newline_does_not_glue_two_isbns_together(self):
        self.path.write_text(isbn(1), encoding="utf-8")                                 # no "\n" at the end
        tool.IsbnCollector(self.path).add(isbn(2))
        self.assertEqual(self.lines(), [isbn(1), isbn(2)])

    def test_invalid_isbns_are_refused(self):
        c = tool.IsbnCollector(self.path)
        self.assertFalse(c.add("9780345391804"))                                        # bad checksum
        self.assertFalse(c.add("not an isbn"))
        self.assertFalse(self.path.exists())

    def test_two_collectors_on_one_file_do_not_clash_after_a_restart(self):
        tool.IsbnCollector(self.path).add(isbn(1))
        again = tool.IsbnCollector(self.path)                                           # a later session
        self.assertFalse(again.add(isbn(1)))
        self.assertTrue(again.add(isbn(2)))
        self.assertEqual(self.lines(), [isbn(1), isbn(2)])

    def test_the_file_is_the_input_import_books_reads(self):
        from tools import import_books
        c = tool.IsbnCollector(self.path)
        for n in (1, 2, 3):
            c.add(isbn(n))
        isbns, problems = import_books.read_isbns(self.path)
        self.assertEqual((isbns, problems), ([isbn(1), isbn(2), isbn(3)], []))


class RegisterAndAnnounceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.collector = tool.IsbnCollector(Path(self.dir.name) / "isbns.txt")

    def test_a_new_isbn_is_written_and_beeps_a_repeat_does_neither(self):
        beeper = mock.Mock()
        self.assertTrue(tool.register(isbn(1), self.collector, beeper))
        beeper.beep.assert_called_once()
        self.assertFalse(tool.register(isbn(1), self.collector, beeper))
        beeper.beep.assert_called_once()                                                # still just the one
        self.assertEqual(self.collector.path.read_text(encoding="utf-8"), isbn(1) + "\n")

    def test_it_works_without_a_beeper(self):
        self.assertTrue(tool.register(isbn(1), self.collector, None))

    def test_the_title_comes_from_the_catalogue_first_then_open_library(self):
        finder = FakeFinder(Book(isbn(1), "In The Catalogue"))
        online = FakeOnline({isbn(2): "Only Online"})
        self.assertEqual(tool.announcement(isbn(1), True, self.collector, finder, online), f"+ {isbn(1)}  In The Catalogue")
        self.assertEqual(online.asked, [])                                              # the catalogue answered: no request
        self.assertEqual(tool.announcement(isbn(2), True, self.collector, finder, online), f"+ {isbn(2)}  Only Online")
        self.assertEqual(online.asked, [isbn(2)])

    def test_no_title_is_said_plainly_and_offline_is_fine(self):
        finder = FakeFinder()
        self.assertEqual(tool.announcement(isbn(3), True, self.collector, finder, FakeOnline()), f"+ {isbn(3)}  (no title found)")
        self.assertEqual(tool.announcement(isbn(3), True, self.collector, finder, None), f"+ {isbn(3)}  (no title found)")

    def test_a_repeat_says_where_it_already_is(self):
        line = tool.announcement(isbn(1), False, self.collector, FakeFinder(Book(isbn(1), "Dune")), None)
        self.assertEqual(line, f"= {isbn(1)}  Dune  (already in {self.collector.path})")


class BeepTest(unittest.TestCase):
    def test_the_beep_is_a_short_valid_wav(self):
        with wave.open(io.BytesIO(tool.beep_wav())) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 22050))
            seconds = w.getnframes() / w.getframerate()
            self.assertTrue(0.05 < seconds < 0.3, seconds)                              # short
            frames = w.readframes(w.getnframes())
        self.assertNotEqual(frames, b"\x00" * len(frames))                              # it is not silence
        self.assertEqual(frames[:2], b"\x00\x00")                                       # starts at zero: no click

    def test_the_beeper_plays_through_the_audio_driver_off_the_main_thread(self):
        played = []

        class Audio:
            def play(self, wav, volume):
                played.append((len(wav), volume))
        tool.Beeper(Audio()).beep()
        for _ in range(100):
            if played:
                break
            time.sleep(0.01)
        self.assertEqual(len(played), 1)
        self.assertGreater(played[0][0], 100)

    def test_without_a_speaker_it_rings_the_terminal_bell(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tool.Beeper(None).beep()
        self.assertEqual(out.getvalue(), "\a")

    def test_a_speaker_that_fails_falls_back_to_the_bell_and_never_raises(self):
        class Broken:
            def play(self, wav, volume):
                raise OSError("no sound card")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tool.Beeper(Broken()).beep()
            for _ in range(100):
                if out.getvalue():
                    break
                time.sleep(0.01)
        self.assertEqual(out.getvalue(), "\a")

    def test_from_config_survives_a_missing_audio_system(self):
        from robot.config import load_config
        with mock.patch("robot.drivers.audio.Audio", side_effect=RuntimeError("paplay not found")):
            self.assertIsNone(tool.Beeper.from_config(load_config()).audio)


try:
    import cv2
    import numpy as np
    from pyzbar import pyzbar  # noqa: F401
    LIVE_SKIP = None
except ImportError as exc:
    cv2 = np = None
    LIVE_SKIP = f"needs pyzbar and OpenCV ({exc})"


class FakeCamera:
    """Shows one frame forever (a book's barcode), like a pile of books held up one at a time."""

    def __init__(self, frame):
        self.frame, self.width, self.height = frame, frame.shape[1], frame.shape[0]

    def read_frame(self, timeout=2.0):
        time.sleep(0.01)
        return self.frame

    def close(self):
        pass


@unittest.skipIf(LIVE_SKIP, LIVE_SKIP)
class LiveCollectTest(unittest.TestCase):
    """The real run_live loop with a fake camera showing a real barcode image and the window calls stubbed out."""

    def barcode_frame(self, code):
        from tests.helpers.ean13 import bitmap
        w, h, gray = bitmap(code, module_px=3)
        image = np.frombuffer(gray, dtype=np.uint8).reshape(h, w)
        return cv2.cvtColor(np.ascontiguousarray(image), cv2.COLOR_GRAY2BGR)

    def run_live(self, code, collector, beeper, online, frames=60):
        from robot.app import build_finder
        from robot.config import load_config
        finder = build_finder(load_config())
        calls = {"n": 0}

        def wait_key(_):
            calls["n"] += 1
            return ord("q") if calls["n"] > frames else -1
        out = io.StringIO()
        with mock.patch("robot.drivers.camera.Camera", return_value=FakeCamera(self.barcode_frame(code))), \
                mock.patch.object(cv2, "imshow"), mock.patch.object(cv2, "waitKey", wait_key), \
                mock.patch.object(cv2, "destroyAllWindows"), contextlib.redirect_stdout(out):
            self.assertEqual(tool.run_live(finder, 0, None, collector, beeper, online), 0)
            time.sleep(0.5)                                              # the title is printed from another thread
        return out.getvalue()

    def test_a_scanned_book_is_collected_once_with_a_beep_and_its_title(self):
        with tempfile.TemporaryDirectory() as d:
            collector = tool.IsbnCollector(Path(d) / "pile.txt")
            beeper = mock.Mock()
            out = self.run_live("9780441172719", collector, beeper, None)               # Dune, in the sample catalogue
            self.assertEqual(collector.path.read_text(encoding="utf-8"), "9780441172719\n")   # once, however many frames
            beeper.beep.assert_called_once()
            self.assertEqual(out.count("+ 9780441172719  Dune"), 1, out)

    def test_a_book_not_in_the_catalogue_gets_its_open_library_title(self):
        code = "9780140449136"
        with tempfile.TemporaryDirectory() as d:
            collector = tool.IsbnCollector(Path(d) / "pile.txt")
            out = self.run_live(code, collector, mock.Mock(), FakeOnline({code: "The Odyssey"}))
            self.assertIn(f"+ {code}  The Odyssey", out)
            self.assertEqual(collector.path.read_text(encoding="utf-8"), code + "\n")

    def test_a_book_already_in_the_file_neither_beeps_nor_is_added_again(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "pile.txt"
            path.write_text("9780441172719\n", encoding="utf-8")
            beeper = mock.Mock()
            out = self.run_live("9780441172719", tool.IsbnCollector(path), beeper, None)
            beeper.beep.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), "9780441172719\n")
            self.assertIn("= 9780441172719  Dune  (already in", out)


class CommandLineTest(unittest.TestCase):
    def test_append_needs_live(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            tool.main(["--list", "--append", "x.txt"])
        self.assertIn("--append only works with --live", err.getvalue())

    def test_append_is_in_the_help_and_the_old_modes_still_work(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            tool.main(["--help"])
        self.assertIn("--append FILE", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(tool.main(["--list"]), 0)
        self.assertIn("9780345391803", out.getvalue())

    def test_live_with_append_wires_the_collector_into_run_live(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(tool, "run_live", return_value=0) as run_live, \
                contextlib.redirect_stdout(io.StringIO()) as out, mock.patch.object(tool.Beeper, "from_config", return_value=tool.Beeper(None)):
            path = Path(d) / "pile.txt"
            path.write_text(isbn(1) + "\n", encoding="utf-8")
            self.assertEqual(tool.main(["--live", "--append", str(path)]), 0)
        collector = run_live.call_args.args[3]
        self.assertIsInstance(collector, tool.IsbnCollector)
        self.assertEqual(len(collector), 1)                                              # what was in the file
        self.assertIn("Collecting ISBNs into", out.getvalue())
        self.assertIn("(1 already there)", out.getvalue())

    def test_live_without_append_has_no_collector(self):
        with mock.patch.object(tool, "run_live", return_value=0) as run_live:
            self.assertEqual(tool.main(["--live"]), 0)
        self.assertIsNone(run_live.call_args.args[3])


if __name__ == "__main__":
    unittest.main()
