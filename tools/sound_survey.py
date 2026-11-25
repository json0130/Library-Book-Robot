"""The sound survey: the TurtleBot visits a grid of listening points, stands still at each one while the noise logger listens, and comes
back. The readings (noise_log.csv, with the map position of each) then become the sound map: tools/noise_map.py, shown in the operator
page's Sound view.

    python tools/sound_survey.py plan                      # READ-ONLY: choose the points (1 m apart, clear of walls and zones), print the
                                                           # route and its length, write data/noise/survey_plan.json (the Sound view shows it)
    python tools/sound_survey.py plan --spacing 1.5 --start 0.37 0.0
    python tools/sound_survey.py run                       # DRIVES the saved plan: asks for `yes`, then goes point to point
    python tools/sound_survey.py run --dwell 10 --speed 0.2 --max 6      # stand 10 s at each point; only the first 6 points

THE ROBOT DRIVES, point after point, for many minutes: only with the area clear and someone at the power switch (the same rules as
tools/goto_test.py, which does each leg). It checks every leg on the map first (a leg the planner could not do is skipped), skips a point
it cannot reach, stops the whole survey on a bumper hit, after three failed legs in a row, when the base is no longer localised or the
link is lost, and on Ctrl-C. At the end it drives back to where it started.

Points are chosen only on ground the LiDAR map knows to be free: a part of the room the map never saw (shadow behind furniture, or
not driven) gets no points. Extend the map first if the survey must cover it.

The noise logger writes no reading while the robot drives or speaks (and for 1 s after), so the room is measured only while it stands still
at a point: with --dwell 8 that is about 12 readings per point. Calibrate the microphones first (docs/sound_map.md), or the map is only
good for comparing places. Needs the same things as goto_test.py: the tunnel, ROBOT_LINK_TOKEN, the Dell started with run_robot_link.sh --amcl.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import goto_test                                                              # noqa: E402

PLAN = ROOT / "data" / "noise" / "survey_plan.json"
LOG = ROOT / "data" / "noise" / "survey_log.json"
DEFAULT_SPACING_M = 1.0
DEFAULT_CLEAR_M = 0.35          # a listening point is at least this far from any wall or zone (the robot's radius is 0.20)
DEFAULT_DWELL_S = 8.0
MAX_FAILS_IN_A_ROW = 3


class SurveyError(Exception):
    pass


def plan_waypoints(root: Path, start: tuple, spacing: float = DEFAULT_SPACING_M, min_clear: float = DEFAULT_CLEAR_M,
                   box: Optional[tuple] = None) -> list:
    """[{"k", "x", "y"}] in the order to visit them: a lattice of points `spacing` apart through `start`, kept when they are inside the floor,
    `min_clear` from every wall and keep-out zone, in known free space, and reachable from `start`; ordered nearest first."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage as ndi
    made = goto_test.clearance_grid(root)
    if made is None:
        raise SurveyError("no map file here (turtlebot/maps/lidar_map.yaml): the survey is planned on it")
    clearance, info, _zones = made
    free = (np.array(Image.open(info.image))[::-1] > 240)
    res = info.resolution
    r = max(1, int(round(min_clear / res)))
    known = ndi.uniform_filter(free.astype(float), size=2 * r + 1) >= 0.9               # no unknown ground (outside the walls) round the point
    passable = clearance >= goto_test.ROBOT_RADIUS_M + 0.05
    labels, _n = ndi.label(passable, structure=np.ones((3, 3)))

    def cell(x, y):
        return int((y - info.origin_y) / res), int((x - info.origin_x) / res)

    h, w = clearance.shape
    s = cell(*start)
    if not (0 <= s[0] < h and 0 <= s[1] < w) or labels[s] == 0:
        raise SurveyError(f"the start ({start[0]:.2f}, {start[1]:.2f}) is not on ground the planner can use (too near a wall or zone, or off the map)")
    from display.server import plan_json
    p = plan_json(root)
    x0, y0, x1, y1 = box or ((p["x0"], p["y0"], p["x0"] + p["width"], p["y0"] + p["height"]) if p.get("available")
                             else (info.origin_x, info.origin_y, info.origin_x + w * res, info.origin_y + h * res))
    points = []
    i0, i1 = math.floor((x0 - start[0]) / spacing), math.ceil((x1 - start[0]) / spacing)
    j0, j1 = math.floor((y0 - start[1]) / spacing), math.ceil((y1 - start[1]) / spacing)
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            x, y = start[0] + i * spacing, start[1] + j * spacing
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                continue
            c = cell(x, y)
            if 0 <= c[0] < h and 0 <= c[1] < w and clearance[c] >= min_clear and known[c] and labels[c] == labels[s]:
                points.append((x, y))
    order, here, left = [], start, list(points)
    while left:
        nxt = min(left, key=lambda q: math.hypot(q[0] - here[0], q[1] - here[1]))
        left.remove(nxt)
        order.append(nxt)
        here = nxt
    return [{"k": k + 1, "x": round(x, 2), "y": round(y, 2)} for k, (x, y) in enumerate(order)]


