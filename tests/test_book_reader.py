import asyncio
import unittest

from robot.bus import Bus
from robot.config import load_config
from robot.drivers.fake import FakeCamera
from robot.events import BookIdentified, BookPlaced
from robot.perception.book_reader import BookReader, RfidBookReader, build_reader
from robot.perception.book_scanner import BookScanner, CameraBookReader
from robot.services.books import BookFinder, Catalog
from robot.config import ROOT

MARTIAN = "9780553418026"


def finder():
    return BookFinder(Catalog.load(ROOT / "data" / "catalog.csv"), online_fallback=False)


class BookReaderTest(unittest.TestCase):
    def test_the_camera_scanner_is_a_book_reader_and_publishes_book_identified(self):
        async def go():
            bus, seen = Bus(), []
            bus.subscribe(BookIdentified, seen.append)
            camera = FakeCamera()
            camera.show(isbn=MARTIAN)
            reader = build_reader("camera", bus, camera, finder(), timeout_s=1)
            await bus.publish(BookPlaced())
            await reader.wait()
            return reader, seen
        reader, seen = asyncio.run(go())
        self.assertIsInstance(reader, BookReader)
        self.assertIs(CameraBookReader, BookScanner)
        self.assertEqual([b.title for b in seen], ["The Martian"])

    def test_the_default_reader_is_the_camera(self):
        reader = build_reader("", Bus(), FakeCamera(), finder())
        self.assertIsInstance(reader, CameraBookReader)
        self.assertEqual(load_config().books["reader"], "camera")

    def test_rfid_is_a_stub_that_says_what_to_do(self):
        with self.assertRaises(NotImplementedError) as caught:
            build_reader("rfid", Bus(), FakeCamera(), finder())
        message = str(caught.exception)
        self.assertIn("RFID", message)
        self.assertIn("not implemented", message)
        self.assertIn("book_reader.py", message)
        self.assertIn("books.reader: camera", message)
        with self.assertRaises(NotImplementedError):
            RfidBookReader(Bus(), finder())

    def test_an_unknown_reader_is_a_clear_error(self):
        with self.assertRaises(ValueError) as caught:
            build_reader("barcode-gun", Bus(), FakeCamera(), finder())
        self.assertIn("camera or rfid", str(caught.exception))

    def test_the_interface_is_what_the_state_machine_relies_on(self):
        reader = BookReader(Bus(), finder())
        self.assertFalse(reader.scanning)
        asyncio.run(reader.wait())


if __name__ == "__main__":
    unittest.main()
