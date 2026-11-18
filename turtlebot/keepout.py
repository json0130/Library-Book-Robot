#!/usr/bin/env python3
"""Keep-out zones: places the planner must never send the robot, such as under a desk (the LiDAR at 30 cm sees only the
desk's legs, and the robot is 1.2 m tall).

They are drawn as walls into the planner's map only (/map_clean, by nodes/map_cleaner.py). AMCL keeps the real map: walls
that are not there would not match what the LiDAR sees and would confuse the localisation. The visitor screen's map is
made from the saved map file (tools/map_to_plan.py), so it has no zones either.

The zones are in config/keepout.yaml, in the LiDAR map's coordinates (metres in the `map` frame: the numbers
tools/teach_poses.py and the click-to-go page show):

    clearance_m: 0.25
    zones:
      - {name: desk_row_1, rect: [x1, y1, x2, y2]}               # two opposite corners
      - {name: printer, polygon: [[x, y], [x, y], [x, y]]}       # three or more corners, in order round the edge

Checks (`check`): every corner is on the map, and no zone covers the station or a shelf pose of the map's floor, or comes
closer to one than clearance_m (the robot could not get there).

    python3 keepout.py --map maps/lidar_map.yaml       check config/keepout.yaml against the map, the station and the shelves
    python3 keepout.py --map maps/lab2.yaml --keepout other.yaml --floorplan ../data/floorplan.yaml --shelves ../data/shelves.csv

No ROS in here, only numpy and PyYAML (Python 3.8 on the Dell). nodes/map_cleaner.py, run_robot_link.sh,
tools/keepout_preview.py and tools/teach_poses.py use it.
"""

import argparse
import csv
import math
import os
import re
import sys
from collections import namedtuple

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(HERE, "config", "keepout.yaml")
DEFAULT_FLOORPLAN = os.path.join(HERE, "..", "data", "floorplan.yaml")
DEFAULT_SHELVES = os.path.join(HERE, "..", "data", "shelves.csv")
DEFAULT_CLEARANCE = 0.25
OCCUPIED = 100                                   # what a zone's cells become in the OccupancyGrid
NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

Zone = namedtuple("Zone", "name kind points")    # kind "rect" or "polygon"; points: the corners in order round the edge
MapInfo = namedtuple("MapInfo", "resolution origin_x origin_y width height image")