def simplify(points: list, upto: Optional[int] = None, merges=(), skip=()) -> tuple:
    """(points, notes): the first `upto` points only, each group in `merges` (lists of point numbers k) replaced by one point at the groups's middle,
    in the place of the group's first point; then the numbers in `skip` (as they are after that) are left out; then numbered 1, 2, 3 ... again.
    notes say what was done, in words."""
    pts = [dict(q) for q in points]
    notes = []
    if upto is not None:
        pts = [q for q in pts if q["k"] <= upto]
        notes.append(f"only points 1 to {upto}")
    for group in merges:
        ks = sorted(set(group))
        found = [q for q in pts if q["k"] in ks]
        if len(found) != len(ks):
            raise SurveyError(f"--merge {','.join(map(str, group))}: not all of those points exist (points go from 1 to {max((q['k'] for q in pts), default=0)})")
        mid = {"k": ks[0], "x": round(sum(q["x"] for q in found) / len(found), 2), "y": round(sum(q["y"] for q in found) / len(found), 2)}
        pts = [mid if q["k"] == ks[0] else q for q in pts if q["k"] == ks[0] or q["k"] not in ks]
        notes.append(f"points {', '.join(map(str, ks))} are one point at ({mid['x']:.2f}, {mid['y']:.2f})")
    pts = [dict(q, k=i + 1) for i, q in enumerate(pts)]                       # numbers as the person sees them after --upto and --merge
    if skip:
        gone = [q for q in pts if q["k"] in set(skip)]
        if len(gone) != len(set(skip)):
            raise SurveyError(f"--skip {','.join(map(str, skip))}: not all of those points exist (points go from 1 to {max((q['k'] for q in pts), default=0)})")
        pts = [q for q in pts if q["k"] not in set(skip)]
        notes.append("left out " + ", ".join(f"({q['x']:.2f}, {q['y']:.2f})" for q in gone))
    return [dict(q, k=i + 1) for i, q in enumerate(pts)], notes


def check_legs(root: Path, start: tuple, points: list, min_clear: float) -> list:
    """Problems to know before driving: a point too near a wall or zone, or a leg (also the way back to the start) the planner will refuse."""
    problems = []
    made = goto_test.clearance_grid(root)
    here = start
    for q in points + [{"k": 0, "x": start[0], "y": start[1]}]:
        label = f"point {q['k']} ({q['x']:.2f}, {q['y']:.2f})" if q["k"] else f"the way back to the start ({q['x']:.2f}, {q['y']:.2f})"
        if made is not None and q["k"]:
            clearance, info, _ = made
            r, c = int((q["y"] - info.origin_y) / info.resolution), int((q["x"] - info.origin_x) / info.resolution)
            if not (0 <= r < clearance.shape[0] and 0 <= c < clearance.shape[1]) or clearance[r, c] < min_clear:
                problems.append(f"{label} is closer than {min_clear} m to a wall or zone")
        why = goto_test.route_problem(goto_test.route_check(root, here, (q["x"], q["y"])))
        if why and q["k"]:
            problems.append(f"leg to {label}: {why}")
        elif why:
            problems.append(f"{label}: {why}")
        here = (q["x"], q["y"])
    return problems


def tour_length(start: tuple, points: list) -> float:
    total, here = 0.0, start
    for q in points:
        total += math.hypot(q["x"] - here[0], q["y"] - here[1])
        here = (q["x"], q["y"])
    return total + math.hypot(here[0] - start[0], here[1] - start[1])


def estimate_minutes(length_m: float, count: int, speed: float, dwell: float) -> float:
    return (length_m * 1.4 / max(speed, 0.05) + count * (dwell + 8.0)) / 60.0      # 1.4: the way round furniture; 8 s: turning and settling per point


def save_plan(path: Path, start: tuple, points: list, spacing: float, min_clear: float, notes=()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"start": {"x": start[0], "y": start[1]}, "spacing": spacing, "min_clear": min_clear, "points": points,
                                "made": time.strftime("%Y-%m-%d %H:%M:%S"), "length_m": round(tour_length(start, points), 1), "notes": list(notes)},
                               indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------------------------- driving it
