"""Synthetic footage for testing the editor before any real footage exists: ffmpeg test patterns with a tone, a fake take
(events.jsonl + meta.json, as `sim --film` and `film.py record` write them), a still, and a storyboard that uses all of it.

    python tools/video/synthetic.py OUT_DIR          # then: python tools/video/edit.py --storyboard OUT_DIR/storyboard.yaml --preview
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def make_clip(path: Path, seconds: float, size: str = "640x360", pattern: str = "testsrc2", tone: int = 440) -> Path:
    """A test pattern with a sine tone."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"{pattern}=size={size}:rate=30:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000:duration={seconds}",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
    subprocess.run(cmd + [str(path)], check=True)
    return path


def make_still(path: Path, colour: str, text: str) -> Path:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (800, 480), colour)
    d = ImageDraw.Draw(img)
    d.text((400, 240), text, fill="white", anchor="mm", font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48))
    img.save(path)
    return path


def make_take(root: Path, name: str, start: float, zone: str) -> Path:
    """recordings-style take: camera.mp4 + face.mp4 that started at `start`, and the events a visit would log."""
    take = root / name
    take.mkdir(parents=True, exist_ok=True)
    make_clip(take / "camera.mp4", 12, "640x480", "testsrc2", 330)
    make_clip(take / "face.mp4", 12, "1024x600", "smptebars", 550)
    (take / "camera.mp4.json").write_text(json.dumps({"started": start + 0.5, "fps": 30}))
    meta = {"take": name, "start_time": start, "sources": {"face": {"file": "face.mp4", "started": start, "error": None}}}
    (take / "meta.json").write_text(json.dumps(meta))
    speech = zone == "common"
    events = [{"t": start + 0.2, "type": "start"}, {"t": start + 0.2, "type": "zone", "zone": zone, "source": "forced"},
              {"t": start + 1.0, "type": "person_detected"},
              {"t": start + 1.5, "type": "greeting", "zone": zone, "text": "Hello! My name is Stuart."},
              {"t": start + 1.6, "type": "reply", "text": "Hello! My name is Stuart.", "channel": "speech" if speech else "screen"},
              {"t": start + 3.0, "type": "speech_in", "text": "can I return this book?"},
              {"t": start + 4.5, "type": "rfid_read", "title": "Cosmos", "isbn": "9780345539434"},
              {"t": start + 5.0, "type": "reply", "text": "Thanks! Cosmos is returned. Want something similar?",
               "channel": "speech" if speech else "screen"}]
    if not speech:
        events.append({"t": start + 5.0, "type": "screen_text", "text": "Cosmos is returned. Want something similar?"})
    events += [{"t": start + 2.0 + i, "type": "sound_level", "db": 38.0 + (i % 3) if not speech else 61.0 + i % 4, "unit": "dB"}
               for i in range(8)]
    with (take / "events.jsonl").open("w") as f:
        for e in sorted(events, key=lambda e: e["t"]):
            f.write(json.dumps(e) + "\n")
    return take


def make_all(out: Path) -> Path:
    """Everything, plus storyboard.yaml (about 30 s) that uses every segment type. Returns the storyboard's path."""
    out.mkdir(parents=True, exist_ok=True)
    rec = out / "recordings"
    make_take(rec, "common1", 1_800_000_000.0, "common")
    make_take(rec, "quiet1", 1_800_000_100.0, "quiet")
    make_clip(out / "phone.mp4", 10, "360x640", "testsrc", 660)        # portrait, like the phone footage
    make_still(out / "common.png", "#b9591f", "common")
    make_still(out / "quiet.png", "#2f6f9f", "quiet")
    board = f"""
output: {out}/out/synthetic.mp4
max_seconds: 180
recordings: {rec}
voiceover: {{wav: {out}/missing_voiceover.wav}}
segments:
  - {{type: title, duration: 2, title: Stuart, subtitle: A library robot that reads the room}}
  - type: clip
    src: {out}/phone.mp4
    in: 1
    out: 9
    speed: 4
    text: Libraries have quiet zones and lively zones.
    vo: Libraries are not one kind of space.
  - type: split
    zone: common
    left: {{take: common1, source: camera, in: "event:greeting-1s", out: "event:reply#2+3s", label: Robot}}
    right: {{take: common1, source: face, in: "event:greeting-1s", label: Face screen}}
    audio_from: left
    auto_captions: true
  - type: clip
    zone: quiet
    take: quiet1
    source: face
    in: "event:greeting-1s"
    out: end-2s
    auto_captions: true
    sound_meter: true
    operator_driven: true
    captions: [{{at: 0.2, dur: 1.5, text: "(the patron places the book)"}}]
  - {{type: stills, duration: 2, images: [{out}/common.png, {out}/quiet.png], labels: [Common area, Quiet zone], text: Same task. Different style.}}
"""
    path = out / "storyboard.yaml"
    path.write_text(board)
    return path


if __name__ == "__main__":
    print(make_all(Path(sys.argv[1] if len(sys.argv) > 1 else "video/build/synthetic")))
