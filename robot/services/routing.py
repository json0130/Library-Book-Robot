"""A route on the robot's own map, for the drawn directions: round the walls instead of through them.

The floor plan can name a picture of the map the robot drew (`base_map:`, display/ui/maps/<name>.json, made by
tools/map_to_plan.py). That file also holds a coarse grid of where a person can walk; this finds the shortest way across
it (A*, eight directions) and pulls the corners tight, so the line on the visitor's screen follows the corridor. It is a
drawing for people, like the L-shaped line it replaces: the robot's planner on the base chooses the real path.

    walk = Walkable.load("display/ui/maps/lab.json")
    route = walk.route((-4.8, 3.0), (0.2, 0.9))       # [(x, y), ...] from the start to the goal, or None
"""

from __future__ import annotations

import heapq
import json
import math
from pathlib import Path
from typing import Optional

SQRT2 = math.sqrt(2)
NEIGHBOURS = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
              (1, 1, SQRT2), (1, -1, SQRT2), (-1, 1, SQRT2), (-1, -1, SQRT2))


class Walkable:
    def __init__(self, rows: list, res: float, x0: float, y0: float):
        self.rows, self.res, self.x0, self.y0 = rows, float(res), float(x0), float(y0)   # rows[0] is the lowest y
        self.h, self.w = len(rows), len(rows[0]) if rows else 0

    @classmethod
    def load(cls, path) -> Optional["Walkable"]:
        try:
            walk = json.loads(Path(path).read_text(encoding="utf-8"))["walk"]
            return cls(walk["rows"], walk["res"], walk["x0"], walk["y0"])
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return None

    def cell(self, x: float, y: float) -> tuple:
        return int((x - self.x0) // self.res), int((y - self.y0) // self.res)

    def point(self, c: int, r: int) -> tuple:
        return (self.x0 + (c + 0.5) * self.res, self.y0 + (r + 0.5) * self.res)

    def walkable(self, c: int, r: int) -> bool:
        return 0 <= c < self.w and 0 <= r < self.h and self.rows[r][c] == "1"

    def nearest(self, c: int, r: int, limit: int = 30) -> Optional[tuple]:
        """The walkable cell closest to (c, r): a shelf or a doorway is next to the floor, not on it."""
        best, found = None, None
        for dr in range(-limit, limit + 1):
            for dc in range(-limit, limit + 1):
                d = dc * dc + dr * dr
                if (best is None or d < best) and self.walkable(c + dc, r + dr):
                    best, found = d, (c + dc, r + dr)
        return found

    def clear(self, a: tuple, b: tuple) -> bool:
        """True if the straight line between two cells stays on walkable cells."""
        steps = max(abs(b[0] - a[0]), abs(b[1] - a[1])) * 2 or 1
        return all(self.walkable(round(a[0] + (b[0] - a[0]) * i / steps), round(a[1] + (b[1] - a[1]) * i / steps))
                   for i in range(steps + 1))

    def route(self, start: tuple, goal: tuple) -> Optional[list]:
        """Points [(x, y), ...] from `start` to `goal` (map metres) along walkable ground, or None if there is no way. A start or goal
        that is not on walkable ground (a shelf, a doorway) is joined to the nearest ground that is."""
        s, g = self.nearest(*self.cell(*start)), self.nearest(*self.cell(*goal))
        if s is None or g is None:
            return None
        best, came, queue = {s: 0.0}, {}, [(0.0, s)]
        while queue:
            _, cur = heapq.heappop(queue)
            if cur == g:
                break
            for dc, dr, cost in NEIGHBOURS:
                nxt = (cur[0] + dc, cur[1] + dr)
                if not self.walkable(*nxt) or (dc and dr and not (self.walkable(cur[0] + dc, cur[1]) and self.walkable(cur[0], cur[1] + dr))):
                    continue
                score = best[cur] + cost
                if score < best.get(nxt, 1e18):
                    best[nxt], came[nxt] = score, cur
                    heapq.heappush(queue, (score + math.hypot(g[0] - nxt[0], g[1] - nxt[1]), nxt))
        if g not in best:
            return None
        cells = [g]
        while cells[-1] != s:
            cells.append(came[cells[-1]])
        cells.reverse()
        keep, i = [cells[0]], 0                        # pull the corners tight: skip ahead while the line is still clear
        while i < len(cells) - 1:
            j = len(cells) - 1
            while j > i + 1 and not self.clear(cells[i], cells[j]):
                j -= 1
            keep.append(cells[j])
            i = j
        points = [tuple(start)] + [self.point(*c) for c in keep[1:-1]] + [tuple(goal)]    # the ends are where the line must start and end
        return [(round(x, 2), round(y, 2)) for x, y in points]


def length(points) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))