def run_survey(link, root: Path, points: list, start: tuple, speed: float, dwell: float, say: Callable, wait: Callable = time.sleep,
               leg_timeout_s: float = 150.0, log_path: Path = LOG) -> int:
    """Visit the points. Returns 0 when all were done and the robot is back, 1 when it stopped early."""
    log, fails = [], 0

    def write():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps({"started": time.strftime("%Y-%m-%d %H:%M:%S"), "speed": speed, "dwell_s": dwell, "visits": log}, indent=1),
                            encoding="utf-8")

    def go(label, x, y, yaw=0.0):
        """One leg: checked on the map, then driven. (outcome, reason)"""
        p = link.pose
        problem = goto_test.route_problem(goto_test.route_check(root, (p.get("x", 0.0), p.get("y", 0.0)), (x, y)))
        if problem:
            return "skipped", problem
        result = {}
        code = goto_test.drive(link, label, x, y, yaw, speed, leg_timeout_s, say, wait=wait, result=result, verbose=False)
        return ("arrived" if code == 0 else result.get("status", "failed")), result.get("reason", "")

    try:
        for n, q in enumerate(points, 1):
            if not link.available():
                say("stopping: the base is NOT LOCALISED or the link is down.")
                write()
                return 1
            label = f"point {q['k']} of {len(points)} ({q['x']:.2f}, {q['y']:.2f})"
            outcome, reason = go(label, q["x"], q["y"])
            visit = {"k": q["k"], "x": q["x"], "y": q["y"], "outcome": outcome, "reason": reason, "time": time.strftime("%H:%M:%S")}
            if outcome == "arrived":
                fails = 0
                p = link.pose
                visit["pose"] = [p.get("x"), p.get("y"), p.get("yaw")]
                say(f"{label}: arrived, listening for {dwell:.0f} s")
                wait(dwell)
            else:
                say(f"{label}: {outcome}{(' - ' + reason) if reason else ''}")
                if outcome == "interrupted" or reason == "bumper":
                    log.append(visit)
                    write()
                    say("stopping the survey: " + ("Ctrl-C." if outcome == "interrupted" else "the bumper was hit: check the robot, then start again."))
                    return 1
                if outcome != "skipped":
                    fails += 1
                    if fails >= MAX_FAILS_IN_A_ROW:
                        log.append(visit)
                        write()
                        say(f"stopping the survey: {fails} legs failed in a row.")
                        return 1
            log.append(visit)
            write()
        say("all points done: driving back to where it started")
        outcome, reason = go("the start", start[0], start[1])
        say(f"back at the start: {outcome}{(' - ' + reason) if reason else ''}")
        write()
        return 0 if outcome == "arrived" else 1
    except KeyboardInterrupt:
        say("Ctrl-C: stopping")
        link.cancel()
        wait(2.0)
        write()
        return 1


