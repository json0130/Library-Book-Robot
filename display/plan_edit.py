"""Saving what the operator page's floor-plan editor drew: the keep-out zones (turtlebot/config/keepout.yaml) and the sections of
the floor (data/floorplan.yaml), without driving the robot to their corners.

    save_edits(root, keepout_rects, sections)

`keepout_rects` is the complete list of rectangle zones {name, rect: [x1, y1, x2, y2]} and `sections` the complete list of the
floor's sections {name, x, y, w, h}: what is not in the list is removed. Polygon zones, landmarks, the station and the shelf poses
are never touched. Everything is checked first (the same checks as `tools/teach_poses.py` and `turtlebot/keepout.py`: a zone
off the map, over the station or a shelf pose, or nearer than clearance_m; a section that no longer holds its shelf's pose),
and nothing is written when anything is wrong. The files are edited as text, so the comments and layout stay, and each is copied
to data/backups/<name>.<time>.bak first.
"""

from __future__ import annotations

import csv
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from tools import teach_poses as tp                 # its text editing of floorplan.yaml (and keepout on sys.path)
import keepout                                       # turtlebot/keepout.py (teach_poses puts turtlebot/ on sys.path)
from robot.services.floorplan import FloorPlan, FloorPlanError

ROOT = Path(__file__).resolve().parent.parent
MIN_SIDE_M = 0.10                                   # a zone or section thinner than this is a line, not an area
SECTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")
MAX_ITEMS = 200


