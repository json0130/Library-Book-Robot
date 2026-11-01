"""Teach the robot where things are: drive the TurtleBot there (keyboard teleop on the Dell, or push it) and record its pose.

    python tools/teach_poses.py                          # the Dell at turtlebot.base_url, token from $ROBOT_LINK_TOKEN
    python tools/teach_poses.py --floor 2 --base-url http://192.168.50.2:8010

It shows the live pose (RTAB-Map `map` frame, metres and radians: the same numbers the robot drives to) and takes commands:

    pose                              show the pose now (also in the prompt)
    watch [seconds]                   print the pose every half second (Ctrl-C stops)
    shelf <code> [section name]       this pose is the shelf's pose in data/shelves.csv (map_x, map_y, map_yaw). A shelf the
                                      file does not have yet needs its section, and is added on the floor given by --floor
    section <name> corner1|corner2    the two opposite corners of a section's rectangle in data/floorplan.yaml (floor --floor)
    landmark <kind> <name>            a lift, help_desk, entrance, exit, stairs, toilets or other point of the floor plan
    station                           where the robot waits for visitors (the floor plan's `station`)
    list                              what is waiting to be saved
    save                              write the files, then run tools/catalog_check.py
    discard                           forget what is waiting
    quit

Nothing is written until `save`. Then each file is first copied to data/backups/<name>.<timestamp>.bak, edited in place (the
comments and the layout of the files stay), and checked: if the floor plan no longer loads, the backup is put back. The base
must be localised (RTAB-Map knows where it is) or nothing is recorded: a pose taught while lost would send the robot to the
wrong place.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/...`

from robot.services.floorplan import LANDMARK_KINDS, FloorPlan, FloorPlanError

TODO = re.compile(r"\s*TODO:[^.]*placeholder pose[^.]*\.", re.IGNORECASE)
SHELF_COLUMNS = ("shelf", "section", "floor", "zone", "map_x", "map_y", "map_yaw", "description")


class TeachError(ValueError):
    """A command that cannot be done; the message says why."""


def fmt(value: float) -> str:
    text = f"{value:.2f}"
    return "0.00" if text in ("-0.00",) else text


# ---------------------------------------------------------------------------------------------- shelves.csv
def update_shelves(text: str, poses: dict, new_rows: dict) -> str:
    """shelves.csv with the poses of `poses` {code: (x, y, yaw)} filled in (and the "TODO: placeholder pose" sentence of their
    description removed), and `new_rows` {code: (section, floor, zone, (x, y, yaw))} added at the end. Comment lines, the order
    of the rows and everything else stay as they are. Raises TeachError for a code that is not in the file."""
    lines = text.splitlines()
    out, header, seen = [], None, set()
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            out.append(line)
            continue
        fields = next(csv.reader([line]))
        if header is None:
            header = [h.strip() for h in fields]
            missing = [c for c in SHELF_COLUMNS if c not in header]
            if missing:
                raise TeachError(f"shelves.csv has no column {', '.join(missing)}")
            out.append(line)
            continue
        row = dict(zip(header, fields))
        code = row.get("shelf", "").strip()
        if code in poses:
            x, y, yaw = poses[code]
            row.update(map_x=fmt(x), map_y=fmt(y), map_yaw=fmt(yaw), description=TODO.sub("", row.get("description", "")).strip())
            buf = io.StringIO()
            csv.writer(buf, lineterminator="").writerow([row.get(h, "") for h in header])
            line = buf.getvalue()
            seen.add(code)
        out.append(line)
    if header is None:
        raise TeachError("shelves.csv has no header row")
    unknown = sorted(set(poses) - seen)
    if unknown:
        raise TeachError(f"not in shelves.csv: {', '.join(unknown)}")
    for code, (section, floor, zone, (x, y, yaw)) in new_rows.items():
        row = {"shelf": code, "section": section, "floor": str(floor), "zone": zone, "map_x": fmt(x), "map_y": fmt(y),
               "map_yaw": fmt(yaw), "description": f"{section} (added by teach_poses)"}
        buf = io.StringIO()
        csv.writer(buf, lineterminator="").writerow([row.get(h, "") for h in header])
        out.append(buf.getvalue())
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------------- floorplan.yaml
FLOOR = re.compile(r"^(\s*)-\s+level:\s*(\d+)\s*(#.*)?$")
ITEM = re.compile(r"^(\s*)-\s+\{.*\}\s*(#.*)?$")


def _floor_block(lines: list, level: int) -> tuple:
    """(start, end) line numbers of a floor's block (end exclusive)."""
    start, indent = None, ""
    for i, line in enumerate(lines):
        m = FLOOR.match(line)
        if m and int(m.group(2)) == level:
            start, indent = i, m.group(1)
            break
    if start is None:
        raise TeachError(f"level {level} is not in the floor plan (floors there: "
                         + ", ".join(sorted({FLOOR.match(l).group(2) for l in lines if FLOOR.match(l)}, key=int)) + ")")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = FLOOR.match(line)
        if (m and m.group(1) == indent) or not line.startswith(" "):          # the next floor, or a top-level key
            end = j
            break
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1                                                            # blank and comment lines belong to what follows
    return start, end