# ---------------------------------------------------------------------------------------------- the command line
def main(argv=None, ask: Callable[[str], str] = input, say: Callable = print, root: Path = ROOT, link=None, wait: Callable = time.sleep) -> int:
    ap = argparse.ArgumentParser(description="Plan and drive a sound survey: listening points on a grid.")
    ap.add_argument("what", choices=("plan", "run"), help="plan: choose and show the points (nothing moves). run: drive them")
    ap.add_argument("--spacing", type=float, default=DEFAULT_SPACING_M, help=f"metres between points (default {DEFAULT_SPACING_M})")
    ap.add_argument("--clearance", type=float, default=DEFAULT_CLEAR_M, help=f"a point is this far from walls and zones (default {DEFAULT_CLEAR_M})")
    ap.add_argument("--start", type=float, nargs=2, metavar=("X", "Y"), help="where the robot starts (plan: default the station; run: where it is now)")
    ap.add_argument("--dwell", type=float, default=DEFAULT_DWELL_S, help=f"seconds to stand still and listen at each point (default {DEFAULT_DWELL_S:.0f})")
    ap.add_argument("--speed", type=float, default=goto_test.DEFAULT_SPEED, help="speed limit in m/s (default 0.2)")
    ap.add_argument("--max", type=int, default=None, help="visit only the first N points")
    ap.add_argument("--upto", type=int, default=None, metavar="N", help="plan: keep only points 1 to N of the lattice tour (the rest are left out)")
    ap.add_argument("--skip", default="", metavar="K,K,..", help="plan: leave these point numbers out (numbered as after --upto/--merge), e.g. where something stands now")
    ap.add_argument("--merge", action="append", default=[], metavar="K,K,..", help="plan: replace those point numbers by ONE point in their middle (repeat for more groups, e.g. --merge 5,6 --merge 9,10,11); "
                    "the numbers are those of the plan without --upto/--merge")
    ap.add_argument("--replan", action="store_true", help="run: plan again from where the robot is now instead of using the saved plan")
    ap.add_argument("--base-url", default=None, help="the base's API (default turtlebot.base_url)")
    args = ap.parse_args(argv)
    if not 1.0 <= args.spacing <= 5.0 or not 0.3 <= args.clearance <= 1.5 or not 2.0 <= args.dwell <= 60.0:
        say("not started: --spacing 1-5 m, --clearance 0.3-1.5 m, --dwell 2-60 s")
        return 2
    if not 0.05 <= args.speed <= goto_test.MAX_SPEED:
        say(f"not started: --speed 0.05 to {goto_test.MAX_SPEED} m/s")
        return 2
    plan_file, log_file = root / "data" / "noise" / "survey_plan.json", root / "data" / "noise" / "survey_log.json"
    try:
        if args.what == "plan":
            from display.server import plan_json
            station = plan_json(root).get("station") or {"x": 0.0, "y": 0.0}
            start = tuple(args.start) if args.start else (station["x"], station["y"])
            points = plan_waypoints(root, start, args.spacing, args.clearance)
            if not points:
                say("no listening points fit (try a smaller --spacing or --clearance).")
                return 1
            merges = []
            for g in args.merge:
                try:
                    merges.append([int(v) for v in g.split(",") if v.strip()])
                except ValueError:
                    say(f"not planned: --merge {g!r} must be point numbers like 5,6")
                    return 2
            try:
                skip = [int(v) for v in args.skip.split(",") if v.strip()]
            except ValueError:
                say(f"not planned: --skip {args.skip!r} must be point numbers like 6")
                return 2
            points, notes = simplify(points, args.upto, merges, skip)
            if not points:
                say("not planned: no points left.")
                return 1
            save_plan(plan_file, start, points, args.spacing, args.clearance, notes)
            for n in notes:
                say("  " + n)
            for problem in check_legs(root, start, points, args.clearance):
                say("  ! " + problem)
            length = tour_length(start, points)
            say(f"{len(points)} listening points {args.spacing} m apart, at least {args.clearance} m from walls and zones, starting from "
                f"({start[0]:.2f}, {start[1]:.2f}); the tour is about {length:.0f} m and would take about "
                f"{estimate_minutes(length, len(points), args.speed, args.dwell):.0f} minutes at {args.speed} m/s with {args.dwell:.0f} s at each point.")
            say("first points: " + ", ".join(f"{q['k']}:({q['x']:.1f},{q['y']:.1f})" for q in points[:6]) + (" ..." if len(points) > 6 else ""))
            say(f"saved {plan_file.relative_to(root)}: look at it in the operator page's Sound map panel (tick 'Survey points and planned route'), then `run`.")
            return 0
        # run
        try:
            settings = goto_test.LinkSettings.from_config(goto_test.load_config(), base_url=args.base_url)
        except goto_test.LinkConfigError as exc:
            raise goto_test.Refused(str(exc))
        link = goto_test.connect(settings, say, link=link)
    except (SurveyError, goto_test.Refused) as exc:
        say(f"not started: {exc}")
        return 2
    try:
        say(goto_test.describe(link))
        if link.ros_ok is False or link.localised is not True:
            say("not started: the base is NOT LOCALISED (or ROS is down). Turn the robot on the spot about 15-20 degrees, stop, wait 5 s and try again.")
            return 2
        p = link.pose
        here = (p.get("x", 0.0), p.get("y", 0.0))
        try:
            if args.replan or not plan_file.is_file():
                points = plan_waypoints(root, tuple(args.start) if args.start else here, args.spacing, args.clearance)
            else:
                points = json.loads(plan_file.read_text(encoding="utf-8"))["points"]
        except (SurveyError, OSError, ValueError, KeyError) as exc:
            say(f"not started: {exc}")
            return 2
        if args.max:
            points = points[:args.max]
        if not points:
            say("not started: no points to visit.")
            return 2
        length = tour_length(here, points)
        say(f"survey: {len(points)} points, about {length:.0f} m, about {estimate_minutes(length, len(points), args.speed, args.dwell):.0f} minutes, "
            f"{args.dwell:.0f} s at each, at {args.speed} m/s at most; it ends back here ({here[0]:.2f}, {here[1]:.2f}).")
        if not (root / "turtlebot" / "maps" / "lidar_map.yaml").is_file():
            say("not started: no map file to check the legs against.")
            return 2
        if ask("The robot will drive on its own for that long. Is the area clear AND is someone at the power switch? Type yes to start: ").strip().lower() != "yes":
            say("not started: not confirmed.")
            return 2
        return run_survey(link, root, points, here, args.speed, args.dwell, say, wait=wait, log_path=log_file)
    finally:
        link.stop_threads()


if __name__ == "__main__":
    sys.exit(main())
