import asyncio
import unittest
from unittest import mock

from robot.bus import Bus
from robot.config import ROOT
from robot.drivers.fake import FakeCamera, FakeFrame
from robot.events import BookPlaced, BookScanned, ScanFailed
from robot.perception.book_scanner import BookScanner, DecoderUnavailable
from robot.services.books import BookFinder, Catalog
from tests.helpers.ean13 import bitmap, check_digit

try:
    import cv2
    import numpy as np
    from pyzbar import pyzbar  # noqa: F401
    DECODER_SKIP = None
except ImportError as exc:                       # also raised when libzbar is missing
    cv2 = np = None
    DECODER_SKIP = f"needs pyzbar and OpenCV ({exc}); sudo apt install libzbar0 python3-opencv && pip install pyzbar"

HITCH = "9780345391803"
STICKER = "590123412345" + check_digit("590123412345")      # EAN-13, not an ISBN


def finder():
    return BookFinder(Catalog.load(ROOT / "data" / "catalog.csv"), online_fallback=False)


def barcode_image(digits, module_px=3):
    w, h, gray = bitmap(digits, module_px=module_px)
    return np.frombuffer(gray, dtype=np.uint8).reshape(h, w).copy()


@unittest.skipIf(DECODER_SKIP, DECODER_SKIP)
class DecodeIsbnTest(unittest.TestCase):
    def decode(self, image):
        from robot.perception.book_scanner import decode_isbn
        return decode_isbn(image)

    def test_plain_gray_and_color(self):
        img = barcode_image(HITCH)
        self.assertEqual(self.decode(img), HITCH)
        self.assertEqual(self.decode(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)), HITCH)

    def test_rotated_any_way(self):
        img = barcode_image(HITCH)
        for k in (1, 2, 3):                                    # 90, 180, 270 degrees
            self.assertEqual(self.decode(np.rot90(img, k).copy()), HITCH, f"rotated {90 * k}")

    def test_slightly_blurred(self):
        img = cv2.GaussianBlur(barcode_image(HITCH), (5, 5), 1.2)
        self.assertEqual(self.decode(img), HITCH)
        self.assertEqual(self.decode(np.rot90(img).copy()), HITCH)

    def test_small_and_low_contrast(self):
        small = barcode_image(HITCH, module_px=2)
        self.assertEqual(self.decode(small), HITCH)
        dull = (small.astype(np.float32) * 0.35 + 140).astype(np.uint8)          # grey on grey
        self.assertEqual(self.decode(dull), HITCH)

    def test_non_isbn_and_blank_images_give_none(self):
        self.assertIsNone(self.decode(barcode_image(STICKER)))                    # library sticker style
        self.assertIsNone(self.decode(np.full((200, 300), 255, np.uint8)))
        self.assertIsNone(self.decode(np.random.RandomState(1).randint(0, 255, (200, 300), dtype=np.uint8)))

    def test_isbn_wins_when_a_sticker_is_in_the_same_picture(self):
        both = np.vstack([barcode_image(STICKER), barcode_image(HITCH)])
        self.assertEqual(self.decode(both), HITCH)

    def test_isbn10_barcode_values_are_converted(self):
        from robot.services.isbn import to_isbn13
        self.assertEqual(to_isbn13("0345391802"), HITCH)