def _list_range(lines: list, start: int, end: int, key: str) -> Optional[tuple]:
    """(key line, first item line, one past the last item line) of `sections:` / `landmarks:` in a floor block."""
    for i in range(start, end):
        m = re.match(rf"^(\s*){key}:\s*(#.*)?$", lines[i])
        if m:
            j = i + 1
            while j < end and (ITEM.match(lines[j]) or not lines[j].strip() or lines[j].lstrip().startswith("#")):
                j += 1
            while j > i + 1 and (not lines[j - 1].strip() or lines[j - 1].lstrip().startswith("#")):
                j -= 1
            return i, i + 1, j
    return None


def _upsert(lines: list, level: int, key: str, name: str, match: Callable[[str], bool], render: Callable[[Optional[str]], str]) -> None:
    """Replace the list item of `key` for which match(line) holds with render(old line), or add render(None) at the end of the list."""
    start, end = _floor_block(lines, level)
    found = _list_range(lines, start, end, key)
    if found is None:                                                       # no such list yet: start one after `height:`
        base = next((i for i in range(start, end) if re.match(r"^\s*height:", lines[i])), start)
        indent = re.match(r"^(\s*)", lines[start]).group(1) + "    "
        lines[base + 1:base + 1] = [f"{indent}{key}:", f"{indent}  - {render(None)}"]
        return
    key_line, first, last = found
    item_indent = (ITEM.match(lines[first]).group(1) if first < last and ITEM.match(lines[first])
                   else re.match(r"^(\s*)", lines[key_line]).group(1) + "  ")
    for i in range(first, last):
        if ITEM.match(lines[i]) and match(lines[i]):
            lines[i] = f"{item_indent}- {render(lines[i])}"
            return
    lines.insert(last, f"{item_indent}- {render(None)}")


