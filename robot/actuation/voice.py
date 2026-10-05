"""Speak a message in the current style: TTS from the AI server, played at the style's volume.

The TTS server has no whispering voice yet, so "whisper" plays the normal voice quietly.
Without a TTS function (the simulator) it only logs what it would say.
"""

from __future__ import annotations

import logging
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
        wav = self.synthesize(text)
        if self.face is not None:
            self.face.talk_audio(wav)
        self.audio.play(wav, volume)
