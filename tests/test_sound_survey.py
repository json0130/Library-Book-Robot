"""tools/sound_survey.py: the listening points, and the survey driven against the fake base."""

import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests.test_goto_test import make_root
from tools import goto_test, sound_survey
from tools.fake_base import FakeBase, FakeBaseServer


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()                              # a 10 x 10 m empty room (walls round the edge), a zone desk1 [x 1..2, y 1..3]
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_points_keep_their_distance_from_walls_and_zones_and_start_near_the_start(self):
        points = sound_survey.plan_waypoints(self.root, (0.0, 0.0), spacing=1.0, min_clear=0.4)
        self.assertGreater(len(points), 20)
        for q in points:
            self.assertFalse(1.0 - 0.4 < q["x"] < 2.0 + 0.4 and 1.0 - 0.4 < q["y"] < 3.0 + 0.4, q)   # clear of desk1
            self.assertLess(abs(q["x"]), 4.6)
            self.assertLess(abs(q["y"]), 4.6)
        self.assertEqual([q["k"] for q in points], list(range(1, len(points) + 1)))
        first = points[0]
        self.assertLessEqual(max(abs(first["x"]), abs(first["y"])), 1.0)                      # the nearest point to the start first

    def test_a_tour_is_not_longer_than_a_sensible_bound_and_its_time_is_estimated(self):
        points = sound_survey.plan_waypoints(self.root, (0.0, 0.0), spacing=1.5, min_clear=0.4)
        length = sound_survey.tour_length((0.0, 0.0), points)
        self.assertLess(length, len(points) * 3.0)
        self.assertGreater(sound_survey.estimate_minutes(length, len(points), 0.2, 8), 1.0)

    def test_the_start_must_be_on_usable_ground(self):
        with self.assertRaises(sound_survey.SurveyError):
            sound_survey.plan_waypoints(self.root, (1.5, 2.0))                                 # inside desk1

    def test_plan_writes_the_file_the_operator_page_shows_and_changes_nothing_else(self):
        said = []
        self.assertEqual(sound_survey.main(["plan", "--spacing", "1.5"], say=said.append, root=self.root), 0)
        plan = json.loads((self.root / "data" / "noise" / "survey_plan.json").read_text())
        self.assertGreater(len(plan["points"]), 5)
        self.assertEqual(plan["start"], {"x": 0.0, "y": 0.0})
        self.assertIn("listening points", " ".join(said))

    def test_bad_numbers_are_refused(self):
        for argv in (["plan", "--spacing", "0.2"], ["plan", "--dwell", "1"], ["run", "--speed", "9"]):
            self.assertEqual(sound_survey.main(argv, say=lambda s: None, root=self.root), 2, argv)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"ROBOT_LINK_TOKEN": "tok"})
        env.start()
        self.addCleanup(env.stop)
        self.said, self.asked = [], []

    def serve(self, **kw):
        base = FakeBase("tok", start=kw.pop("start", (0.0, 0.0, 0.0)), speed=kw.pop("speed", 8.0), level=1, **kw)
        server = FakeBaseServer(base).start()
        self.addCleanup(server.stop)
        return base, server

    def run_tool(self, argv, server, answer="yes"):
        def ask(prompt):
            self.asked.append(prompt)
            return answer
        return sound_survey.main(argv + ["--base-url", server.url], ask=ask, say=self.said.append, root=self.root, wait=lambda s: None)

    def test_nothing_drives_unless_the_answer_is_yes(self):
        base, server = self.serve()
        self.assertEqual(self.run_tool(["run", "--spacing", "3", "--max", "2"], server, answer="no"), 2)
        self.assertEqual((base.x, base.y), (0.0, 0.0))
        self.assertTrue(all("power switch" in a for a in self.asked))

    def test_it_visits_the_points_listens_at_each_and_comes_back_and_logs_every_visit(self):
        base, server = self.serve()
        code = self.run_tool(["run", "--spacing", "3", "--max", "3", "--dwell", "2"], server)
        self.assertEqual(code, 0, self.said)
        said = " ".join(self.said)
        self.assertEqual(said.count("arrived, listening"), 3)
        self.assertIn("back at the start: arrived", said)
        log = json.loads((self.root / "data" / "noise" / "survey_log.json").read_text())
        self.assertEqual([v["outcome"] for v in log["visits"]], ["arrived"] * 3)
        self.assertAlmostEqual(base.x, 0.0, delta=0.4)
        self.assertAlmostEqual(base.y, 0.0, delta=0.4)

    def test_three_failed_legs_in_a_row_stop_the_survey(self):
        base, server = self.serve(fail_rate=1.0)
        self.assertEqual(self.run_tool(["run", "--spacing", "3", "--max", "6", "--dwell", "2"], server), 1)
        self.assertIn("legs failed in a row", " ".join(self.said))
        log = json.loads((self.root / "data" / "noise" / "survey_log.json").read_text())
        self.assertEqual(len(log["visits"]), 3)

    def test_not_localised_is_not_started(self):
        base, server = self.serve(unlocalised_for=60.0)
        self.assertEqual(self.run_tool(["run", "--spacing", "3"], server), 2)
        self.assertIn("NOT LOCALISED", " ".join(self.said))
        self.assertEqual(self.asked, [])


class SimplifyTest(unittest.TestCase):
    PTS = [{"k": k, "x": float(k), "y": 0.0} for k in range(1, 16)]

    def test_only_the_first_n_points_and_groups_become_one_point_in_the_middle(self):
        pts, notes = sound_survey.simplify(self.PTS, 12, [[5, 6], [9, 10, 11]])
        self.assertEqual([(q["k"], q["x"]) for q in pts], [(1, 1.0), (2, 2.0), (3, 3.0), (4, 4.0), (5, 5.5), (6, 7.0), (7, 8.0), (8, 10.0), (9, 12.0)])
        self.assertEqual(len(notes), 3)
        self.assertIn("5, 6", notes[1])

    def test_without_options_nothing_changes_and_missing_points_are_refused(self):
        pts, notes = sound_survey.simplify(self.PTS)
        self.assertEqual((pts, notes), (self.PTS, []))
        with self.assertRaises(sound_survey.SurveyError):
            sound_survey.simplify(self.PTS, 12, [[11, 13]])                       # 13 is beyond --upto

    def test_skipped_points_are_left_out_and_the_rest_is_renumbered(self):
        pts, notes = sound_survey.simplify(self.PTS, 12, [[5, 6], [9, 10, 11]], [6])         # numbers as after the merges: 6 is old point 7
        self.assertEqual([(q["k"], q["x"]) for q in pts], [(1, 1.0), (2, 2.0), (3, 3.0), (4, 4.0), (5, 5.5), (6, 8.0), (7, 10.0), (8, 12.0)])
        self.assertIn("left out (7.00, 0.00)", notes[-1])
        with self.assertRaises(sound_survey.SurveyError):
            sound_survey.simplify(self.PTS, 12, [], [20])

    def test_the_input_is_not_changed(self):
        before = [dict(q) for q in self.PTS]
        sound_survey.simplify(self.PTS, 12, [[5, 6]])
        self.assertEqual(self.PTS, before)


if __name__ == "__main__":
    unittest.main()
