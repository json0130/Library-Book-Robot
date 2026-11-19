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


PARTS_DIR = ASSETS / "parts"                      # optional close-up photos of the real parts: rfid_reader.jpg, lidar.jpg, screen.jpg, base.jpg


def parts(path: Path, size=(1920, 1080)) -> Path:
    """The prototype photo with its parts named (face screen, LiDAR, laptop, base), and a tile for each close-up photo found in
    video/assets/parts/ (the RFID reader is not on the prototype yet: its tile says a photo is still needed)."""
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    d.text((size[0] / 2, 50), "The parts", font=f(60, True), fill=INK, anchor="ma")
    photo = Image.open(ASSETS / "paper" / "prototype.jpg").convert("RGB")
    ph = size[1] - 190
    photo = photo.resize((int(photo.width * ph / photo.height), ph))
    px, py = 120, 150
    img.paste(photo, (px, py))
    s = ph / 953                                          # the photo as shown in the paper is 638 x 953
    def at(x, y):
        return (px + x * s, py + y * s)
    tx0 = px + photo.width + 60
    callouts = [("Face screen", at(305, 140), (tx0, py + 20), TEAL),
                ("Hokuyo LiDAR", at(300, 495), (tx0, py + 100), ORANGE),
                ("Laptop: map, navigation, sound level", at(300, 380), (tx0, py + 180), BLUE),
                ("TurtleBot 2 base", at(320, 790), (tx0, py + 260), "#7a8794")]
    for label, target, text_at, colour in callouts:
        d.line((text_at[0] - 14, text_at[1] + 22, target[0], target[1]), fill=colour, width=4)
        d.ellipse((target[0] - 9, target[1] - 9, target[0] + 9, target[1] + 9), fill=colour)
        d.rounded_rectangle((text_at[0] - 12, text_at[1], text_at[0] + d.textlength(label, font=f(34, True)) + 22, text_at[1] + 46),
                            radius=23, fill=CARD, outline=colour, width=3)
        d.text((text_at[0] + 5, text_at[1] + 6), label, font=f(34, True), fill=INK)
    # a row of close-up photos of the real parts, as large as the free space allows: the LiDAR, the face screen, the RFID reader (the reader
    # is not on the prototype yet: its tile says a photo is still needed if there is none)
    tiles = [("Hokuyo LiDAR", "lidar.jpg"), ("Face screen", "screen.jpg"), ("RFID reader (RC522)", "rfid_reader.jpg")]
    tw, th, ty = 330, 440, py + 370
    for i, (label, name) in enumerate(tiles):
        x = tx0 + 30 + i * 360
        if (PARTS_DIR / name).is_file():
            t = Image.open(PARTS_DIR / name).convert("RGB")
            t.thumbnail((tw, th))
            img.paste(t, (x + (tw - t.width) // 2, ty + th - t.height))
        else:
            d.rounded_rectangle((x, ty + th - 300, x + tw, ty + th), radius=14, outline=LINE, width=3)
            d.text((x + tw // 2, ty + th - 150), "photo needed", font=f(28), fill=MUTED, anchor="mm")
        d.text((x + tw // 2, ty + th + 14), label, font=f(30, True), fill=INK, anchor="ma")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


if __name__ == "__main__":
    print(system(ASSETS / "system.png"))
    print(parts(ASSETS / "parts.png"))
    sys.exit(0)
