"""The map as a small PNG for GET /api/map: no ROS, no numpy, no imaging library (zlib and struct from the standard library).

The occupancy grid (-1 unknown, 0 free ... 100 occupied) becomes an 8-bit grey picture: unknown 205, free 254, in between 160,
occupied 0. Row 0 of the picture is the TOP, the largest y, as people expect a map to look. Rows and columns that are all unknown
are cut off (a margin is kept), so a lab in the corner of a big grid is a small picture; `origin_x` / `origin_y` then say where the
picture's bottom-left corner is in the map frame.
"""

import hashlib
import struct
import zlib
from array import array
from typing import Optional

UNKNOWN, FREE, UNCERTAIN, OCCUPIED = 205, 254, 160, 0
FREE_BELOW = 50                                   # a cell under this chance of an obstacle is free ...
OCCUPIED_FROM = 65                                # ... one at or above this is an obstacle (rules.GridView.OCCUPIED)
MAX_SIDE = 4000                                   # cells; a bigger grid is refused rather than sent


def _table() -> bytes:
    table = bytearray([UNCERTAIN]) * 256
    for raw in range(256):
        value = raw - 256 if raw >= 128 else raw           # the signed byte an int8 occupancy value was stored as
        if value < 0:
            table[raw] = UNKNOWN
        elif value < FREE_BELOW:
            table[raw] = FREE
        elif value >= OCCUPIED_FROM:
            table[raw] = OCCUPIED
    return bytes(table)


TABLE = _table()


def png(width: int, height: int, rows: bytes) -> bytes:
    """An 8-bit greyscale PNG from `height` rows of `width` bytes each (joined)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + rows[i * width:(i + 1) * width] for i in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def render(grid, margin: int = 10) -> Optional[dict]:
    """grid: rules.GridView. Returns {version, resolution, width, height, origin_x, origin_y, png} or None if there is nothing to show."""
    width, height, res = int(grid.width), int(grid.height), float(grid.resolution)
    if width <= 0 or height <= 0 or res <= 0 or width > MAX_SIDE or height > MAX_SIDE or len(grid.data) < width * height:
        return None
    grey = array("b", grid.data[:width * height]).tobytes().translate(TABLE)
    rows = [grey[i * width:(i + 1) * width] for i in range(height)]                 # row 0 = the lowest y
    known = [i for i, row in enumerate(rows) if row.strip(bytes([UNKNOWN]))]
    if not known:
        return None
    first, last = known[0], known[-1]
    left = min(width - len(rows[i].lstrip(bytes([UNKNOWN]))) for i in known)
    right = max(len(rows[i].rstrip(bytes([UNKNOWN]))) for i in known)
    first, last = max(0, first - margin), min(height - 1, last + margin)
    left, right = max(0, left - margin), min(width, right + margin)
    kept = rows[first:last + 1][::-1]                                               # flip: the top of the picture is the largest y
    out_w, out_h = right - left, len(kept)
    data = png(out_w, out_h, b"".join(row[left:right] for row in kept))
    return {"version": int(hashlib.md5(data).hexdigest()[:8], 16), "resolution": res, "width": out_w, "height": out_h,
            "origin_x": round(float(grid.origin_x) + left * res, 4), "origin_y": round(float(grid.origin_y) + first * res, 4), "png": data}
