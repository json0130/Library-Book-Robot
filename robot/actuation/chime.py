"""A soft two-note chime, generated with the standard library, for "excuse me" in a common area."""

from __future__ import annotations

import io
import math
import struct
import wave

RATE = 22050


def chime_wav(notes=(660.0, 880.0), note_s: float = 0.16, gap_s: float = 0.03, volume: float = 0.5) -> bytes:
    """A short rising chime as a 16-bit mono WAV: each note fades in and out so it doesn't click."""
    samples = []
    for hz in notes:
        n = int(note_s * RATE)
        for i in range(n):
            envelope = min(1.0, i / (0.02 * RATE), (n - i) / (0.06 * RATE))
            samples.append(int(32000 * volume * envelope * math.sin(2 * math.pi * hz * i / RATE)))
        samples += [0] * int(gap_s * RATE)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", s) for s in samples))
    return buf.getvalue()
