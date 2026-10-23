import io
import logging
import math
import shutil
import struct
import subprocess
import unittest
import wave
from unittest import mock

from robot.services.speech import shape_voice


def tone(seconds=1.0, rate=24000, hz=220):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate)))
                               for i in range(int(seconds * rate))))
    return buf.getvalue()


def info(raw):
    with wave.open(io.BytesIO(raw)) as w:
        return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes() / w.getframerate()


class ShapeVoiceTest(unittest.TestCase):
    def test_one_or_more_leaves_the_voice_alone(self):
        wav = tone()
        self.assertIs(shape_voice(wav), wav)
        self.assertIs(shape_voice(wav, 1.2), wav)

    @unittest.skipUnless(shutil.which("sox"), "sox is not installed")
    def test_faster_pace_shortens_the_clip_and_keeps_the_format(self):
        wav = tone(2.0)
        quicker = shape_voice(wav, pace=1.25)
        (rate, ch, width, secs), (rate0, ch0, width0, secs0) = info(quicker), info(wav)
        self.assertEqual((rate, ch, width), (rate0, ch0, width0))
        self.assertAlmostEqual(secs, secs0 / 1.25, delta=0.1)

    @unittest.skipUnless(shutil.which("sox"), "sox is not installed")
    def test_deepening_keeps_the_format_and_the_pace(self):
        wav = tone()
        deeper = shape_voice(wav, 0.8)
        self.assertNotEqual(deeper, wav)
        (rate, ch, width, secs), (rate0, ch0, width0, secs0) = info(deeper), info(wav)
        self.assertEqual((rate, ch, width), (rate0, ch0, width0))
        self.assertAlmostEqual(secs, secs0, delta=0.1)

    def test_without_sox_the_voice_is_unchanged_and_says_why(self):
        wav = tone(0.1)
        with mock.patch("robot.services.speech.shutil.which", return_value=None):
            with self.assertLogs("robot.speech", level="WARNING") as logs:
                self.assertIs(shape_voice(wav, 0.8), wav)
        self.assertIn("sox is not installed", logs.output[0])

    def test_a_sox_failure_is_not_fatal(self):
        wav = tone(0.1)
        with mock.patch("robot.services.speech.shutil.which", return_value="/usr/bin/sox"), \
                mock.patch("robot.services.speech.subprocess.run", side_effect=subprocess.CalledProcessError(2, "sox")):
            with self.assertLogs("robot.speech", level="WARNING"):
                self.assertIs(shape_voice(wav, 0.8), wav)


class SpeechPortTest(unittest.TestCase):
    def test_tts_uses_its_own_port_and_the_llm_keeps_the_main_one(self):
        from robot.config import load_config
        from robot.services.speech import Speech
        cfg = load_config()
        self.assertEqual(Speech.from_config(cfg).port, 8791)
        self.assertEqual(cfg.ai_port, 7898)
        cfg.robot["ai_server"]["tts_port"] = None                      # unset: back to the main port
        self.assertEqual(Speech.from_config(cfg).port, cfg.ai_port)


if __name__ == "__main__":
    unittest.main()
