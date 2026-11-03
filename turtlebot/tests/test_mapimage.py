"""robot_link.mapimage and GET /api/map (core.map_info): no ROS, no imaging library."""

import base64
import struct
import unittest
import zlib

from support import Clock, FakeBackend  # noqa: F401  (puts turtlebot/ on the path)
from robot_link import mapimage, rules
from robot_link.core import LinkCore, Settings


def decode(data):
    """A minimal PNG reader for the 8-bit grey pictures made here: (width, height, [row bytes])."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        assert struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0] == zlib.crc32(kind + body) & 0xFFFFFFFF
        if kind == b"IHDR":
            size = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    w, h = size[0], size[1]
    raw = zlib.decompress(idat)
    assert all(raw[i * (w + 1)] == 0 for i in range(h))
    return w, h, [raw[i * (w + 1) + 1:(i + 1) * (w + 1)] for i in range(h)]


def grid(width, height, cells, res=0.05, ox=-1.0, oy=-2.0):
    data = [-1] * (width * height)
    for (cx, cy), v in cells.items():
        data[cy * width + cx] = v
    return rules.GridView(width, height, res, ox, oy, tuple(data))


class RenderTest(unittest.TestCase):
    def test_values_become_grey_levels(self):
        g = grid(5, 5, {(1, 1): 0, (2, 1): 100, (3, 1): 55, (1, 2): 20})
        image = mapimage.render(g, margin=0)
        w, h, rows = decode(image["png"])
        self.assertEqual((w, h), (3, 2))                                    # cut to the known cells
        self.assertEqual(rows[1], bytes([mapimage.FREE, mapimage.OCCUPIED, mapimage.UNCERTAIN]))   # the lowest y is the bottom row
        self.assertEqual(rows[0][0], mapimage.FREE)

    def test_picture_is_flipped_and_the_origin_follows_the_crop(self):
        g = grid(10, 10, {(4, 3): 0, (6, 7): 100}, res=0.1, ox=-1.0, oy=-2.0)
        image = mapimage.render(g, margin=1)
        w, h, rows = decode(image["png"])
        self.assertEqual((w, h), (5, 7))                                    # x 3..7, y 2..8
        self.assertAlmostEqual(image["origin_x"], -1.0 + 3 * 0.1)
        self.assertAlmostEqual(image["origin_y"], -2.0 + 2 * 0.1)
        self.assertEqual(rows[-2][1], mapimage.FREE)                         # cell (4, 3): column 4-3, row from the bottom 3-2
        self.assertEqual(rows[1][3], mapimage.OCCUPIED)                      # cell (6, 7): the high y is near the top

    def test_nothing_known_means_no_picture(self):
        self.assertIsNone(mapimage.render(grid(4, 4, {})))
        self.assertIsNone(mapimage.render(rules.GridView(0, 0, 0.05, 0, 0, ())))
        self.assertIsNone(mapimage.render(rules.GridView(3, 3, 0.05, 0, 0, (0, 0))))      # too little data

    def test_same_map_same_version(self):
        a, b = mapimage.render(grid(6, 6, {(2, 2): 0})), mapimage.render(grid(6, 6, {(2, 2): 0}))
        c = mapimage.render(grid(6, 6, {(2, 2): 100}))
        self.assertEqual(a["version"], b["version"])
        self.assertNotEqual(a["version"], c["version"])


class MapBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.seq, self.grid = 1, None

    def map_grid(self):
        return None if self.grid is None else (self.seq, self.grid)


class MapInfoTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.b = MapBackend()
        self.core = LinkCore(self.b, Settings(map_render_min_s=10.0), clock=self.clock)

    def test_no_map_yet(self):
        self.assertEqual(self.core.map_info(), {"ok": True, "available": False})

    def test_map_comes_with_its_place_in_the_map_frame(self):
        self.b.grid = grid(10, 10, {(4, 3): 0, (6, 7): 100}, res=0.1)
        body = self.core.map_info()
        self.assertTrue(body["ok"] and body["available"] and body["changed"])
        self.assertEqual((body["frame"], body["resolution"]), ("map", 0.1))
        self.assertEqual(set(body["origin"]), {"x", "y"})
        w, h, _ = decode(base64.b64decode(body["png_base64"]))
        self.assertEqual((w, h), (body["width"], body["height"]))

    def test_have_the_newest_gets_no_picture(self):
        self.b.grid = grid(10, 10, {(4, 3): 0})
        first = self.core.map_info()
        again = self.core.map_info(have=first["version"])
        self.assertFalse(again["changed"])
        self.assertNotIn("png_base64", again)
        self.assertTrue(self.core.map_info(have=first["version"] + 1)["changed"])

    def test_a_changed_map_is_drawn_at_most_every_ten_seconds(self):
        self.b.grid = grid(10, 10, {(4, 3): 0})
        first = self.core.map_info()
        self.b.grid, self.b.seq = grid(10, 10, {(4, 3): 100}), 2
        self.clock.advance(3)
        self.assertEqual(self.core.map_info()["version"], first["version"])
        self.clock.advance(8)
        self.assertNotEqual(self.core.map_info()["version"], first["version"])

    def test_a_new_message_with_the_same_content_keeps_the_version(self):
        self.b.grid = grid(10, 10, {(4, 3): 0})
        first = self.core.map_info()
        self.b.seq = 2
        self.clock.advance(11)
        self.assertEqual(self.core.map_info(have=first["version"])["changed"], False)


if __name__ == "__main__":
    unittest.main()
