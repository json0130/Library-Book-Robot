"""Draws the TurtleBot's LiDAR map with the keep-out zones on it, so they can be checked before the robot uses them.

    python3 tools/keepout_preview.py                      turtlebot/maps/lidar_map.yaml + turtlebot/config/keepout.yaml
                                                          -> turtlebot/maps/lidar_map_keepout.png
    python3 tools/keepout_preview.py --map turtlebot/maps/lab2.yaml --keepout other.yaml --out /tmp/zones.png --scale 6

Zones are red with their names (orange: a zone with a problem), the station is a blue arrow, the shelf poses of its floor
are green dots. The checks the robot makes at start (turtlebot/keepout.py) are run and printed: exit code 1 if a zone
cannot be used (the picture is still written, to show where). The map files are only read.

Needs numpy, PyYAML and Pillow, no ROS: it runs on the Dell (Python 3.8) and on the Jetson.
"""

import argparse
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "turtlebot"))

import keepout                                       # noqa: E402

ZONE, BAD, STATION, SHELF = (220, 30, 30), (255, 140, 0), (30, 90, 230), (20, 150, 40)


def render(info, zones, places, bad=(), scale=4, yaw=0.0):
    """The map picture (PIL RGB image) with the zones, the station and the shelves drawn on it."""
    from PIL import Image, ImageDraw
    with Image.open(info.image) as picture:
        base = picture.convert("RGB")
    base = base.resize((info.width * scale, info.height * scale), Image.NEAREST)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    def px(x, y):
        return ((x - info.origin_x) / info.resolution * scale, (info.height - (y - info.origin_y) / info.resolution) * scale)

    for zone in zones:
        colour = BAD if zone.name in bad else ZONE
        corners = [px(x, y) for x, y in zone.points]
        draw.polygon(corners, fill=colour + (90,), outline=colour + (255,))
        draw.line(corners + corners[:1], fill=colour + (255,), width=2)
        cx = sum(c[0] for c in corners) / len(corners)
        cy = sum(c[1] for c in corners) / len(corners)
        draw.text((cx + 1, cy + 1), zone.name, fill=(255, 255, 255, 255))
        draw.text((cx, cy), zone.name, fill=(0, 0, 0, 255))
    for label, (x, y) in places.items():
        if label == "station":
            continue
        cx, cy = px(x, y)
        r = max(3, scale)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=SHELF + (255,))
        draw.text((cx + r + 2, cy - r), label.replace("shelf ", ""), fill=SHELF + (255,))
    if "station" in places:
        x, y = places["station"]
        cx, cy = px(x, y)
        r, length = max(4, scale + 2), max(14, 4 * scale)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=STATION + (255,), width=2)
        draw.line((cx, cy, cx + math.cos(yaw) * length, cy - math.sin(yaw) * length), fill=STATION + (255,), width=3)
        draw.text((cx + r + 2, cy + r), "station", fill=STATION + (255,))
    return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")


def station_yaw(floorplan):
    try:
        import yaml
        with open(floorplan) as f:
            return float(((yaml.safe_load(f) or {}).get("station") or {}).get("yaw", 0.0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--map", default=os.path.join(ROOT, "turtlebot", "maps", "lidar_map.yaml"), help="the LiDAR map (map_saver .yaml)")
    ap.add_argument("--keepout", default=keepout.DEFAULT_FILE, help="the zones (default turtlebot/config/keepout.yaml)")
    ap.add_argument("--floorplan", default=os.path.join(ROOT, "data", "floorplan.yaml"))
    ap.add_argument("--shelves", default=os.path.join(ROOT, "data", "shelves.csv"))
    ap.add_argument("--out", default=None, help="the picture to write (default: <map>_keepout.png beside the map)")
    ap.add_argument("--scale", type=int, default=4, help="screen pixels per map cell (default 4: a 5 cm cell is 4 pixels)")
    args = ap.parse_args(argv)
    try:
        info = keepout.map_info(args.map)
        zones, clearance = keepout.load(args.keepout)
        places = keepout.poses(args.floorplan, args.shelves)
    except keepout.KeepoutError as exc:
        for line in exc.problems:
            print("keepout_preview: %s" % line, file=sys.stderr)
        return 1
    problems = keepout.check(zones, keepout.bounds(info), places, clearance)
    bad = {z.name for z in zones if any(p.startswith(z.name + " ") or p.startswith(z.name + ":") for p in problems)}
    out = args.out or os.path.splitext(os.path.abspath(args.map))[0] + "_keepout.png"
    picture = render(info, zones, places, bad, max(1, args.scale), station_yaw(args.floorplan))
    picture.save(out)
    print("keepout_preview: %d zone(s) drawn on %s -> %s" % (len(zones), args.map, out))
    for zone in zones:
        x0, x1 = min(p[0] for p in zone.points), max(p[0] for p in zone.points)
        y0, y1 = min(p[1] for p in zone.points), max(p[1] for p in zone.points)
        print("    %-20s %-7s x %.2f to %.2f, y %.2f to %.2f" % (zone.name, zone.kind, x0, x1, y0, y1))
    if problems:
        print("keepout_preview: the robot would refuse these zones:")
        for line in problems:
            print("    %s" % line)
        return 1
    print("keepout_preview: all zones can be used")
    return 0


if __name__ == "__main__":
    sys.exit(main())
