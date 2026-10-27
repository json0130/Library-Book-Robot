"""Speak a message in the current style: TTS from the AI server, played at the style's volume.

The TTS server has no whispering voice yet, so "whisper" plays the normal voice quietly.
Without a TTS function (the simulator) it only logs what it would say. If the TTS server or the
speaker fails, it logs why and carries on, so one bad reply never aborts the dialogue step.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("robot.voice")


FIRST_CHUNK_OVER = 60            # a first sentence longer than this is made in two pieces, so the first words come sooner
FIRST_CHUNK_MIN = 20             # ...the first piece is at least this long


def split_sentences(text: str) -> list:
    from robot.services.llm import split_sentences as split
    return [s.strip() for s in split(text) if s.strip()]


def split_first(sentences: list) -> list:
    """The TTS server takes about 3 s plus 0.15 s per character, so a long first sentence is cut at its first comma
    (or dash, or semicolon) after FIRST_CHUNK_MIN characters: the first piece is ready in a third of the time."""
    if not sentences or len(sentences[0]) <= FIRST_CHUNK_OVER:
        return sentences
    first = sentences[0]
    for i, ch in enumerate(first):
        if ch in ",;:" and i + 1 >= FIRST_CHUNK_MIN and len(first) - i > FIRST_CHUNK_MIN:
            return [first[:i + 1].strip(), first[i + 1:].strip()] + sentences[1:]
    return sentences


class Voice:
    def __init__(self, audio, synthesize: Optional[Callable[[str], bytes]] = None, face=None,
                 whisper_volume: float = 0.35, cache_dir: Optional[Path] = None, cache_tag: str = ""):
        self.audio = audio
        self.synthesize = synthesize        # text -> WAV bytes, e.g. services.speech.Speech.synthesize
        self.face = face                    # FaceDisplay, for the talking mouth
        self.whisper_volume = whisper_volume
        self._cache: dict = {}                      # sentence -> WAV, for the robot's fixed lines
        self.cache_dir = Path(cache_dir) if cache_dir else None     # the fixed lines also kept on disk: instant after a restart
        self.cache_tag = cache_tag                  # voice and shaping: a changed voice does not reuse old speech
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")

    def chime(self, style) -> None:
        """A soft two-note chime at the style's volume ("excuse me"). Never raises: a missing speaker is logged."""
        from robot.actuation.chime import chime_wav
        volume = style.volume * 0.6
        log.info("[voice chime, volume %.2f]", volume)
        try:
            self.audio.play(chime_wav(), volume)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not play the chime (%s)", exc)

    CACHE_MAX = 64

    def _wav(self, text: str) -> bytes:
        """The speech for one sentence: from the cache (the robot's fixed lines are made ahead, see prepare), else
        from the TTS server. Raises like synthesize()."""
        wav = self._cache.get(text)
        if wav is None:
            wav = self._from_disk(text)
        if wav is None:
            wav = self.synthesize(text)
            if len(self._cache) < self.CACHE_MAX:
                self._cache[text] = wav
        return wav

    def _disk_path(self, text: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / (hashlib.sha1(f"{self.cache_tag}|{text}".encode("utf-8")).hexdigest()[:20] + ".wav")

    def _from_disk(self, text: str) -> Optional[bytes]:
        path = self._disk_path(text)
        try:
            wav = path.read_bytes() if path is not None and path.exists() else None
        except OSError:
            return None
        if wav:
            self._cache[text] = wav
        return wav or None

    def _to_disk(self, text: str, wav: bytes) -> None:
        path = self._disk_path(text)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(wav)
            tmp.replace(path)
        except OSError as exc:
            log.info("could not keep the speech for %r on disk (%s)", text, exc)

    def prepare(self, texts) -> None:
        """Make the speech for lines the robot says often (hello, the offer, goodbye) now, so they are spoken
        at once later. Blocking: run it in a thread at start-up. Failures are logged and skipped."""
        if self.synthesize is None:
            return
        for text in texts:
            for sentence in split_first(split_sentences(text)):       # the same pieces say() will ask for
                try:
                    wav = self._wav(sentence)
                    if self._disk_path(sentence) is not None and not self._disk_path(sentence).exists():
                        self._to_disk(sentence, wav)
                except (OSError, EOFError, ValueError) as exc:
                    log.info("could not prepare %r (%s)", sentence, exc)
                    return

    def say(self, text: str, style) -> None:
        """Speak sentence by sentence: while one plays, the next is being made, so the first words come as soon
        as the first sentence is ready instead of after the whole reply."""
        volume = style.volume * (self.whisper_volume if style.voice == "whisper" else 1.0)
        log.info("[voice %s, volume %.2f] %s", style.voice, volume, text)
        if self.synthesize is None:
            if self.face is not None:
                self.face.talk_text(text)
            return
        sentences = split_first(split_sentences(text) or [text])
        pending = self._pool.submit(self._wav, sentences[0])
        for i in range(len(sentences)):
            try:
                wav = pending.result()
            except (OSError, EOFError, ValueError) as exc:
                log.warning("text-to-speech failed (%s); the reply is only shown", exc)
                return
            if i + 1 < len(sentences):
                pending = self._pool.submit(self._wav, sentences[i + 1])      # made while this one plays
            if self.face is not None:
                self.face.talk_audio(wav)
            try:
                self.audio.play(wav, volume)
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("could not play the reply (%s)", exc)
                if self.face is not None:
                    self.face.stop_talking()
                return
