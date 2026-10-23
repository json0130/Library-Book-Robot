"""The library's floor plan (data/floorplan.yaml): floors, sections as rectangles, landmarks, the robot's station.

Coordinates are metres in the map frame of data/shelves.csv (map_x, map_y), one frame per floor. The plan is used to
draw directions on the screen and by tools/catalog_check.py to check the shelves against it. It holds the building
only, never anything about visitors.

    plan = FloorPlan.load("data/floorplan.yaml")      # raises FloorPlanError naming every problem
    plan.floor(3).section("History")                  # Section(name, x, y, w, h)
    plan.where_section("History")                     # (Floor, Section) or None
    plan.lift(2)                                      # the lift Landmark on level 2, or None
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LANDMARK_KINDS = ("lift", "help_desk", "entrance", "stairs", "toilets", "other")


class FloorPlanError(ValueError):
    """The floor plan file is missing, not YAML, or describes something impossible. `problems` lists them all."""

    def __init__(self, source: str, problems: list):
        self.source, self.problems = source, list(problems)
        super().__init__(f"{source}: " + "; ".join(self.problems))


@dataclass(frozen=True)
class Section:
    name: str
    x: float
    y: float
    w: float
    h: float

    def contains(self, x: float, y: float, margin: float = 0.0) -> bool:
        return (self.x - margin <= x <= self.x + self.w + margin) and (self.y - margin <= y <= self.y + self.h + margin)

    @property
    def centre(self) -> tuple:
        return (round(self.x + self.w / 2, 2), round(self.y + self.h / 2, 2))


@dataclass(frozen=True)
class Landmark:
    kind: str
    name: str
    x: float
    y: float


@dataclass(frozen=True)
class Floor:
    level: int
    name: str
    width: float
    height: float
    sections: tuple = ()
    landmarks: tuple = ()

    def section(self, name: str) -> Optional[Section]:
        wanted = name.strip().lower()
        return next((s for s in self.sections if s.name.lower() == wanted), None)

    def landmark(self, kind: str) -> Optional[Landmark]:
        return next((m for m in self.landmarks if m.kind == kind), None)


@dataclass(frozen=True)
class Station:
    level: int
    x: float
    y: float
    yaw: float = 0.0


@dataclass
class FloorPlan:
    floors: dict = field(default_factory=dict)          # level -> Floor, in level order
    station: Optional[Station] = None
    source: str = ""

    # ---- looking things up ----
    def floor(self, level) -> Optional[Floor]:
        try:
            return self.floors.get(int(level))
        except (TypeError, ValueError):
            return None

    def where_section(self, name: str) -> Optional[tuple]:
        """(Floor, Section) for a section name (case does not matter), or None."""
        for floor in self.floors.values():
            section = floor.section(name)
            if section is not None:
                return floor, section
        return None

    def lift(self, level) -> Optional[Landmark]:
        floor = self.floor(level)
        return floor.landmark("lift") if floor else None

    def sections_on(self, level) -> list:
        floor = self.floor(level)
        return [s.name for s in floor.sections] if floor else []

    # ---- loading ----
    @classmethod
    def load(cls, path) -> "FloorPlan":
        import yaml
        path = Path(path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise FloorPlanError(path.name, [f"cannot read it ({exc})"]) from exc
        except yaml.YAMLError as exc:
            raise FloorPlanError(path.name, [f"not valid YAML ({exc})"]) from exc
        return cls.from_dict(data, path.name)

    @classmethod
    def from_dict(cls, data, source: str = "floorplan") -> "FloorPlan":
        """Build and check a plan; raises FloorPlanError listing every problem."""
        problems: list = []
        if not isinstance(data, dict) or not isinstance(data.get("floors"), list) or not data["floors"]:
            raise FloorPlanError(source, ["it needs a `floors:` list with at least one floor"])
        floors: dict = {}
        for i, raw in enumerate(data["floors"], 1):
            floor = _floor(raw, f"floor #{i}", problems)
            if floor is None:
                continue
            if floor.level in floors:
                problems.append(f"level {floor.level} is defined twice")
                continue
            floors[floor.level] = floor
        names: dict = {}
        for floor in floors.values():
            for s in floor.sections:
                key = s.name.lower()
                if key in names:
                    problems.append(f"section {s.name!r} is on level {names[key]} and on level {floor.level}; "
                                    "a section name must be unique")
                names[key] = floor.level
        station = None
        raw = data.get("station")
        if raw is not None:
            try:
                station = Station(int(raw["level"]), float(raw["x"]), float(raw["y"]), float(raw.get("yaw", 0.0)))
            except (KeyError, TypeError, ValueError):
                problems.append("station: needs level, x and y (numbers)")
            else:
                floor = floors.get(station.level)
                if floor is None:
                    problems.append(f"station: level {station.level} is not a floor of the plan")
                elif not (0 <= station.x <= floor.width and 0 <= station.y <= floor.height):
                    problems.append(f"station ({station.x:g}, {station.y:g}) is outside level {station.level} "
                                    f"({floor.width:g} x {floor.height:g} m)")
        if problems:
            raise FloorPlanError(source, problems)
        return cls(dict(sorted(floors.items())), station, source)


def _number(raw: dict, key: str, where: str, problems: list, positive: bool = False) -> Optional[float]:
    try:
        value = float(raw[key])
    except (KeyError, TypeError, ValueError):
        problems.append(f"{where}: `{key}` must be a number")
        return None
    if positive and value <= 0:
        problems.append(f"{where}: `{key}` must be more than 0")
        return None
    return value


def _floor(raw, where: str, problems: list) -> Optional[Floor]:
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be a mapping with level, width, height, sections")
        return None
    try:
        level = int(raw["level"])
    except (KeyError, TypeError, ValueError):
        problems.append(f"{where}: `level` must be a whole number")
        return None
    where = f"level {level}"
    width = _number(raw, "width", where, problems, positive=True)
    height = _number(raw, "height", where, problems, positive=True)
    if width is None or height is None:
        return None
    sections = []
    for j, s in enumerate(raw.get("sections") or [], 1):
        if not isinstance(s, dict) or not str(s.get("name") or "").strip():
            problems.append(f"{where}: section #{j} needs a name")
            continue
        name = str(s["name"]).strip()
        here = f"{where}, section {name!r}"
        values = [_number(s, k, here, problems, positive=k in ("w", "h")) for k in ("x", "y", "w", "h")]
        if None in values:
            continue
        x, y, w, h = values
        if x < 0 or y < 0 or x + w > width + 1e-9 or y + h > height + 1e-9:
            problems.append(f"{here}: the rectangle ({x:g}, {y:g}, {w:g} x {h:g}) is outside the floor "
                            f"({width:g} x {height:g} m)")
            continue
        sections.append(Section(name, x, y, w, h))
    landmarks = []
    for j, m in enumerate(raw.get("landmarks") or [], 1):
        if not isinstance(m, dict):
            problems.append(f"{where}: landmark #{j} must be a mapping")
            continue
        kind = str(m.get("kind") or "").strip()
        if kind not in LANDMARK_KINDS:
            problems.append(f"{where}: landmark #{j} has kind {kind!r}; use one of {', '.join(LANDMARK_KINDS)}")
            continue
        here = f"{where}, landmark {m.get('name') or kind}"
        x, y = _number(m, "x", here, problems), _number(m, "y", here, problems)
        if x is None or y is None:
            continue
        if not (0 <= x <= width and 0 <= y <= height):
            problems.append(f"{here}: ({x:g}, {y:g}) is outside the floor")
            continue
        landmarks.append(Landmark(kind, str(m.get("name") or kind.replace("_", " ").title()), x, y))
    return Floor(level, str(raw.get("name") or f"Level {level}"), width, height, tuple(sections), tuple(landmarks))