class ScannerFlowTest(unittest.TestCase):
    """BookPlaced -> scan -> BookScanned / ScanFailed, on the bus, with the fake camera."""

    def run_flow(self, camera, timeout_s=0.5, poll_s=0.02, decode=None):
        async def go():
            bus, seen = Bus(), []
            bus.subscribe(BookScanned, seen.append)
            bus.subscribe(ScanFailed, seen.append)
            scanner = BookScanner(bus, camera, finder(), timeout_s=timeout_s, poll_s=poll_s, decode=decode)
            await bus.publish(BookPlaced())
            await scanner.wait()
            return seen, scanner
        return asyncio.run(go())

    def test_book_placed_publishes_book_scanned_with_the_catalogue_record(self):
        camera = FakeCamera()
        camera.show(isbn=HITCH)
        (event,), _ = self.run_flow(camera)
        self.assertIsInstance(event, BookScanned)
        self.assertEqual((event.isbn, event.title, event.author), (HITCH, "The Hitchhiker's Guide to the Galaxy",
                                                                   "Douglas Adams"))
        self.assertEqual((event.shelf, event.slot, event.found_in_catalog), ("Level 2 SF-ADA", 0, True))
        self.assertIn("galaxy", event.summary)

    def test_valid_isbn_unknown_to_the_catalogue(self):
        camera = FakeCamera()
        camera.show(isbn="9780140449136")
        (event,), _ = self.run_flow(camera)
        self.assertIsInstance(event, BookScanned)
        self.assertEqual((event.isbn, event.title, event.found_in_catalog), ("9780140449136", None, False))

    def test_nothing_readable_publishes_scan_failed_at_once_for_a_still_camera(self):
        (event,), _ = self.run_flow(FakeCamera())
        self.assertIsInstance(event, ScanFailed)
        self.assertIn("no ISBN barcode", event.reason)

    def test_live_camera_is_polled_until_the_timeout(self):
        class Live(FakeCamera):
            static = False
            frames = 0

            def read_frame(self):
                Live.frames += 1
                return FakeFrame(None)

        (event,), _ = self.run_flow(Live(), timeout_s=0.3, poll_s=0.05)
        self.assertIsInstance(event, ScanFailed)
        self.assertGreater(Live.frames, 2)

    def test_book_found_on_a_later_frame(self):
        class Live(FakeCamera):
            static = False
            n = 0

            def read_frame(self):
                Live.n += 1
                return FakeFrame(HITCH if Live.n >= 3 else None)

        (event,), _ = self.run_flow(Live(), timeout_s=2, poll_s=0.01)
        self.assertIsInstance(event, BookScanned)
        self.assertEqual(Live.n, 3)

    def test_camera_error_and_missing_decoder_become_scan_failed(self):
        class Broken(FakeCamera):
            def read_frame(self):
                raise OSError("camera unplugged")

        with self.assertLogs("robot.scanner", level="ERROR"):
            (event,), _ = self.run_flow(Broken())
        self.assertIsInstance(event, ScanFailed)
        self.assertIn("camera unplugged", event.reason)

        def no_decoder(frame):
            raise DecoderUnavailable("No module named 'pyzbar'")

        (event,), _ = self.run_flow(FakeCamera(), decode=no_decoder)
        self.assertIsInstance(event, ScanFailed)
        self.assertIn("pyzbar", event.reason)

    def test_a_second_placement_during_a_scan_is_ignored(self):
        async def go():
            bus, seen = Bus(), []
            bus.subscribe(BookScanned, seen.append)
            camera = FakeCamera()
            camera.show(isbn=HITCH)
            scanner = BookScanner(bus, camera, finder())
            await bus.publish(BookPlaced())
            await bus.publish(BookPlaced())
            await scanner.wait()
            return seen
        self.assertEqual(len(asyncio.run(go())), 1)

    def test_publisher_is_not_blocked_while_scanning(self):
        class Slow(FakeCamera):
            static = False

            def read_frame(self):
                import time
                time.sleep(0.2)
                return FakeFrame(None)

        async def go():
            bus = Bus()
            scanner = BookScanner(bus, Slow(), finder(), timeout_s=0.3, poll_s=0.01)
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            await bus.publish(BookPlaced())
            elapsed = loop.time() - t0
            await scanner.wait()
            return elapsed
        self.assertLess(asyncio.run(go()), 0.1)


@unittest.skipIf(DECODER_SKIP, DECODER_SKIP)
class ScannerWithRealDecoderTest(unittest.TestCase):
    def scan(self, image):
        async def go():
            bus, seen = Bus(), []
            bus.subscribe(BookScanned, seen.append)
            bus.subscribe(ScanFailed, seen.append)
            camera = FakeCamera()
            camera.show(image=image)              # the fake camera's decode() sends real images to pyzbar
            scanner = BookScanner(bus, camera, finder(), timeout_s=0.5)
            await bus.publish(BookPlaced())
            await scanner.wait()
            return seen[0]
        return asyncio.run(go())

    def test_barcode_image_through_the_whole_scanner(self):
        event = self.scan(np.rot90(barcode_image(HITCH), 2).copy())
        self.assertIsInstance(event, BookScanned)
        self.assertEqual(event.title, "The Hitchhiker's Guide to the Galaxy")

    def test_sticker_only_image_fails(self):
        self.assertIsInstance(self.scan(barcode_image(STICKER)), ScanFailed)


if __name__ == "__main__":
    unittest.main()
