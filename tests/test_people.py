import asyncio
import io
import logging
import unittest

from robot.actuation.face import FaceDisplay
from robot.app import build_app, fake_drivers
from robot.bus import Bus
from robot.config import load_config
from robot.events import ContextChanged, ReplySentence
from robot.perception.people import PeopleCounter
from robot.sim.keyboard import Sim

FACE = {"box": [10, 60, 60, 10], "detection_confidence": 0.99}


class ScriptedVision:
    """detect() returns the next scripted list of faces (an Exception in the script is raised)."""

    def __init__(self, *counts):
        self.script = list(counts)

    def detect(self, jpg):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return [dict(FACE) for _ in range(item)]


class JpegCamera:
    def read_jpeg(self):
        return b"jpeg"


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def make_counter(*counts, **kwargs):
    seen = []

    async def on_change(count, person_ids=()):
        seen.append(count)

    clock = Clock()
    counter = PeopleCounter(Bus(), JpegCamera(), ScriptedVision(*counts), on_change=on_change,
                            clock=clock, **kwargs)
    return counter, seen, clock


def steps(counter, clock, n, dt=1.0):
    async def go():
        for _ in range(n):
            await counter.step()
            clock.now += dt
    asyncio.run(go())


class PeopleCounterTest(unittest.TestCase):
    def test_a_face_is_confirmed_after_two_samples_in_a_row(self):
        counter, seen, clock = make_counter(1, 1)
        steps(counter, clock, 1)
        self.assertEqual(seen, [])                       # one sighting could be a mistake
        steps(counter, clock, 1)
        self.assertEqual(seen, [1])
        self.assertEqual(counter.count, 1)

    def test_a_single_spurious_face_is_ignored(self):
        counter, seen, clock = make_counter(1, 0, 1, 0)
        steps(counter, clock, 4)
        self.assertEqual(seen, [])

    def test_extra_face_needs_to_stay_to_count_and_takes_the_smallest_count_seen(self):
        counter, seen, clock = make_counter(1, 1, 3, 2, 2)
        steps(counter, clock, 5)
        self.assertEqual(seen, [1, 2])

    def test_one_missed_frame_does_not_make_the_visitor_leave(self):
        counter, seen, clock = make_counter(1, 1, 0, 1, 1)
        steps(counter, clock, 5)
        self.assertEqual(seen, [1])

    def test_visitor_looking_down_for_a_few_seconds_is_not_leaving(self):
        counter, seen, clock = make_counter(1, 1, 0, 0, 0, 0, 1, leave_after_s=8)
        steps(counter, clock, 7)
        self.assertEqual(seen, [1])
        self.assertEqual(counter.count, 1)

    def test_nobody_for_leave_after_s_means_everyone_left(self):
        counter, seen, clock = make_counter(1, 1, *[0] * 10, leave_after_s=8)
        steps(counter, clock, 12)
        self.assertEqual(seen, [1, 0])

    def test_leaving_clock_restarts_when_a_face_comes_back(self):
        counter, seen, clock = make_counter(1, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, leave_after_s=5)
        steps(counter, clock, 11)
        self.assertEqual(seen, [1])

    def test_one_of_two_people_leaving_lowers_the_count_after_the_delay(self):
        counter, seen, clock = make_counter(2, 2, 1, 1, 1, 1, leave_after_s=3)
        steps(counter, clock, 6)
        self.assertEqual(seen, [2, 1])

    def test_default_change_publishes_context_changed(self):
        bus = Bus()
        events = []
        bus.subscribe(ContextChanged, events.append)
        counter = PeopleCounter(bus, JpegCamera(), ScriptedVision(2, 2), map_zone="quiet")
        asyncio.run(self._two_steps(counter))
        self.assertEqual([(e.zone, e.group_size) for e in events], [("quiet", 2)])

    @staticmethod
    async def _two_steps(counter):
        await counter.step()
        await counter.step()

    def test_detections_are_kept_for_the_live_view(self):
        counter, _, clock = make_counter(1)
        steps(counter, clock, 1)
        self.assertEqual(counter.detections[0]["box"], FACE["box"])

    def test_run_survives_a_server_failure_and_logs_it_once(self):
        counter, seen, _ = make_counter(OSError("down"), OSError("down"), 1, 1, interval_s=0)

        async def go():
            task = asyncio.create_task(counter.run())
            while counter.vision.script:
                await asyncio.sleep(0)
            await asyncio.sleep(0.05)
            task.cancel()
        with self.assertLogs("robot.people", level="INFO") as logs:
            asyncio.run(go())
        self.assertEqual(seen, [1])
        self.assertEqual(sum("face detection failed" in line for line in logs.output), 1)


