"""Send the TurtleBot to a shelf, a section or a point, once, to test that it can navigate there (AMCL, the keep-out zones, move_base).

    python tools/goto_test.py check                  # read-only: the link, ROS, whether the base is localised, its pose and battery
    python tools/goto_test.py plan  shelf 1-FIC-A    # read-only: where it would go, how far, and whether the target is clear of the zones
    python tools/goto_test.py shelf 1-FIC-A          # DRIVES to that shelf's pose (data/shelves.csv)
    python tools/goto_test.py section Fiction        # DRIVES to the pose of the first shelf in that section
    python tools/goto_test.py xy 0.5 3.0 [yaw]       # DRIVES to a point of the map (metres, radians)

THE ROBOT DRIVES by itself, with the speed limit below (0.2 m/s unless --speed): do it only with the area clear and someone at the
power switch. The tool asks you to type `yes` for that before it sends anything. Ctrl-C cancels the goal and stops the robot. The Dell
also stops by itself 3 s after this program stops sending heartbeats, and a keyboard-teleop window on the Dell overrules navigation
(Ctrl-C that first). Needs the ssh tunnel (scripts/robot_link_tunnel.sh), ROBOT_LINK_TOKEN in the environment, and the Dell started with
`./run_robot_link.sh --amcl --local`. The goal is sent through the same link code as the robot program (robot/drivers/turtlebot_link.py),
so it also tests that.

Exit code: 0 arrived (or a read-only command worked), 1 the goal failed or was cancelled, 2 nothing was sent (link down, not localised,
target not allowed, not confirmed).
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "turtlebot"))                                            # keepout.py (shared with the Dell)

from robot.config import load_config                                                   # noqa: E402
from robot.drivers.turtlebot_link import Command, LinkConfigError, LinkSettings, TurtleBotLink   # noqa: E402

DEFAULT_SPEED = 0.2
ROBOT_RADIUS_M = 0.20          # config/robot_shape.yaml: the planner will not start from, or pass, anything nearer than this to a wall or zone
MAX_SPEED = 0.4
TERMINAL = ("succeeded", "failed", "cancelled")


class Refused(Exception):
    """Nothing is sent: the reason, in a sentence."""


# ---------------------------------------------------------------------------------------------- the target
def shelf_rows(root: Path, floor: int) -> list:
    """[(code, section, x, y, yaw)] of the shelves of the floor that have a pose."""
    rows = []
    with open(root / "data" / "shelves.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(line for line in f if not line.lstrip().startswith("#")):
            try:
                if int(row["floor"]) == floor:
                    rows.append((row["shelf"], row.get("section", ""), float(row["map_x"]), float(row["map_y"]), float(row.get("map_yaw") or 0)))
            except (KeyError, TypeError, ValueError):
                continue
    return rows


def resolve(root: Path, kind: str, args: list, floor: int) -> tuple:
    """(label, x, y, yaw) of what was asked for. Raises Refused."""
    if kind == "xy":
        try:
            x, y = float(args[0]), float(args[1])
            yaw = float(args[2]) if len(args) > 2 else 0.0
        except (IndexError, ValueError):
            raise Refused("usage: xy X Y [YAW]   (metres in the map frame, yaw in radians)")
        return f"the point ({x:.2f}, {y:.2f})", x, y, yaw
    if not args:
        raise Refused(f"usage: {kind} NAME")
    name = " ".join(args)
    rows = shelf_rows(root, floor)
    if kind == "shelf":
        for code, _section, x, y, yaw in rows:
            if code.lower() == name.lower():
                return f"shelf {code}", x, y, yaw
        raise Refused(f"no shelf {name!r} with a pose on level {floor} (shelves there: {', '.join(r[0] for r in rows) or 'none'})")
    for code, section, x, y, yaw in rows:                 # a section: where its first shelf is
        if section.lower() == name.lower():
            return f"the {section} section (shelf {code})", x, y, yaw
    raise Refused(f"no section {name!r} with a shelf pose on level {floor} (sections with shelves there: "
                  f"{', '.join(sorted({r[1] for r in rows})) or 'none'})")


def zone_problem(root: Path, x: float, y: float) -> str:
    """Why (x, y) must not be a goal: inside a keep-out zone or nearer to one than its clearance. '' when it is fine."""
    from display.server import plan_json
    plan = plan_json(root)
    clearance = float(plan.get("clearance_m") or 0.25)
    for z in plan.get("keepout") or []:
        xs, ys = [p[0] for p in z["points"]], [p[1] for p in z["points"]]
        dx = max(min(xs) - x, 0.0, x - max(xs))
        dy = max(min(ys) - y, 0.0, y - max(ys))
        d = math.hypot(dx, dy)
        if d < clearance:
            return f"({x:.2f}, {y:.2f}) is {'inside' if d == 0 else f'{d:.2f} m from'} the keep-out zone {z['name']} (clearance {clearance:.2f} m)"
    if plan.get("available"):
        if not (plan["x0"] <= x <= plan["x0"] + plan["width"] and plan["y0"] <= y <= plan["y0"] + plan["height"]):
            return f"({x:.2f}, {y:.2f}) is outside the floor plan"
    return ""


def clearance_grid(root: Path, map_yaml: Optional[Path] = None):
    """(clearance, info, zones) or None: metres from every cell of the LiDAR map to the nearest wall or keep-out zone (row 0 = the lowest y),
    the map's description (resolution, origin) and the zones. The planner keeps the robot's centre this far from them."""
    import keepout
    import numpy as np
    from PIL import Image
    from scipy import ndimage as ndi
    map_yaml = Path(map_yaml) if map_yaml else root / "turtlebot" / "maps" / "lidar_map.yaml"
    if not map_yaml.is_file():
        return None
    info = keepout.map_info(map_yaml)
    grid = np.array(Image.open(info.image))[::-1]                      # row 0 = the lowest y
    occupied = grid < 50
    zones_file = root / "turtlebot" / "config" / "keepout.yaml"
    zones = keepout.load(zones_file)[0] if zones_file.is_file() else []
    painted = keepout.paint(occupied.astype(np.uint8), info.resolution, info.origin_x, info.origin_y, zones, value=1)[0].astype(bool)
    labels, n = ndi.label(occupied, structure=np.ones((3, 3)))          # specks of a few cells are noise, as the map cleaner thinks
    sizes = ndi.sum(occupied, labels, range(1, n + 1)) if n else []
    specks = np.isin(labels, [i + 1 for i, s in enumerate(sizes) if s < 4])
    return ndi.distance_transform_edt(~(painted & ~specks)) * info.resolution, info, zones


def route_check(root: Path, start: tuple, goal: tuple, radius: float = ROBOT_RADIUS_M, map_yaml: Optional[Path] = None) -> Optional[dict]:
    """An offline estimate of what the planner will find: the LiDAR map with the keep-out zones painted on, and the widest route from
    `start` to `goal` (x, y). {"start": metres of clearance at the start, "goal": the same at the goal, "route": the clearance of the
    tightest point of the best route, "tight_at": (x, y) of that point, "start_near", "tight_near": what is nearest there}. None when
    there is no map file to check against. The real planner also sees what the cameras see now, so this can only say "no" for sure."""
    import heapq

    import keepout
    import numpy as np
    made = clearance_grid(root, map_yaml)
    if made is None:
        return None
    clearance, info, zones = made

    def cell(p):
        return int((p[1] - info.origin_y) / info.resolution), int((p[0] - info.origin_x) / info.resolution)

    def at(r, c):
        return info.origin_x + (c + 0.5) * info.resolution, info.origin_y + (r + 0.5) * info.resolution

    def near(p, d):
        best = min(((keepout.distance(z.points, p[0], p[1]), z.name) for z in zones), default=(1e9, ""))
        return f"the keep-out zone {best[1]}" if best[0] <= d + 1.5 * info.resolution else "a wall or something on the map"

    height, width = clearance.shape
    s, g = cell(start), cell(goal)
    if not (0 <= s[0] < height and 0 <= s[1] < width and 0 <= g[0] < height and 0 <= g[1] < width):
        return {"start": 0.0, "goal": 0.0, "route": 0.0, "tight_at": start, "start_near": "outside the map", "tight_near": "outside the map"}
    r0, r1 = max(0, min(s[0], g[0]) - 60), min(height, max(s[0], g[0]) + 61)      # search a window round both ends, 3 m of margin
    c0, c1 = max(0, min(s[1], g[1]) - 60), min(width, max(s[1], g[1]) + 61)
    best = np.full(clearance.shape, -1.0)
    came = {}
    best[s] = clearance[s]
    queue = [(-clearance[s], s)]
    while queue:
        value, (r, c) = heapq.heappop(queue)
        value = -value
        if value < best[r, c]:
            continue
        if (r, c) == g:
            break
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                nr, nc = r + dr, c + dc
                if (dr or dc) and r0 <= nr < r1 and c0 <= nc < c1:
                    v = min(value, clearance[nr, nc])
                    if v > best[nr, nc]:
                        best[nr, nc] = v
                        came[(nr, nc)] = (r, c)
                        heapq.heappush(queue, (-v, (nr, nc)))
    route = float(max(best[g], 0.0))
    path, cur = [], g
    while cur in came:
        path.append(cur)
        cur = came[cur]
    tight = min(path, key=lambda q: clearance[q]) if path else s
    tight_at = at(*tight)
    return {"start": float(clearance[s]), "goal": float(clearance[g]), "route": route, "tight_at": tight_at,
            "start_near": near(start, float(clearance[s])), "tight_near": near(tight_at, float(clearance[tight]))}


def route_problem(rc: Optional[dict], radius: float = ROBOT_RADIUS_M) -> str:
    """Why the planner would refuse: '' when the check finds nothing wrong (or could not run)."""
    if rc is None:
        return ""
    if rc["start"] < radius:
        return (f"the robot stands only {rc['start']:.2f} m from {rc['start_near']}: the planner will not start from closer than {radius:.2f} m "
                f"(the robot's radius). Move the robot about {radius - rc['start'] + 0.1:.1f} m away from it (back towards the station) and run this again.")
    if rc["goal"] < radius:
        return f"the target is only {rc['goal']:.2f} m from {rc['tight_near']}: the planner will not go that close (needs {radius:.2f} m)."
    if rc["route"] < radius:
        return (f"no route is wide enough: the tightest place on the best way is {rc['route']:.2f} m wide at about "
                f"({rc['tight_at'][0]:.2f}, {rc['tight_at'][1]:.2f}), next to {rc['tight_near']} (the robot needs {radius:.2f} m clear). "
                "Make that gap bigger (the floor plan panel: shrink or move the zone) or push the furniture.")
    return ""


# ---------------------------------------------------------------------------------------------- the link
def connect(settings: LinkSettings, say: Callable, wait_s: float = 10.0, link: Optional[TurtleBotLink] = None) -> TurtleBotLink:
    link = link or TurtleBotLink(settings)
    link.start_threads()
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if link.status == "connected" and link.pose.get("x") is not None:
            return link
        time.sleep(0.2)
    link.stop_threads()
    if link.unreachable or link.status == "lost":
        raise Refused(f"cannot reach the base at {settings.base_url}: is the tunnel up (scripts/robot_link_tunnel.sh robot@<the Dell's address> 8010) "
                      "and the Dell started with ./run_robot_link.sh --amcl --local?")
    raise Refused("the base answered but sent no pose yet")


def describe(link: TurtleBotLink) -> str:
    i = link.info()
    p = i["pose"]
    return (f"link {i['status']}, ROS {'up' if i['ros_ok'] else 'DOWN' if i['ros_ok'] is False else '?'}, "
            f"{'LOCALISED' if i['localised'] else 'NOT LOCALISED' if i['localised'] is False else 'localised ?'}, "
            f"pose x {p.get('x', float('nan')):.2f} y {p.get('y', float('nan')):.2f} yaw {p.get('yaw', float('nan')):.2f}, "
            f"battery {('%.0f%%' % (100 * i['battery'])) if i['battery'] is not None else '?'}")


def drive(link: TurtleBotLink, label: str, x: float, y: float, yaw: float, speed: float, timeout_s: float, say: Callable,
          wait: Callable = time.sleep, result: Optional[dict] = None, verbose: bool = True) -> int:
    """Send one goal and follow it to the end. Returns 0 arrived, 1 failed or cancelled. `result`, when given, gets {"status", "reason"};
    verbose=False prints only the start and the end."""
    result = result if result is not None else {}
    events = []
    link.sink = events.append
    for attempt in range(1, 4):                                # the Dell's ROS can be slow to answer: ask again, a few times
        if link.run_command(Command("max_speed", {"m_s": round(speed, 3)})):
            say(f"speed limit {speed:.2f} m/s applied.")
            break
        say(f"  speed limit not applied (try {attempt} of 3)")
        wait(1.0)
    else:
        say("the speed limit could not be applied: the base's own limit (0.25 m/s, hokuyo_costmap.yaml) still holds.")
    gid = link.new_goal_id()
    link.send_goal(gid, x, y, yaw)
    say(f"goal {gid} sent: driving to {label} at {speed:.2f} m/s at most. Ctrl-C cancels it; the power switch is the last resort.")
    start, last_line = time.monotonic(), ""
    try:
        while True:
            wait(1.0)
            nav = dict(link.nav)
            mine = nav.get("goal_id") == gid
            status = nav.get("status") if mine else "waiting for the base to accept"
            p = link.pose
            line = (f"  {status}"
                    + (f", {nav['distance_left']:.2f} m left" if mine and nav.get("distance_left") is not None else "")
                    + f"   pose ({p.get('x', float('nan')):.2f}, {p.get('y', float('nan')):.2f}) yaw {p.get('yaw', float('nan')):.2f}")
            if line != last_line and verbose:
                say(line)
                last_line = line
            failed = [e for e in events if type(e).__name__ == "NavFailed" and getattr(e, "goal_id", None) == gid]
            if failed:
                result.update(status="failed", reason=failed[0].reason or "failed")
                say(f"FAILED: {failed[0].reason or 'failed'} (see docs/robot_link.md, POST /api/goal, for what the reasons mean)")
                return 1
            if mine and status == "succeeded":
                result.update(status="succeeded", reason="")
                say(f"ARRIVED at {label}: pose ({p.get('x', float('nan')):.2f}, {p.get('y', float('nan')):.2f}), "
                    f"{math.hypot(p.get('x', 0) - x, p.get('y', 0) - y):.2f} m from the goal")
                return 0
            if mine and status in ("failed", "cancelled"):
                result.update(status=status, reason=str(nav.get("reason") or ""))
                say(f"{status.upper()}: {nav.get('reason') or 'no reason given'}")
                return 1
            if time.monotonic() - start > timeout_s:
                result.update(status="timeout", reason="timeout")
                say(f"no result after {timeout_s:.0f} s: cancelling")
                link.cancel(gid)
                wait(2.0)
                return 1
    except KeyboardInterrupt:
        result.update(status="interrupted", reason="ctrl-c")
        say("Ctrl-C: cancelling the goal")
        link.cancel(gid)
        wait(2.0)
        say(f"  base says: {link.nav.get('status')} {link.nav.get('reason') or ''}".rstrip())
        return 1


# ---------------------------------------------------------------------------------------------- the command line
def main(argv=None, ask: Callable[[str], str] = input, say: Callable = print, root: Path = ROOT, link: Optional[TurtleBotLink] = None) -> int:
    ap = argparse.ArgumentParser(description="Drive the TurtleBot to a shelf, section or point to test navigation.", add_help=True)
    ap.add_argument("what", nargs="+", help="check | plan <shelf|section|xy ...> | shelf CODE | section NAME | xy X Y [YAW]")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED, help=f"speed limit in m/s (default {DEFAULT_SPEED}, at most {MAX_SPEED})")
    ap.add_argument("--timeout", type=float, default=150.0, help="cancel the goal after this many seconds (default 150)")
    ap.add_argument("--floor", type=int, default=None, help="the floor of the base's map (default turtlebot.floor, else 1)")
    ap.add_argument("--base-url", default=None, help="the base's API (default turtlebot.base_url of config/robot.yaml)")
    args = ap.parse_args(argv)
    cfg = load_config()
    floor = args.floor if args.floor is not None else int((cfg.turtlebot.get("floor") or 1))
    dry = args.what[0] == "plan"
    words = args.what[1:] if dry else args.what
    kind = words[0]
    try:
        if not 0.05 <= args.speed <= MAX_SPEED:
            raise Refused(f"--speed must be between 0.05 and {MAX_SPEED} m/s")
        if kind not in ("check", "shelf", "section", "xy"):
            raise Refused("what: check | plan <...> | shelf CODE | section NAME | xy X Y [YAW]")
        target = None
        if kind != "check":
            label, x, y, yaw = target = resolve(root, kind, words[1:], floor)
            problem = zone_problem(root, x, y)
            if problem:
                raise Refused(f"{label}: {problem}")
        try:
            settings = LinkSettings.from_config(cfg, base_url=args.base_url)
        except LinkConfigError as exc:
            raise Refused(str(exc))
        link = connect(settings, say, link=link)
    except Refused as exc:
        say(f"not sent: {exc}")
        return 2
    try:
        say(describe(link))
        if kind == "check":
            return 0 if link.localised and link.ros_ok is not False else 2
        label, x, y, yaw = target
        p = link.pose
        say(f"from ({p.get('x', float('nan')):.2f}, {p.get('y', float('nan')):.2f}) to {label} at ({x:.2f}, {y:.2f}) facing {yaw:.2f} rad: "
            f"{math.hypot(x - p.get('x', 0), y - p.get('y', 0)):.1f} m in a straight line")
        rc = route_check(root, (p.get("x", 0.0), p.get("y", 0.0)), (x, y))
        if rc is None:
            say("route check: no map file here (turtlebot/maps/lidar_map.yaml), so the route was not checked.")
        else:
            say(f"route check (an estimate from the map and the zones): clearance at the robot {rc['start']:.2f} m, at the target {rc['goal']:.2f} m, "
                f"tightest point of the best route {rc['route']:.2f} m (the robot needs {ROBOT_RADIUS_M:.2f} m).")
        problem = route_problem(rc)
        if problem:
            say(f"not sent: {problem}")
            return 2
        if dry:
            say("(plan only: nothing was sent)")
            return 0
        if link.ros_ok is False or link.localised is not True:
            say("not sent: the base is NOT LOCALISED (or ROS is down). Turn the robot on the spot about 15-20 degrees, stop, wait 5 s and run `check` again.")
            return 2
        answer = ask("Is the area clear AND is someone at the power switch? Type yes to drive: ").strip().lower()
        if answer != "yes":
            say("not sent: not confirmed.")
            return 2
        return drive(link, label, x, y, yaw, args.speed, args.timeout, say)
    finally:
        link.stop_threads()


if __name__ == "__main__":
    sys.exit(main())
