"""Push-to-talk: cutting a sentence out of the microphone's audio, transcribing it, and handing it to the dialogue as UserUtterance."""

import array
import asyncio
import threading
import unittest

from robot.bus import Bus
from robot.drivers.fake import FakeAudio
from robot.events import UserUtterance
from robot.operator import Operator
from robot.perception.listener import CHUNK_S, Capture, Listener, capture, rms, wav_bytes
from tests.test_dialogue import make_app

N = int(16000 * CHUNK_S)


def piece(level: int) -> bytes:
    """100 ms of a square wave whose RMS is `level`."""
    return array.array("h", [level, -level] * (N // 2)).tobytes()


QUIET, LOUD = piece(150), piece(3000)


def clip(*parts):
    """[(level piece, how many 100 ms pieces)] -> a list of pieces."""
    return [p for p, n in parts for _ in range(n)]


class CaptureTest(unittest.TestCase):
    def test_rms_is_the_level_of_the_samples(self):
        self.assertAlmostEqual(rms(piece(1000)), 1000, delta=1)
        self.assertEqual(rms(b""), 0.0)

    def test_a_sentence_is_cut_out_and_ends_after_a_pause(self):
        got = capture(clip((QUIET, 8), (LOUD, 10), (QUIET, 30)))
        self.assertEqual(got.reason, "speech")
        pieces = len(got.pcm) // len(LOUD)
        self.assertGreaterEqual(pieces, 10 + 10)                    # the speech, the 1 s pause, and a little before it
        self.assertLess(pieces, 10 + 10 + 4)

    def test_nothing_said_gives_up_after_wait_s(self):
        got = capture(clip((QUIET, 100)), wait_s=2.0)
        self.assertEqual((got.pcm, got.reason), (None, "silence"))

    def test_a_click_is_not_speech(self):
        got = capture(clip((QUIET, 8), (LOUD, 1), (QUIET, 30)))
        self.assertEqual((got.pcm, got.reason), (None, "click"))

    def test_a_noisy_room_raises_the_speech_level(self):
        noisy = piece(900)                                          # room noise above min_rms 500: speech must be 3 times that
        got = capture(clip((noisy, 80)))                            # a steady hum is not a person talking
        self.assertEqual((got.pcm, got.reason), (None, "silence"))
        got = capture(clip((noisy, 8), (piece(3000), 10), (noisy, 30)))      # but someone clearly louder than the hum is
        self.assertEqual(got.reason, "speech")

    def test_it_stops_at_max_s(self):
        got = capture(clip((QUIET, 6), (LOUD, 200)), max_s=3.0)
        self.assertEqual(got.reason, "speech")
        self.assertLessEqual(len(got.pcm) // len(LOUD), 31)

    def test_the_stop_event_ends_it_early(self):
        stop = threading.Event()

        def chunks():
            yield from clip((QUIET, 6), (LOUD, 5))
            stop.set()
            yield from clip((LOUD, 50))
        got = capture(chunks(), stop=stop)
        self.assertEqual(got.reason, "speech")
        self.assertLess(len(got.pcm) // len(LOUD), 20)

    def test_a_microphone_that_stops_is_reported(self):
        self.assertEqual(capture([]).reason, "stream")

    def test_wav_bytes_is_a_16k_mono_wav(self):
        import io
        import wave
        with wave.open(io.BytesIO(wav_bytes(LOUD * 5))) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 16000))


class ListenerTest(unittest.TestCase):
    def listener(self, heard, text="can you recommend something about space"):
        bus, audio, self.sent, self.events, self.said = Bus(), FakeAudio(), [], [], []
        audio.heard = heard
        bus.subscribe(UserUtterance, lambda e: self.events.append(e))

        def transcribe(wav):
            self.sent.append(wav)
            if isinstance(text, Exception):
                raise text
            return text
        return Listener(audio, transcribe, bus, status=lambda kind, t: self.events.append((kind, t)), say=self.said.append)

    def test_speech_becomes_a_user_utterance_from_the_mic(self):
        listener = self.listener(clip((QUIET, 8), (LOUD, 10), (QUIET, 30)))

        async def go():
            return await listener.listen()
        self.assertEqual(asyncio.run(go()), "can you recommend something about space")
        said = [e for e in self.events if isinstance(e, UserUtterance)]
        self.assertEqual([(e.text, e.source) for e in said], [("can you recommend something about space", "mic")])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][:4], b"RIFF")
        self.assertEqual([e[0] for e in self.events if isinstance(e, tuple)], ["listening", "hearing", "transcribing", "idle"])
        self.assertFalse(listener.active)

    def test_silence_says_so_and_sends_nothing_to_the_server(self):
        listener = self.listener(clip((QUIET, 100)))
        listener.wait_s = 2.0
        self.assertIsNone(asyncio.run(listener.listen()))
        self.assertEqual(self.sent, [])
        self.assertTrue(any("didn't hear" in t for t in self.said))
        self.assertFalse(any(isinstance(e, UserUtterance) for e in self.events))

    def test_an_empty_transcript_is_not_published(self):
        listener = self.listener(clip((QUIET, 8), (LOUD, 10), (QUIET, 30)), text="  ")
        self.assertIsNone(asyncio.run(listener.listen()))
        self.assertFalse(any(isinstance(e, UserUtterance) for e in self.events))

    def test_a_server_error_is_said_and_does_not_raise(self):
        listener = self.listener(clip((QUIET, 8), (LOUD, 10), (QUIET, 30)), text=OSError("no route to host"))
        self.assertIsNone(asyncio.run(listener.listen()))
        self.assertTrue(any("speech recognition failed" in t for t in self.said))
        self.assertFalse(listener.active)

    def test_a_second_listen_while_listening_ends_the_first(self):
        listener = self.listener([])

        async def go():
            listener.active = True
            self.assertIsNone(await listener.listen())
            self.assertTrue(listener._stop.is_set())
        asyncio.run(go())


class OperatorListenTest(unittest.TestCase):
    def test_listen_without_a_microphone_says_how_to_get_one(self):
        said = []
        op = Operator(make_app(), say=said.append)
        self.assertFalse(asyncio.run(op.run("listen")))
        self.assertTrue(any("--live" in t for t in said))

    def test_listen_starts_the_listener_and_a_second_listen_ends_it(self):
        app = make_app()
        calls = []

        class Stub:
            active = False

            async def listen(self):
                calls.append("listen")

            def finish(self):
                calls.append("finish")
        app.listener = Stub()
        op = Operator(app)

        async def go():
            self.assertTrue(await op.run("listen"))
            await op.listen_task
            app.listener.active = True
            self.assertTrue(await op.run("listen"))
        asyncio.run(go())
        self.assertEqual(calls, ["listen", "finish"])

    def test_the_operator_page_may_send_listen_and_has_a_talk_button(self):
        from display.server import OPERATOR_COMMANDS
        from pathlib import Path
        self.assertIn("listen", OPERATOR_COMMANDS)
        html = (Path(__file__).resolve().parents[1] / "display" / "ui" / "operator.html").read_text()
        self.assertIn('id="talk"', html)
        self.assertIn('send("listen")', html)

    def test_the_state_tells_the_page_what_the_microphone_is_doing(self):
        app = make_app()
        self.assertEqual(app.state_info()["mic"], "none")                  # no listener: the Talk button is disabled
        app.listener = object()
        app.mic_status("listening")
        self.assertEqual(app.state_info()["mic"], "listening")
        app.mic_status("idle", "hello there")
        self.assertEqual((app.state_info()["mic"], app.state_info()["heard"]), ("idle", "hello there"))


if __name__ == "__main__":
    unittest.main()
