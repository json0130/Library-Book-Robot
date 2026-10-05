"""Face display client: set_emotion() / talk() drive the face page served by display/server.py.

    from robot.actuation.face import set_emotion, talk, mouth_levels
    set_emotion("happy", hold_s=3.0)     # needs `python -m display.server` running
    talk(levels=mouth_levels(wav_bytes))  # move the mouth with the loudness of a WAV as it plays
    talk(seconds=2.0)                     # generic talking for 2 s (no audio), talk(seconds=0) stops

Emotion names and aliases come from robot/core/expression.py; the AI server's emotion model labels
(angry/disgust/fear/happy/sad/surprise/neutral) are all accepted. "idle" is the resting face.
"""

from __future__ import annotations

import io
import json
import math
import urllib.error
import urllib.request
import wave
from array import array

from robot.core.expression import EMOTIONS, IDLE, normalize, valid_names  # noqa: F401 (re-exported)

DEFAULT_HOLD_S = 3.0
MAX_HOLD_S = 3600.0
DEFAULT_PORT = 8765
TALK_FPS = 30
MAX_TALK_S = 120.0
CHARS_PER_SECOND = 14.0   # rough speaking rate, for talking without audio


def make_event(name: str, hold_s: float = DEFAULT_HOLD_S) -> dict:
    """The event sent to the page: {"emotion": str, "hold_s": float}. Raises ValueError if invalid."""
    emotion = normalize(name)
    if emotion is None:
        raise ValueError(f"unknown emotion {name!r}; valid: {', '.join(valid_names())}")
    try:
        hold = float(hold_s)
    except (TypeError, ValueError):
        raise ValueError(f"hold_s must be a number, got {hold_s!r}") from None
    if not 0 < hold <= MAX_HOLD_S:
        raise ValueError(f"hold_s must be between 0 and {MAX_HOLD_S:g} seconds")
    return {"emotion": emotion, "hold_s": hold}


def make_talk_event(levels=None, fps: float = TALK_FPS, seconds=None, delay_s: float = 0.0) -> dict:
    """{"talk": {...}} event: a mouth-opening envelope (levels 0..1 at fps), or `seconds` of generic
    talking. seconds=0 stops talking. delay_s lets the page start later to line up with audio latency.
    Raises ValueError if invalid."""
    try:
        delay = float(delay_s)
        if not 0 <= delay <= 5:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("delay_s must be between 0 and 5 seconds") from None
    if levels is not None:
        try:
            fps = float(fps)
            levels = [round(min(1.0, max(0.0, float(v))), 3) for v in levels]
        except (TypeError, ValueError):
            raise ValueError("levels must be a list of numbers and fps a number") from None
        if not 1 <= fps <= 120:
            raise ValueError("fps must be between 1 and 120")
        if len(levels) / fps > MAX_TALK_S:
            raise ValueError(f"talk envelope is longer than {MAX_TALK_S:g} s")
        return {"talk": {"levels": levels, "fps": fps, "delay_s": delay}}
    try:
        secs = float(seconds)
    except (TypeError, ValueError):
        raise ValueError("give levels or seconds") from None
    if not 0 <= secs <= MAX_TALK_S:
        raise ValueError(f"seconds must be between 0 and {MAX_TALK_S:g}")
    return {"talk": {"seconds": secs, "delay_s": delay}}


def mouth_levels(wav_bytes: bytes, fps: float = TALK_FPS) -> list[float]:
    """Loudness envelope of a PCM WAV, fps values per second scaled to 0..1 (1 = loud speech).

    Each window's RMS is scaled by the loud end of the clip (95th percentile), so quiet and loud
    voices both open the mouth fully, and a noise floor keeps the mouth shut in pauses.
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        width, channels, rate = w.getsampwidth(), w.getnchannels(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 1:
        samples = [b - 128 for b in raw]
    elif width in (2, 4):
        samples = array("h" if width == 2 else "i", raw)
    else:
        raise ValueError(f"unsupported WAV sample width {width}")
    step = max(1, int(rate / fps)) * channels
    rms = []
    for i in range(0, len(samples), step):
        chunk = samples[i:i + step]
        rms.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)) if len(chunk) else 0.0)
    if not rms:
        return []
    loud = sorted(rms)[int(0.95 * (len(rms) - 1))] or 1.0
    levels = []
    for r in rms:
        v = (r / loud - 0.08) / 0.92            # below 8% of loud speech counts as silence
        levels.append(round(min(1.0, max(0.0, v)) ** 0.7, 3))
    return levels


def _post(path: str, event: dict, host: str, port: int, timeout: float) -> bool:
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=json.dumps(event).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def talk(levels=None, seconds=None, fps: float = TALK_FPS, delay_s: float = 0.0,
         host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 2.0) -> bool:
    """Animate the mouth: with levels (see mouth_levels) or for `seconds` of generic talking.
    Returns False if the display is not running. Raises ValueError if the arguments are invalid."""
    event = make_talk_event(levels, fps, seconds, delay_s)["talk"]
    return _post("/talk", event, host, port, timeout)


def set_emotion(name: str, hold_s: float = DEFAULT_HOLD_S,
                host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 2.0) -> bool:
    """Show an emotion on the display for hold_s seconds, then fall back to idle.

    Returns False if the display is not running. Raises ValueError for an unknown emotion.
    """
    return _post("/emotion", make_event(name, hold_s), host, port, timeout)


class FaceDisplay:
    """The face page as an actuator: emotions, talking and (later) screen layout and text.

    Works whether or not the display is running: if it is not, calls only log, and it is
    tried again after RETRY_S seconds in case it was started later.
    """

    RETRY_S = 20.0

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, enabled: bool = True):
        import logging
        self.host, self.port, self.enabled = host, port, enabled
        self.log = logging.getLogger("robot.face")
        self.available: bool | None = None       # None until the first attempt
        self._retry_at = 0.0

    def _post(self, fn, *args, **kwargs) -> bool:
        import time
        if not self.enabled or time.monotonic() < self._retry_at:
            return False
        ok = fn(*args, host=self.host, port=self.port, timeout=0.3, **kwargs)
        if not ok:
            self._retry_at = time.monotonic() + self.RETRY_S
            if self.available is not False:
                self.log.info("[face] display not running on port %d; only printing expressions", self.port)
        self.available = ok
        return ok

    def show_emotion(self, emotion: str, hold_s: float = DEFAULT_HOLD_S) -> bool:
        shown = self._post(set_emotion, emotion, hold_s)
        self.log.info("[face] %s for %gs%s", emotion, hold_s, "" if shown else " (not sent)")
        return shown

    def talk_audio(self, wav: bytes, delay_s: float = 0.1) -> None:
        try:
            levels = mouth_levels(wav)
        except (ValueError, EOFError):
            return
        self._post(talk, levels=levels, delay_s=delay_s)

    def talk_text(self, text: str) -> None:
        self._post(talk, seconds=min(MAX_TALK_S, max(0.6, len(text) / CHARS_PER_SECOND)))

    def set_layout(self, layout: str) -> None:
        """full_face or split (face plus a text panel). The page does not support split yet."""
        self.log.info("[face] layout %s", layout)

    def show_text(self, text: str) -> None:
        """Text on the screen. The page cannot show text yet, so this only logs."""
        self.log.info("[screen] %s", text)