class FrameCamera:
    def peek(self):
        return "frame"

    def encode_jpeg(self, frame):
        return b"jpeg"


def make_people_counter(script, **kwargs):
    """A counter whose camera 'sees' faces and whose tracker gives them the scripted person numbers."""
    seen = []

    async def on_change(count, person_ids=()):
        seen.append((count, person_ids))

    faces = [len(i) for i in script]
    ids = [i for i in script if i]                      # the tracker is only asked when a face was detected
    clock = Clock()
    counter = PeopleCounter(Bus(), FrameCamera(), ScriptedVision(*faces), on_change=on_change,
                            clock=clock, identify=lambda frame, dets: ids.pop(0), **kwargs)
    return counter, seen, clock


class PersonPresenceTest(unittest.TestCase):
    def test_a_person_is_confirmed_after_two_samples_and_reported_by_number(self):
        counter, seen, clock = make_people_counter([[1], [1]])
        steps(counter, clock, 1)
        self.assertEqual(seen, [])
        steps(counter, clock, 1)
        self.assertEqual(seen, [(1, (1,))])
        self.assertEqual(counter.detections[0]["person"], 1)

    def test_a_face_seen_once_is_not_a_person(self):
        counter, seen, clock = make_people_counter([[1], [], [2], []])
        steps(counter, clock, 4)
        self.assertEqual(seen, [])

    def test_a_visitor_who_leaves_and_returns_is_the_same_person_1(self):
        counter, seen, clock = make_people_counter([[1], [1]] + [[]] * 10 + [[1], [1]], leave_after_s=8)
        steps(counter, clock, 14)
        self.assertEqual(seen, [(1, (1,)), (0, ()), (1, (1,))])

    def test_a_new_face_after_that_is_person_2(self):
        counter, seen, clock = make_people_counter([[1], [1]] + [[]] * 10 + [[2], [2]], leave_after_s=8)
        steps(counter, clock, 14)
        self.assertEqual(seen[-1], (1, (2,)))

    def test_two_people_and_one_of_them_leaving(self):
        counter, seen, clock = make_people_counter([[1, 2], [1, 2]] + [[1]] * 9, leave_after_s=8)
        steps(counter, clock, 11)
        self.assertEqual(seen, [(2, (1, 2)), (1, (1,))])

    def test_looking_down_for_a_few_seconds_keeps_the_person(self):
        counter, seen, clock = make_people_counter([[1], [1], [], [], [], [], [1]], leave_after_s=8)
        steps(counter, clock, 7)
        self.assertEqual(seen, [(1, (1,))])

    def test_faces_too_small_to_tell_apart_are_not_counted(self):
        counter, seen, clock = make_people_counter([[None], [None], [None]])
        steps(counter, clock, 3)
        self.assertEqual(seen, [])


class HelloOnArrivalTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_a_face_makes_the_robot_say_hello_and_leaving_resets_it(self):
        cfg = load_config()
        cfg.robot.setdefault("books", {})["online_fallback"] = False
        app = build_app(cfg, fake_drivers(), face=FaceDisplay(enabled=False), log_sessions=False)
        sim = Sim(app, io.StringIO())
        said = []
        app.bus.subscribe(ReplySentence, lambda e: said.append(e.text))
        counter = PeopleCounter(app.bus, JpegCamera(), ScriptedVision(1, 1, 0, 0), leave_after_s=1,
                                on_change=sim.people_changed, clock=Clock())

        async def go():
            await counter.step()
            await counter.step()
            self.assertTrue(said[0].startswith("Hello!"))
            self.assertEqual(app.dialogue.state, "invited")
            counter.clock.now += 1
            await counter.step()
            counter.clock.now += 1
            await counter.step()
            self.assertEqual(app.dialogue.state, "idle")
        asyncio.run(go())
        self.assertEqual(len(said), 1)


if __name__ == "__main__":
    unittest.main()
