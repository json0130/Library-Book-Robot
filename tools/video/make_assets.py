"""Drawn assets for the video: video/assets/system.png (the system overview, as Fig. 5 of the paper).

    python tools/video/make_assets.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "video" / "assets"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
BG, CARD, LINE, INK, MUTED = "#10161d", "#18212b", "#3a4652", "#eef2f6", "#9fb3c6"
TEAL, ORANGE, BLUE = "#3cc7cf", "#b9591f", "#2f6f9f"


def f(size, bold=False):
    return ImageFont.truetype(BOLD if bold else FONT, size)


def arrow(d, x0, y, x1, label, colour=TEAL):
    d.line((x0, y, x1, y), fill=colour, width=5)
    tip = 18 if x1 > x0 else -18
    d.polygon([(x1, y), (x1 - tip, y - 11), (x1 - tip, y + 11)], fill=colour)
    d.text(((x0 + x1) / 2, y - 16), label, font=f(28), fill=INK, anchor="md")


def system(path: Path, size=(1920, 1080)) -> Path:
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    d.text((size[0] / 2, 70), "How Stuart works", font=f(64, True), fill=INK, anchor="ma")
    d.text((size[0] / 2, 160), "The Jetson decides what Stuart says and shows. The base decides how to get there safely,",
           font=f(32), fill=MUTED, anchor="ma")
    d.text((size[0] / 2, 202), "and measures the sound level along the way.", font=f(32), fill=MUTED, anchor="ma")
    boxes = [((150, 290, 780, 960), "Jetson Orin Nano", "interaction",
              ["Camera, book reader (RFID)", "State machine, zone style", "Grounded LLM, speech", "Face screen, light, pad"]),
             ((1140, 290, 1770, 960), "TurtleBot 2 + laptop", "base",
              ["Map and localisation", "Navigation to sections", "Obstacles, bumper stop", "Microphone, sound map"])]
    for (x0, y0, x1, y1), title, sub, rows in boxes:
        d.rounded_rectangle((x0, y0, x1, y1), radius=26, fill=CARD, outline=LINE, width=3)
        d.text(((x0 + x1) / 2, y0 + 34), title, font=f(42, True), fill=INK, anchor="ma")
        d.text(((x0 + x1) / 2, y0 + 92), sub, font=f(30), fill=MUTED, anchor="ma")
        for i, row in enumerate(rows):
            ry = y0 + 170 + i * 120
            d.rounded_rectangle((x0 + 40, ry, x1 - 40, ry + 92), radius=16, fill=BG, outline=LINE, width=2)
            d.text(((x0 + x1) / 2, ry + 46), row, font=f(33), fill=INK, anchor="mm")
    arrow(d, 800, 470, 1120, "goal, cancel")
    arrow(d, 1120, 610, 800, "pose, status")
    arrow(d, 1120, 750, 800, "sound level", colour=ORANGE)
    d.text((960, 860), "local network", font=f(28), fill=MUTED, anchor="ma")
    for cx, (label, colour) in zip((size[0] * 0.27, size[0] * 0.73), (("Common area: greets and speaks", ORANGE),
                                                                      ("Quiet zone: waits, answers as text, no sound", BLUE))):
        w = d.textlength(label, font=f(28, True)) + 56
        d.rounded_rectangle((cx - w / 2, 990, cx + w / 2, 1046), radius=28, fill=colour)
        d.text((cx, 1018), label, font=f(28, True), fill="#ffffff", anchor="mm")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


if __name__ == "__main__":
    print(system(ASSETS / "system.png"))
    sys.exit(0)
