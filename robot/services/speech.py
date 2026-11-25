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
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

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
                 deepen: float = 1.0, pace: float = 1.0, engine: Optional[str] = None):
        self.host, self.port, self.voice, self.timeout = host, port, voice, timeout
        self.deepen, self.pace = deepen, pace
        self.engine = engine                # "kokoro" (fast) or "indextts" (slow); None: the server's default

    @classmethod
    def from_config(cls, cfg) -> "Speech":
        voice = cfg.robot.get("voice") or {}
        port = cfg.robot["ai_server"].get("tts_port") or cfg.ai_port         # TTS may be on its own port
        return cls(cfg.ai_host, int(port), voice.get("tts_voice", "af_heart"),
                   float(cfg.robot["ai_server"].get("timeout_s", 300)), float(voice.get("deepen", 1.0)),
                   float(voice.get("pace", 1.0)), voice.get("tts_engine") or None)

    @property
    def cache_tag(self) -> str:
        return f"{self.host}:{self.port}|{self.engine}|{self.voice}|{self.deepen}|{self.pace}"

    def check(self, timeout: float = 2.0) -> Optional[str]:
        """None if the text-to-speech server accepts a connection, else a sentence saying why not."""
        try:
            socket.create_connection((self.host, self.port), timeout=timeout).close()
        except OSError as exc:
            return (f"the text-to-speech server at {self.host}:{self.port} is not reachable ({exc.__class__.__name__}): "
                    "the robot will only show what it says. Start the TTS server there, or fix `ai_server.tts_port` in config/robot.yaml")
        return None

    def parameters(self) -> dict:
        """The TTS request's parameters: the voice, and the engine when one is chosen (the server offers both on one port)."""
        return {"voice": self.voice, **({"engine": self.engine} if self.engine else {})}

    def synthesize(self, text: str) -> bytes:
        """Text -> WAV bytes. Raises OSError/EOFError (connection) or ValueError (bad response)."""
        response = request(self.host, self.port, "TTS", self.parameters(), text, timeout=self.timeout)
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
