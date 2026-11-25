"""The operator page's Start / Stop: stopped, the camera shows but no face makes the robot say hello and no book is taken."""

import asyncio
import unittest
from pathlib import Path

from robot.operator import Operator
from tests.test_people import make_counter, steps
from tests.test_state_machine import new_app

ROOT = Path(__file__).resolve().parents[1]


class FaceCounterPauseTest(unittest.TestCase):
    def test_a_paused_counter_does_not_sample_and_forgets_who_it_saw(self):
        paused = [False]
        counter, seen, clock = make_counter(1, 1, 1, paused=lambda: paused[0])
        steps(counter, clock, 2)
        self.assertEqual(counter.count, 1)
        paused[0] = True
        counter.interval_s = 0

        async def one_tick():
            task = asyncio.ensure_future(counter.run())
            await asyncio.sleep(0.01)
            task.cancel()
        asyncio.run(one_tick())
        self.assertEqual((counter.count, counter.detections), (0, []))
        self.assertEqual(len(counter.vision.script), 1)              # the camera was not asked while paused

    def test_forget_resets_the_engagement_too(self):
        from robot.perception.people import EngagementDetector
        counter, _, _ = make_counter(engagement=EngagementDetector())
        counter.engagement.state = "engaged"
        counter.forget()
        self.assertEqual(counter.engagement.state, "absent")


class StartStopCommandTest(unittest.TestCase):
    def test_stop_ends_the_visit_quietly_and_start_runs_again(self):
        app = new_app()
        said = []
        op = Operator(app, say=said.append)
        asyncio.run(op.run("engage"))
        self.assertNotEqual(app.dialogue.state, "IDLE")
        self.assertTrue(asyncio.run(op.run("system", "stop")))
        self.assertFalse(app.active)
        self.assertEqual(app.dialogue.state, "IDLE")
        self.assertFalse(app.state_info()["active"])
        self.assertTrue(asyncio.run(op.run("system", "start")))
        self.assertTrue(app.active and app.state_info()["active"])
        self.assertFalse(asyncio.run(op.run("system", "maybe")))
        self.assertIn("usage: system start|stop", said[-1])

    def test_the_display_server_passes_the_command_on(self):
        from display.server import OPERATOR_COMMANDS
        self.assertIn("system", OPERATOR_COMMANDS)

    def test_the_operator_page_has_the_button(self):
        page = (ROOT / "display" / "ui" / "operator.html").read_text()
        self.assertIn('id="sysbtn"', page)
        self.assertIn('send("system"', page)


class DefaultCameraTest(unittest.TestCase):
    """From a terminal the webcam and faces are on and the robot starts stopped; piped input keeps the fake camera."""

    def run_cli(self, argv, tty):
        import sys
        import robot.__main__ as cli
        import robot.sim.keyboard as keyboard
        captured = {}

        def fake_run_sim(cfg, **kw):
            captured.update(kw)

            async def done():
                return 0
            return done()

        class Stdin:
            def isatty(self):
                return tty
        old_run, old_stdin = keyboard.run_sim, sys.stdin
        keyboard.run_sim, sys.stdin = fake_run_sim, Stdin()
        try:
            self.assertEqual(cli.main(["sim", "--no-log", *argv]), 0)
        finally:
            keyboard.run_sim, sys.stdin = old_run, old_stdin
        return captured

    def test_from_a_terminal(self):
        kw = self.run_cli([], tty=True)
        self.assertEqual((kw["use_camera"], kw["use_faces"], kw["start_stopped"], kw["camera_optional"]), (True, True, True, True))

    def test_start_running_at_once(self):
        self.assertFalse(self.run_cli(["--start"], tty=True)["start_stopped"])

    def test_no_camera(self):
        kw = self.run_cli(["--no-camera"], tty=True)
        self.assertEqual((kw["use_camera"], kw["use_faces"], kw["start_stopped"]), (False, False, False))

    def test_piped_input_keeps_the_fake_camera(self):
        kw = self.run_cli([], tty=False)
        self.assertEqual((kw["use_camera"], kw["use_faces"], kw["start_stopped"]), (False, False, False))

    def test_asking_for_the_camera_makes_it_required(self):
        kw = self.run_cli(["--camera"], tty=False)
        self.assertEqual((kw["use_camera"], kw["camera_optional"]), (True, False))


if __name__ == "__main__":
    unittest.main()


class FaceDetectorChoiceTest(unittest.TestCase):
    def test_a_server_that_does_not_answer_falls_back_to_opencv(self):
        from robot.config import load_config
        from robot.sim.keyboard import face_detector
        cfg = load_config()
        cfg.robot["ai_server"].update(host="127.0.0.1", port=9, mode="remote")     # nothing listens on port 9
        said = []

        class S:
            def say(self, text):
                said.append(text)
        try:
            from robot.services.local_faces import LocalFaceDetector
            LocalFaceDetector()
        except Exception:
            self.skipTest("OpenCV faces not available here")
        detector, by = face_detector(cfg, S())
        self.assertEqual(type(detector).__name__, "LocalFaceDetector")
        self.assertIn("OpenCV", by)
        self.assertIn("does not answer", said[-1])

    def test_the_page_is_told_when_detection_fails(self):
        counter, _, clock = make_counter(OSError("server down"), 1)
        told = []
        counter.on_status = lambda: told.append(counter._last_error)
        counter.interval_s = 0

        async def two_ticks():
            task = asyncio.ensure_future(counter.run())
            await asyncio.sleep(0.02)
            task.cancel()
        try:
            asyncio.run(two_ticks())
        except IndexError:
            pass
        self.assertEqual(told[0], "server down")