class KeepoutError(ValueError):
    """The zones cannot be used. `problems` lists every reason, one line each."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# ---------------------------------------------------------------------------------------------- reading the file
def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError
    return float(value)


def parse(data):
    """(zones, clearance_m) from the loaded YAML. Raises KeepoutError listing everything wrong."""
    problems = []
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise KeepoutError(["the file must be a mapping with `zones:` (and optionally `clearance_m:`)"])
    unknown = sorted(set(data) - {"zones", "clearance_m"})
    if unknown:
        problems.append("unknown key(s) %s (only zones and clearance_m)" % ", ".join(map(str, unknown)))
    clearance = data.get("clearance_m", DEFAULT_CLEARANCE)
    try:
        clearance = _number(clearance)
        if clearance < 0:
            raise ValueError
    except ValueError:
        problems.append("clearance_m must be a number of metres, 0 or more")
        clearance = DEFAULT_CLEARANCE
    items = data.get("zones") or []
    if not isinstance(items, list):
        raise KeepoutError(problems + ["zones: must be a list"])
    zones, seen = [], set()
    for i, item in enumerate(items, 1):
        label = "zone %d" % i
        if not isinstance(item, dict):
            problems.append("%s: must be {name: ..., rect: [...]} or {name: ..., polygon: [...]}" % label)
            continue
        name = item.get("name", "")
        if isinstance(name, bool):                          # YAML reads a bare yes / no / on / off as true or false
            problems.append("%s: the name %s was read as %s: put it in quotes" % (label, "on/yes" if name else "off/no", name))
            continue
        name = str(name).strip()
        if not name:
            problems.append("%s has no name" % label)
        else:
            label = name
            if name in seen:
                problems.append("%s: the name is used twice" % name)
            seen.add(name)
        shapes = [k for k in ("rect", "polygon") if k in item]
        extra = sorted(set(item) - {"name", "rect", "polygon"})
        if extra:
            problems.append("%s: unknown key(s) %s" % (label, ", ".join(map(str, extra))))
        if len(shapes) != 1:
            problems.append("%s: needs exactly one of rect: [x1, y1, x2, y2] or polygon: [[x, y], ...]" % label)
            continue
        try:
            if shapes[0] == "rect":
                r = item["rect"]
                if not isinstance(r, list) or len(r) != 4:
                    raise ValueError
                x1, y1, x2, y2 = (_number(v) for v in r)
                if x1 == x2 or y1 == y2:
                    problems.append("%s: the rectangle has no area (two corners on one line)" % label)
                    continue
                lo_x, hi_x, lo_y, hi_y = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
                points = [(lo_x, lo_y), (hi_x, lo_y), (hi_x, hi_y), (lo_x, hi_y)]
            else:
                p = item["polygon"]
                if not isinstance(p, list) or len(p) < 3:
                    problems.append("%s: a polygon needs three or more corners" % label)
                    continue
                points = []
                for corner in p:
                    if not isinstance(corner, list) or len(corner) != 2:
                        raise ValueError
                    points.append((_number(corner[0]), _number(corner[1])))
                if abs(_area(points)) < 1e-6:
                    problems.append("%s: the polygon has no area" % label)
                    continue
        except ValueError:
            problems.append("%s: %s must hold numbers (metres): %s" % (label, shapes[0], "[x1, y1, x2, y2]" if shapes[0] == "rect"
                                                                         else "[[x, y], [x, y], [x, y], ...]"))
            continue
        zones.append(Zone(label, shapes[0], points))
    if problems:
        raise KeepoutError(problems)
    return zones, clearance


def load(path):
    """(zones, clearance_m) from a keepout.yaml. Raises KeepoutError (a missing file is an error: say no zones with `zones: []`)."""
    import yaml
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except OSError as exc:
        raise KeepoutError(["cannot read %s: %s" % (path, exc.strerror or exc)])
    except yaml.YAMLError as exc:
        raise KeepoutError(["%s is not valid YAML: %s" % (path, str(exc).splitlines()[0])])
    return parse(data)


# ---------------------------------------------------------------------------------------------- geometry
def _area(points):
    return 0.5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]))


def inside(points, xs, ys):
    """Which of the points (xs, ys) are inside the polygon (even-odd rule). Works on numpy arrays and on plain numbers."""
    xs, ys = np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)
    result = np.zeros(np.broadcast(xs, ys).shape, dtype=bool)
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        if y1 == y2:
            continue                                         # a horizontal edge never crosses the ray to the right
        crosses = (y1 > ys) != (y2 > ys)
        x_at = x1 + (ys - y1) * (x2 - x1) / (y2 - y1)
        result ^= crosses & (xs < x_at)
    return result


def distance(points, x, y):
    """How far (x, y) is from the zone: 0 inside or on the edge, else the distance to the nearest edge."""
    if inside(points, x, y):
        return 0.0
    best = float("inf")
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        dx, dy = x2 - x1, y2 - y1
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return best


def zone_cells(zone, width, height, resolution, origin_x, origin_y):
    """(rows, cols) of the grid cells a zone covers: every cell whose centre is inside it, and every cell its edge passes
    through, so a zone never comes out smaller than drawn (a zone thinner than a cell still paints a line of cells; an edge
    exactly on a cell boundary paints the cell beyond it too, whatever the rounding). Row 0 is the lowest y, as in a
    nav_msgs/OccupancyGrid. Cells off the grid are left out."""
    pts = np.array(zone.points, dtype=float)

    def cell(v, origin):                                      # the nudge: 0.6 / 0.1 = 5.999... must land where 1.0 / 0.1 does
        return np.floor((np.asarray(v) - origin) / resolution + 1e-6).astype(int)
    c0 = max(0, int(cell(pts[:, 0].min(), origin_x)))
    c1 = min(width - 1, int(cell(pts[:, 0].max(), origin_x)))
    r0 = max(0, int(cell(pts[:, 1].min(), origin_y)))
    r1 = min(height - 1, int(cell(pts[:, 1].max(), origin_y)))
    mask = np.zeros((height, width), dtype=bool)
    if c0 > c1 or r0 > r1:
        return np.nonzero(mask)
    cols = np.arange(c0, c1 + 1)
    rows = np.arange(r0, r1 + 1)
    cx = origin_x + (cols + 0.5) * resolution
    cy = origin_y + (rows + 0.5) * resolution
    gx, gy = np.meshgrid(cx, cy)
    mask[r0:r1 + 1, c0:c1 + 1] = inside(zone.points, gx, gy)
    for (x1, y1), (x2, y2) in zip(zone.points, zone.points[1:] + zone.points[:1]):
        steps = max(1, int(math.ceil(math.hypot(x2 - x1, y2 - y1) / (resolution / 4.0))))
        t = np.linspace(0.0, 1.0, steps + 1)
        ec = cell(x1 + t * (x2 - x1), origin_x)
        er = cell(y1 + t * (y2 - y1), origin_y)
        ok = (ec >= 0) & (ec < width) & (er >= 0) & (er < height)
        mask[er[ok], ec[ok]] = True
    return np.nonzero(mask)


def paint(grid, resolution, origin_x, origin_y, zones, value=OCCUPIED):
    """A copy of the occupancy grid (rows x cols, row 0 = lowest y) with every zone filled with `value`.
    Returns (grid, {zone name: cells painted})."""
    out = np.array(grid, copy=True)
    height, width = out.shape
    counts = {}
    for zone in zones:
        rows, cols = zone_cells(zone, width, height, resolution, origin_x, origin_y)
        out[rows, cols] = value
        counts[zone.name] = int(len(rows))
    return out, counts


# ---------------------------------------------------------------------------------------------- the map and the poses
def _pgm_size(path):
    with open(path, "rb") as f:
        head = f.read(1024)
    fields, pos = [], 0
    while len(fields) < 3 and pos < len(head):
        if head[pos:pos + 1].isspace():
            pos += 1
            continue
        if head[pos:pos + 1] == b"#":
            end = head.find(b"\n", pos)
            pos = len(head) if end < 0 else end + 1
            continue
        end = pos
        while end < len(head) and not head[end:end + 1].isspace():
            end += 1
        fields.append(head[pos:end])
        pos = end
    if len(fields) < 3 or fields[0] not in (b"P5", b"P2"):
        return None
    return int(fields[1]), int(fields[2])


def map_info(yaml_path):
    """The size and place of a saved map (a map_saver / map_server .yaml and its picture). Raises KeepoutError."""
    import yaml
    try:
        with open(yaml_path) as f:
            meta = yaml.safe_load(f)
        image = str(meta["image"]).strip()
        resolution = float(meta["resolution"])
        origin = [float(v) for v in meta["origin"]]
    except OSError as exc:
        raise KeepoutError(["cannot read the map %s: %s" % (yaml_path, exc.strerror or exc)])
    except (yaml.YAMLError, KeyError, TypeError, ValueError):
        raise KeepoutError(["%s is not a map_saver .yaml (it needs image, resolution and origin)" % yaml_path])
    if len(origin) > 2 and abs(origin[2]) > 1e-6:
        raise KeepoutError(["%s is rotated (origin yaw %g): keep-out zones need a map with yaw 0, as map_saver writes"
                            % (yaml_path, origin[2])])
    if not os.path.isabs(image):
        image = os.path.join(os.path.dirname(os.path.abspath(yaml_path)), image)
    try:
        size = _pgm_size(image)
        if size is None:                                    # map_server also reads PNG and friends
            from PIL import Image
            with Image.open(image) as picture:
                size = picture.size
    except OSError as exc:
        raise KeepoutError(["cannot read the map's picture %s: %s" % (image, exc.strerror or exc)])
    return MapInfo(resolution, origin[0], origin[1], size[0], size[1], image)


def bounds(info):
    """(x0, y0, x1, y1) of the map in metres."""
    return (info.origin_x, info.origin_y, info.origin_x + info.width * info.resolution, info.origin_y + info.height * info.resolution)


def poses(floorplan_path=None, shelves_path=None):
    """{label: (x, y)}: the station and the shelf poses on the station's floor (the floor the robot's map is of)."""
    import yaml
    out, level = {}, None
    if floorplan_path:
        try:
            with open(floorplan_path) as f:
                station = (yaml.safe_load(f) or {}).get("station") or {}
            out["station"] = (float(station["x"]), float(station["y"]))
            level = int(station.get("level", 1))
        except OSError as exc:
            raise KeepoutError(["cannot read %s: %s" % (floorplan_path, exc.strerror or exc)])
        except (yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError):
            raise KeepoutError(["%s has no usable station: (x and y)" % floorplan_path])
    if shelves_path:
        try:
            with open(shelves_path, newline="") as f:
                rows = [l for l in f if l.strip() and not l.lstrip().startswith("#")]
        except OSError as exc:
            raise KeepoutError(["cannot read %s: %s" % (shelves_path, exc.strerror or exc)])
        for row in csv.DictReader(rows):
            try:
                if level is not None and int(row.get("floor") or 0) != level:
                    continue
                out["shelf %s" % row["shelf"].strip()] = (float(row["map_x"]), float(row["map_y"]))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue                                    # a row without a pose yet: catalog_check.py reports it
    return out


def check(zones, area, places, clearance=DEFAULT_CLEARANCE):
    """Problems (a list of lines, empty when all is well): a corner off the map `area` (x0, y0, x1, y1), or a zone over a
    pose of `places` {label: (x, y)} or nearer to it than `clearance` metres."""
    x0, y0, x1, y1 = area
    problems = []
    for zone in zones:
        off = [(x, y) for x, y in zone.points if not (x0 <= x <= x1 and y0 <= y <= y1)]
        if off:
            problems.append("%s: corner (%.2f, %.2f) is outside the map (x %.2f to %.2f, y %.2f to %.2f)"
                            % (zone.name, off[0][0], off[0][1], x0, x1, y0, y1))
        for label, (x, y) in sorted(places.items()):
            d = distance(zone.points, x, y)
            if d == 0.0:
                problems.append("%s covers the %s at (%.2f, %.2f)" % (zone.name, label, x, y))
            elif d < clearance:
                problems.append("%s is %.2f m from the %s at (%.2f, %.2f): closer than clearance_m %.2f, so the robot could "
                                "not get there" % (zone.name, d, label, x, y, clearance))
    return problems


def check_files(map_yaml, keepout_path=DEFAULT_FILE, floorplan_path=DEFAULT_FLOORPLAN, shelves_path=DEFAULT_SHELVES):
    """(zones, clearance, problems) for a map and the data files; problems is empty when the zones can be used.
    Raises KeepoutError when a file cannot be read at all."""
    zones, clearance = load(keepout_path)
    info = map_info(map_yaml)
    return zones, clearance, check(zones, bounds(info), poses(floorplan_path, shelves_path), clearance)


# ---------------------------------------------------------------------------------------------- editing the file
ITEM = re.compile(r"^(\s*)-\s*\{\s*name:\s*([^,}]+?)\s*[,}]")


def set_rect_text(text, name, rect):
    """keepout.yaml's text with the rectangle zone `name` = (x1, y1, x2, y2) written in: its line replaced if it is there,
    else added at the end of `zones:`. Comments and everything else stay as they are."""
    if not NAME.match(name):
        raise KeepoutError(["a zone name is letters, digits, _ and - (at most 40): %r" % name])
    item = "{name: %s, rect: [%s]}" % (name, ", ".join("%.2f" % v for v in rect))
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = ITEM.match(line)
        if m and m.group(2).strip("'\"") == name:
            lines[i] = "%s- %s" % (m.group(1), item)
            return "\n".join(lines)
    key = next((i for i, l in enumerate(lines) if re.match(r"^zones:", l)), None)
    if key is None:
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines + ["zones:", "  - " + item, ""])
    if re.match(r"^zones:\s*\[\s*\]", lines[key]):
        comment = re.search(r"#.*$", lines[key])
        lines[key] = "zones:" + ("   " + comment.group(0) if comment else "")
    last, indent = key, "  "
    for j in range(key + 1, len(lines)):
        m = ITEM.match(lines[j])
        if m:
            last, indent = j, m.group(1)
        elif lines[j].strip() and not lines[j].startswith((" ", "#")):
            break                                           # the next top-level key
    lines.insert(last + 1, "%s- %s" % (indent, item))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check the keep-out zones against a saved map, the station and the shelf poses.")
    ap.add_argument("--map", required=True, help="the LiDAR map (a map_saver .yaml)")
    ap.add_argument("--keepout", default=DEFAULT_FILE, help="the zones (default config/keepout.yaml)")
    ap.add_argument("--floorplan", default=DEFAULT_FLOORPLAN, help="its station: (default ../data/floorplan.yaml)")
    ap.add_argument("--shelves", default=DEFAULT_SHELVES, help="the shelf poses (default ../data/shelves.csv)")
    args = ap.parse_args(argv)
    try:
        zones, clearance, problems = check_files(args.map, args.keepout, args.floorplan, args.shelves)
    except KeepoutError as exc:
        for line in exc.problems:
            print("keepout: %s" % line)
        return 1
    if problems:
        print("keepout: %s cannot be used with %s:" % (args.keepout, args.map))
        for line in problems:
            print("    %s" % line)
        return 1
    print("keepout: %d zone(s) OK%s" % (len(zones), (": " + ", ".join(z.name for z in zones)) if zones else " (none: nothing is painted)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
