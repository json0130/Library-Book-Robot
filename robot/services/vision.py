"""Face detection, recognition, enrollment and emotion on the AI server (one JPEG per request)."""

from __future__ import annotations

import base64
import socket

from vendor.ai_server.llm import request

MAX_IMAGE_BYTES = 600_000
# mode -> (server command, request parameters)
MODES = {
    "detect": ("FACE", {"operation": "detect"}),
    "recognize": ("FACE", {"operation": "recognize"}),
    "emotion": ("EMOTION", {}),
}


def image_request(host: str, port: int, command: str, parameters: dict, jpg: bytes,
                  robot_id: str = socket.gethostname(), timeout: float = 30) -> dict:
    """Send one JPEG with a FACE or EMOTION command and return the response parameters.

    Raises OSError/EOFError on connection problems and ValueError on a bad or ERR response.
    """
    if not 0 < len(jpg) <= MAX_IMAGE_BYTES:
        raise ValueError("Image must be at most 600,000 bytes")
    return request(host, port, command, dict(parameters, format="jpeg"),
                   base64.b64encode(jpg).decode("ascii"), robot_id, timeout)["parameters"]


class Vision:
    def __init__(self, host: str, port: int = 7898, timeout: float = 30):
        self.host, self.port, self.timeout = host, port, timeout

    def _call(self, mode: str, jpg: bytes) -> list[dict]:
        command, params = MODES[mode]
        return image_request(self.host, self.port, command, params, jpg, timeout=self.timeout).get("detections", [])

    def detect(self, jpg: bytes) -> list[dict]:
        return self._call("detect", jpg)

    def recognize(self, jpg: bytes) -> list[dict]:
        return self._call("recognize", jpg)

    def emotions(self, jpg: bytes) -> list[dict]:
        """[{"emotion": "happy", "confidence": 87.0, "box": [...]}, ...] (confidence in percent)."""
        return self._call("emotion", jpg)

    def enroll(self, jpg: bytes, name: str) -> dict:
        return image_request(self.host, self.port, "FACE", {"operation": "enroll", "name": name}, jpg,
                             timeout=self.timeout)
