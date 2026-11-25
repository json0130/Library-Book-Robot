"""Play a clip backwards, in pieces so it does not fill the memory (the reverse filter holds every frame of what it reverses).

    python tools/video/reverse.py video/footage/cad_book_retrieve_original.webm video/footage/cad_book_return_full.mp4 \\
        --crop 1060:924:640:222 --width 800

The CAD simulation of the lift taking a book out, played in reverse, is the book going in. --crop is w:h:x:y of the source picture (the 3D
view), --width scales the result (even number; the clip is only ever shown small). The clip is cut into --piece second pieces, each is reversed,
and they are joined last first. The output is 30 fps H.264 without sound.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FPS = 30


def run(args: list) -> None:
    res = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[-1200:])


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(out.stdout.strip())


def piece_bounds(total: float, piece: float) -> list:
    """[(start, length)] of the pieces, in order."""
    out, t = [], 0.0
    while t < total - 1e-6:
        out.append((round(t, 3), round(min(piece, total - t), 3)))
        t += piece
    return out


def reverse_clip(src: Path, dst: Path, crop: str = "", width: int = 0, piece: float = 4.0) -> Path:
    work = Path(tempfile.mkdtemp(prefix="reverse-", dir=dst.parent))
    try:
        steps = ([f"crop={crop}"] if crop else []) + ([f"scale={width}:-2"] if width else []) + [f"fps={FPS}"]
        flat = work / "flat.mp4"                                  # constant frame rate, cropped, small: easy to cut at exact times
        run(["-i", str(src), "-an", "-vf", ",".join(steps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", str(flat)])
        parts = []
        for n, (start, length) in enumerate(piece_bounds(duration(flat), piece)):
            part = work / f"p{n:03d}.mp4"
            run(["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(flat), "-vf", "reverse", "-an", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(FPS), str(part)])
            parts.append(part)
        listing = work / "list.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in reversed(parts)))        # last piece first
        run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
             "-r", str(FPS), "-movflags", "+faststart", str(dst)])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return dst


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="reverse.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--crop", default="", help="w:h:x:y of the source picture to keep")
    p.add_argument("--width", type=int, default=0)
    p.add_argument("--piece", type=float, default=4.0, help="seconds reversed at a time (default %(default)s)")
    args = p.parse_args(argv)
    dst = Path(args.dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    reverse_clip(Path(args.src), dst, args.crop, args.width, args.piece)
    print(f"wrote {dst}: {duration(dst):.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