def update_floorplan(text: str, level: int, sections: Optional[dict] = None, landmarks: Optional[list] = None,
                     station: Optional[tuple] = None) -> str:
    """floorplan.yaml with sections {name: (x, y, w, h)}, landmarks [(kind, name, x, y)] and the station (x, y, yaw) written
    in. A section or landmark that is there already (same name, and kind for a landmark) has its numbers replaced, anything
    else on its line (a `zone:`) kept; a new one is added to the end of the floor's list. Comments and layout stay."""
    lines = text.split("\n")
    for name, (x, y, w, h) in (sections or {}).items():
        def keep_zone(old, name=name, x=x, y=y, w=w, h=h):
            zone = re.search(r"zone:\s*(\w+)", old or "")
            return "{name: %s, x: %s, y: %s, w: %s, h: %s%s}" % (name, fmt(x), fmt(y), fmt(w), fmt(h),
                                                               f", zone: {zone.group(1)}" if zone else "")
        _upsert(lines, level, "sections", name, lambda l, n=name: re.search(r"name:\s*" + re.escape(n) + r"\s*[,}]", l, re.I) is not None, keep_zone)
    for kind, name, x, y in landmarks or []:
        def render(old, kind=kind, name=name, x=x, y=y):
            return "{kind: %s, name: %s, x: %s, y: %s}" % (kind, name, fmt(x), fmt(y))
        _upsert(lines, level, "landmarks", name, lambda l, k=kind, n=name: re.search(r"kind:\s*" + k + r"\s*,", l) is not None
                and re.search(r"name:\s*" + re.escape(n) + r"\s*[,}]", l, re.I) is not None, render)
    if station is not None:
        x, y, yaw = station
        i = next((i for i, l in enumerate(lines) if re.match(r"^station:", l)), None)
        block = [f"  level: {level}", f"  x: {fmt(x)}", f"  y: {fmt(y)}", f"  yaw: {fmt(yaw)}"]
        if i is None:
            while lines and not lines[-1].strip():
                lines.pop()
            lines += ["", "station:"] + block + [""]
        else:
            j = i + 1
            while j < len(lines) and (lines[j].startswith(" ") or not lines[j].strip()) and lines[j].strip():
                j += 1
            lines[i + 1:j] = block
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------- the teacher
class Teacher:
    """Holds what has been taught and not yet saved. `pose()` returns (x, y, yaw, localised); `check` runs the catalogue check."""

    def __init__(self, pose: Callable[[], tuple], shelves_path: Path, floorplan_path: Path, floor: int,
                 backup_dir: Optional[Path] = None, check: Optional[Callable[[], int]] = None,
                 clock: Callable[[], float] = time.time):
        self.pose, self.shelves_path, self.floorplan_path, self.floor = pose, Path(shelves_path), Path(floorplan_path), int(floor)
        self.backup_dir = Path(backup_dir) if backup_dir else self.shelves_path.parent / "backups"
        self.check, self.clock = check, clock
        self.shelves: dict = {}                  # code -> (x, y, yaw)
        self.new_shelves: dict = {}              # code -> (section, floor, zone, pose)
        self.corners: dict = {}                  # section -> {1: (x, y), 2: (x, y)}
        self.landmarks: list = []                # (kind, name, x, y)
        self.station: Optional[tuple] = None

    # ---- recording ----
    def here(self) -> tuple:
        try:
            x, y, yaw, localised = self.pose()
        except Exception as exc:                 # the link failed, the base answered something odd...
            raise TeachError(f"cannot read the base's pose: {exc}") from exc
        if not localised:
            raise TeachError("the base is not localised (RTAB-Map does not know where it is): drive a little in a mapped place "
                             "until it is, then try again. Nothing was recorded.")
        return x, y, yaw

    def known_shelves(self) -> dict:
        """{code: (section, floor, zone)} from shelves.csv."""
        out = {}
        if not self.shelves_path.is_file():
            return out
        lines = [l for l in self.shelves_path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.lstrip().startswith("#")]
        for row in csv.DictReader(lines):
            out[row["shelf"].strip()] = (row.get("section", ""), row.get("floor", ""), row.get("zone", ""))
        return out

    def command(self, line: str) -> str:
        """Run one command; returns what to print. Raises TeachError."""
        words = line.split()
        if not words:
            return ""
        cmd, args = words[0].lower(), words[1:]
        if cmd == "pose":
            return self.describe()
        if cmd == "list":
            return self.pending()
        if cmd == "discard":
            self.__init__(self.pose, self.shelves_path, self.floorplan_path, self.floor, self.backup_dir, self.check, self.clock)
            return "forgotten"
        if cmd == "save":
            return self.save()
        if cmd == "shelf":
            return self.teach_shelf(args)
        if cmd == "section":
            return self.teach_section(args)
        if cmd == "landmark":
            return self.teach_landmark(args)
        if cmd == "station":
            x, y, yaw = self.here()
            self.station = (x, y, yaw)
            return f"station: ({fmt(x)}, {fmt(y)}, yaw {fmt(yaw)}) on level {self.floor}"
        raise TeachError(f"unknown command {cmd!r}; try: pose watch shelf section landmark station list save discard quit")

    def describe(self) -> str:
        try:
            x, y, yaw, localised = self.pose()
        except Exception as exc:
            return f"no pose: {exc}"
        return f"x {x:.2f}  y {y:.2f}  yaw {yaw:.2f}  {'localised' if localised else 'NOT LOCALISED'}"

    def teach_shelf(self, args: list) -> str:
        if not args:
            raise TeachError("usage: shelf <code> [section name]")
        code, section = args[0], " ".join(args[1:])
        known = self.known_shelves()
        if code not in known and not section:
            raise TeachError(f"{code} is not in shelves.csv: say its section too, e.g. `shelf {code} Fiction`")
        x, y, yaw = self.here()
        if code in known:
            self.shelves[code] = (x, y, yaw)
            return f"shelf {code}: map_x {fmt(x)}, map_y {fmt(y)}, map_yaw {fmt(yaw)}"
        zone = next((z for s, _, z in known.values() if s.lower() == section.lower() and z), "common")
        self.new_shelves[code] = (section, self.floor, zone, (x, y, yaw))
        return f"new shelf {code} in {section} (level {self.floor}, {zone}): map_x {fmt(x)}, map_y {fmt(y)}, map_yaw {fmt(yaw)}"

    def teach_section(self, args: list) -> str:
        if len(args) < 2 or args[-1].lower() not in ("corner1", "corner2"):
            raise TeachError("usage: section <name> corner1|corner2")
        name, corner = " ".join(args[:-1]), int(args[-1][-1])
        x, y, _ = self.here()
        have = self.corners.setdefault(name, {})
        have[corner] = (x, y)
        if len(have) < 2:
            return f"section {name}: corner {corner} at ({fmt(x)}, {fmt(y)}); now the opposite corner"
        (x1, y1), (x2, y2) = have[1], have[2]
        return f"section {name}: rectangle x {fmt(min(x1, x2))}, y {fmt(min(y1, y2))}, {fmt(abs(x2 - x1))} x {fmt(abs(y2 - y1))} m"

    def teach_landmark(self, args: list) -> str:
        if len(args) < 2 or args[0] not in LANDMARK_KINDS:
            raise TeachError(f"usage: landmark <kind> <name>; kinds: {', '.join(LANDMARK_KINDS)}")
        x, y, _ = self.here()
        kind, name = args[0], " ".join(args[1:])
        self.landmarks = [l for l in self.landmarks if (l[0], l[1].lower()) != (kind, name.lower())] + [(kind, name, x, y)]
        return f"landmark {kind} {name}: ({fmt(x)}, {fmt(y)}) on level {self.floor}"

    def rectangles(self) -> dict:
        return {n: (min(c[1][0], c[2][0]), min(c[1][1], c[2][1]), abs(c[2][0] - c[1][0]), abs(c[2][1] - c[1][1]))
                for n, c in self.corners.items() if len(c) == 2}

    def pending(self) -> str:
        rows = [f"shelf {c}: {tuple(map(fmt, p))}" for c, p in self.shelves.items()]
        rows += [f"new shelf {c} in {s} (level {f})" for c, (s, f, _, _) in self.new_shelves.items()]
        rows += [f"section {n}: {tuple(map(fmt, r))}" for n, r in self.rectangles().items()]
        rows += [f"section {n}: only one corner so far (not saved)" for n, c in self.corners.items() if len(c) < 2]
        rows += [f"landmark {k} {n}: ({fmt(x)}, {fmt(y)})" for k, n, x, y in self.landmarks]
        if self.station:
            rows.append("station: " + ", ".join(map(fmt, self.station)))
        return "\n".join(rows) if rows else "(nothing waiting)"

    # ---- saving ----
    def backup(self, path: Path) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        target = self.backup_dir / f"{path.name}.{stamp}.bak"
        n = 1
        while target.exists():
            n += 1
            target = self.backup_dir / f"{path.name}.{stamp}-{n}.bak"
        shutil.copy2(path, target)
        return target

    def save(self) -> str:
        """Write the shelves and the floor plan. Returns a report. The floor plan is checked and put back if it no longer loads."""
        done = []
        if self.shelves or self.new_shelves:
            old = self.shelves_path.read_text(encoding="utf-8")
            new = update_shelves(old, self.shelves, self.new_shelves)
            done.append((self.shelves_path, self.backup(self.shelves_path)))
            self.shelves_path.write_text(new, encoding="utf-8")
        rects, plan_change = self.rectangles(), bool(self.landmarks or self.station)
        if rects or plan_change:
            old = self.floorplan_path.read_text(encoding="utf-8")
            new = update_floorplan(old, self.floor, rects, self.landmarks, self.station)
            backup = self.backup(self.floorplan_path)
            self.floorplan_path.write_text(new, encoding="utf-8")
            try:
                FloorPlan.load(self.floorplan_path)
            except FloorPlanError as exc:
                shutil.copy2(backup, self.floorplan_path)
                raise TeachError(f"the floor plan would not load with these ({exc}); {self.floorplan_path.name} was put back. "
                                 "The floor plan's rectangle for the floor is 0..width and 0..height: do the poses fit it?") from exc
            done.append((self.floorplan_path, backup))
        if not done:
            return "nothing to save (only complete things are saved: a section needs both corners)"
        report = [f"saved {p.name} (the old one is {b})" for p, b in done]
        self.__init__(self.pose, self.shelves_path, self.floorplan_path, self.floor, self.backup_dir, self.check, self.clock)
        if self.check is not None:
            report.append(f"catalog_check exit code {self.check()}")
        return "\n".join(report)