class EditError(ValueError):
    """Everything wrong with the edit, as a list of plain sentences."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _num(value, what: str) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise EditError([f"{what}: not a number"])
    if x != x or abs(x) > 1000:
        raise EditError([f"{what}: not a sensible number of metres"])
    return round(x, 2)


def clean_rects(items) -> dict:
    """{name: (x1, y1, x2, y2)} from the posted zones, checked."""
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise EditError(["keepout must be a list of zones"])
    out, problems = {}, []
    for item in items:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not keepout.NAME.match(name):
            problems.append(f"a keep-out zone's name is letters, digits, _ and - (at most 40): {name!r}")
            continue
        if name in out:
            problems.append(f"keep-out zone {name}: the name is used twice")
            continue
        try:
            rect = item["rect"]
            if len(rect) != 4:
                raise TypeError
            x1, y1, x2, y2 = (_num(v, f"zone {name}") for v in rect)
        except (KeyError, TypeError):
            problems.append(f"keep-out zone {name}: needs rect [x1, y1, x2, y2]")
            continue
        except EditError as exc:
            problems += exc.problems
            continue
        if abs(x2 - x1) < MIN_SIDE_M or abs(y2 - y1) < MIN_SIDE_M:
            problems.append(f"keep-out zone {name}: {abs(x2 - x1):.2f} x {abs(y2 - y1):.2f} m is too thin (at least {MIN_SIDE_M:.2f} m each way)")
            continue
        out[name] = (x1, y1, x2, y2)
    if problems:
        raise EditError(problems)
    return out


def clean_sections(items) -> dict:
    """{name: (x, y, w, h)} from the posted sections, checked."""
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise EditError(["sections must be a list"])
    out, problems = {}, []
    for item in items:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not SECTION_NAME.match(name) or name != name.strip():
            problems.append(f"a section's name is letters, digits, spaces, _ and - (at most 40): {name!r}")
            continue
        if name.lower() in {n.lower() for n in out}:
            problems.append(f"section {name}: the name is used twice")
            continue
        try:
            x, y, w, h = (_num(item.get(k), f"section {name} {k}") for k in ("x", "y", "w", "h"))
        except EditError as exc:
            problems += exc.problems
            continue
        if w < MIN_SIDE_M or h < MIN_SIDE_M:
            problems.append(f"section {name}: {w:.2f} x {h:.2f} m is too small (at least {MIN_SIDE_M:.2f} m each way)")
            continue
        out[name] = (x, y, w, h)
    if problems:
        raise EditError(problems)
    return out


# ---------------------------------------------------------------------------------------------- the text of the two files
def zones_text(old: str, rects: dict) -> str:
    """keepout.yaml's text with its rectangle zones made to be `rects` {name: (x1, y1, x2, y2)}: the ones not named are removed,
    the others written in (replaced, or added at the end). Polygon zones and the comments stay."""
    import yaml
    try:
        data = yaml.safe_load(old) or {}
    except yaml.YAMLError as exc:
        raise EditError([f"keepout.yaml cannot be read: {exc}"])
    zones = [z for z in (data.get("zones") or []) if isinstance(z, dict)] if isinstance(data, dict) else []
    have_rect = {str(z.get("name")) for z in zones if "rect" in z}
    have_poly = {str(z.get("name")) for z in zones if "polygon" in z}
    clash = sorted(set(rects) & have_poly)
    if clash:
        raise EditError([f"keep-out zone {n}: a polygon zone has that name already (polygons are edited in the file)" for n in clash])
    drop = have_rect - set(rects)
    lines = []
    for line in old.split("\n"):
        m = keepout.ITEM.match(line)
        if m and m.group(2).strip("'\"") in drop:
            continue
        lines.append(line)
    text = "\n".join(lines)
    for name, rect in rects.items():
        text = keepout.set_rect_text(text, name, rect)
    lines = text.split("\n")
    key = next((i for i, l in enumerate(lines) if re.match(r"^zones:", l)), None)
    if key is not None and not any(keepout.ITEM.match(l) for l in lines[key + 1:]) and not re.match(r"^zones:\s*\[", lines[key]):
        comment = re.search(r"#.*$", lines[key])
        lines[key] = "zones: []" + ("   " + comment.group(0) if comment else "")
        text = "\n".join(lines)
    return text


def sections_text(old: str, level: int, sections: dict) -> str:
    """floorplan.yaml's text with the sections of floor `level` made to be `sections` {name: (x, y, w, h)}: the ones not named are
    removed, the others written in (a section's own `zone:` stays). Everything else stays."""
    lines = old.split("\n")
    try:
        start, end = tp._floor_block(lines, level)
    except tp.TeachError as exc:
        raise EditError([str(exc)])
    for i in range(start, end):                                  # `sections: []` would hide the list from the editor: start it afresh
        if re.match(r"^\s*sections:\s*\[\s*\]\s*(#.*)?$", lines[i]):
            del lines[i]
            start, end = tp._floor_block(lines, level)
            break
    found = tp._list_range(lines, start, end, "sections")
    if found is not None:
        key_line, first, last = found
        keep = []
        for i in range(first, last):
            m = re.search(r"name:\s*([^,}]+?)\s*[,}]", lines[i]) if tp.ITEM.match(lines[i]) else None
            if m and m.group(1).strip("'\"") not in sections:
                continue
            keep.append(lines[i])
        lines[first:last] = keep
        if not any(tp.ITEM.match(l) for l in keep) and not sections:
            lines[key_line] = re.sub(r"sections:.*$", "sections: []", lines[key_line])
    return tp.update_floorplan("\n".join(lines), level, sections=sections)


# ---------------------------------------------------------------------------------------------- checking and saving
def _shelf_rows(shelves_path: Path, level: int) -> list:
    rows = []
    try:
        with open(shelves_path, newline="") as f:
            for row in csv.DictReader(line for line in f if not line.lstrip().startswith("#")):
                try:
                    if int(row["floor"]) == level:
                        rows.append((row["shelf"], row.get("section", ""), float(row["map_x"]), float(row["map_y"])))
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        pass
    return rows


def problems_with(root: Path, level: int, plan_text: str, zones_text_: str, map_yaml: Optional[Path]) -> list:
    """What is wrong with these two files together (empty: all well)."""
    tmp = Path(tempfile.mkdtemp(prefix="plan_edit_"))
    try:
        (tmp / "floorplan.yaml").write_text(plan_text, encoding="utf-8")
        (tmp / "keepout.yaml").write_text(zones_text_, encoding="utf-8")
        try:
            plan = FloorPlan.load(tmp / "floorplan.yaml")
        except FloorPlanError as exc:
            return [str(exc)]
        problems = []
        floor = plan.floor(level)
        for code, section, x, y in _shelf_rows(root / "data" / "shelves.csv", level):
            found = floor.section(section) if floor is not None and section else None
            if floor is not None and section and found is None:
                problems.append(f"shelf {code} belongs to the section {section}, which is not on the floor plan any more")
            elif found is not None and not found.contains(x, y):
                problems.append(f"shelf {code} at ({x:.2f}, {y:.2f}) is outside its section {section}: the section must contain the pose")
        try:
            zones, clearance = keepout.load(tmp / "keepout.yaml")
        except keepout.KeepoutError as exc:
            return problems + list(exc.problems)
        area = keepout.bounds(keepout.map_info(map_yaml)) if map_yaml is not None and Path(map_yaml).is_file() else None
        if area is not None:
            problems += keepout.check(zones, area, keepout.poses(tmp / "floorplan.yaml", root / "data" / "shelves.csv"), clearance)
        else:                                            # no map to check against: still keep the station and shelves clear
            problems += keepout.check(zones, (-1e9, -1e9, 1e9, 1e9), keepout.poses(tmp / "floorplan.yaml", root / "data" / "shelves.csv"), clearance)
        return problems
    except keepout.KeepoutError as exc:
        return list(exc.problems)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _backup(path: Path, backup_dir: Path, clock) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    target, n = backup_dir / f"{path.name}.{stamp}.bak", 1
    while target.exists():
        n += 1
        target = backup_dir / f"{path.name}.{stamp}-{n}.bak"
    shutil.copy2(path, target)
    return target


def save_edits(root: Path, keepout_items, section_items, level: Optional[int] = None, map_yaml: Optional[Path] = None,
               clock=time.time) -> dict:
    """Check, then write keepout.yaml and floorplan.yaml. Returns {"ok": True, "changed": [...], "backups": [...]}; raises EditError
    (nothing written) listing every problem."""
    root = Path(root)
    plan_path = root / "data" / "floorplan.yaml"
    keep_path = root / "turtlebot" / "config" / "keepout.yaml"
    rects = clean_rects(keepout_items) if keepout_items is not None else None        # None: leave that file alone
    sections = clean_sections(section_items) if section_items is not None else None
    old_plan = plan_path.read_text(encoding="utf-8")
    old_keep = keep_path.read_text(encoding="utf-8") if keep_path.is_file() else "zones: []\n"
    if level is None:                                            # the floor of the robot's map: the station's
        station = FloorPlan.load(plan_path).station
        level = int(station.level) if station is not None else 1
    if map_yaml is None:
        map_yaml = root / "turtlebot" / "maps" / "lidar_map.yaml"
    new_plan = sections_text(old_plan, level, sections) if sections is not None else old_plan
    new_keep = zones_text(old_keep, rects) if rects is not None else old_keep
    problems = problems_with(root, level, new_plan, new_keep, map_yaml)
    if problems:
        raise EditError(problems)
    changed, backups = [], []
    backup_dir = root / "data" / "backups"
    for path, old, new in ((plan_path, old_plan, new_plan), (keep_path, old_keep, new_keep)):
        if new == old:
            continue
        if path.is_file():
            backups.append(str(_backup(path, backup_dir, clock)))
        path.write_text(new, encoding="utf-8")
        changed.append(path.name)
    return {"ok": True, "changed": changed, "backups": backups,
            "zones": len(rects) if rects is not None else None, "sections": len(sections) if sections is not None else None}
