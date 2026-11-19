"""Tracks of real drives, from a robot session log, for the simulated map to replay (so the map matches the footage of the drive).

    python tools/video/trajectory.py LOG.jsonl --window 18:27:35-18:28:26 --window 18:28:38-18:29:35 -o video/takes/trips_18nov.json
    python tools/video/trajectory.py LOG.jsonl --list                      # the drives in the log: when each started and arrived

The log is data/logs/session-*.jsonl, written by the robot program while it drove (PoseUpdated about every 5 s, NavProgress,
NavArrived). A window is local clock time, HH:MM:SS-HH:MM:SS on the day of the log. Each track is the poses in the window as
[seconds from the window's start, x, y, yaw] in the map's frame (metres, radians), starting from the pose the robot had at the window's start.
Replay it with: python -m robot sim --base fake --base-replay FILE, then `drive` once for each track, in order
(tools/fake_base.py follows the track at its real speed). The footage lines up if the window starts when the clip starts: the phone's file
name is its start time (20261118_182736.mp4 = 18:27:36); both clocks follow internet time, within about a second.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path


def read_log(path: Path) -> list:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def local_day(rows: list) -> dt.date:
    return dt.datetime.fromtimestamp(rows[0]["t"]).date()


def parse_window(text: str, day: dt.date) -> tuple:
    a, b = text.split("-")
    def epoch(hms: str) -> float:
        h, m, s = (int(x) for x in hms.split(":"))
        return dt.datetime.combine(day, dt.time(h, m, s)).timestamp()
    return epoch(a), epoch(b)


def drives(rows: list) -> list:
    """[(goal id, started, arrived or None)] from NavProgress / NavArrived / NavFailed / NavCancelled."""
    seen, out = {}, []
    for r in rows:
        goal = (r.get("data") or {}).get("goal_id")
        if r["event"] == "NavProgress" and goal not in seen:
            seen[goal] = [goal, r["t"], None]
            out.append(seen[goal])
        elif r["event"] in ("NavArrived", "NavFailed", "NavCancelled") and goal in seen and seen[goal][2] is None:
            seen[goal][2] = r["t"]
    return [tuple(d) for d in out]


def track(rows: list, t0: float, t1: float, name: str = "") -> dict:
    """The poses from t0 to t1, in seconds from t0. The first point is the last pose at or before t0 (the robot standing at the start)."""
    poses = [(r["t"], r["data"]) for r in rows if r["event"] == "PoseUpdated"]
    before = [p for p in poses if p[0] <= t0]
    inside = [p for p in poses if t0 < p[0] <= t1]
    if not before and not inside:
        raise ValueError(f"no poses in the log between {t0} and {t1}")
    start = before[-1][1] if before else inside[0][1]
    points = [[0.0, start["x"], start["y"], start["yaw"]]]
    for t, d in inside:
        points.append([round(t - t0, 2), d["x"], d["y"], d["yaw"]])
    if points[-1][0] < t1 - t0:
        points.append([round(t1 - t0, 2), *points[-1][1:]])               # the robot stays where it stopped until the window ends
    return {"name": name, "start": t0, "duration": round(t1 - t0, 2), "points": [[p[0], round(p[1], 3), round(p[2], 3), round(p[3], 3)] for p in points]}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="trajectory.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("log")
    p.add_argument("--window", action="append", default=[], help="HH:MM:SS-HH:MM:SS (local time); repeat for each drive")
    p.add_argument("--name", action="append", default=[], help="a name for each window")
    p.add_argument("--list", action="store_true", help="list the drives in the log")
    p.add_argument("-o", "--output", help="the JSON file to write")
    args = p.parse_args(argv)
    rows = read_log(Path(args.log))
    if args.list:
        for goal, started, arrived in drives(rows):
            fmt = lambda t: dt.datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "-"
            print(f"{goal}  started {fmt(started)}  ended {fmt(arrived)}" + (f"  ({arrived - started:.0f} s)" if arrived else ""))
        return 0
    if not args.window or not args.output:
        p.error("give at least one --window and -o FILE (or --list)")
    day = local_day(rows)
    tracks = []
    for i, w in enumerate(args.window):
        t0, t1 = parse_window(w, day)
        tracks.append(track(rows, t0, t1, args.name[i] if i < len(args.name) else f"drive{i + 1}"))
        print(f"{tracks[-1]['name']}: {len(tracks[-1]['points'])} points over {tracks[-1]['duration']:.0f} s")
    Path(args.output).write_text(json.dumps({"source": str(args.log), "tracks": tracks}, indent=1))
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
