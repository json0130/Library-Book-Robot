"""Speaker and microphone through PulseAudio (paplay / parecord)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def find_device(kind: str, name: str) -> str:
    """First PulseAudio source or sink (kind) whose name contains `name` (case-insensitive)."""
    devices = subprocess.run(["pactl", "list", "short", kind], capture_output=True,
                             text=True, check=True).stdout
    for line in devices.splitlines():
        device = line.split("\t")[1]
        if name.lower() in device.lower() and not device.endswith(".monitor"):
            return device
    what = "microphone" if kind == "sources" else "speaker"
    raise ValueError(f"No {what} matching {name!r}; see `pactl list short {kind}`")


class Audio:
    def __init__(self, speaker: Optional[str] = None, mic: Optional[str] = None):
        for tool in ("paplay", "parecord", "pactl"):
            if shutil.which(tool) is None:
                raise RuntimeError(f"{tool} not found; install pulseaudio-utils")
        self.speaker = find_device("sinks", speaker) if speaker else None
        self.mic = find_device("sources", mic) if mic else None

    def play(self, wav: bytes, volume: float = 1.0) -> None:
        """Play a WAV and block until it finishes. volume 0..1 (PulseAudio's 65536 = 100%)."""
        cmd = ["paplay", f"--volume={int(max(0.0, min(1.0, volume)) * 65536)}"]
        if self.speaker:
            cmd.append(f"--device={self.speaker}")
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(wav)
            f.flush()
            subprocess.run(cmd + [f.name], check=True)

    def record(self, seconds: float) -> bytes:
        """Record a 16 kHz mono WAV for `seconds` and return its bytes."""
        cmd = ["parecord", "--rate=16000", "--channels=1", "--format=s16le", "--file-format=wav"]
        if self.mic:
            cmd.append(f"--device={self.mic}")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rec.wav"
            proc = subprocess.Popen(cmd + [str(path)])
            try:
                proc.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait()
            return path.read_bytes()
