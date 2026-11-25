"""The voice tells the base when it is making noise, so the base can mark those microphone readings as the robot's own."""

import unittest

from robot.actuation.voice import Voice
from robot.core.style import StyleVector
from robot.drivers.fake import FakeAudio


def style(voice="normal"):
    return StyleVector(voice=voice, volume=0.8)


class SpeakingFlagTest(unittest.TestCase):
    def voice(self, synth=lambda text: b"WAV:" + text.encode(), audio=None):
        calls = []
        audio = audio or FakeAudio()
        original = audio.play
        audio.play = lambda wav, volume=1.0: (calls.append(("play", wav)), original(wav, volume))[1]
        v = Voice(audio, synth, on_speaking=lambda flag: calls.append(("speaking", flag)))
        return v, calls

    def test_true_when_the_first_sentence_starts_and_false_when_the_last_ends(self):
        v, calls = self.voice()
        v.say("Hello there. How are you today? Nice to meet you.", style())
        kinds = [c if c[0] == "speaking" else ("play",) for c in calls]
        self.assertEqual(kinds, [("speaking", True), ("play",), ("play",), ("play",), ("speaking", False)])

    def test_true_once_for_a_whole_reply_not_once_per_sentence(self):
        v, calls = self.voice()
        v.say("One. Two. Three.", style())
        self.assertEqual([c for c in calls if c[0] == "speaking"], [("speaking", True), ("speaking", False)])

    def test_the_chime_is_the_robots_noise_too(self):
        v, calls = self.voice()
        v.chime(style())
        self.assertEqual([c if c[0] == "speaking" else ("play",) for c in calls], [("speaking", True), ("play",), ("speaking", False)])

    def test_false_even_when_the_speaker_fails(self):
        class Broken(FakeAudio):
            def play(self, wav, volume=1.0):
                raise OSError("no speaker")
        calls = []
        v = Voice(Broken(), lambda t: b"x", on_speaking=calls.append)
        v.say("Hello.", style())
        v.chime(style())
        self.assertEqual(calls, [True, False, True, False])

    def test_nothing_is_sent_when_nothing_is_played(self):
        calls = []
        v = Voice(FakeAudio(), None, on_speaking=calls.append)                     # the simulator without --speak
        v.say("Hello.", style())
        self.assertEqual(calls, [])
        def failing(text):
            raise OSError("tts down")
        v = Voice(FakeAudio(), failing, on_speaking=calls.append)
        v.say("Hello.", style())                                                   # the reply is only shown
        self.assertEqual(calls, [])

    def test_a_broken_listener_does_not_stop_the_speech(self):
        def boom(flag):
            raise RuntimeError("link down")
        audio = FakeAudio()
        v = Voice(audio, lambda t: b"x", on_speaking=boom)
        with self.assertLogs("robot.voice", "ERROR"):
            v.say("Hello.", style())
        self.assertEqual(len(audio.played), 1)

    def test_without_a_base_the_voice_is_as_before(self):
        audio = FakeAudio()
        Voice(audio, lambda t: b"x").say("Hello.", style())
        self.assertEqual(len(audio.played), 1)

    def test_the_apps_voice_reports_to_the_link(self):
        from tests.test_returning_link import link_app
        app, link = link_app()
        app.actuators.voice.synthesize = lambda text: b"x"
        app.actuators.voice.say("Hello.", style())
        self.assertEqual(link.speaking, [True, False])


if __name__ == "__main__":
    unittest.main()
