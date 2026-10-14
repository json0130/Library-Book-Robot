import asyncio
import unittest

from robot.bus import Bus
from robot.events import BookPlaced
from robot.perception.book_scanner import DecoderUnavailable
from robot.perception.book_watch import BookWatcher


class Camera:
    def peek(self):
        return object()


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def make_watcher(sightings, ready=lambda: True, **kwargs):
    """A watcher whose camera 'sees' the scripted ISBNs (None = no barcode), one per step."""
    bus, placed, clock = Bus(), [], Clock()
    bus.subscribe(BookPlaced, placed.append)
    script = list(sightings)
    watcher = BookWatcher(bus, Camera(), ready, probe=lambda frame: script.pop(0), clock=clock, **kwargs)
    return watcher, placed, clock


def look(watcher, clock, n, dt=0.4):
    async def go():
        for _ in range(n):
            await watcher.step()
            clock.now += dt
    asyncio.run(go())


class BookWatcherTest(unittest.TestCase):
    def test_a_barcode_in_view_places_a_book(self):
        watcher, placed, clock = make_watcher([None, "9780553294385"])
        look(watcher, clock, 2)
        self.assertEqual(len(placed), 1)

    def test_a_book_that_stays_in_view_is_scanned_once(self):
        watcher, placed, clock = make_watcher(["9780553294385"] * 20)
        look(watcher, clock, 20)
        self.assertEqual(len(placed), 1)

    def test_a_different_book_is_scanned_right_away(self):
        watcher, placed, clock = make_watcher(["9780553294385", "9780553294385", "9780441172719"])
        look(watcher, clock, 3)
        self.assertEqual(len(placed), 2)

    def test_the_same_book_again_after_it_left_the_view(self):
        watcher, placed, clock = make_watcher(["9780553294385"] + [None] * 10 + ["9780553294385"], rearm_after_s=3)
        look(watcher, clock, 12)
        self.assertEqual(len(placed), 2)

    def test_a_brief_gap_does_not_rescan_the_same_book(self):
        watcher, placed, clock = make_watcher(["9780553294385", None, None, "9780553294385"], rearm_after_s=3)
        look(watcher, clock, 4)
        self.assertEqual(len(placed), 1)

    def test_nothing_starts_while_the_robot_is_busy_then_it_does(self):
        busy = [True]
        watcher, placed, clock = make_watcher(["9780553294385"] * 3, ready=lambda: not busy[0])
        look(watcher, clock, 2)
        self.assertEqual(placed, [])
        busy[0] = False
        look(watcher, clock, 1)
        self.assertEqual(len(placed), 1)

    def test_run_stops_quietly_without_the_decoder_and_survives_camera_errors(self):
        def probe(frame):
            raise DecoderUnavailable("no pyzbar")
        bus = Bus()
        watcher = BookWatcher(bus, Camera(), lambda: True, probe=probe)
        with self.assertLogs("robot.bookwatch", level="WARNING") as logs:
            asyncio.run(watcher.run())                                  # returns instead of looping
        self.assertIn("no pyzbar", logs.output[0])

        calls = []

        def flaky(frame):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("camera hiccup")
            return "9780553294385"
        placed = []
        bus.subscribe(BookPlaced, placed.append)
        watcher = BookWatcher(bus, Camera(), lambda: True, interval_s=0, probe=flaky)

        async def go():
            task = asyncio.create_task(watcher.run())
            while not placed:
                await asyncio.sleep(0.01)
            task.cancel()
        with self.assertLogs("robot.bookwatch", level="WARNING"):
            asyncio.run(go())
        self.assertEqual(len(placed), 1)


if __name__ == "__main__":
    unittest.main()
