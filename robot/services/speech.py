"""Speech-to-text and text-to-speech on the AI server, over the server team's protocol.

The server's only TTS voice is af_heart (a woman's), so the robot reshapes it after synthesis with sox,
set in config/robot.yaml under `voice:`:

    pace     1.12 speaks 12% faster at the same pitch (`tempo`): brisker and more energetic
    deepen   0.8 lowers pitch and formants together (`speed`), the way to sound deeper; 1.0 = off

A voice installed on the server would be better than either.
"""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from vendor.ai_server import stt, tts
from vendor.ai_server.llm import request

log = logging.getLogger("robot.speech")


def shape_voice(wav: bytes, deepen: float = 1.0, pace: float = 1.0) -> bytes:
    """The WAV with its voice reshaped: `deepen` below 1 lowers pitch (0.8 is about four semitones), `pace`
    above 1 speeds it up without changing pitch. 1.0 for both leaves it alone. If sox is missing or fails
    the voice is returned unchanged."""
    deepen = min(deepen, 1.0)
    if deepen == 1.0 and pace == 1.0:
        return wav
    if shutil.which("sox") is None:
        log.warning("sox is not installed (sudo apt install sox), so the voice is not reshaped")
        return wav
    effects = (["speed", f"{deepen:g}"] if deepen < 1.0 else []) + ["tempo", f"{pace / deepen:g}"]
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d) / "in.wav", Path(d) / "out.wav"
        src.write_bytes(wav)
        try:
            subprocess.run(["sox", str(src), str(dst), *effects], check=True, capture_output=True, timeout=15)
            return dst.read_bytes()
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not reshape the voice (%s); using it as it is", exc)
            return wav


class Speech:
    def __init__(self, host: str, port: int = 7898, voice: str = "af_heart", timeout: float = 300,
                 deepen: float = 1.0, pace: float = 1.0):
        self.host, self.port, self.voice, self.timeout = host, port, voice, timeout
        self.deepen, self.pace = deepen, pace

    @classmethod
    def from_config(cls, cfg) -> "Speech":
        voice = cfg.robot.get("voice") or {}
        port = cfg.robot["ai_server"].get("tts_port") or cfg.ai_port         # TTS may be on its own port
        return cls(cfg.ai_host, int(port), voice.get("tts_voice", "af_heart"),
                   float(cfg.robot["ai_server"].get("timeout_s", 300)), float(voice.get("deepen", 1.0)),
                   float(voice.get("pace", 1.0)))

    def synthesize(self, text: str) -> bytes:
        """Text -> WAV bytes. Raises OSError/EOFError (connection) or ValueError (bad response)."""
        response = request(self.host, self.port, "TTS", {"voice": self.voice}, text, timeout=self.timeout)
        raw, _ = tts.validate_wav(response.get("content"), response["parameters"])
        return shape_voice(raw, self.deepen, self.pace)

    def transcribe_file(self, wav: Path) -> str:
        """Transcript of an uncompressed PCM WAV file."""
        pcm, dtype, rate, channels, _ = stt.read_wav(wav)
        response = request(self.host, self.port, "STT",
                           dict(format="raw_pcm", dtype=dtype, sample_rate=rate, channels=channels),
                           base64.b64encode(pcm).decode("ascii"), timeout=self.timeout)
        return response["content"].strip()

    def transcribe(self, wav: bytes) -> str:
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(wav)
            f.flush()
            return self.transcribe_file(Path(f.name))
