"""Film mode (robot/film.py): the forced zone, the event log, the camera recording. All of it is off unless asked for."""

import asyncio
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path

from robot.bus import Bus
from robot.camera_feed import CameraFeed
from robot.config import ROOT, load_config
from robot.events import (BookIdentified, ContextChanged, NavArrived, NavigationRequested, NavProgress, NoiseReading, PersonEngaged,
                          PoseUpdated, ReplySentence, ReturningProgress, ScreenUpdate, StateEntered, UserUtterance)
from robot.film import FilmLog, FrameRecorder, take_dir
from tests.test_noise_zone import noisy, zone_app
from tests.test_dialogue import run


class ForcedZoneTest(unittest.TestCase):
    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_without_the_flag_the_map_still_decides(self):
        app = zone_app(zone_from_pose=True, zone_dwell_s=0)
        self.assertIsNone(app.forced_zone)
        run(app, ContextChanged("common", 1), PoseUpdated(4.0, 1.5, 0.0, True))       # the Science section: quiet
        self.assertEqual((app.context.zone, app.zone_source), ("quiet", "map"))

    def test_a_forced_zone_holds_over_the_map_the_noise_and_the_operator(self):
        app = zone_app(zone_from_pose=True, zone_dwell_s=0)
        asyncio.run(app.force_zone("common"))
        run(app, PoseUpdated(4.0, 1.5, 0.0, True))                                  # a quiet section on the map
        self.assertEqual((app.context.zone, app.zone_source), ("common", "forced"))
        noisy(app, 30)                                                               # and a silent room
        run(app, ContextChanged("quiet", 1))                                         # and the operator's Quiet button
        self.assertEqual(app.context.zone, "common")
        self.assertEqual(app.style.voice, "normal")

    def test_a_forced_quiet_zone_is_not_turned_common_by_noise(self):
        app = zone_app(zone_from_pose=True, zone_dwell_s=0)
        asyncio.run(app.force_zone("quiet"))
        noisy(app, 80)
        run(app, PoseUpdated(10.0, 7.0, 0.0, True))                                 # the common corridor
        self.assertEqual((app.context.zone, app.style.voice), ("quiet", "whisper"))

    def test_only_quiet_or_common(self):
        app = zone_app()
        with self.assertRaises(ValueError):
            asyncio.run(app.force_zone("loud"))


class FilmLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.now = [100.0]
        self.zone, self.channels = ["common"], [("speech", "screen")]
        self.log = FilmLog(self.dir / "events.jsonl", get_zone=lambda: self.zone[0], get_channels=lambda: self.channels[0],
                           get_zone_source=lambda: "forced", clock=lambda: self.now[0])
        self.bus = Bus()
        self.log.attach(self.bus)

    def rows(self):
        self.log.close()
        return [json.loads(line) for line in (self.dir / "events.jsonl").read_text().splitlines()]

    def send(self, *events):
        for e in events:
            asyncio.run(self.bus.publish(e))
            self.now[0] += 1.0

    def test_a_visit_in_the_common_area_is_logged_with_times(self):
        self.log.start(take="t1")
        self.send(PersonEngaged((1,)), StateEntered("ENGAGED.GREET", "common", text="Hello! My name is Stuart."),
                  UserUtterance("can I return this book?"), BookIdentified("9780345539434", title="Cosmos", section="Science"),
                  ReplySentence("Thanks! Cosmos is returned."))
        rows = self.rows()
        kinds = [r["type"] for r in rows]
        for kind in ("start", "zone", "person_detected", "state", "greeting", "speech_in", "rfid_read", "reply", "stop"):
            self.assertIn(kind, kinds)
        self.assertEqual(next(r for r in rows if r["type"] == "zone"), {"t": 100.0, "type": "zone", "zone": "common", "source": "forced"})
        self.assertEqual(next(r for r in rows if r["type"] == "rfid_read")["title"], "Cosmos")
        reply = next(r for r in rows if r["type"] == "reply")
        self.assertEqual((reply["text"], reply["channel"]), ("Thanks! Cosmos is returned.", "speech"))
        times = [r["t"] for r in rows]
        self.assertEqual(times, sorted(times))

    def test_quiet_zone_answers_are_marked_as_shown_not_spoken(self):
        self.zone, self.channels = ["quiet"], [("gaze", "spotlight", "print")]
        self.send(ReplySentence("Cosmos is returned."),
                  ScreenUpdate("ENGAGED.ANSWER", "quiet", "split", view="book_card", payload={"message": "Cosmos is returned."}),
                  ScreenUpdate("ENGAGED.ANSWER", "quiet", "split", view="book_card", payload={"message": "Cosmos is returned."}),
                  ScreenUpdate("ENGAGED.ANSWER", "common", "full_face", view="face", payload={"caption": ""}))
        rows = self.rows()
        self.assertEqual(next(r for r in rows if r["type"] == "reply")["channel"], "screen")
        shown = [r for r in rows if r["type"] == "screen_text"]
        self.assertEqual([r["text"] for r in shown], ["Cosmos is returned."])        # once, and never for a common-area screen

    def test_the_trip_and_the_sound_level(self):
        self.send(NavigationRequested("", "Fiction", 1.234, -0.5, 0.0, section="Fiction", shelf="1-FIC-A"), NavArrived("g-1"),
                  ReturningProgress("unloading", 0, section="Fiction", shelf="1-FIC-A"))
        asyncio.run(self.bus.publish(NoiseReading(41.27, unit="dB", calibrated=True)))
        self.now[0] += 0.1
        asyncio.run(self.bus.publish(NoiseReading(55.0, unit="dB", calibrated=True)))     # too soon after the last: dropped
        rows = self.rows()
        goal = next(r for r in rows if r["type"] == "goal_sent")
        self.assertEqual((goal["x"], goal["section"]), (1.23, "Fiction"))
        self.assertIn("goal_reached", [r["type"] for r in rows])
        self.assertEqual(next(r for r in rows if r["type"] == "unload")["shelf"], "1-FIC-A")
        self.assertEqual([r["db"] for r in rows if r["type"] == "sound_level"], [41.3])

    def test_the_first_report_of_each_goal_is_its_drive_start(self):
        self.send(NavProgress("g-2", 3.0), NavProgress("g-2", 2.5), NavProgress("g-3", 1.0))
        rows = [r for r in self.rows() if r["type"] == "drive_started"]
        self.assertEqual([(r["goal"], r["distance_left"]) for r in rows], [("g-2", 3.0), ("g-3", 1.0)])      # once per goal

    def test_events_are_logged_before_the_dialogue_handles_them(self):
        bus, seen = Bus(), []

        async def slow_dialogue(event):
            seen.append(("dialogue", type(event).__name__))
            self.now[0] += 5.0                                      # it speaks for five seconds

        bus.subscribe(object, slow_dialogue)
        self.log.attach(bus)                                        # attached later, but runs first
        asyncio.run(bus.publish(PersonEngaged((1,))))
        rows = self.rows()
        self.assertEqual(next(r for r in rows if r["type"] == "person_detected")["t"], 100.0)

    def test_each_spoken_sentence_is_logged_with_its_length(self):
        self.log.on_speak("Hello! My name is Stuart.", 1.234)
        row = next(r for r in self.rows() if r["type"] == "speak")
        self.assertEqual((row["text"], row["seconds"], row["zone"]), ("Hello! My name is Stuart.", 1.23, "common"))

    def test_a_take_name_is_a_plain_word(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(take_dir("common-1_b", Path(d)).is_dir())
            for bad in ("", "../x", "a b", "a/b"):
                with self.assertRaises(ValueError):
                    take_dir(bad, Path(d))


class FrameRecorderTest(unittest.TestCase):
    def setUp(self):
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("OpenCV is not installed")

    def test_frames_are_repeated_to_keep_wall_clock_time(self):
        import cv2
        import numpy as np
        now = [10.0]
        with tempfile.TemporaryDirectory() as d:
            rec = FrameRecorder(Path(d) / "cam.mp4", fps=10, clock=lambda: now[0])
            frame = np.zeros((48, 64, 3), np.uint8)
            for _ in range(5):                       # one frame a second for 5 s: a 10 fps video of about 5 s
                rec.write(frame)
                now[0] += 1.0
            rec.write(frame)
            rec.close()
            self.assertEqual(rec.frames, 51)
            cap = cv2.VideoCapture(str(Path(d) / "cam.mp4"))
            self.assertTrue(cap.isOpened())
            self.assertEqual(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), 51)
            side = json.loads((Path(d) / "cam.mp4.json").read_text())
            self.assertEqual((side["started"], side["frames"]), (10.0, 51))

    def test_the_camera_feed_records_the_boxed_frame(self):
        import numpy as np

        class Cam:
            def __init__(self):
                self.frame = np.zeros((120, 160, 3), np.uint8)

            def peek(self):
                return self.frame

            def encode_jpeg(self, frame, quality=60):
                return b"jpg"

        class Rec:
            frames = []

            def write(self, frame):
                self.frames.append(frame)

        counter = type("C", (), {"detections": [{"box": [10, 60, 60, 10], "person": 1}]})()
        rec = Rec()
        feed = CameraFeed(Cam(), counter, "127.0.0.1", 1, recorder=rec)
        self.assertEqual(feed.jpeg(), b"jpg")
        self.assertEqual(len(rec.frames), 1)
        self.assertEqual(rec.frames[0].shape, (120, 160, 3))                          # full size, before shrinking
        self.assertGreater(int(rec.frames[0].sum()), 0)                              # with the box drawn on it


