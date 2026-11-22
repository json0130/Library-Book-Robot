"""Hear the visitor: record from the microphone, stop when they stop talking, transcribe on the AI server, and publish
the words as a UserUtterance (source "mic"), so the state machine treats speech exactly like typed text.

It is push-to-talk: the Talk button on the operator page (or `listen` typed in the simulator) opens the microphone once.
A second press ends the recording early. The robot does not listen all the time, so it never answers its own voice.

How it decides when to stop (all measured on 100 ms pieces of 16 kHz audio):

    waiting   nothing louder than the speech level yet: give up after `wait_s` ("I didn't hear anything")
    speaking  the visitor is talking: keep recording
    done      `silence_s` of quiet after speech, or `max_s` in all

The speech level is max(listen.min_rms, 3 x the room's noise measured in the first half second), so a noisy
room does not keep it recording. Shorter than `min_speech_s` of speech counts as a click and is thrown away.
"""

from __future__ import annotations

import array
import asyncio
import io
import logging
import math
import sys
import threading
import wave
from contextlib import closing
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from robot.events import UserUtterance

log = logging.getLogger("robot.listener")

RATE = 16000
SAMPLE_WIDTH = 2
CHUNK_S = 0.1
FLOOR_FACTOR = 3.0                # speech must be this much louder than the room's own noise
FLOOR_CHUNKS = 5                  # the first half second measures the room's own noise
PRE_ROLL_CHUNKS = 3               # keep this much from before the first loud piece, so the first word is not clipped


def rms(pcm: bytes) -> float:
    """Loudness of 16-bit little-endian mono PCM (the same scale as the min_speech_rms setting)."""
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return math.sqrt(sum(x * x for x in samples) / len(samples)) if samples else 0.0


def wav_bytes(pcm: bytes, rate: int = RATE) -> bytes:
    """Raw 16-bit mono PCM as a WAV file."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


@dataclass
class Capture:
    pcm: Optional[bytes]          # the speech, or None
    reason: str                   # "speech" | "silence" (nothing was said) | "click" (too short) | "stream" (the mic stopped)
    level: float = 0.0            # the speech's RMS, for tuning min_speech_rms


def capture(chunks: Iterable[bytes], min_rms: float = 500, wait_s: float = 6.0, max_s: float = 15.0,
            silence_s: float = 1.0, min_speech_s: float = 0.3, chunk_s: float = CHUNK_S,
            stop: Optional[threading.Event] = None,
            on_speaking: Callable[[], None] = lambda: None) -> Capture:
    """Cut one utterance out of a stream of audio pieces. `stop` (set from another thread) ends it now."""
    wait_n, max_n, quiet_n = int(wait_s / chunk_s), int(max_s / chunk_s), max(1, int(silence_s / chunk_s))
    need_n = max(1, int(min_speech_s / chunk_s))
    recent: list = []                       # the pieces before the speech started
    floor_levels: list = []
    kept: list = []
    loud_n = quiet_run = total = 0
    speaking = False
    threshold = float(min_rms)
    for chunk in chunks:
        total += 1
        level = rms(chunk)
        if len(floor_levels) < FLOOR_CHUNKS and not speaking:
            floor_levels.append(level)
            threshold = max(float(min_rms), FLOOR_FACTOR * min(floor_levels))
        loud = level >= threshold
        if not speaking:
            recent = (recent + [chunk])[-PRE_ROLL_CHUNKS:]
            if loud:
                speaking = True
                kept = list(recent)
                loud_n = 1
                on_speaking()
            elif total >= wait_n or (stop is not None and stop.is_set()):
                return Capture(None, "silence")
            continue
        kept.append(chunk)
        loud_n += loud
        quiet_run = 0 if loud else quiet_run + 1
        if quiet_run >= quiet_n or total >= max_n or (stop is not None and stop.is_set()):
            break
    else:
        if not speaking:
            return Capture(None, "stream")
    if loud_n < need_n:
        return Capture(None, "click")
    pcm = b"".join(kept)
    return Capture(pcm, "speech", rms(pcm))


class Listener:
    """audio: has stream(chunk_s) giving raw PCM pieces (drivers/audio.py). transcribe: WAV bytes -> text
    (services.speech.Speech.transcribe). status(kind, text) is told what is happening, for the operator page."""

    def __init__(self, audio, transcribe: Callable[[bytes], str], bus, min_rms: float = 500, wait_s: float = 6.0,
                 max_s: float = 15.0, silence_s: float = 1.0, status: Callable[[str, str], None] = lambda kind, text: None,
                 say: Callable[[str], None] = lambda text: None):
        self.audio, self.transcribe, self.bus = audio, transcribe, bus
        self.min_rms, self.wait_s, self.max_s, self.silence_s = min_rms, wait_s, max_s, silence_s
        self.status, self.say = status, say
        self.active = False
        self._stop = threading.Event()

    @classmethod
    def from_config(cls, cfg, audio, speech, bus, **kw) -> "Listener":
        heard = cfg.robot.get("listen") or {}
        return cls(audio, speech.transcribe, bus,
                   min_rms=float(heard.get("min_rms") or 300),
                   wait_s=float(heard.get("wait_s", 6)), max_s=float(heard.get("max_s", 15)),
                   silence_s=float(heard.get("silence_s", 1.0)), **kw)

    def finish(self) -> None:
        """End the recording now (the Talk button pressed a second time)."""
        self._stop.set()

    def _record(self, loop) -> Capture:
        def speaking() -> None:
            loop.call_soon_threadsafe(self.status, "hearing", "")
        with closing(self.audio.stream(CHUNK_S)) as chunks:
            return capture(chunks, self.min_rms, self.wait_s, self.max_s, self.silence_s, stop=self._stop,
                           on_speaking=speaking)

    async def listen(self) -> Optional[str]:
        """Listen for one utterance, publish it, and return the words (None if nothing was understood).
        A second call while listening ends the recording instead."""
        if self.active:
            self.finish()
            return None
        self.active, loop = True, asyncio.get_running_loop()
        self._stop.clear()
        text = None
        try:
            self.status("listening", "")
            self.say("  listening... talk now")
            try:
                got = await loop.run_in_executor(None, self._record, loop)
            except (OSError, ValueError) as exc:
                self.say(f"  the microphone failed: {exc}")
                self.status("idle", "the microphone failed")
                return None
            if got.pcm is None:
                note = {"silence": "I didn't hear anything", "click": "that was too short to be speech",
                        "stream": "the microphone stopped sending sound"}[got.reason]
                self.say(f"  {note}")
                self.status("idle", note)
                return None
            self.status("transcribing", "")
            self.say(f"  heard {len(got.pcm) / (RATE * SAMPLE_WIDTH):.1f} s of speech (level {got.level:.0f}); transcribing...")
            try:
                text = (await loop.run_in_executor(None, self.transcribe, wav_bytes(got.pcm))).strip()
            except (OSError, EOFError, ValueError, KeyError) as exc:
                self.say(f"  speech recognition failed: {exc.__class__.__name__}: {exc}")
                self.status("idle", "speech recognition failed")
                return None
            if not text:
                self.say("  nothing was recognised")
                self.status("idle", "nothing recognised")
                return None
            log.info("[mic] %s", text)
            self.say(f"  heard: {text!r}")
            self.status("idle", text)
            await self.bus.publish(UserUtterance(text, source="mic"))
            return text
        finally:
            self.active = False
