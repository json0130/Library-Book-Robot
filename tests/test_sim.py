import asyncio
import io
import logging
import subprocess
import unittest
from unittest import mock

from robot.actuation.face import FaceDisplay
from robot.actuation.voice import Voice
from robot.app import build_app, fake_drivers, sim_drivers
from robot.config import load_config
from robot.core.style import StyleVector
from robot.drivers.fake import FakeAudio, FakeFrame
from robot.events import ReplySentence
from robot.sim.keyboard import Sim, run_sim

MARTIAN = "9780553418026"


def make_sim(drivers=None):
    cfg = load_config()
    cfg.robot.setdefault("books", {})["online_fallback"] = False
    cfg.robot.setdefault("screen", {})["greeting_hold_s"] = 0
    app = build_app(cfg, drivers or fake_drivers(), face=FaceDisplay(enabled=False), log_sessions=False,
                    persist_storage=False)
    out = io.StringIO()
    sim = Sim(app, out)
    sim.said = []
    app.bus.subscribe(ReplySentence, lambda e: sim.said.append(e.text))
    return sim, out


def run(sim, *lines):
    async def go():
        for line in lines:
            await sim.handle(line)
    asyncio.run(go())


class RealWebcamLikeCamera:
    """Looks like the real Camera to the sim: no show()/clear(). Shows the Martian's barcode (as a fake frame)."""

    static = False

    def read_frame(self, timeout=2.0):
        return FakeFrame(MARTIAN)

    def close(self):
        pass


def sim_with_real_camera():
    drivers = fake_drivers()
    drivers.camera = RealWebcamLikeCamera()
    sim, out = make_sim(drivers)
    sim.app.scanner.decode = lambda frame: frame.isbn        # no pyzbar needed to test the routing
    return sim, out


class SimTypingTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_plain_sentences_are_the_visitor_speaking(self):
        sim, _ = make_sim()
        run(sim, "book " + MARTIAN, "can you explain it?")
        self.assertEqual(sim.app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
        self.assertIn("astronaut", " ".join(sim.said))                  # the catalogue summary was said
        run(sim, "hello there")                                          # not a command: said to the robot
        self.assertEqual(sim.app.dialogue.state, "ENGAGED.ANYTHING_ELSE")   # still about the book (no LLM to answer)
        self.assertIn("place it on my pad", sim.said[-2])
        run(sim, "can I scan another book?")
        self.assertEqual(sim.app.dialogue.state, "ENGAGED.AWAIT_BOOK")

    def test_recommend_by_sentence(self):
        sim, _ = make_sim()
        run(sim, "book " + MARTIAN, "could you recommend something similar?")
        self.assertIn("If you like The Martian", " ".join(sim.said))

    def test_commands_with_bad_arguments_are_not_spoken(self):
        sim, out = make_sim()
        run(sim, "feel")
        self.assertIn("usage problem with `feel`", out.getvalue())
        self.assertEqual(sim.said, [])                                   # the robot did not hear "feel"

    def test_say_prefix_and_zone_commands_still_work(self):
        sim, _ = make_sim()
        run(sim, "say hi", "quiet group")
        self.assertEqual(sim.app.context.key, "quiet_group")

    def test_book_with_the_real_webcam_scans_what_it_sees(self):
        sim, out = sim_with_real_camera()
        run(sim, "book")
        self.assertIn("hold the book's barcode to the camera", out.getvalue())
        self.assertEqual(sim.app.dialogue.state, "ENGAGED.OFFER")
        run(sim, "can you explain it")
        self.assertEqual(sim.app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
        run(sim, "scan")                                                  # `scan` alone is the same as `book`
        self.assertEqual(sim.app.dialogue.state, "ENGAGED.OFFER")

    def test_fake_camera_only_commands_say_so_with_the_real_webcam(self):
        sim, out = sim_with_real_camera()
        run(sim, "book " + MARTIAN, "book none", "scan photo.png")
        self.assertEqual(out.getvalue().count("fake camera (run without --camera)"), 3)
        self.assertEqual(sim.app.dialogue.state, "IDLE")


class WhereAndGuideCommandsTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_where_finds_a_book_by_a_loosely_typed_title(self):
        sim, _ = make_sim()
        run(sim, "where dune")
        self.assertIn("Dune is on shelf 2-SF-B, in the Science Fiction section on level 2", sim.said[-1])
        run(sim, "where hitchhikers")
        self.assertIn("The Hitchhiker's Guide to the Galaxy is on shelf 2-SF-A", sim.said[-1])

    def test_where_takes_an_isbn_and_says_when_nothing_matches(self):
        sim, out = make_sim()
        run(sim, "where 9780345539434")
        self.assertIn("Cosmos is on shelf 3-SCI-A", sim.said[-1])
        before = len(sim.said)
        run(sim, "where nonexistent thing")
        self.assertEqual(len(sim.said), before)
        self.assertIn("no book in the catalogue matches 'nonexistent thing'", out.getvalue())

    def test_where_alone_is_a_usage_hint_not_a_crash(self):
        sim, out = make_sim()
        run(sim, "where")
        self.assertIn("usage problem with `where`", out.getvalue())

    def test_guide_after_where_publishes_the_pose_and_the_fake_base_logs_it(self):
        sim, _ = make_sim()
        run(sim, "where dune", "guide")
        self.assertIn("Okay, follow me to Dune", sim.said[-1])
        self.assertIn(("navigate", {"x": 1.0, "y": 4.0, "yaw": 1.57}), sim.app.actuators.nav.esp32.sent)

    def test_the_full_flow_scan_recommend_guide(self):
        sim, _ = make_sim()
        run(sim, "book 9780345539434", "recommend", "guide")
        self.assertIn("A Brief History of Time", sim.said[-2])
        self.assertIn("follow me to A Brief History of Time", sim.said[-1])
        self.assertIn(("navigate", {"x": 4.0, "y": 1.0, "yaw": 3.14}), sim.app.actuators.nav.esp32.sent)

    def test_guide_with_nowhere_to_go(self):
        sim, _ = make_sim()
        run(sim, "guide")
        self.assertIn("Ask me where a book is", sim.said[-1])
        self.assertNotIn("navigate", [cmd for cmd, _ in sim.app.actuators.nav.esp32.sent])

    def test_reload_reads_the_data_files_again_and_survives_a_broken_file(self):
        sim, out = make_sim()
        run(sim, "reload")
        self.assertIn("reloaded: 12 books, 7 shelves, 4 links", out.getvalue())
        sim.app.finder.graph._sources = ("/nonexistent/catalog.csv", None, None, None)         # as if the file were broken
        run(sim, "reload", "where dune")
        self.assertIn("could not reload, keeping the old data", out.getvalue())
        self.assertIn("Dune is on shelf 2-SF-B", sim.said[-1])                                  # the old data still works

    def test_the_help_lists_the_new_commands(self):
        sim, out = make_sim()
        run(sim, "help")
        self.assertIn("where <title or isbn>", out.getvalue())
        self.assertIn("guide ", out.getvalue())


class VoiceFailureTest(unittest.TestCase):
    def setUp(self):
        self.style = StyleVector(voice="normal", volume=0.8)

    def voice(self, synthesize, audio=None):
        face = mock.Mock()
        return Voice(audio or FakeAudio(), synthesize, face, 0.35), face

    def test_tts_server_failure_is_logged_not_raised(self):
        voice, face = self.voice(mock.Mock(side_effect=OSError("server down")))
        with self.assertLogs("robot.voice", level="WARNING") as logs:
            voice.say("Hello.", self.style)
        self.assertIn("text-to-speech failed", logs.output[0])
        face.talk_audio.assert_not_called()

    def test_bad_tts_answer_is_logged_not_raised(self):
        voice, _ = self.voice(mock.Mock(side_effect=ValueError("invalid WAV")))
        with self.assertLogs("robot.voice", level="WARNING"):
            voice.say("Hello.", self.style)

    def test_speaker_failure_stops_the_mouth(self):
        audio = mock.Mock()
        audio.play.side_effect = subprocess.CalledProcessError(1, "paplay")
        voice, face = self.voice(lambda text: b"wav", audio)
        with self.assertLogs("robot.voice", level="WARNING"):
            voice.say("Hello.", self.style)
        face.talk_audio.assert_called_once()
        face.stop_talking.assert_called_once()

    def test_normal_speech_plays_at_the_style_volume(self):
        audio = FakeAudio()
        voice, face = self.voice(lambda text: b"wav", audio)
        with self.assertLogs("robot.voice", level="INFO"):
            voice.say("Hello.", self.style)
        self.assertEqual(audio.played, [(3, 0.8)])
        face.talk_audio.assert_called_once_with(b"wav")

    def test_dialogue_step_survives_a_tts_failure(self):
        sim, _ = make_sim()
        sim.app.actuators.voice.synthesize = mock.Mock(side_effect=OSError("down"))
        with self.assertLogs("robot.voice", level="WARNING"):
            run(sim, "book " + MARTIAN, "explain")
        self.assertEqual(sim.app.dialogue.state, "ENGAGED.ANYTHING_ELSE")   # the step ran to its end


class SentenceBySentenceTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.style = StyleVector(voice="normal", volume=0.8)

    def test_a_reply_is_made_and_played_one_sentence_at_a_time(self):
        made = []
        audio = FakeAudio()
        voice = Voice(audio, lambda text: made.append(text) or text.encode(), None)
        voice.say("Dune is about a desert planet. It is a big epic! Would you like it?", self.style)
        self.assertEqual(made, ["Dune is about a desert planet.", "It is a big epic!", "Would you like it?"])
        self.assertEqual(len(audio.played), 3)

    def test_the_fixed_lines_are_made_ahead_and_not_asked_for_again(self):
        made = []
        voice = Voice(FakeAudio(), lambda text: made.append(text) or b"wav", None)
        voice.prepare(["Hello! My name is Stuart."])
        self.assertEqual(made, ["Hello!", "My name is Stuart."])
        voice.say("Hello! My name is Stuart.", self.style)
        self.assertEqual(len(made), 2)                                   # spoken from the cache: no new request

    def test_a_long_first_sentence_is_cut_at_its_first_comma_so_the_first_words_come_sooner(self):
        made = []
        voice = Voice(FakeAudio(), lambda text: made.append(text) or b"wav", None)
        voice.say("I can take back a book, tell you about one, recommend something, or show you where a topic is. What would you like?",
                  self.style)
        self.assertEqual(made[0], "I can take back a book,")
        self.assertEqual(made[1], "tell you about one, recommend something, or show you where a topic is.")
        self.assertEqual(made[2], "What would you like?")
        made.clear()
        voice.say("Short one, with a comma. And another.", self.style)
        self.assertEqual(made, ["Short one, with a comma.", "And another."])          # short sentences are left alone

    def test_the_fixed_lines_are_kept_on_disk_and_survive_a_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            made = []
            first = Voice(FakeAudio(), lambda text: made.append(text) or b"wav", None, cache_dir=d, cache_tag="a")
            first.prepare(["Hello! My name is Stuart."])
            self.assertEqual(len(made), 2)
            again = Voice(FakeAudio(), lambda text: made.append(text) or b"wav", None, cache_dir=d, cache_tag="a")
            again.say("Hello! My name is Stuart.", self.style)
            self.assertEqual(len(made), 2)                                # read from disk, not asked of the server
            other = Voice(FakeAudio(), lambda text: made.append(text) or b"wav", None, cache_dir=d, cache_tag="b voice")
            other.say("Hello!", self.style)
            self.assertEqual(len(made), 3)                                # a different voice does not reuse it

    def test_a_failure_part_way_stops_the_reply_without_raising(self):
        def synth(text):
            if "second" in text:
                raise OSError("server down")
            return b"wav"
        audio = FakeAudio()
        voice = Voice(audio, synth, None)
        logging.disable(logging.NOTSET)
        with self.assertLogs("robot.voice", level="WARNING"):
            voice.say("The first one. The second one. The third one.", self.style)
        self.assertEqual(len(audio.played), 1)


class SimStartupTest(unittest.TestCase):
    def test_sim_drivers_default_to_fakes(self):
        d = sim_drivers(load_config())
        self.assertTrue(hasattr(d.camera, "show"))                      # the fake camera

    def test_a_missing_webcam_is_a_clean_error(self):
        cfg = load_config()
        err = io.StringIO()
        with mock.patch("robot.sim.keyboard.sim_drivers", side_effect=OSError("Could not open camera 0")), \
                mock.patch("sys.stderr", err):
            code = asyncio.run(run_sim(cfg, io.StringIO(""), io.StringIO(), log_sessions=False, use_camera=True))
        self.assertEqual(code, 1)
        self.assertIn("Cannot start the simulator: Could not open camera 0", err.getvalue())


if __name__ == "__main__":
    unittest.main()
