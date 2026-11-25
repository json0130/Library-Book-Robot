"""Turn the TurtleBot's noise log (noise_log.csv, made by turtlebot/nodes/noise_logger.py) into a sound map: how loud it is in each
square of the floor, for the operator page's "Sound" view (display/ui/operator.html reads data/noise/noise_grid.json through /noise.json).

    python tools/noise_map.py run_data/noise_log.csv                    # writes data/noise/noise_grid.json
    python tools/noise_map.py a.csv b.csv --cell 0.5 --min-n 2          # several runs together; squares of 0.5 m; at least 2 readings each
    python tools/noise_map.py noise_log.csv --pull robot@172.24.7.173   # fetch the newest log from the Dell first (ssh)
    python tools/noise_map.py noise_log.csv --png sound_map.png         # also a picture on the LiDAR map
    python tools/noise_map.py noise_log.csv --floor -39                 # squares at or below -39 dBFS are drawn grey: "quieter than the microphone can tell"

Positions are the robot's pose on the `map` frame at the time of each reading (x_m, y_m), so the squares line up with the floor plan. (The logger
looked them up from /odom, which is NOT the map frame, until the evening of 2026-11-18; such a log needs --odom-to-map. The logger now uses tf map -> base_footprint
and writes no row while the robot is not localised.)
Sound levels: with a calibration (turtlebot/calibrate_noise.py) the CSV has `sound_dB`, real decibels, and the map says dB. Without
it only `noise_dB` (dBFS, always negative) exists: the map is then only a comparison between places ("louder than"), and it says so.
The noise logger writes no row while the robot speaks or drives (and for 1 s after), so every reading is of the room, not of the robot.

Which microphone: by default only the LEFT one (--mic left). On 2026-11-18 the right USB mic read a constant -48.6 dBFS whatever its gain
(its own noise floor, about 60 dB on the phone), and the logger's `noise_dB` averages both mics, so that constant would flatten every quiet
square. The left mic follows its gain and the room. Both mics have a floor, though: a place quieter than it reads as the floor everywhere.
--floor FLOOR_DB marks those squares so they are shown as "quiet" instead of with a number that means nothing.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "noise" / "noise_grid.json"
DEFAULT_CELL_M = 0.5
POINT_CELL_M = 0.25            # the samples the operator page blends into a smooth map (the squares above are for numbers)


COLUMNS = {"both": ("sound_dB", "noise_dB"), "left": ("left_sound_dB", "left_dB"), "right": ("right_sound_dB", "right_dB")}


def read_rows(paths, mic: str = "both", odom_to_map=None, until_s=None) -> tuple:
    """([(x, y, level_dB)], calibrated, skipped). Uses the calibrated column when every file has it, else the dBFS one.
    mic: "both" (the logger's combined noise_dB / sound_dB), "left" or "right" (that microphone only).
    odom_to_map: (rotation in degrees, shift x, shift y) for a log written in the odometry frame (the logger before 2026-11-18 evening): turns and shifts
    every position into the map frame. until_s: leave out the rows after this many seconds (time_s) of each file."""
    if odom_to_map is not None:
        th = math.radians(odom_to_map[0])
        cos_t, sin_t, shift = math.cos(th), math.sin(th), (odom_to_map[1], odom_to_map[2])
    if mic not in COLUMNS:
        raise ValueError("mic must be both, left or right")
    cal_col, raw_col = COLUMNS[mic]
    raw = []
    calibrated = True
    for path in paths:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            if "x_m" not in fields or "y_m" not in fields or not ({"noise_dB", "sound_dB"} & set(fields)):
                raise ValueError(f"{path}: not a noise log (it needs x_m, y_m and noise_dB or sound_dB columns; found {fields})")
            if cal_col not in fields:
                calibrated = False
            if not calibrated and raw_col not in fields:
                raise ValueError(f"{path}: no {raw_col} column for the {mic} microphone (use --mic both)")
            raw.append(list(reader))
    rows, skipped = [], 0
    for table in raw:
        for row in table:
            try:
                x, y = float(row["x_m"]), float(row["y_m"])
                value = float(row[cal_col if calibrated else raw_col])
                if until_s is not None and float(row["time_s"]) > until_s:
                    continue
                if odom_to_map is not None:
                    x, y = cos_t * x - sin_t * y + shift[0], sin_t * x + cos_t * y + shift[1]
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue
            if not all(math.isfinite(v) for v in (x, y, value)):
                skipped += 1
                continue
            rows.append((x, y, value))
    return rows, calibrated, skipped


def floor_hint(rows) -> Optional[float]:
    """The quietest tenth of the readings: in a quiet room this is the microphone's own floor (what --floor should be, plus a little)."""
    if len(rows) < 10:
        return None
    values = sorted(v for _, _, v in rows)
    return round(values[len(values) // 10], 1)


def build(rows, calibrated: bool, cell: float = DEFAULT_CELL_M, min_n: int = 1, floor: Optional[float] = None) -> dict:
    """The grid: {"unit", "calibrated", "cell", "floor", "cells": [{x, y, n, median, max, quiet?}], "points": [{x, y, n, v}] (0.25 m, for the smooth map), "stats"}; x, y are the middle of each square.
    A square whose median is at or below `floor` is flagged `quiet` (the microphone cannot tell how quiet); `stats` then cover the other squares."""
    if cell <= 0.05 or cell > 5:
        raise ValueError("--cell must be between 0.1 and 5 metres")
    squares: dict = {}
    for x, y, value in rows:
        squares.setdefault((math.floor(x / cell), math.floor(y / cell)), []).append(value)
    cells = [{"x": round((i + 0.5) * cell, 3), "y": round((j + 0.5) * cell, 3), "n": len(v),
              "median": round(statistics.median(v), 1), "max": round(max(v), 1)}
             for (i, j), v in sorted(squares.items()) if len(v) >= min_n]
    fine: dict = {}
    for x, y, value in rows:
        fine.setdefault((math.floor(x / POINT_CELL_M), math.floor(y / POINT_CELL_M)), []).append(value)
    points = [{"x": round((i + 0.5) * POINT_CELL_M, 3), "y": round((j + 0.5) * POINT_CELL_M, 3), "n": len(v), "v": round(statistics.median(v), 1)}
              for (i, j), v in sorted(fine.items()) if len(v) >= min_n]
    if floor is not None:
        for c in cells:
            if c["median"] <= floor:
                c["quiet"] = True
    medians = [c["median"] for c in cells if not c.get("quiet")] or [c["median"] for c in cells]
    return {"available": True, "unit": "dB" if calibrated else "dBFS", "calibrated": calibrated, "cell": cell, "floor": floor,
            "cells": cells, "points": points, "readings": len(rows), "min_n": min_n, "made": time.strftime("%Y-%m-%d %H:%M:%S"),
            "stats": {"min": min(medians) if medians else None, "max": max(medians) if medians else None,
                      "quietest": min(c["median"] for c in cells) if cells else None}}


def png(grid: dict, map_yaml: Path, out: Path) -> None:
    """The squares drawn over the LiDAR map (yellow = quietest ... dark red = loudest)."""
    import numpy as np
    from PIL import Image, ImageDraw
    sys.path.insert(0, str(ROOT / "turtlebot"))
    import keepout
    info = keepout.map_info(map_yaml)
    base = Image.open(info.image).convert("RGB")
    cells = grid["cells"]
    xs, ys = [c["x"] for c in cells], [c["y"] for c in cells]
    pad = 1.5
    x0, x1, y0, y1 = min(xs) - pad, max(xs) + pad, min(ys) - pad, max(ys) + pad
    scale = 4
    k = info.resolution
    def px(x, y):
        return ((x - info.origin_x) / k) * scale, (info.height - (y - info.origin_y) / k) * scale
    big = base.resize((base.width * scale, base.height * scale), Image.NEAREST)
    draw = ImageDraw.Draw(big, "RGBA")
    lo, hi = grid["stats"]["min"], grid["stats"]["max"]
    for c in cells:
        t = 0.5 if hi == lo else (c["median"] - lo) / (hi - lo)
        colour = (154, 160, 166, 150) if c.get("quiet") else (255, int(230 - 200 * max(0.0, min(1.0, t))), int(60 - 60 * max(0.0, min(1.0, t))), 170)
        a, b = px(c["x"] - grid["cell"] / 2, c["y"] + grid["cell"] / 2), px(c["x"] + grid["cell"] / 2, c["y"] - grid["cell"] / 2)
        draw.rectangle([a, b], fill=colour, outline=(80, 80, 80, 200))
        draw.text((a[0] + 3, a[1] + 3), "quiet" if c.get("quiet") else f"{c['median']:.0f}", fill=(0, 0, 0, 255))
    left, top = px(x0, y1)
    right, bottom = px(x1, y0)
    big.crop((int(max(0, left)), int(max(0, top)), int(min(big.width, right)), int(min(big.height, bottom)))).save(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Make a sound map (squares of the floor with their loudness) from the noise log.")
    ap.add_argument("csv", nargs="+", help="noise_log.csv file(s) from the TurtleBot (several runs are put together)")
    ap.add_argument("--cell", type=float, default=DEFAULT_CELL_M, help=f"size of a square in metres (default {DEFAULT_CELL_M})")
    ap.add_argument("--mic", choices=["left", "right", "both"], default="left",
                    help="which microphone's readings to map (default left: the right mic has a constant noise floor, see the top of this file)")
    ap.add_argument("--floor", type=float, default=None, metavar="DB",
                    help="squares at or below this level are shown as 'quiet' (the microphone cannot tell how quiet): the log's quietest tenth plus ~2")
    ap.add_argument("--odom-to-map", type=float, nargs=3, metavar=("DEG", "X", "Y"),
                    help="the log has positions from /odom (logger before 2026-11-18 evening): turn them by DEG degrees and shift by X, Y metres into the map frame "
                         "(fit from where the survey stopped: see docs/sound_map.md)")
    ap.add_argument("--until", type=float, default=None, metavar="SECONDS", help="leave out the rows after this many seconds (time_s) of the log")
    ap.add_argument("--min-n", type=int, default=1, help="leave out squares with fewer readings (default 1)")
    ap.add_argument("--out", default=str(OUT), help=f"where the grid goes (default {OUT.relative_to(ROOT)})")
    ap.add_argument("--png", default=None, help="also draw it on the LiDAR map into this picture")
    ap.add_argument("--map", default=str(ROOT / "turtlebot" / "maps" / "lidar_map.yaml"), help="the LiDAR map for --png")
    ap.add_argument("--pull", default=None, metavar="USER@HOST", help="first copy the Dell's turtlebot/run_data/noise_log.csv here (ssh) to the first csv path")
    args = ap.parse_args(argv)
    if args.pull:
        src = f"{args.pull}:lbr-dell/turtlebot/run_data/noise_log.csv"
        r = subprocess.run(["scp", "-q", src, args.csv[0]], capture_output=True, text=True)
        if r.returncode:
            print(f"could not copy {src}: {r.stderr.strip() or r.returncode}")
            return 2
        print(f"copied {src} -> {args.csv[0]}")
    try:
        rows, calibrated, skipped = read_rows(args.csv, args.mic, tuple(args.odom_to_map) if args.odom_to_map else None, args.until)
        if not rows:
            print("no readings with a position in that log yet (the logger writes none while the robot drives or speaks).")
            return 1
        grid = build(rows, calibrated, args.cell, args.min_n, args.floor)
        grid["mic"] = args.mic
    except (OSError, ValueError) as exc:
        print(f"noise_map: {exc}")
        return 2
    grid["source"] = [Path(p).name for p in args.csv]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(grid, indent=1), encoding="utf-8")
    s = grid["stats"]
    print(f"{len(rows)} readings ({skipped} skipped) -> {len(grid['cells'])} squares of {args.cell} m: {s['min']} to {s['max']} {grid['unit']} (median per square)"
          + ("" if calibrated else "\n  NOT CALIBRATED: these are dBFS, only good for comparing places. Calibrate (turtlebot/calibrate_noise.py), then measure again."))
    quiet = sum(1 for c in grid["cells"] if c.get("quiet"))
    hint = floor_hint(rows)
    if args.floor is None and hint is not None:
        print(f"  the quietest tenth of the readings is {hint} {grid['unit']}. If that is the quiet room, the microphone's floor is about there: add --floor {hint + 2:g} "
              "to show squares at or below it as 'quiet' instead of with a number.")
    elif args.floor is not None:
        print(f"  {quiet} of {len(grid['cells'])} squares are at or below the floor {args.floor:g} and are shown as 'quiet'.")
    print(f"wrote {out}")
    if args.png:
        png(grid, Path(args.map), Path(args.png))
        print(f"wrote {args.png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
