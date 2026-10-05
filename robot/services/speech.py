"""Speech-to-text and text-to-speech on the AI server, over the server team's protocol."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

from vendor.ai_server import stt, tts
from vendor.ai_server.llm import request


class Speech:
    def __init__(self, host: str, port: int = 7898, voice: str = "af_heart", timeout: float = 300):
        self.host, self.port, self.voice, self.timeout = host, port, voice, timeout

    def synthesize(self, text: str) -> bytes:
        """Text -> WAV bytes. Raises OSError/EOFError (connection) or ValueError (bad response)."""
        response = request(self.host, self.port, "TTS", {"voice": self.voice}, text, timeout=self.timeout)
        raw, _ = tts.validate_wav(response.get("content"), response["parameters"])
        return raw

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
