"""Turns a map the TurtleBot saved (map_saver: a .pgm and its .yaml) into the vector picture the visitor screen draws
under the floor plan: `display/ui/maps/<name>.json`.

    python tools/map_to_plan.py ~/vslam_map.yaml lab                 # writes display/ui/maps/lab.json
    python tools/map_to_plan.py ~/vslam_map.yaml lab --show          # also prints the outline of the floor in map metres

The picture is in the map frame (metres, x to the right, y UP, like the map and every pose), so the plan puts a section or a
shelf where the robot would drive to. The occupancy grid is tidied for a visitor: speckle is closed up, specks of noise are
dropped, and what is left becomes two shapes, the floor the robot saw and the walls and furniture round it, each as one SVG
path in metres. Nothing here needs ROS (numpy and scipy only), and the map file itself is only read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy import ndimage

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "display" / "ui" / "maps"
FREE_FROM, OCCUPIED_BELOW = 250, 50            # pgm grey: map_saver writes 254 free, 0 occupied, 205 unknown
MIN_FLOOR_CELLS, MIN_WALL_CELLS = 60, 6        # a floor patch or wall fragment smaller than this is noise


def read_pgm(path: Path) -> np.ndarray:
    """The pixels of a binary (P5) PGM as a 2-D uint8 array, row 0 = the top of the picture."""
    data = path.read_bytes()
    fields, pos = [], 0
    while len(fields) < 4:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        fields.append(data[pos:end])
        pos = end
    if fields[0] != b"P5" or int(fields[3]) > 255:
        raise SystemExit(f"{path}: not an 8-bit binary PGM")
    width, height = int(fields[1]), int(fields[2])
    return np.frombuffer(data, dtype=np.uint8, count=width * height, offset=pos + 1).reshape(height, width)


def read_yaml(path: Path) -> dict:
    import yaml
    meta = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {"image": meta["image"], "resolution": float(meta["resolution"]), "origin": [float(v) for v in meta["origin"][:2]]}


def keep_big(mask: np.ndarray, at_least: int) -> np.ndarray:
    labels, count = ndimage.label(mask, structure=np.ones((3, 3)))
    if not count:
        return mask
    sizes = ndimage.sum(mask, labels, range(1, count + 1))
    return np.isin(labels, [i + 1 for i, n in enumerate(sizes) if n >= at_least])


def path_of(mask: np.ndarray, res: float, ox: float, oy: float) -> str:
    """SVG path (metres, y up) of a boolean grid: row runs, joined with the identical run in the row below."""
    height = mask.shape[0]
    open_runs: dict = {}                      # (c0, c1) -> row it started on
    rects = []
    for r in range(height + 1):               # picture row r, 0 = top; one extra empty row closes the last runs
        runs = set()
        if r < height:
            padded = np.concatenate(([False], mask[r], [False]))
            edges = np.flatnonzero(padded[1:] != padded[:-1])
            runs = {(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2])}
        for run in list(open_runs):
            if run not in runs:
                rects.append((run[0], open_runs.pop(run), run[1], r))
        for run in runs:
            open_runs.setdefault(run, r)
    out = []
    for c0, r0, c1, r1 in sorted(rects, key=lambda t: (t[1], t[0])):
        x, y = ox + c0 * res, oy + (height - r1) * res            # the rectangle's lower-left corner, y up
        out.append(f"M{x:.2f} {y:.2f}h{(c1 - c0) * res:.2f}v{(r1 - r0) * res:.2f}h{-(c1 - c0) * res:.2f}z")
    return "".join(out)


def walk_grid(floor: np.ndarray, walls: np.ndarray, res: float, ox: float, oy: float, step: int = 2, clearance_m: float = 0.2) -> dict:
    """Where a person (and the robot) can walk, on a coarser grid (`step` cells to a square) for routing the directions:
    floor, kept `clearance_m` away from anything that is not floor. rows[0] is the LOWEST y; "1" is walkable."""
    h = floor.shape[0]
    free = ndimage.binary_erosion(floor & ~ndimage.binary_dilation(walls, iterations=int(round(clearance_m / res))),
                                  iterations=1)
    flipped = free[::-1]                                # row 0 = the lowest y, as the routes are made
    coarse = flipped[:flipped.shape[0] // step * step, :flipped.shape[1] // step * step]
    coarse = coarse.reshape(coarse.shape[0] // step, step, coarse.shape[1] // step, step).all(axis=(1, 3))
    ys, xs = np.nonzero(coarse)
    r0, r1, c0, c1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    cell = res * step
    return {"res": round(cell, 3), "x0": round(ox + c0 * cell, 3), "y0": round(oy + r0 * cell, 3),
            "w": int(c1 - c0), "h": int(r1 - r0),
            "rows": ["".join("1" if v else "0" for v in row[c0:c1]) for row in coarse[r0:r1]]}


def build(pgm: np.ndarray, res: float, origin: list) -> dict:
    free = pgm >= FREE_FROM
    occupied = pgm < OCCUPIED_BELOW
    floor = keep_big(ndimage.binary_closing(free, structure=np.ones((3, 3)), iterations=3), MIN_FLOOR_CELLS)
    near = ndimage.binary_dilation(floor, iterations=int(round(1.0 / res)))          # furniture and walls within a metre of floor
    walls = keep_big(ndimage.binary_dilation(occupied & near, iterations=1), MIN_WALL_CELLS) & ~floor
    ys, xs = np.nonzero(floor | walls)
    if not len(xs):
        raise SystemExit("the map has nothing in it")
    h = pgm.shape[0]
    ox, oy = origin
    bounds = {"x0": round(ox + xs.min() * res, 2), "x1": round(ox + (xs.max() + 1) * res, 2),
              "y0": round(oy + (h - 1 - ys.max()) * res, 2), "y1": round(oy + (h - ys.min()) * res, 2)}
    return {"resolution": res, "bounds": bounds, "floor": path_of(floor, res, ox, oy), "walls": path_of(walls, res, ox, oy),
            "walk": walk_grid(floor, walls, res, ox, oy)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("yaml", type=Path, help="the map_saver .yaml (its .pgm is read from beside it)")
    ap.add_argument("name", help="the map's name: the file written is display/ui/maps/<name>.json")
    ap.add_argument("--out", type=Path, default=OUT_DIR, help="where to write it")
    ap.add_argument("--show", action="store_true", help="print the bounds of what was found")
    args = ap.parse_args(argv)
    meta = read_yaml(args.yaml)
    picture = build(read_pgm(args.yaml.parent / meta["image"]), meta["resolution"], meta["origin"])
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / f"{args.name}.json"
    target.write_text(json.dumps(picture, separators=(",", ":")), encoding="utf-8")
    b = picture["bounds"]
    print(f"{target}: {target.stat().st_size // 1024} KB, x {b['x0']} to {b['x1']} m, y {b['y0']} to {b['y1']} m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
