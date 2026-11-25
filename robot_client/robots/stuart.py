"""
robots/stuart.py — Stuart, the library robot, as a CARES client
================================================================
The orchestration server holds the conversation; Stuart says it in his own
voice and shows it on his own face, the same way the library robot does:

  speech   the library robot's TTS (config/robot.yaml `voice:`, the local AI or
           the AI server by AI_MODE), reshaped with sox, played on its speaker
  mouth    the face page (python -m display.server) moves with the sound
  tags     [HAPPY], [SAD], [SURPRISED], [THINKING]... become face emotions
  mic      the kit's VoiceInputModule (VAD, or Enter to record), muted while
           Stuart talks

If the TTS server or the speaker is missing, the face still talks for as long
as the sentence would take, so the server's pacing and barge-in keep working.

    scripts/start_local_ai.sh start            # the TTS (or AI_MODE=remote)
    python3 -m display.server &                # the face, port 8765
    .venv/bin/python run.py stuart             # this client, WS on 8766
"""

import logging
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)            # the library robot's packages: robot/, vendor/

from robot.actuation.face import FaceDisplay, normalize  # noqa: E402
from robot.config import load_config  # noqa: E402
from robot.services.speech import Speech as RobotTTS  # noqa: E402

from client import BasicClient  # noqa: E402
from InputModules.text_input import TextInputModule  # noqa: E402
from InputModules.voice_input import VoiceInputModule  # noqa: E402
from OutputModules.console_output import ConsoleOutputModule  # noqa: E402
from OutputModules.sim_tts_output import SimTTSOutputModule  # noqa: E402

logger = logging.getLogger(__name__)

# Server tags with no face of their own; everything else goes through the face's
# emotion names and aliases (robot/core/expression.py: happy, sad, surprised...).
TAG_FACES = {"DEFAULT": "neutral", "GREETING": "happy", "WAVE": "happy", "CONFUSED": "thinking", "THINKING": "thinking"}
EMOTION_HOLD_S = 4.0


def face_for(tag: str):
    """The face emotion for a server tag ("HAPPY", "[WAVE]"), or None for one Stuart cannot show."""
    tag = tag.strip().strip("[]").upper()
    return TAG_FACES.get(tag) or normalize(tag)


class Speech(SimTTSOutputModule):
    """Stuart's voice. The queue, demo-step ACKs, barge-in and tts_state are the kit's;
    say() speaks one sentence and makes the next one while this one plays."""

    def __init__(self, name, config, tts=None, audio=None, face=None):
        super().__init__(name, config)
        self.tts, self.audio, self.face = tts, audio, face
        self.volume = float(self.config.get("volume", 1.0))
        self.base_pace = tts.pace if tts else 1.0
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stuart-tts")
        self._ready = {}                  # sentence -> Future of its WAV

    def _make(self, sentence):
        fut = self._ready.pop(sentence, None) or self._pool.submit(self.tts.synthesize, sentence)
        try:
            return fut.result()
        except (OSError, EOFError, ValueError) as e:
            logger.warning(f"[Stuart] text-to-speech failed ({e}); only the face talks")
            return None

    def _make_next(self):
        with self._progress_lock:
            sentences, i = self._current_sentences, self._current_idx
        if 0 <= i < len(sentences) - 1 and sentences[i + 1] not in self._ready:
            self._ready[sentences[i + 1]] = self._pool.submit(self.tts.synthesize, sentences[i + 1])

    def say(self, sentence: str):
        wav = self._make(sentence) if self.tts else None
        if self.tts:
            self._make_next()
        if wav is None or self.audio is None:
            if self.face:
                self.face.talk_text(sentence)
            return super().say(sentence)          # logs it and waits as long as saying it takes
        logger.info(f"[Stuart] {sentence[:70]}")
        if self.face:
            self.face.talk_audio(wav)
        try:
            self.audio.play(wav, self.volume)
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(f"[Stuart] could not play the speech ({e})")
            if self.face:
                self.face.stop_talking()

    def interrupt(self) -> str:
        self._ready.clear()
        return super().interrupt()

    def stop(self):
        super().stop()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def update_voice_config(self, voice_config: dict):
        """A persona's rate ("+10%") speeds Stuart up or slows him down. The TTS has one voice
        (af_heart, config/robot.yaml), so voice names and gender are not changed."""
        m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)%", str(voice_config.get("rate", "")).strip())
        if self.tts and m:
            self.tts.pace = max(0.5, self.base_pace * (1 + float(m.group(1)) / 100))
        logger.info(f"[Stuart] voice config {voice_config} -> pace {self.tts.pace if self.tts else '-'}")


class Robot(BasicClient):

    def __init__(self, config_file: str):
        super().__init__(config_file)
        cfg = load_config()                       # config/robot.yaml, AI_MODE / AI_HOST honoured
        display = cfg.display
        self.face = FaceDisplay(display["host"], int(display["port"]), background=True)
        audio = None
        try:
            from robot.drivers.audio import Audio
            audio = Audio(cfg.robot.get("devices", {}).get("speaker"))
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
            logger.warning(f"[Setup] No speaker ({e}): Stuart's face talks, nothing is heard")
        tts = RobotTTS.from_config(cfg)
        problem = tts.check()
        if problem:                               # kept anyway: the local AI may still be starting
            logger.warning(f"[Setup] {problem}")
        logger.info(f"[Setup] TTS {tts.host}:{tts.port} ({tts.engine or 'default'} {tts.voice}), "
                    f"face on {display['host']}:{display['port']}")

        if "speech" in self.config.get("modules", []) and not self.register_input_module(
                VoiceInputModule("voice_input", self.config.get("voice_config", {}))):
            logger.info("[Setup] No microphone (PyAudio): typed input instead")
            self.register_input_module(TextInputModule("text_input", {}))
        self.register_output_module(ConsoleOutputModule("console_output", self.config.get("console_config", {})))
        self.register_output_module(Speech("speech", self.config.get("speech_config", {}), tts, audio, self.face))

    def on_emotion_detected(self, emotion_tag: str):
        emotion = face_for(emotion_tag)
        if emotion:
            self.face.show_emotion(emotion, EMOTION_HOLD_S)
        else:
            logger.info(f"[Gesture] {emotion_tag.strip().upper()}: no face for it")