def main(argv=None) -> int:
    from robot.app import build_link
    from robot.config import load_config
    from robot.drivers.turtlebot_link import LinkConfigError, LinkError
    cfg = load_config()
    books = cfg.books
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--base-url", default=None, help="the Dell's address (default turtlebot.base_url)")
    p.add_argument("--floor", type=int, default=None, help="the floor the base is on (default turtlebot.floor, else the station's)")
    p.add_argument("--shelves", type=Path, default=cfg.path(books["shelves_path"]))
    p.add_argument("--floorplan", type=Path, default=cfg.path(books.get("floorplan_path") or "data/floorplan.yaml"))
    p.add_argument("--no-check", action="store_true", help="do not run catalog_check after saving")
    args = p.parse_args(argv)
    try:
        link = build_link(cfg, base_url=args.base_url)
    except LinkConfigError as exc:
        print(f"teach_poses: {exc}", file=sys.stderr)
        return 2
    floor = args.floor
    if floor is None:
        floor = cfg.turtlebot.get("floor")
    if floor is None:
        try:
            floor = FloorPlan.load(args.floorplan).station.level
        except (FloorPlanError, AttributeError):
            floor = 1

    def pose() -> tuple:
        try:
            state = link.fetch_state()
            p_ = state["pose"]
            return float(p_["x"]), float(p_["y"]), float(p_["yaw"]), bool(p_.get("localised"))
        except (LinkError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    def check() -> int:
        from tools.catalog_check import main as check_main
        return check_main(["--shelves", str(args.shelves), "--floorplan", str(args.floorplan)])

    teacher = Teacher(pose, args.shelves, args.floorplan, floor, check=None if args.no_check else check)
    print(f"teach_poses: the base at {link.settings.base_url}, level {floor}. Type `help` for the commands.\n  {teacher.describe()}")
    while True:
        try:
            line = input(f"[{teacher.describe()}]\nteach> " if sys.stdin.isatty() else "")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        word = line.strip().split(None, 1)
        if word and word[0].lower() in ("quit", "exit"):
            break
        if word and word[0].lower() in ("help", "?"):
            print(__doc__)
            continue
        if word and word[0].lower() == "watch":
            try:
                until = time.time() + float(word[1]) if len(word) > 1 else float("inf")
                while time.time() < until:
                    print(f"  {teacher.describe()}", flush=True)
                    time.sleep(0.5)
            except (KeyboardInterrupt, ValueError):
                pass
            continue
        try:
            reply = teacher.command(line)
        except TeachError as exc:
            reply = f"  ! {exc}"
        if reply:
            print(reply, flush=True)
    unsaved = teacher.pending()
    if unsaved != "(nothing waiting)":
        print("not saved (use `save` first):\n" + unsaved, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
