"""Robot face expressions: valid names, aliases, and set_emotion() / talk() to drive the display.

    from modules.expression import set_emotion, talk, mouth_levels
    set_emotion("happy", hold_s=3.0)     # needs `python main.py display` running
    talk(levels=mouth_levels(wav_bytes))  # move the mouth with the loudness of a WAV as it plays
    talk(seconds=2.0)                     # generic talking for 2 s (no audio), talk(seconds=0) stops

The AI server's emotion model labels (angry/disgust/fear/happy/sad/surprise/neutral) are all
accepted, along with a few synonyms. "idle" is the resting face, not one of the seven emotions.
"""

from __future__ import annotations

import io
import json
import math
import urllib.error
import urllib.request
import wave
from array import array

EMOTIONS = ("happy", "sad", "angry", "surprise", "fear", "disgust", "neutral")
IDLE = "idle"
DEFAULT_HOLD_S = 3.0
MAX_HOLD_S = 3600.0
DEFAULT_PORT = 8765
TALK_FPS = 30
MAX_TALK_S = 120.0
CHARS_PER_SECOND = 14.0   # rough speaking rate, for talking without audio

ALIASES = {
    "happy": "happy", "joy": "happy", "happiness": "happy", "smile": "happy",
    "sad": "sad", "sadness": "sad", "unhappy": "sad",
    "angry": "angry", "anger": "angry", "mad": "angry",
    "surprise": "surprise", "surprised": "surprise", "shock": "surprise",
    "fear": "fear", "scared": "fear", "afraid": "fear", "fearful": "fear",
    "disgust": "disgust", "disgusted": "disgust",
    "neutral": "neutral", "calm": "neutral", "normal": "neutral",
    "idle": IDLE, "rest": IDLE,
}


def normalize(name: str) -> str | None:
    """Canonical emotion name (or "idle") for a name or alias, None if unknown."""
    return ALIASES.get(str(name).strip().lower())


def valid_names() -> list[str]:
    return list(EMOTIONS) + [IDLE]


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