class FilmFlagsTest(unittest.TestCase):
    def test_record_needs_the_camera(self):
        from robot.sim.keyboard import run_sim
        err = io.StringIO()
        import contextlib
        with contextlib.redirect_stderr(err):
            code = asyncio.run(run_sim(load_config(), stdin=io.StringIO(""), out=io.StringIO(), log_sessions=False,
                                       record_path="/tmp/x.mp4"))
        self.assertEqual(code, 1)
        self.assertIn("--record needs the real webcam", err.getvalue())

    def test_the_cli_has_the_film_flags(self):
        import argparse
        from robot import __main__ as cli
        captured = {}

        def fake_run_sim(cfg, **kw):
            captured.update(kw)

            async def done():
                return 0
            return done()

        import robot.sim.keyboard as keyboard
        old = keyboard.run_sim
        keyboard.run_sim = fake_run_sim
        try:
            self.assertEqual(cli.main(["sim", "--hold-zone", "quiet", "--film", "q1", "--no-log"]), 0)
        finally:
            keyboard.run_sim = old
        self.assertEqual((captured["force_zone"], captured["film_take"], captured["record_path"]), ("quiet", "q1", None))
        with self.assertRaises(SystemExit), contextlib_quiet():
            cli.main(["sim", "--zone", "loud"])

    def test_the_pages_show_the_film_extras_only_with_the_parameter(self):
        face = (ROOT / "display" / "ui" / "index.html").read_text()
        self.assertIn('params.get("film") === "1"', face)
        self.assertIn("#b9591f", face)
        self.assertIn("#2f6f9f", face)
        op = (ROOT / "display" / "ui" / "operator.html").read_text()
        self.assertIn('get("film") === "map"', op)


def contextlib_quiet():
    import contextlib
    return contextlib.redirect_stderr(io.StringIO())


if __name__ == "__main__":
    unittest.main()


class VoiceHookTest(unittest.TestCase):
    def test_the_voice_reports_each_sentence_as_it_starts(self):
        import wave as wavemod
        from robot.actuation.voice import Voice, wav_seconds
        from robot.core.style import StyleVector

        def tone(text):
            buf = io.BytesIO()
            with wavemod.open(buf, "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(1000)
                w.writeframes(b"\0\0" * 500)
            return buf.getvalue()

        played, told = [], []
        audio = type("A", (), {"play": lambda self, wav, volume: played.append(len(told))})()
        voice = Voice(audio, tone)
        voice.on_sentence = lambda text, seconds: told.append((text, seconds))
        voice.say("Hello there. I am Stuart.", StyleVector())
        self.assertEqual(told, [("Hello there.", 0.5), ("I am Stuart.", 0.5)])
        self.assertEqual(played, [1, 2])                                    # told just before each one plays
        self.assertEqual(wav_seconds(b"not a wav"), 0.0)


class FaceOnlyScreenTest(unittest.TestCase):
    """styles.yaml `screen_content: false`: the screen shows only the face while the speech carries the words (the paper's common
    area). Off by default; the film takes switch it on with --config-set styles.common_alone.screen_content=false."""

    def app(self, face_only: bool):
        from robot.actuation.face import FaceDisplay
        from robot.app import build_app, fake_drivers
        from robot.sim.keyboard import apply_overrides
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        cfg = load_config()
        cfg.robot.setdefault("books", {})["online_fallback"] = False
        cfg.robot.setdefault("screen", {})["greeting_hold_s"] = 0
        if face_only:
            apply_overrides(cfg, ["styles.common_alone.screen_content=false"])
        app = build_app(cfg, fake_drivers(), face=FaceDisplay(enabled=False), log_sessions=False, persist_storage=False)
        app.screens = []
        app.bus.subscribe(ScreenUpdate, app.screens.append)
        return app

    def visit(self, app, zone):
        from robot.events import BookIdentified, ContextChanged, UserUtterance
        run(app, ContextChanged(zone, 1), PersonEngaged(), UserUtterance("tell me about cosmos"))
        return {u.view for u in app.screens}

    def test_off_by_default_a_common_area_shows_the_book(self):
        self.assertIn("book_card", self.visit(self.app(False), "common"))

    def test_face_only_common_area_never_shows_content(self):
        self.assertEqual(self.visit(self.app(True), "common"), {"face"})

    def test_the_quiet_zone_still_reads_on_the_screen(self):
        self.assertIn("book_card", self.visit(self.app(True), "quiet"))

    def test_the_style_says_so(self):
        app = self.app(True)
        run(app, ContextChanged("common", 1))
        self.assertFalse(app.style.screen_content)
        self.assertIn("screen=face-only", app.style.summary())
