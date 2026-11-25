"""tools/goto_test.py against the fake base: what it refuses, what it asks, and that it drives only after `yes`."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from tests.test_plan_edit import FLOORPLAN, KEEPOUT, SHELVES
from tools import goto_test
from tools.fake_base import FakeBase, FakeBaseServer


def make_root():
    root = Path(tempfile.mkdtemp())
    (root / "data").mkdir()
    (root / "turtlebot" / "config").mkdir(parents=True)
    (root / "data" / "floorplan.yaml").write_text(FLOORPLAN)
    (root / "data" / "shelves.csv").write_text(SHELVES)
    (root / "turtlebot" / "config" / "keepout.yaml").write_text(KEEPOUT)
    maps = root / "turtlebot" / "maps"                       # a 10 x 10 m room, empty, walls round the edge (0, 0 in the middle)
    maps.mkdir()
    grid = np.full((200, 200), 254, dtype=np.uint8)
    grid[0, :] = grid[-1, :] = grid[:, 0] = grid[:, -1] = 0
    Image.fromarray(grid).save(maps / "lidar_map.pgm")
    (maps / "lidar_map.yaml").write_text("image: lidar_map.pgm\nresolution: 0.05\norigin: [-5.0, -5.0, 0.0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n")
    return root


class GotoTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"ROBOT_LINK_TOKEN": "tok"})
        env.start()
        self.addCleanup(env.stop)
        self.said = []
        self.asked = []

    def serve(self, **kw):
        base = FakeBase("tok", start=kw.pop("start", (0.0, 0.0, 0.0)), speed=kw.pop("speed", 5.0), level=1, **kw)
        server = FakeBaseServer(base).start()
        self.addCleanup(server.stop)
        return base, server

    def run_tool(self, argv, server, answer="yes"):
        def ask(prompt):
            self.asked.append(prompt)
            return answer
        return goto_test.main(argv + ["--base-url", server.url, "--timeout", "30"], ask=ask, say=self.said.append, root=self.root)

    def test_check_reads_the_base_and_moves_nothing(self):
        base, server = self.serve()
        self.assertEqual(self.run_tool(["check"], server), 0)
        self.assertIn("LOCALISED", " ".join(self.said))
        self.assertEqual(self.asked, [])
        self.assertEqual((base.x, base.y), (0.0, 0.0))

    def test_plan_says_where_it_would_go_and_sends_nothing(self):
        base, server = self.serve()
        self.assertEqual(self.run_tool(["plan", "shelf", "1-FIC-A"], server), 0)
        self.assertIn("plan only", " ".join(self.said))
        self.assertEqual(self.asked, [])
        self.assertEqual((base.x, base.y), (0.0, 0.0))

    def test_it_does_not_drive_unless_the_answer_is_yes(self):
        base, server = self.serve()
        for answer in ("", "y", "no", "yes please"):
            self.assertEqual(self.run_tool(["shelf", "1-FIC-A"], server, answer=answer), 2, answer)
        self.assertEqual((base.x, base.y), (0.0, 0.0))
        self.assertTrue(all("power switch" in a for a in self.asked))

    def test_yes_drives_to_the_shelf_pose_and_reports_arrival(self):
        base, server = self.serve()
        goto_test.TERMINAL                                                            # (documented states)
        with mock.patch.object(goto_test.time, "sleep", lambda s: None):              # no real waiting between polls
            code = self.run_tool(["shelf", "1-FIC-A", "--speed", "0.3"], server)
        self.assertEqual(code, 0, self.said)
        self.assertIn("ARRIVED", " ".join(self.said))
        self.assertAlmostEqual(base.x, 0.52, delta=0.3)
        self.assertAlmostEqual(base.y, 4.42, delta=0.3)

    def test_a_section_goes_to_its_first_shelf(self):
        label, x, y, yaw = goto_test.resolve(self.root, "section", ["fiction"], 1)
        self.assertEqual((x, y), (0.52, 4.42))
        self.assertIn("Fiction", label)

    def test_unknown_names_and_bad_numbers_are_refused_before_any_connection(self):
        base, server = self.serve()
        for argv in (["shelf", "nope"], ["section", "Nowhere"], ["xy", "1"], ["xy", "a", "b"], ["shelf"], ["--speed", "9", "check"]):
            self.assertEqual(self.run_tool(argv, server), 2, argv)
        self.assertEqual(self.asked, [])

    def test_a_target_inside_or_next_to_a_keepout_zone_is_refused(self):
        base, server = self.serve()
        self.assertEqual(self.run_tool(["xy", "1.5", "2.0"], server), 2)            # inside desk1 [1,1,2,3]
        self.assertEqual(self.run_tool(["xy", "1.1", "3.1"], server), 2)            # 0.1 m from it
        self.assertIn("keep-out zone desk1", " ".join(self.said))
        self.assertEqual(self.asked, [])

    def test_the_route_check_finds_a_way_from_the_station_in_the_open(self):
        rc = goto_test.route_check(self.root, (0.0, 0.0), (0.52, 4.42))
        self.assertGreaterEqual(rc["route"], 0.45)
        self.assertEqual(goto_test.route_problem(rc), "")

    def test_a_robot_too_close_to_a_zone_is_not_sent_because_the_planner_would_refuse(self):
        base, server = self.serve(start=(0.9, 1.9, 0.0))                              # 0.1 m from desk1 [x 1..2, y 1..3]
        self.assertEqual(self.run_tool(["shelf", "1-FIC-A"], server), 2)
        said = " ".join(self.said)
        self.assertIn("stands only", said)
        self.assertIn("keep-out zone desk1", said)
        self.assertEqual(self.asked, [])
        self.assertEqual((base.x, base.y), (0.9, 1.9))

    def test_a_gap_that_is_too_narrow_is_found_before_driving(self):
        (self.root / "turtlebot" / "config" / "keepout.yaml").write_text(
            "clearance_m: 0.25\nzones:\n  - {name: wall_a, rect: [-4.9, 2.0, 0.0, 2.3]}\n  - {name: wall_b, rect: [0.3, 2.0, 4.9, 2.3]}\n")
        base, server = self.serve()
        self.assertEqual(self.run_tool(["shelf", "1-FIC-A"], server), 2)                # a 0.3 m gap: the robot needs 0.4 m
        self.assertIn("no route is wide enough", " ".join(self.said))
        self.assertEqual(self.asked, [])

    def test_the_plan_command_reports_the_same_refusal_without_sending(self):
        base, server = self.serve(start=(0.9, 1.9, 0.0))
        self.assertEqual(self.run_tool(["plan", "shelf", "1-FIC-A"], server), 2)
        self.assertIn("route check", " ".join(self.said))

    def test_the_speed_limit_is_asked_for_before_the_goal_and_a_refusal_does_not_stop_the_drive(self):
        base, server = self.serve()
        with mock.patch.object(goto_test.time, "sleep", lambda s: None):
            self.assertEqual(self.run_tool(["shelf", "1-FIC-A", "--speed", "0.3"], server), 0)
        self.assertIn("speed limit 0.30 m/s applied", " ".join(self.said))

    def test_not_localised_is_not_driven(self):
        base, server = self.serve(unlocalised_for=60.0)
        self.assertEqual(self.run_tool(["shelf", "1-FIC-A"], server), 2)
        self.assertIn("NOT LOCALISED", " ".join(self.said))
        self.assertEqual(self.asked, [])

    def test_no_token_and_no_base_are_said_plainly(self):
        with mock.patch.dict(os.environ, {"ROBOT_LINK_TOKEN": ""}):
            self.assertEqual(goto_test.main(["check", "--base-url", "http://127.0.0.1:9"], say=self.said.append, root=self.root), 2)
        self.assertIn("ROBOT_LINK_TOKEN", " ".join(self.said))
        link_wait = goto_test.connect
        with mock.patch.object(goto_test, "connect", lambda settings, say, wait_s=10.0, link=None: link_wait(settings, say, wait_s=1.0, link=link)):
            self.assertEqual(goto_test.main(["check", "--base-url", "http://127.0.0.1:9"], say=self.said.append, root=self.root), 2)
        self.assertIn("tunnel", " ".join(self.said))


if __name__ == "__main__":
    unittest.main()
