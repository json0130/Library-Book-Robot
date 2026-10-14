"""Speak a message in the current style: TTS from the AI server, played at the style's volume.

The TTS server has no whispering voice yet, so "whisper" plays the normal voice quietly.
Without a TTS function (the simulator) it only logs what it would say. If the TTS server or the
speaker fails, it logs why and carries on, so one bad reply never aborts the dialogue step.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Callable, Optional

log = logging.getLogger("robot.voice")


class Voice:
    def __init__(self, audio, synthesize: Optional[Callable[[str], bytes]] = None, face=None,
                 whisper_volume: float = 0.35):
        self.audio = audio
        self.synthesize = synthesize        # text -> WAV bytes, e.g. services.speech.Speech.synthesize
        self.face = face                    # FaceDisplay, for the talking mouth
        self.whisper_volume = whisper_volume

    def say(self, text: str, style) -> None:
        volume = style.volume * (self.whisper_volume if style.voice == "whisper" else 1.0)
        log.info("[voice %s, volume %.2f] %s", style.voice, volume, text)
        if self.synthesize is None:
            if self.face is not None:
                self.face.talk_text(text)
            return
        try:
            wav = self.synthesize(text)
        except (OSError, EOFError, ValueError) as exc:
            log.warning("text-to-speech failed (%s); the reply is only shown", exc)
            return
        if self.face is not None:
            self.face.talk_audio(wav)
        try:
            self.audio.play(wav, volume)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not play the reply (%s)", exc)
            if self.face is not None:
                self.face.stop_talking()
